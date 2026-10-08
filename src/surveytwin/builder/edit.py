"""Adding, changing, removing and reordering questions: each returns a new Survey.

The survey passed in, and any plan already made from it, never change. Question
IDs are never renumbered. A question can be named by its export tag (``Q3``)
or its internal ID (``QID3``); a name that could mean two questions is refused.

On a survey read from a QSF, an edit touches only the question it names: every
other question, block and flow element stays exactly as read. An edit that
would lose Qualtrics content the builder cannot represent (recode values, option
images, write-in options, a type change), or that would leave logic reading a
removed question or a question that now comes later, raises
``UnsupportedEdit`` and changes nothing.
"""

from __future__ import annotations

import copy
import difflib
import re
from typing import Any, Iterable, Sequence

from ..errors import DraftError, SurveyError, UnsupportedEdit
from ..runtime.choice_logic import LOGIC_KEYS, logic_of as choice_logic_of
from ..runtime.instrument_plan import remove_template_questions
from ..survey import Survey, _find_dependencies
from .build import _payload
from .convert import question_to_template
from .draft import ANSWERABLE_TYPES, DRAFT_FIELDS, DraftIssue, DraftQuestion
from .images import ImageResolver, resolve_image
from .policy import BuilderPolicy
from .validate import RESERVED_ID, errors_only, expanded_column_names, id_issues, validate_question

_QID_NUMBER = re.compile(r"^QID(\d+)$")
_Q_NUMBER = re.compile(r"^Q(\d+)$")
_QID_ANYWHERE = re.compile(r"QID\d+")
_PIPED_QID = re.compile(r"\$\{q://(?:\d+_)?(QID\d+)")  # "1_QID5" is a Loop & Merge pipe
MULTI_SELECTORS = {"MAVR", "MAHR", "MACOL", "MSB"}


# ---------------------------------------------------------------------------
# finding blocks and questions
# ---------------------------------------------------------------------------


def _block_elements(template: dict[str, Any], block_id: str | None = None) -> list[dict[str, Any]]:
    """Every Block element (a block the flow shows twice appears twice)."""
    found: list[dict[str, Any]] = []

    def walk(elements: Any) -> None:
        if not isinstance(elements, list):
            return
        for element in elements:
            if not isinstance(element, dict):
                continue
            if element.get("ElementType") == "Block":
                if block_id is None or str(element.get("BlockID", "")) == block_id:
                    found.append(element)
            walk(element.get("Elements"))

    walk(template.get("Elements", []))
    return found


def _questions_by_qid(template: dict[str, Any]) -> dict[str, tuple[str, dict[str, Any]]]:
    """QuestionID -> (block ID, question), first occurrence."""
    out: dict[str, tuple[str, dict[str, Any]]] = {}
    for block in _block_elements(template):
        for q in block.get("Questions") or []:
            if isinstance(q, dict) and q.get("QuestionID"):
                out.setdefault(str(q["QuestionID"]), (str(block.get("BlockID", "")), q))
    return out


def _block_order(template: dict[str, Any], block_id: str) -> list[str]:
    blocks = _block_elements(template, block_id)
    if not blocks:
        return []
    return [str(q.get("QuestionID", "")) for q in blocks[0].get("Questions") or [] if isinstance(q, dict)]


def _tag(question: dict[str, Any]) -> str:
    # Exports can carry a stray space or no-break space around a tag; a name
    # given without it must still find the question.
    return str(question.get("QuestionName") or question.get("QuestionID") or "").strip()


def _label(qid: str, question: dict[str, Any] | None) -> str:
    tag = _tag(question) if question else ""
    return f"{tag} ({qid})" if tag and tag != qid else qid


def _issue(message: str, code: str, *, question_id: str = "", index: int = -1, field: str = "", severity: str = "error") -> DraftIssue:
    return DraftIssue(severity=severity, message=message, code=code, question_id=question_id, question_index=index, field=field)


def _resolve(survey: Survey, identifier: Any) -> str:
    """The QuestionID a caller's name refers to: an export tag or a QuestionID."""
    template = survey._base_template
    by_qid = _questions_by_qid(template)
    name = str(identifier).strip() if identifier is not None else ""
    if not name:
        raise DraftError([_issue("A question ID is required.", "id.missing", field="id")])
    tag_matches = [qid for qid, (_b, q) in by_qid.items() if _tag(q) == name]
    # An internal ID names its own question, even when another question's export
    # tag happens to be that text.
    candidates = [name] if name in by_qid else sorted(set(tag_matches))
    if len(candidates) > 1:
        # A dropped question does not exist: a name that also fits one that does means that one.
        dropped = _dropped_qids(survey)
        existing = [c for c in candidates if c not in dropped]
        if len(existing) == 1:
            candidates = existing
    if len(candidates) > 1:
        described = ", ".join(_label(c, by_qid[c][1]) for c in candidates)
        raise DraftError([_issue(f"{name!r} could mean more than one question: {described}. Use the internal ID (QID...).", "id.ambiguous", question_id=name, field="id")])
    if not candidates:
        known = sorted(set(by_qid) | {_tag(q) for _b, q in by_qid.values()})
        close = difflib.get_close_matches(name, known, n=3, cutoff=0.5)
        hint = f" Did you mean: {', '.join(close)}?" if close else ""
        raise DraftError([_issue(f"No question {name!r} in this survey.{hint}", "id.unknown", question_id=name, field="id")])
    qid = candidates[0]
    if qid in survey.exclude_questions:
        raise DraftError([_issue(f"{name!r} was excluded when the survey was read.", "id.excluded", question_id=name, field="id")])
    return qid


def _check_block_editable(survey: Survey, block_id: str) -> None:
    if block_id in survey.exclude_blocks:
        raise SurveyError(f"Block {block_id} is dropped from this survey; edit the survey before dropping blocks.")
    for block in _block_elements(survey._base_template, block_id):
        experiment = block.get("Experiment")
        if isinstance(experiment, dict):
            kind = "conjoint" if experiment.get("kind") == "conjoint" else "MaxDiff"
            raise UnsupportedEdit(
                f"Block {block_id} is a {kind} block: its tasks are drawn for each twin from its definition. "
                "Edit the definition (survey.standard) and read it again with read_survey."
            )


def _visible(template: dict[str, Any], survey: Survey) -> dict[str, Any]:
    """The template without the survey's dropped blocks: what exists."""
    from ..prompts.instrument import _filter_qsf_template_blocks

    if not survey.exclude_blocks:
        return template
    return _filter_qsf_template_blocks(template, survey.exclude_blocks, excluded_question_ids=survey.exclude_questions)


def _taken_tags(template: dict[str, Any], *, except_qids: Iterable[str] = ()) -> set[str]:
    skip = set(except_qids)
    return {_tag(q) for qid, (_b, q) in _questions_by_qid(template).items() if qid not in skip}


def _template_columns(template: dict[str, Any], *, except_qids: Iterable[str] = ()) -> dict[str, str]:
    """Extra column name -> the export tag of the question that adds it."""
    skip = set(except_qids)
    columns: dict[str, str] = {}
    for qid, (_b, q) in _questions_by_qid(template).items():
        if qid in skip:
            continue
        tag = _tag(q)
        qtype = str(q.get("QuestionType", ""))
        selector = str((q.get("Settings") or {}).get("Selector", ""))
        names: list[str] = []
        if qtype == "MC" and selector in MULTI_SELECTORS:
            names = expanded_column_names(tag, "multi_select", options=len(q.get("Options") or []))
        elif qtype == "Matrix":
            names = expanded_column_names(tag, "grid", rows=len(q.get("Rows") or []))
        elif qtype == "Slider":
            names = [f"{tag}_{k}" for k in range(1, len(q.get("Statements") or [""]) + 1)]
        elif qtype == "TE" and selector == "FORM":
            names = [f"{tag}_{k}" for k in range(1, len(q.get("Rows") or []) + 1)]
        for column in names:
            columns.setdefault(column, tag)
    return columns


def _export_columns(template: dict[str, Any]) -> list[str]:
    """The column names of the Qualtrics-layout CSVs for this template, from the
    export's own column plan (every question type, write-in columns included)."""
    from ..results import exports as ex
    from ..results.layout import Layout
    from ..spec.read_template import survey_from_template

    layout = Layout(survey_from_template(template))
    header, _desc, _imports, _rows = ex._build_public_surveytwin_csv([], {}, {}, [], answer_resolver=ex._resolve_label_answer, layout=layout)
    return [str(h) for h in header[1:]]


def _duplicate_columns(template: dict[str, Any]) -> set[str]:
    seen: set[str] = set()
    repeated: set[str] = set()
    for column in ["TWIN_ID"] + _export_columns(template):
        if column in seen:
            repeated.add(column)
        seen.add(column)
    return repeated


_DUPLICATES_CACHE: dict[str, frozenset[str]] = {}


def _duplicates_of(template: dict[str, Any]) -> set[str]:
    import hashlib

    key = hashlib.sha256(json_text(template).encode("utf-8")).hexdigest()
    if key not in _DUPLICATES_CACHE:
        if len(_DUPLICATES_CACHE) > 64:
            _DUPLICATES_CACHE.clear()
        _DUPLICATES_CACHE[key] = frozenset(_duplicate_columns(template))
    return set(_DUPLICATES_CACHE[key])


def _check_new_column_clashes(before: dict[str, Any], after: dict[str, Any], question_id: str = "") -> None:
    """Refuse an edit that makes a CSV column name appear twice (clashes already in
    an imported file are left alone). Checked on what exists: dropped blocks'
    questions are not in any output."""
    new = sorted(_duplicates_of(after) - _duplicates_of(before))
    if new:
        raise DraftError(
            [
                _issue(
                    f"The column name {column!r} would appear twice in the CSV files; choose another ID.",
                    "id.column_clash",
                    question_id=question_id,
                    field="id",
                )
                for column in new
            ]
        )


def _flow_positions(template: dict[str, Any]) -> dict[str, int]:
    """QuestionID -> position in flow order (first appearance)."""
    positions: dict[str, int] = {}
    for block in _block_elements(template):
        for q in block.get("Questions") or []:
            if isinstance(q, dict) and q.get("QuestionID"):
                positions.setdefault(str(q["QuestionID"]), len(positions))
    return positions


def _piped(question: dict[str, Any] | None, keys: tuple[str, ...]) -> set[str]:
    value = [question.get(k) for k in keys if question.get(k)] if question else []
    return set(_PIPED_QID.findall(json_text(value)))


def _piping_issues(
    template: dict[str, Any],
    qid: str,
    question: dict[str, Any],
    *,
    dropped: Iterable[str] = (),
    fields: Iterable[str] | None = None,
    before: dict[str, Any] | None = None,
) -> list[DraftIssue]:
    """Piped text (in the text, options or rows) must read a question that exists
    and comes earlier in the flow. ``template`` holds only what exists. Only the
    ``fields`` an edit changes are checked (all by default), and only pipes the
    question did not have ``before`` the edit: piping already there is left as it
    is (a dropped question it reads counts as never shown)."""
    positions = _flow_positions(template)
    dropped_set = set(dropped)
    checked = set(fields) if fields is not None else None
    issues: list[DraftIssue] = []
    for field_name, keys in (("text", ("QuestionText",)), ("options", ("Options", "Columns")), ("rows", ("Rows", "Statements"))):
        if checked is not None and field_name not in checked:
            continue
        for other in sorted(_piped(question, keys) - _piped(before, keys)):
            if other == qid:
                continue
            if other in dropped_set:
                issues.append(_issue(f"The question pipes {other}, which was dropped from the survey.", "text.piping_dropped", question_id=_tag(question), field=field_name))
            elif other not in positions:
                issues.append(_issue(f"The question pipes {other}, which is not in this survey.", "text.piping_unknown", question_id=_tag(question), field=field_name))
            elif positions[other] > positions.get(qid, -1):
                issues.append(_issue(f"The question pipes {other}, which comes later in the survey.", "text.piping_later", question_id=_tag(question), field=field_name))
    return issues


def _dropped_qids(survey: Survey) -> set[str]:
    from ..runtime.instrument_plan import question_ids_in_blocks

    return set(question_ids_in_blocks(survey._base_template, survey.exclude_blocks))


def json_text(value: Any) -> str:
    import json

    return json.dumps(value, ensure_ascii=False)


def _answerable_count(template: dict[str, Any]) -> int:
    from ..survey import Question

    return sum(1 for _qid, (block, q) in _questions_by_qid(template).items() if Question(q, block).kind == "answerable")


def _next_qid_number(survey: Survey, template: dict[str, Any]) -> int:
    """One past every internal ID the survey has used: in the template (dropped
    blocks too), in the QSF (questions outside the flow too), in questions added
    or removed earlier, and before the survey was last built from a draft (its
    ``NextQuestionNumber``), so a removed question's ID is never given to a new one."""
    from .build import NEXT_QID_KEY

    numbers = [int(m.group(1)) for qid in _questions_by_qid(template) if (m := _QID_NUMBER.match(qid))]
    for source in (template, survey._base_template):
        floor = (source.get("Metadata") or {}).get(NEXT_QID_KEY)
        if isinstance(floor, int) and not isinstance(floor, bool) and floor > 0:
            numbers.append(floor - 1)
    for entry in survey.edits:
        for key in ("questions", "added", "removed"):
            for item in entry.get(key) or []:
                m = _QID_NUMBER.match(str(item.get("qid", "")) if isinstance(item, dict) else "")
                if m:
                    numbers.append(int(m.group(1)))
    for element in (survey._qsf or {}).get("SurveyElements") or []:
        if isinstance(element, dict) and element.get("Element") == "SQ":
            m = _QID_NUMBER.match(str(element.get("PrimaryAttribute", "")))
            if m:
                numbers.append(int(m.group(1)))
    return max(numbers, default=0) + 1


def _next_tag(taken: set[str], question: DraftQuestion | None = None, columns: Iterable[str] = ()) -> str:
    """The next ``Q<n>`` past the highest used, skipping one that is a column name
    already or whose own columns (``Q<n>_1``, ... for ``question``'s type) would
    repeat an ID or column."""
    from .validate import _draft_columns_as

    numbers = [int(m.group(1)) for t in taken if (m := _Q_NUMBER.match(t))]
    avoid = set(taken) | set(columns)
    n = max(numbers, default=0) + 1
    while f"Q{n}" in avoid or (question is not None and any(c in avoid for c in _draft_columns_as(question, f"Q{n}"))):
        n += 1
    return f"Q{n}"


def _draft_pipe_problems(template: dict[str, Any], authoring: dict[str, dict[str, Any]], dropped: Iterable[str] = ()) -> dict[tuple[str, str, str], str]:
    """Piped text in builder questions' drafts (text, options and rows, whether or not
    the question's type shows them) that reads a question the survey does not have,
    a dropped one, a later one, or the question itself: building the survey's draft
    again refuses each. ``template`` holds what exists. -> {(qid, field, pipe): code}"""
    positions = _flow_positions(template)
    dropped_set = set(dropped)
    problems: dict[tuple[str, str, str], str] = {}
    for qid in positions:
        entry = authoring.get(qid)
        draft = entry.get("draft") if isinstance(entry, dict) else None
        if not isinstance(draft, dict):
            continue
        for field_name in ("text", "options", "rows"):
            for other in set(_PIPED_QID.findall(json_text(draft.get(field_name)))):
                if other == qid:
                    code = "text.piping_self"
                elif other in dropped_set:
                    code = "text.piping_dropped"
                elif other not in positions:
                    code = "text.piping_unknown"
                elif positions[other] > positions[qid]:
                    code = "text.piping_later"
                else:
                    continue
                problems[(qid, field_name, other)] = code
    return problems


_PIPE_PROBLEM_WORDS = {
    "text.piping_self": "its own internal ID; a question cannot show its own answer",
    "text.piping_dropped": "which was dropped from the survey",
    "text.piping_unknown": "which is not in this survey",
    "text.piping_later": "which would come later in the survey",
}


def _check_draft_pipes(survey: Survey, after_template: dict[str, Any], after_authoring: dict[str, dict[str, Any]]) -> None:
    """Refuse an edit that leaves a builder question's draft piping a missing, dropped,
    later or its own question, in any of its text, options and rows: building the
    survey's draft would refuse it (a pipe in a field the type does not show stays in
    the draft, and a type change shows it). Problems already there are left alone."""
    dropped = _dropped_qids(survey)
    before = _draft_pipe_problems(_visible(survey._base_template, survey), survey._authoring, dropped)
    after = _draft_pipe_problems(_visible(after_template, survey), after_authoring, dropped)
    by_qid = _questions_by_qid(after_template)
    new = [(key, code) for key, code in after.items() if key not in before]
    if new:
        raise DraftError(
            [
                _issue(
                    f"The question's {field_name} pipes {other}, {_PIPE_PROBLEM_WORDS[code]}.",
                    code,
                    question_id=_tag(by_qid[qid][1]) if qid in by_qid else qid,
                    field=field_name,
                )
                for (qid, field_name, other), code in sorted(new)
            ]
        )


def _replace_in_blocks(template: dict[str, Any], block_id: str, questions: list[dict[str, Any]]) -> None:
    for block in _block_elements(template, block_id):
        block["Questions"] = copy.deepcopy(questions)


def _log(survey: Survey, entry: dict[str, Any]) -> list[dict[str, Any]]:
    return [dict(e) for e in survey.edits] + [entry]


def _count_questions(template: dict[str, Any]) -> int:
    return len(_questions_by_qid(template))


# ---------------------------------------------------------------------------
# add
# ---------------------------------------------------------------------------


def add_questions(
    survey: Survey,
    questions: Sequence[dict[str, Any] | DraftQuestion] | dict[str, Any] | DraftQuestion,
    *,
    block_id: str | None = None,
    after_question_id: str | None = None,
    before_question_id: str | None = None,
    image_resolver: ImageResolver | None = None,
    policy: BuilderPolicy | None = None,
) -> Survey:
    """A new survey with these draft questions inserted into one block.

    Position: right after ``after_question_id``, right before
    ``before_question_id``, or at the end of the block. ``block_id`` may be
    left out when the survey has one block or an anchor question names it.
    Supplied IDs are kept; blank ones get the next ``Q<n>``. Each new question
    gets the next free internal ``QID<n>``.
    """
    items = [questions] if isinstance(questions, (dict, DraftQuestion)) else list(questions)
    if not items:
        raise DraftError([_issue("No questions to add.", "add.empty")])
    if after_question_id is not None and before_question_id is not None:
        raise SurveyError("Pass after_question_id or before_question_id, not both.")
    template = copy.deepcopy(survey._base_template)
    by_qid = _questions_by_qid(template)
    anchor_qid = None
    if after_question_id is not None or before_question_id is not None:
        anchor_qid = _resolve(survey, after_question_id if after_question_id is not None else before_question_id)
        anchor_block = by_qid[anchor_qid][0]
        if block_id is not None and block_id != anchor_block:
            raise SurveyError(f"{after_question_id or before_question_id} is in block {anchor_block}, not {block_id}.")
        block_id = anchor_block
    if block_id is None:
        block_ids = list(dict.fromkeys(str(b.get("BlockID", "")) for b in _block_elements(_visible(template, survey))))
        if len(block_ids) != 1:
            raise SurveyError(f"This survey has {len(block_ids)} blocks; pass block_id (one of: {', '.join(block_ids)}).")
        block_id = block_ids[0]
    if not _block_elements(template, block_id):
        known = list(dict.fromkeys(str(b.get("BlockID", "")) for b in _block_elements(template)))
        raise SurveyError(f"Unknown block ID {block_id!r}. Known: {', '.join(known)}")
    _check_block_editable(survey, block_id)

    shape = [
        _issue(f"Question {idx + 1} must be a JSON object, not {type(item).__name__}.", "question.invalid", index=idx)
        for idx, item in enumerate(items)
        if not isinstance(item, (dict, DraftQuestion))
    ]
    if shape:
        raise DraftError(shape)
    drafts = [DraftQuestion.from_dict(item) for item in items]
    carried = [
        _issue(
            f"Question {idx + 1} has the internal ID {d.qid!r} of another survey; new questions get this survey's own. Clear 'qid' first.",
            "qid.not_allowed",
            index=idx,
            field="qid",
        )
        for idx, d in enumerate(drafts)
        if d.qid
    ]
    if carried:
        raise DraftError(carried)
    visible = _visible(template, survey)
    taken = _taken_tags(visible)
    # A blank ID gets a tag no question has used, dropped ones included; an ID
    # that is not text stays, for validation to name.
    new_taken = _taken_tags(template) | {d.id.strip() for d in drafts if isinstance(d.id, str) and d.id.strip()}
    existing_columns = set(_template_columns(visible))
    for d in drafts:
        if isinstance(d.id, str) and not d.id.strip():
            d.id = _next_tag(new_taken, d, existing_columns)
            new_taken.add(d.id)
        elif isinstance(d.id, str):
            d.id = d.id.strip()
    issues: list[DraftIssue] = []
    for idx, d in enumerate(drafts):
        issues.extend(validate_question(d, idx, policy=policy))
    issues.extend(id_issues(drafts, taken=taken, taken_columns=_template_columns(visible)))
    if policy is not None and policy.max_questions is not None and _count_questions(visible) + len(drafts) > policy.max_questions:
        issues.append(_issue(f"The survey exceeds {policy.max_questions} questions.", "survey.too_many"))
    if errors_only(issues):
        raise DraftError(issues)
    warnings = [i for i in issues if i.severity != "error"]

    order = _block_order(template, block_id)
    block_questions = {str(q.get("QuestionID")): q for q in _block_elements(template, block_id)[0].get("Questions") or []}
    if anchor_qid is None:
        position = len(order)
    else:
        position = order.index(anchor_qid) + (1 if after_question_id is not None else 0)
    next_number = _next_qid_number(survey, template)
    new_questions: list[dict[str, Any]] = []
    authoring = copy.deepcopy(survey._authoring)
    unresolved = [dict(m) for m in survey._unresolved_media]
    added: list[dict[str, str]] = []
    for offset, d in enumerate(drafts):
        qid = f"QID{next_number + offset}"
        template_q, missing = question_to_template(d, qid, image_resolver=image_resolver)
        new_questions.append(template_q)
        unresolved.extend(missing)
        authoring[qid] = {"draft": _payload(d), "block_id": block_id}
        added.append({"id": d.id, "qid": qid})
    ordered = [block_questions[q] for q in order]
    ordered[position:position] = new_questions
    _replace_in_blocks(template, block_id, ordered)
    after_visible = _visible(template, survey)
    piping = [i for q in new_questions for i in _piping_issues(after_visible, str(q["QuestionID"]), q, dropped=_dropped_qids(survey))]
    if piping:
        raise DraftError(piping)
    _check_draft_pipes(survey, template, authoring)
    _check_new_column_clashes(_visible(survey._base_template, survey), after_visible)
    return _with_warnings(survey._derive(
        template=template,
        authoring=authoring,
        unresolved_media=unresolved,
        edits=_log(survey, {"op": "add_questions", "block_id": block_id, "position": position, "questions": added}),
    ), warnings)


def _builder_question_template(
    draft: DraftQuestion,
    qid: str,
    current: dict[str, Any],
    old_unresolved: list[dict[str, Any]],
    *,
    new_image: bool,
    image_resolver: ImageResolver | None,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """The template of a builder question changed to ``draft``, and its unresolved
    images. A new image asks the resolver; otherwise the image stays as it is
    (resolved or not), without asking a resolver again. A builder image carries no
    title: its file name (``image_name``) stays in the draft."""
    if new_image:
        return question_to_template(draft, qid, image_resolver=image_resolver)
    new_q, _ignored = question_to_template(draft, qid, image_resolver=lambda _ref: None)
    media = copy.deepcopy(current.get("QuestionMedia") or [])
    for item in media:
        item.pop("title", None)
    if media:
        new_q["QuestionMedia"] = media
        if draft.type == "instruction" and not str(draft.text).strip():
            new_q["QuestionText"] = "[Image stimulus attached]"
    else:
        new_q.pop("QuestionMedia", None)
    return new_q, [{**m, "question": draft.id, "name": str(draft.image_name).strip()} for m in old_unresolved]


def _with_warnings(survey: Survey, warnings: list[DraftIssue]) -> Survey:
    """The edited survey, with the edit's warnings (a policy's demographic warning,
    ...) in ``edit_warnings``; they are not part of the survey's definition."""
    survey.edit_warnings = list(warnings)
    return survey


# ---------------------------------------------------------------------------
# update
# ---------------------------------------------------------------------------


def update_question(
    survey: Survey,
    question_id: str,
    changes: dict[str, Any] | None = None,
    *,
    image_resolver: ImageResolver | None = None,
    policy: BuilderPolicy | None = None,
    **fields: Any,
) -> Survey:
    """A new survey with one question's fields changed.

    ``changes`` (or keyword arguments) use the draft field names: ``text``,
    ``required``, ``options``, ``rows``, ``min_selections``,
    ``max_selections``, ``min_chars``, ``max_chars``, ``min_value``,
    ``max_value``, ``step``, ``decimals``, ``image_id``, ``image_name``,
    ``rationale``, ``type``, and ``id`` (renames the export tag; the internal ID
    stays). A question the builder made takes any of them. A question read from
    a QSF takes only what maps onto it without losing anything (see the module
    notes); anything else raises ``UnsupportedEdit``.
    """
    change = {**(changes or {}), **fields}
    if not change:
        raise DraftError([_issue("No changes given.", "update.empty")])
    unknown = [k for k in change if k not in DRAFT_FIELDS and k != "qid"]
    if unknown:
        raise DraftError([_issue(f"Unknown field {k!r}; the fields are: {', '.join(DRAFT_FIELDS)}.", "field.unknown", question_id=str(question_id), field=k) for k in unknown])
    qid = _resolve(survey, question_id)
    if "qid" in change:
        # A web form may send the whole question back, qid included (from to_draft).
        given = change.pop("qid")
        if given not in (None, "") and str(given).strip() != qid:
            raise DraftError([_issue(f"A question's internal ID cannot change: this is {qid}, not {given!r}.", "qid.fixed", question_id=str(question_id), field="qid")])
        if not change:
            raise DraftError([_issue("No changes given.", "update.empty")])
    template = copy.deepcopy(survey._base_template)
    block_id, current = _questions_by_qid(template)[qid]
    _check_block_editable(survey, block_id)
    visible = _visible(template, survey)
    taken = _taken_tags(visible, except_qids=[qid])
    columns = _template_columns(visible, except_qids=[qid])
    authoring = copy.deepcopy(survey._authoring)
    unresolved = [dict(m) for m in survey._unresolved_media if str(m.get("question_id")) != qid]
    old_unresolved = [dict(m) for m in survey._unresolved_media if str(m.get("question_id")) == qid]

    if qid in authoring and "image_id" in change:
        given = "" if change.get("image_id") is None else str(change["image_id"]).strip()  # 0 is the text "0"
        if given == str(authoring[qid]["draft"].get("image_id") or "").strip():
            change = {k: v for k, v in change.items() if k != "image_id"}  # the same image: nothing to resolve
            if not change:
                return survey  # nothing changes
    warnings: list[DraftIssue] = []
    if qid in authoring:
        from .build import differs_from_draft

        differs = differs_from_draft(current, DraftQuestion.from_dict(authoring[qid]["draft"]), qid)
        if differs:
            raise UnsupportedEdit(
                f"{_label(qid, current)} no longer matches its draft form ({', '.join(differs)} changed outside the builder); "
                "editing it from its draft would lose that."
            )
        merged = {**authoring[qid]["draft"], **change}
        draft = DraftQuestion.from_dict(merged)
        issues = []
        if "id" in change and isinstance(draft.id, str) and not draft.id.strip():
            issues.append(_issue("A question ID cannot be blank.", "id.missing", question_id=_tag(current), field="id"))
        if isinstance(draft.id, str):  # anything else is named by validate_question
            draft.id = draft.id.strip() or _tag(current)
        issues += validate_question(draft, policy=policy) + id_issues([draft], taken=taken, taken_columns=columns)
        if errors_only(issues):
            raise DraftError(issues)
        warnings = [i for i in issues if i.severity != "error" and (i.field in change or i.field == "")]
        new_q, missing = _builder_question_template(
            draft, qid, current, old_unresolved,
            new_image="image_id" in change, image_resolver=image_resolver,
        )
        unresolved.extend(missing)
        authoring[qid] = {**authoring[qid], "draft": _payload(draft)}
    else:
        new_q, missing = _update_qsf_question(visible, qid, current, change, image_resolver=image_resolver, taken=taken, columns=columns)
        unresolved.extend(missing if "image_id" in change else old_unresolved)
        if policy is not None and "text" in change:
            probe = DraftQuestion(type="open_text", id=_tag(new_q), text=str(new_q.get("QuestionText", "")))
            warnings = [i for i in validate_question(probe, policy=policy) if i.code == "text.demographic"]

    for block in _block_elements(template, block_id):
        block["Questions"] = [new_q if isinstance(q, dict) and str(q.get("QuestionID")) == qid else q for q in block.get("Questions") or []]
    after_visible = _visible(template, survey)
    if {"text", "options", "rows"} & set(change):
        piping = _piping_issues(after_visible, qid, new_q, dropped=_dropped_qids(survey), fields=set(change), before=current)
        if piping:
            raise DraftError(piping)
    # The whole-survey rules that building and removing apply.
    if _answerable_count(_visible(survey._base_template, survey)) and not _answerable_count(after_visible):
        raise DraftError([_issue("The survey would have no answerable questions left.", "survey.no_answerable", question_id=_tag(new_q), field="type")])
    _check_draft_pipes(survey, template, authoring)
    _check_new_column_clashes(_visible(survey._base_template, survey), after_visible, _tag(new_q))
    entry = {"op": "update_question", "qid": qid, "question": _tag(new_q), "fields": sorted(change)}
    if "id" in change and _tag(current) != _tag(new_q):
        entry["renamed_from"] = _tag(current)
    return _with_warnings(survey._derive(template=template, authoring=authoring, unresolved_media=unresolved, edits=_log(survey, entry)), warnings)


def _logic_references(template: dict[str, Any], qid: str) -> list[str]:
    """Where logic, piping or skip logic reads this question (its own skip logic included)."""
    places: list[str] = []
    pattern = re.compile(rf"\b{re.escape(qid)}\b")

    def mentions(value: Any) -> bool:
        return bool(pattern.search(str(value)))

    def walk(elements: Any) -> None:
        if not isinstance(elements, list):
            return
        for element in elements:
            if not isinstance(element, dict):
                continue
            etype = element.get("ElementType")
            if etype == "Branch" and mentions(element.get("BranchLogic")):
                places.append(f"branch at FL {element.get('FlowID')}")
            if etype == "EmbeddedData" and mentions(element.get("EmbeddedData")):
                places.append(f"embedded data at FL {element.get('FlowID')}")
            if etype == "Block":
                for q in element.get("Questions") or []:
                    if not isinstance(q, dict):
                        continue
                    other = str(q.get("QuestionID", ""))
                    if q.get("DisplayLogic") and mentions(q.get("DisplayLogic")):
                        places.append(f"display logic of {_label(other, q)}")
                    if choice_logic_of(q) and mentions(choice_logic_of(q)):
                        places.append(f"display logic on the options of {_label(other, q)}")
                    # Skip logic reads the question in its conditions; jumping to it reads nothing.
                    if any(mentions([r.get("ChoiceLocator"), r.get("Locator"), r.get("LeftOperand")]) for r in _skip_rules(q)):
                        places.append(f"skip logic of {_label(other, q)}")
                    rest = {k: v for k, v in q.items() if k not in {"DisplayLogic", "SkipLogic", "QuestionID", *LOGIC_KEYS}}
                    if qid in _PIPED_QID.findall(json_text(rest)):
                        places.append(f"piped text in {_label(other, q)}")
            walk(element.get("Elements"))

    walk(template.get("Elements", []))
    return list(dict.fromkeys(places))


def _qsf_kind(q: dict[str, Any]) -> str:
    qtype = str(q.get("QuestionType", ""))
    settings = q.get("Settings") or {}
    selector = str(settings.get("Selector", ""))
    if q.get("is_unsupported") or q.get("is_timing") or q.get("is_meta") or q.get("is_captcha"):
        return "other"
    if qtype == "DB" or q.get("is_descriptive"):
        return "descriptive"
    if qtype == "MC":
        return "mc_multi" if selector in MULTI_SELECTORS else "mc_single"
    if qtype == "Matrix" and selector == "Likert" and str(settings.get("SubSelector", "")) in {"SingleAnswer", "MultipleAnswer"}:
        return "matrix"
    if qtype == "TE" and selector in {"SL", "ML", "ESTB"}:
        return "text"
    if qtype == "Slider" and selector == "HSLIDER" and len(q.get("Statements") or []) == 1:
        return "slider"
    return "other"


_QSF_FIELDS = {
    "descriptive": {"text", "id", "image_id", "image_name", "image_bytes"},
    "mc_single": {"text", "id", "required", "options", "image_id", "image_name", "image_bytes"},
    "mc_multi": {"text", "id", "required", "options", "min_selections", "max_selections", "image_id", "image_name", "image_bytes"},
    "matrix": {"text", "id", "required", "rows", "options", "image_id", "image_name", "image_bytes"},
    "text": {"text", "id", "required", "min_chars", "max_chars", "image_id", "image_name", "image_bytes"},
    "slider": {"text", "id", "required", "min_value", "max_value", "step", "decimals", "image_id", "image_name", "image_bytes"},
    "other": {"text", "id", "image_id", "image_name", "image_bytes"},
}
_LIST_BLOCKERS = {
    "options": ("OptionsID", "RecodeValues", "TextEntryOptions", "OptionMedia", "DefaultChoices", "DefaultEntries", "AdvancedRandomization",
                "ChoiceDisplayLogic"),
    "rows": ("RowsID", "RowsMedia", "DefaultChoices", "DefaultEntries", "AdvancedRandomization", "ChoiceDisplayLogic"),
    "matrix_options": ("ColumnsID", "ColumnsMedia", "RecodeValues", "DefaultChoices", "DefaultEntries", "AnswerDisplayLogic"),
}
_BLOCKER_NAMES = {
    "OptionsID": "its own choice IDs (recodes or a custom order)",
    "RecodeValues": "recode values",
    "TextEntryOptions": "write-in options",
    "OptionMedia": "option images",
    "DefaultChoices": "default answers",
    "RowsID": "its own row IDs",
    "RowsMedia": "row images",
    "ColumnsID": "its own column IDs",
    "ColumnsMedia": "column images",
    "DefaultEntries": "default answers",
    "AdvancedRandomization": "a custom choice-randomization order",
    # kept by item ID: replacing the list would hand each item the logic of the one at its place
    "ChoiceDisplayLogic": "display logic on its items",
    "AnswerDisplayLogic": "display logic on its scale points",
}


def _update_qsf_question(
    template: dict[str, Any],
    qid: str,
    current: dict[str, Any],
    change: dict[str, Any],
    *,
    image_resolver: ImageResolver | None,
    taken: set[str],
    columns: dict[str, str],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    kind = _qsf_kind(current)
    label = _label(qid, current)
    if "type" in change:
        raise UnsupportedEdit(
            f"{label} was read from the QSF; changing its type would lose its Qualtrics settings. "
            "Remove it and add a new question instead."
        )
    if "rationale" in change:
        raise UnsupportedEdit(f"{label} was read from the QSF; a rationale applies only to questions the builder made.")
    refused = sorted(k for k in change if k not in _QSF_FIELDS[kind])
    if refused:
        raise UnsupportedEdit(
            f"{label} ({current.get('QuestionType')}/{(current.get('Settings') or {}).get('Selector')}) was read from the QSF; "
            f"the builder cannot change its {', '.join(refused)} without losing Qualtrics content. Edit it in Qualtrics."
        )
    q = copy.deepcopy(current)
    settings = dict(q.get("Settings") or {})
    issues: list[DraftIssue] = []

    def issue(message: str, code: str, field: str) -> None:
        issues.append(_issue(message, code, question_id=_tag(current), field=field))

    # Text fields take text (a number is written out); an object or a list is an
    # error, never turned into its printed form.
    for key in ("text", "id", "image_id", "image_name"):
        value = change.get(key)
        if key in change and value is not None and (isinstance(value, bool) or not isinstance(value, (str, int, float))):
            issue(f"'{key}' must be text.", "field.invalid", key)
    if issues:
        raise DraftError(issues)

    if "text" in change:
        text = "" if change["text"] is None else str(change["text"]).strip()
        if not text and not (kind == "descriptive" and q.get("QuestionMedia")):
            issue("Question text is empty.", "text.empty", "text")
        q["QuestionText"] = text
    if "id" in change and str(current.get("QuestionType", "")) == "SBS":
        raise UnsupportedEdit(f"{label} is a side-by-side question; its columns are named by its sub-questions. Rename it in Qualtrics.")
    if "id" in change:
        new_tag = "" if change["id"] is None else str(change["id"]).strip()
        probe = DraftQuestion(type="single_choice", id=new_tag)
        if not new_tag:
            issue("A question ID cannot be blank.", "id.missing", "id")
        else:
            issues.extend(id_issues([probe], taken=taken, taken_columns=columns))
        q["QuestionName"] = new_tag
    if "required" in change:
        if not isinstance(change["required"], bool):
            issue("'required' must be true or false.", "field.invalid", "required")
        else:
            settings["ForceResponse"] = "ON" if change["required"] else "OFF"
            if "ForceResponseType" in settings:
                settings["ForceResponseType"] = settings["ForceResponse"]

    def replace_list(field: str, key: str, blockers: tuple[str, ...], draft_type: str) -> None:
        new = change[field]
        if not isinstance(new, list) or not all(isinstance(v, str) for v in new):
            issue(f"'{field}' must be a list of text.", "field.invalid", field)
            return
        randomization = q.get("Randomization") if isinstance(q.get("Randomization"), dict) else {}
        present = [b for b in blockers if (randomization.get("Advanced") if b == "AdvancedRandomization" else q.get(b))]
        if present:
            raise UnsupportedEdit(
                f"{label} has {', '.join(_BLOCKER_NAMES.get(b, b) for b in present)}; replacing its {field} would lose them. Edit it in Qualtrics."
            )
        cleaned = [v.strip() for v in new if v.strip()]
        old = list(q.get(key) or [])
        if len(cleaned) != len(old):
            places = _logic_references(template, qid)
            if places:
                raise UnsupportedEdit(
                    f"{label} is read by {', '.join(places)}. That logic refers to its {field} by position, so "
                    f"their number cannot change here ({len(old)} now, {len(cleaned)} asked). Relabeling them in place is allowed."
                )
        probe = DraftQuestion(type=draft_type, text="x", options=cleaned if key in {"Options", "Columns"} else ["a", "b"], rows=cleaned if key == "Rows" else ["r"])
        if draft_type == "grid" and key == "Columns":
            probe.rows = list(q.get("Rows") or ["r"])
        if draft_type == "grid" and key == "Rows":
            probe.options = list(q.get("Columns") or ["a", "b"])
        for problem in validate_question(probe):
            if problem.field == field:
                issue(problem.message, problem.code, field)
        subset = str(randomization.get("TotalRandSubset") or "").strip()
        if key == "Options" and subset.isdigit() and int(subset) > len(cleaned):
            issue(f"The question shows a random {subset} of its options; it needs at least that many.", "options.too_few", field)
        q[key] = cleaned

    if "options" in change:
        if kind in {"mc_single", "mc_multi"}:
            replace_list("options", "Options", _LIST_BLOCKERS["options"], "multi_select" if kind == "mc_multi" else "single_choice")
        else:
            replace_list("options", "Columns", _LIST_BLOCKERS["matrix_options"], "grid")
        if kind == "mc_multi" and not ("min_selections" in change or "max_selections" in change):
            probe = DraftQuestion(type="multi_select", text="x", options=list(q.get("Options") or []),
                                  min_selections=_int_or_none(settings.get("MinChoices")), max_selections=_int_or_none(settings.get("MaxChoices")))
            for problem in validate_question(probe):
                if problem.field in {"min_selections", "max_selections"}:
                    issue(problem.message + " (the question's selection limits)", problem.code, "options")
    if "rows" in change:
        replace_list("rows", "Rows", _LIST_BLOCKERS["rows"], "grid")
    if kind == "mc_multi" and ("min_selections" in change or "max_selections" in change):
        low = change.get("min_selections", _int_or_none(settings.get("MinChoices")))
        high = change.get("max_selections", _int_or_none(settings.get("MaxChoices")))
        probe = DraftQuestion(type="multi_select", text="x", options=list(q.get("Options") or []), min_selections=low, max_selections=high)
        for problem in validate_question(probe):
            if problem.field in {"min_selections", "max_selections"}:
                issue(problem.message, problem.code, problem.field)
        for key, value in (("MinChoices", low), ("MaxChoices", high)):
            if value:
                settings[key] = str(value)
            else:
                settings.pop(key, None)
    if kind == "text" and ("min_chars" in change or "max_chars" in change):
        if str(settings.get("ValidationType", "")) == "ContentType" or str(settings.get("ContentType", "") or "").strip():
            raise UnsupportedEdit(f"{label} checks its answer's format ({settings.get('ContentType') or 'content type'}); its length limits cannot be changed here. Edit it in Qualtrics.")
        # A limit counts only when the current validation type uses it (exports keep stale values).
        vtype = str(settings.get("ValidationType", "") or "")
        low_now = _int_or_none(settings.get("MinChars")) if vtype in {"MinChar", "CharRange"} else None
        high_now = _int_or_none(settings.get("TotalChars")) if vtype in {"TotalChar", "CharRange"} else None
        low = change.get("min_chars", low_now)
        high = change.get("max_chars", high_now)
        probe = DraftQuestion(type="open_text", text="x", min_chars=low, max_chars=high)
        for problem in validate_question(probe):
            if problem.field in {"min_chars", "max_chars"}:
                issue(problem.message, problem.code, problem.field)
        for key, value in (("MinChars", low), ("TotalChars", high)):
            if value:
                settings[key] = str(value)
            else:
                settings.pop(key, None)
        from .convert import _validation_type

        settings["ValidationType"] = _validation_type(bool(low), bool(high), "MinChar", "TotalChar", "CharRange")
    if kind == "slider" and any(k in change for k in ("min_value", "max_value", "step", "decimals")):
        rng = dict(q.get("Range") or {})
        low = change.get("min_value", rng.get("Min"))
        high = change.get("max_value", rng.get("Max"))
        step = change.get("step", settings.get("NumericStep"))
        if "step" not in change and settings.get("SnapToGrid") and rng.get("Ticks") and low is not None and high is not None:
            # A snap-to-grid slider's step is its range over its grid lines (as the parser works it out).
            try:
                step = (float(high) - float(low)) / float(rng["Ticks"])
                change = {**change, "step": step}
            except (TypeError, ValueError, ZeroDivisionError):
                pass
        decimals = change.get("decimals", _int_or_none(settings.get("NumericDecimals")))
        probe = DraftQuestion(type="number", text="x", min_value=low, max_value=high, step=step, decimals=decimals)
        for problem in validate_question(probe):
            if problem.field in {"min_value", "max_value", "step", "decimals"}:
                issue(problem.message, problem.code, problem.field)
        try:
            if step is not None and low is not None and high is not None and float(step) > float(high) - float(low):
                issue("The step is larger than the whole range.", "step.above_range", "step")
            for default in q.get("DefaultChoices") or []:
                value = float(default.get("Value")) if isinstance(default, dict) and default.get("Value") not in (None, "") else None
                if value is not None and low is not None and high is not None and not float(low) <= value <= float(high):
                    raise UnsupportedEdit(f"{label} has a default answer ({default.get('Value')}) outside the new range. Edit it in Qualtrics.")
        except (TypeError, ValueError):
            pass
        if not issues:
            from .convert import _number

            if low is not None:
                rng["Min"] = _number(low)
            if high is not None:
                rng["Max"] = _number(high)
            q["Range"] = rng
            if "decimals" in change:
                if decimals is None:
                    settings.pop("NumericDecimals", None)
                else:
                    settings["NumericDecimals"] = int(decimals)
            if "step" in change:
                if step is None:
                    settings.pop("NumericStep", None)
                else:
                    settings["NumericStep"] = _number(step)
    missing: list[dict[str, Any]] = []
    media_now = [m for m in q.get("QuestionMedia") or [] if isinstance(m, dict)]
    if "image_id" in change and media_now and str(change.get("image_id") or "").strip() == str(media_now[0].get("src", "")).strip():
        change = {k: v for k, v in change.items() if k != "image_id"}  # the same image
    if "image_id" in change or "image_name" in change:
        media = [m for m in q.get("QuestionMedia") or [] if isinstance(m, dict)]
        if len(media) > 1:
            raise UnsupportedEdit(f"{label} shows {len(media)} images; the builder edits one image per question. Edit it in Qualtrics.")
        reference = ("" if change.get("image_id") is None else str(change["image_id"]).strip()) if "image_id" in change else ""
        # The file name (image_name) is not written into the question: the twin
        # would read it. It only names an image that did not resolve.
        name = "" if change.get("image_name") is None else str(change["image_name"]).strip()
        if "image_id" in change and not reference:
            q.pop("QuestionMedia", None)
        elif reference:
            src, why = resolve_image(reference, image_resolver)
            if src:
                q["QuestionMedia"] = [{"kind": "image", "src": src}]
            else:
                q.pop("QuestionMedia", None)
                missing.append({"question_id": qid, "question": _tag(q), "reference": reference, "name": name, "detail": why})
    if issues:
        raise DraftError(issues)
    q["Settings"] = settings
    return q, missing


def _skip_rules(question: dict[str, Any]) -> list[dict[str, Any]]:
    rules = question.get("SkipLogic")
    if isinstance(rules, dict):
        rules = list(rules.values())
    return [r for r in rules or [] if isinstance(r, dict)] if isinstance(rules, list) else []


def _int_or_none(value: Any) -> int | None:
    try:
        return None if value in (None, "") else int(float(str(value)))
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# remove
# ---------------------------------------------------------------------------


def remove_questions(survey: Survey, question_ids: str | Sequence[str]) -> Survey:
    """A new survey without these questions (one ID or a list).

    Refused, with every place named, when logic still reads a removed question:
    a branch, display logic, skip logic that jumps to it, piped text or embedded
    data. A block left empty disappears from the flow.
    """
    names = [question_ids] if isinstance(question_ids, str) else list(question_ids)
    if not names:
        raise DraftError([_issue("No questions to remove.", "remove.empty")])
    template = survey._base_template
    by_qid = _questions_by_qid(template)
    qids = list(dict.fromkeys(_resolve(survey, name) for name in names))
    for qid in qids:
        _check_block_editable(survey, by_qid[qid][0])
    after = remove_template_questions(template, qids)
    removed = set(qids)
    after_visible = _visible(after, survey)  # a dropped block's logic reads nothing
    problems = [f"{d.kind} at {d.location} reads {_label(d.reads, by_qid.get(d.reads, ('', None))[1])}" for d in _find_dependencies(after_visible, removed, set())]
    for block in _block_elements(after_visible):
        for q in block.get("Questions") or []:
            for rule in _skip_rules(q):
                destination = str(rule.get("SkipToDestination", ""))
                if destination in removed:
                    problems.append(f"skip logic of {_label(str(q.get('QuestionID')), q)} jumps to {_label(destination, by_qid[destination][1])}")
    if problems:
        raise UnsupportedEdit(
            "Other parts of the survey still read the question(s) to remove; change or remove them first:\n"
            + "\n".join(f"  {p}" for p in dict.fromkeys(problems))
        )
    if _answerable_count(_visible(template, survey)) and not _answerable_count(after_visible):
        raise DraftError([_issue("The survey would have no answerable questions left.", "survey.no_answerable")])
    blocks_before = {str(b.get("BlockID", "")) for b in _block_elements(template)}
    blocks_after = {str(b.get("BlockID", "")) for b in _block_elements(after)}
    authoring = {k: v for k, v in copy.deepcopy(survey._authoring).items() if k not in removed}
    _check_draft_pipes(survey, after, authoring)
    entry = {
        "op": "remove_questions",
        "questions": [{"id": _tag(by_qid[q][1]), "qid": q} for q in qids],
        "blocks_removed": sorted(blocks_before - blocks_after),
    }
    return survey._derive(template=after, authoring=authoring, edits=_log(survey, entry))


# ---------------------------------------------------------------------------
# reorder
# ---------------------------------------------------------------------------


def reorder_questions(
    survey: Survey,
    order: Sequence[str] | None = None,
    *,
    swap: tuple[str | Sequence[str], str | Sequence[str]] | None = None,
    move: tuple[str, str | int] | None = None,
    block_id: str | None = None,
) -> Survey:
    """A new survey with questions of one block in a new order. Give exactly one of:

    - ``order``: every question of the block, in the new order.
    - ``swap=(a, b)``: exchange two questions, or two runs of consecutive
      questions (each a list of IDs), keeping each run's own order.
    - ``move=(question, "up" | "down")``: move one question one place (``-1``
      and ``+1`` also work).

    Questions stay in their block and keep their IDs. Refused when it would
    make a question's display logic or piped text read a question that now
    comes after it, or make skip logic jump backwards.
    """
    modes = [m for m in (order is not None, swap is not None, move is not None) if m]
    if len(modes) != 1:
        raise SurveyError("Give exactly one of: order=[...], swap=(a, b), move=(question, 'up'|'down').")
    template = copy.deepcopy(survey._base_template)
    by_qid = _questions_by_qid(template)

    def block_of(qids: list[str]) -> str:
        blocks = sorted({by_qid[q][0] for q in qids})
        if len(blocks) > 1:
            where = "; ".join(f"{b}: {', '.join(_label(q, by_qid[q][1]) for q in qids if by_qid[q][0] == b)}" for b in blocks)
            raise SurveyError(f"Reordering works within one block; these questions are in {len(blocks)} blocks ({where}). Remove and add a question to move it to another block.")
        found = blocks[0]
        if block_id is not None and block_id != found:
            raise SurveyError(f"The questions are in block {found}, not {block_id}.")
        return found

    if order is not None:
        names = [order] if isinstance(order, str) else list(order)
        qids = [_resolve(survey, n) for n in names]
        repeated = sorted({q for q in qids if qids.count(q) > 1})
        if repeated:
            raise SurveyError("A question appears more than once in the order: " + ", ".join(_label(q, by_qid[q][1]) for q in repeated))
        block = block_of(qids)
        current = _block_order(template, block)
        missing = [q for q in current if q not in qids]
        if missing:
            raise SurveyError(f"The order must list every question of block {block}; missing: " + ", ".join(_label(q, by_qid[q][1]) for q in missing))
        new_order = qids
        op = {"op": "reorder_questions", "mode": "order", "block_id": block}
    elif swap is not None:
        if not isinstance(swap, (tuple, list)) or len(swap) != 2:
            raise SurveyError("swap takes two questions or two lists of consecutive questions: swap=(a, b).")
        groups = []
        for side in swap:
            names = [side] if isinstance(side, str) else list(side)
            if not names:
                raise SurveyError("Each side of swap needs at least one question.")
            groups.append([_resolve(survey, n) for n in names])
        block = block_of(groups[0] + groups[1])
        current = _block_order(template, block)
        spans = []
        for group in groups:
            positions = sorted(current.index(q) for q in group)
            if positions != list(range(positions[0], positions[0] + len(positions))):
                raise SurveyError(
                    "Questions swapped as a group must be next to each other: "
                    + ", ".join(_label(q, by_qid[q][1]) for q in group)
                    + f" are not consecutive in block {block}."
                )
            spans.append((positions[0], positions[-1] + 1))
        (a0, a1), (b0, b1) = sorted(spans)
        if a1 > b0:
            raise SurveyError("The two sides of swap overlap.")
        new_order = current[:a0] + current[b0:b1] + current[a1:b0] + current[a0:a1] + current[b1:]
        op = {"op": "reorder_questions", "mode": "swap", "block_id": block, "swap": [[_tag(by_qid[q][1]) for q in g] for g in groups],
              "swap_qids": [list(g) for g in groups]}
    else:
        if not isinstance(move, (tuple, list)) or len(move) != 2:
            raise SurveyError("move takes a question and a direction: move=('Q3', 'up').")
        name, direction = move
        step = {"up": -1, "down": 1, -1: -1, 1: 1}.get(direction.lower() if isinstance(direction, str) else direction)
        if step is None:
            raise SurveyError("move's direction is 'up' or 'down' (or -1 / +1).")
        qid = _resolve(survey, name)
        block = block_of([qid])
        current = _block_order(template, block)
        pos = current.index(qid)
        target = pos + step
        if not 0 <= target < len(current):
            raise SurveyError(f"{_label(qid, by_qid[qid][1])} is already {'first' if step < 0 else 'last'} in block {block}.")
        new_order = list(current)
        new_order[pos], new_order[target] = new_order[target], new_order[pos]
        op = {"op": "reorder_questions", "mode": "move", "block_id": block, "move": [_tag(by_qid[qid][1]), "up" if step < 0 else "down"],
              "move_qid": qid}

    _check_block_editable(survey, block)
    current = _block_order(template, block)
    problems = _order_problems(template, block, current, new_order, by_qid)
    if problems:
        raise UnsupportedEdit("This order would break the survey's logic:\n" + "\n".join(f"  {p}" for p in problems))
    questions = {str(q.get("QuestionID")): q for q in _block_elements(template, block)[0].get("Questions") or []}
    _replace_in_blocks(template, block, [questions[q] for q in new_order])
    _check_draft_pipes(survey, template, survey._authoring)
    # The internal IDs say which question moved (tags can be renamed later, or repeat).
    op["order"] = [_tag(by_qid[q][1]) for q in new_order]
    op["order_qids"] = list(new_order)
    return survey._derive(template=template, edits=_log(survey, op))


def _order_problems(template: dict[str, Any], block: str, old: list[str], new: list[str], by_qid: dict[str, tuple[str, dict[str, Any]]]) -> list[str]:
    """Logic within the block that was in a valid order before and is not after."""
    old_pos = {q: i for i, q in enumerate(old)}
    new_pos = {q: i for i, q in enumerate(new)}
    problems: list[str] = []
    for qid in old:
        q = by_qid[qid][1]
        reads: set[str] = set()
        if q.get("DisplayLogic"):
            reads |= set(_QID_ANYWHERE.findall(str(q.get("DisplayLogic"))))
        if choice_logic_of(q):  # display logic on its options, matrix statements or scale points
            reads |= set(_QID_ANYWHERE.findall(str(choice_logic_of(q))))
        rest = {k: v for k, v in q.items() if k not in {"DisplayLogic", "SkipLogic", "QuestionID", *LOGIC_KEYS}}
        reads |= set(_PIPED_QID.findall(str(rest)))
        for other in sorted(reads - {qid}):
            if other in old_pos and old_pos[other] < old_pos[qid] and new_pos[other] > new_pos[qid]:
                problems.append(f"{_label(qid, q)} reads {_label(other, by_qid[other][1])}, which would come after it")
        for rule in _skip_rules(q):
            destination = str(rule.get("SkipToDestination", ""))
            if destination in old_pos and old_pos[destination] > old_pos[qid] and new_pos[destination] < new_pos[qid]:
                problems.append(f"skip logic of {_label(qid, q)} would jump backwards to {_label(destination, by_qid[destination][1])}")
    return problems


# ---------------------------------------------------------------------------
# a whole draft sent back
# ---------------------------------------------------------------------------


# Names a form may use for a question type, so the same content is recognized
# whatever it calls its type.
_TYPE_ALIASES = {
    "mc": "single_choice",
    "singlechoice": "single_choice",
    "single": "single_choice",
    "likert": "scale",
    "multi_choice": "multi_select",
    "multiselect": "multi_select",
    "checkbox": "multi_select",
    "matrix": "grid",
    "statement_grid": "grid",
    "text": "open_text",
    "openended": "open_text",
    "open_ended": "open_text",
    "essay": "open_text",
    "numeric": "number",
    "slider": "number",
    "descriptive": "instruction",
}


def _content_key(kind: Any, text: Any, options: Any, rows: Any) -> tuple[Any, ...]:
    """A question's content, for telling which item of an edited form is which question."""
    def norm(value: Any) -> Any:
        if isinstance(value, list):
            return tuple(str(v).strip() for v in value)
        return str(value or "").strip()

    kind_text = str(kind or "").strip().lower()
    return (_TYPE_ALIASES.get(kind_text, kind_text), norm(text), norm(options or []), norm(rows or []))


def apply_draft(
    survey: Survey,
    draft: Any,
    *,
    image_resolver: ImageResolver | None = None,
    policy: BuilderPolicy | None = None,
) -> Survey:
    """The survey with an edited draft (what a web form sends back) applied to it.

    Each draft question is matched to the survey question it came from. A form
    that keeps ``qid`` says which is which: a question keeps the ID it carries,
    and one without an ID is new (even with a deleted question's export tag). A
    form that sends no ``qid`` at all is matched by the same type, text, options
    and rows, then by the same export tag and type (a warning,
    ``apply.piped_changed``, when such a question's text changed while others
    pipe it). A matched question keeps its internal ID, so piped text reads the
    same questions after any reorder, rename or change of text. The other
    questions are new and get IDs no question of this survey ever had. The questions the draft
    leaves out are removed, and their IDs are never given out again: a ``qid``
    this survey gave a question it no longer has (removed, or in a dropped block)
    is refused (``qid.retired``), and so is one it never gave (``qid.unknown``).

    The survey keeps its block, its metadata and its edit history, with one
    ``apply_draft`` entry naming what was added, removed and changed and the new
    order by internal ID; ``next_qid``, ``block_id``, ``block_name`` and
    ``metadata`` in the draft are ignored. The rules ``build_survey`` applies
    hold for the whole survey. Raises ``UnsupportedEdit`` when the survey has no
    draft form (see ``to_draft``) and ``DraftError`` when the draft breaks a rule.
    Returns the survey itself when the draft changes nothing.
    """
    from collections import Counter

    from .build import to_draft
    from .draft import SurveyDraft, as_draft
    from .validate import assign_ids, next_qid_floor, payload_shape_issues, planned_qids, validate_draft

    current = to_draft(survey)  # raises when the survey has no draft form
    shape = payload_shape_issues(draft)
    if shape:
        raise DraftError(shape)
    incoming = as_draft(draft)
    floor = next_qid_floor(current)
    stored = {q.qid: q for q in current.questions}

    # 1. The internal IDs the form kept.
    claimed: set[str] = set()
    retired: list[DraftIssue] = []
    for idx, q in enumerate(incoming.questions):
        if not isinstance(q.qid, str):
            continue  # validate_draft names it
        q.qid = q.qid.strip()
        if not q.qid:
            continue
        m = _QID_NUMBER.match(q.qid)
        if q.qid in stored:
            claimed.add(q.qid)
        elif m and int(m.group(1)) < floor:
            retired.append(_issue(
                f"{q.qid} belonged to a question this survey no longer has (removed, or in a dropped block); an internal "
                "ID is never given to another question. Leave 'qid' blank to add this as a new question.",
                "qid.retired", question_id=str(q.id), index=idx, field="qid",
            ))
    if retired:
        raise DraftError(retired)

    # 2. A form that kept qid (on any question) says which questions are new: the
    # ones without it. A deleted question's ID must not pass to a new question that
    # took its export tag (the website's form reuses tags after a delete). Only a
    # form that dropped qid altogether is matched: by the same content, then by the
    # same export tag and type.
    def key(q: DraftQuestion) -> tuple[Any, ...]:
        return _content_key(q.type, q.text, q.options, q.rows)

    matched_by_tag: list[tuple[DraftQuestion, DraftQuestion]] = []
    kept_qids = any(isinstance(q.qid, str) and q.qid for q in incoming.questions)
    waiting = [] if kept_qids else [q for q in incoming.questions if isinstance(q.qid, str) and not q.qid]
    open_keys = Counter(key(s) for s in current.questions if s.qid not in claimed)
    waiting_keys = Counter(key(q) for q in waiting)
    for q in waiting:
        k = key(q)
        if open_keys[k] == 1 and waiting_keys[k] == 1:
            match = next(s for s in current.questions if s.qid not in claimed and key(s) == k)
            q.qid = match.qid
            claimed.add(match.qid)
    for q in waiting:
        if q.qid or not isinstance(q.id, str) or not q.id.strip():
            continue
        hits = [
            s for s in current.questions
            if s.qid not in claimed and str(s.id).strip() == q.id.strip() and key(s)[0] == key(q)[0]
        ]
        if len(hits) == 1:
            q.qid = hits[0].qid
            claimed.add(hits[0].qid)
            matched_by_tag.append((q, hits[0]))

    # 3. Build the block from the draft under this survey's rules and numbering. A
    # blank export tag gets one no question has used, dropped ones included.
    others = _taken_tags(survey._base_template, except_qids=stored)
    new = SurveyDraft(
        title=incoming.title,
        questions=incoming.questions,
        next_qid=floor or None,
        block_id=current.block_id,
        block_name=current.block_name,
        metadata=current.metadata,
    )
    d = assign_ids(new, taken=others)
    issues = validate_draft(d, policy=policy)
    if errors_only(issues):
        raise DraftError(issues)
    planned = planned_qids(d)

    template = copy.deepcopy(survey._base_template)
    by_qid = _questions_by_qid(template)
    block_id = str(current.block_id)
    kept = set(planned)
    removed = [s for s in current.questions if s.qid not in kept]
    authoring = {k: v for k, v in copy.deepcopy(survey._authoring).items() if k not in {s.qid for s in removed}}
    unresolved = [dict(m) for m in survey._unresolved_media if str(m.get("question_id")) not in stored]
    old_unresolved: dict[str, list[dict[str, Any]]] = {}
    for m in survey._unresolved_media:
        old_unresolved.setdefault(str(m.get("question_id")), []).append(dict(m))
    new_questions: list[dict[str, Any]] = []
    added: list[dict[str, str]] = []
    changed: list[dict[str, Any]] = []
    touched: set[str] = set()
    for q, qid in zip(d.questions, planned):
        payload = _payload(q)
        if qid in stored:
            before = survey._authoring[qid]["draft"]
            current_q = by_qid[qid][1]
            if payload == before:
                template_q, missing = copy.deepcopy(current_q), old_unresolved.get(qid, [])
            else:
                image = str(payload.get("image_id") or "").strip() != str(before.get("image_id") or "").strip()
                template_q, missing = _builder_question_template(
                    q, qid, current_q, old_unresolved.get(qid, []),
                    new_image=image, image_resolver=image_resolver,
                )
                entry: dict[str, Any] = {"id": q.id, "qid": qid, "fields": sorted(k for k in payload if payload.get(k) != before.get(k))}
                if _tag(current_q) != q.id:
                    entry["renamed_from"] = _tag(current_q)
                changed.append(entry)
                touched.add(qid)
            authoring[qid] = {**authoring[qid], "draft": payload}
        else:
            template_q, missing = question_to_template(q, qid, image_resolver=image_resolver)
            authoring[qid] = {"draft": payload, "block_id": block_id}
            added.append({"id": q.id, "qid": qid})
            touched.add(qid)
        new_questions.append(template_q)
        unresolved.extend(missing)

    title_changed = (str(d.title).strip() or "Untitled survey") != survey.name
    if not (added or removed or changed or title_changed) and planned == [s.qid for s in current.questions]:
        return survey
    _replace_in_blocks(template, block_id, new_questions)
    if title_changed:
        template.setdefault("Metadata", {})["SurveyName"] = str(d.title).strip() or "Untitled survey"
    _check_draft_pipes(survey, template, authoring)
    _check_new_column_clashes(_visible(survey._base_template, survey), _visible(template, survey))
    log: dict[str, Any] = {
        "op": "apply_draft",
        "block_id": block_id,
        "added": added,
        "removed": [{"id": _tag(by_qid[s.qid][1]), "qid": s.qid} for s in removed],
        "changed": changed,
        "order": [str(q.id) for q in d.questions],
        "order_qids": list(planned),
    }
    if title_changed:
        log["title"] = str(d.title).strip() or "Untitled survey"
    warnings = [i for i in issues if i.severity != "error" and (i.question_index < 0 or planned[i.question_index] in touched)]
    # A question the form sent without qid and matched only by its export tag, whose
    # text changed while other questions pipe it: say so.
    for q, before in matched_by_tag:
        readers = [str(o.id) for o in d.questions if o.qid != before.qid and before.qid in _PIPED_QID.findall(json_text([o.text, o.options, o.rows]))]
        if readers and str(q.text).strip() != str(before.text).strip():
            warnings.append(_issue(
                f"{q.id} ({before.qid}) was matched by its export tag and its text changed, and {', '.join(readers)} "
                f"pipe{'s' if len(readers) == 1 else ''} its answer: check that the piped text still reads the question you mean.",
                "apply.piped_changed", question_id=str(q.id), severity="warning",
            ))
    return _with_warnings(
        survey._derive(template=template, authoring=authoring, unresolved_media=unresolved, edits=_log(survey, log)),
        warnings,
    )
