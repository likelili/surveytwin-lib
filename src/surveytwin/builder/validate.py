"""Checking a draft: every problem is a ``DraftIssue`` tied to a question and a field.

Errors block ``build_survey``; warnings do not. The rules and messages follow
the website's ``validate_draft``. Additions, all errors: duplicate or reserved
question IDs, an ID that would repeat another question's column name, unknown
question types and field values of the wrong kind, duplicate grid rows or scale
points, and a minimum selection count above the number of options. The
website's product rules (question cap, title, demographic warning) apply only
through a ``BuilderPolicy``.
"""

from __future__ import annotations

import math
import re
from typing import Any, Iterable

from .draft import ANSWERABLE_TYPES, CHOICE_TYPES, QUESTION_TYPES, DraftIssue, DraftQuestion, SurveyDraft, as_draft
from .policy import DEFAULT_POLICY, BuilderPolicy, looks_demographic

RESERVED_ID = re.compile(r"^QID\d+$", re.IGNORECASE)
_QID_FORM = re.compile(r"^QID(\d+)$")
_PIPED_QID = re.compile(r"\$\{q://(?:\d+_)?(QID\d+)")
_Q_NUMBER = re.compile(r"^Q(\d+)$")

# Number questions without bounds run from 0 to 100 (the website's defaults).
DEFAULT_MIN_VALUE = 0
DEFAULT_MAX_VALUE = 100


def _issue(severity: str, message: str, code: str, question: DraftQuestion | None = None, index: int = -1, field: str = "", item: int = -1) -> DraftIssue:
    return DraftIssue(
        severity=severity,
        message=message,
        code=code,
        question_id=str(question.id).strip() if question is not None and isinstance(question.id, str) else "",
        question_index=index,
        field=field,
        item_index=item,
    )


def _is_whole(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(float(value))


def _nonblank(values: Any) -> list[str]:
    return [str(v).strip() for v in values if str(v).strip()] if isinstance(values, list) else []


def _duplicate_index(values: list[Any]) -> int:
    """Position of the first entry that repeats an earlier one (trimmed, ignoring case), or -1."""
    seen: set[str] = set()
    for idx, value in enumerate(values):
        key = str(value).strip().lower()
        if not key:
            continue
        if key in seen:
            return idx
        seen.add(key)
    return -1


def validate_question(question: DraftQuestion, index: int = -1, *, policy: BuilderPolicy | None = None) -> list[DraftIssue]:
    """The rules for one question on its own (no survey-wide or ID rules)."""
    policy = policy or DEFAULT_POLICY
    issues: list[DraftIssue] = []
    q = question

    def add(severity: str, message: str, code: str, field: str = "", item: int = -1) -> None:
        issues.append(_issue(severity, message, code, q, index, field, item))

    for key in q.unknown_fields:
        add("warning", f"Unknown field {key!r} is ignored.", "field.unknown", key)

    for name in ("id", "text", "image_id", "image_name", "rationale", "qid"):
        if not isinstance(getattr(q, name), str):
            add("error", f"'{name}' must be text.", "field.invalid", name)
    if isinstance(q.qid, str) and q.qid.strip() and not _QID_FORM.match(q.qid.strip()):
        add("error", f"'qid' is a question's internal ID, of the form QID<n>; {q.qid!r} is not.", "field.invalid", "qid")
    if not isinstance(q.type, str):
        add("error", "'type' must be text.", "field.invalid", "type")
        return issues
    if q.type not in QUESTION_TYPES:
        add("error", f"Unknown question type {q.type!r}; use one of: {', '.join(QUESTION_TYPES)}.", "type.unknown", "type")
        return issues

    # Field values of the wrong kind.
    if not isinstance(q.required, bool):
        add("error", "'required' must be true or false.", "field.invalid", "required")
    for name in ("options", "rows"):
        value = getattr(q, name)
        if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
            add("error", f"'{name}' must be a list of text.", "field.invalid", name)
    for name in ("min_selections", "max_selections", "min_chars", "max_chars"):
        value = getattr(q, name)
        if value is not None and (not _is_whole(value) or value < 0):
            add("error", f"'{name}' must be a whole number of 0 or more.", "field.invalid", name)
    if q.decimals is not None and (not _is_whole(q.decimals) or not 0 <= q.decimals <= 6):
        add("error", "'decimals' must be a whole number from 0 to 6.", "field.invalid", "decimals")
    for name in ("min_value", "max_value"):
        value = getattr(q, name)
        if value is not None and not _is_number(value):
            add("error", f"'{name}' must be a number.", "field.invalid", name)
    if q.step is not None and (not _is_number(q.step) or q.step <= 0):
        add("error", "'step' must be a number above 0.", "field.invalid", "step")
    if not _is_whole(q.image_bytes) or q.image_bytes < 0:
        add("error", "'image_bytes' must be a whole number of 0 or more.", "field.invalid", "image_bytes")
    if any(i.severity == "error" and i.code == "field.invalid" for i in issues):
        return issues

    text = str(q.text).strip()
    has_image = bool(str(q.image_id).strip())
    if not text and not (q.type == "instruction" and has_image):
        add("error", "Question text is empty.", "text.empty", "text")

    options = _nonblank(q.options)
    if q.type in CHOICE_TYPES:
        if len(options) < 2:
            add("error", "Choice questions need at least 2 options.", "options.too_few", "options")
        dup = _duplicate_index(q.options)
        if dup >= 0:
            add("error", "Options must be unique.", "options.duplicate", "options", dup)
    if q.type == "grid":
        if not _nonblank(q.rows):
            add("error", "Grid questions need at least 1 row statement.", "rows.too_few", "rows")
        if len(options) < 2:
            add("error", "Grid questions need at least 2 scale points.", "options.too_few", "options")
        dup = _duplicate_index(q.rows)
        if dup >= 0:
            add("error", "Row statements must be unique.", "rows.duplicate", "rows", dup)
        dup = _duplicate_index(q.options)
        if dup >= 0:
            add("error", "Scale points must be unique.", "options.duplicate", "options", dup)
    if q.type == "multi_select":
        if q.min_selections is not None and q.max_selections is not None and q.min_selections > q.max_selections:
            add("error", "Minimum selections cannot exceed the maximum.", "min_selections.above_max", "min_selections")
        if q.max_selections is not None and options and q.max_selections > len(options):
            add("error", "Maximum selections cannot exceed the number of options.", "max_selections.above_options", "max_selections")
        if q.min_selections is not None and options and q.min_selections > len(options):
            add("error", "Minimum selections cannot exceed the number of options.", "min_selections.above_options", "min_selections")
    if q.type == "open_text":
        if q.min_chars is not None and q.max_chars is not None and q.min_chars > q.max_chars:
            add("error", "Minimum characters cannot exceed the maximum.", "min_chars.above_max", "min_chars")
    if q.type == "number":
        low = q.min_value if q.min_value is not None else DEFAULT_MIN_VALUE
        high = q.max_value if q.max_value is not None else DEFAULT_MAX_VALUE
        if low >= high:
            note = ""
            if q.max_value is None:
                note = " (an unset maximum means 100)"
            elif q.min_value is None:
                note = " (an unset minimum means 0)"
            add("error", f"Minimum value must be below the maximum value{note}.", "min_value.not_below_max", "min_value")
    if policy.warn_on_demographics and looks_demographic(text):
        add(
            "warning",
            "Looks like a demographic question - persona demographics are already known metadata "
            "and should not be asked in the survey.",
            "text.demographic",
            "text",
        )
    return issues


def expanded_column_names(tag: str, question_type: str, *, options: int = 0, rows: int = 0) -> list[str]:
    """Column names a question adds besides its own ID (multi-select options, grid rows, a number)."""
    if question_type == "multi_select":
        return [f"{tag}_{k}" for k in range(1, options + 1)]
    if question_type == "grid":
        return [f"{tag}_{k}" for k in range(1, rows + 1)]
    if question_type == "number":
        return [f"{tag}_1"]
    return []


def _draft_columns(q: DraftQuestion) -> list[str]:
    return expanded_column_names(str(q.id).strip(), q.type, options=len(_nonblank(q.options)), rows=len(_nonblank(q.rows)))


def id_issues(questions: list[DraftQuestion], *, taken: Iterable[str] = (), taken_columns: dict[str, str] | None = None, index_offset: int = 0) -> list[DraftIssue]:
    """ID rules: unique, not of the reserved form QID<n>, and no clash with a column name.

    ``taken``: IDs already used elsewhere in the survey (for edits).
    ``taken_columns``: column name -> the ID of the existing question that adds it.
    """
    issues: list[DraftIssue] = []
    taken_set = {str(t) for t in taken}
    columns = dict(taken_columns or {})
    first_seen: dict[str, int] = {}
    for idx, q in enumerate(questions):
        qid = str(q.id).strip()
        if not qid:
            continue
        pos = idx + index_offset
        if RESERVED_ID.match(qid):
            issues.append(_issue("error", f"IDs of the form QID<n> are reserved for the survey's internal question IDs; use another ID, such as Q{idx + 1}.", "id.reserved", q, pos, "id"))
        if qid in taken_set:
            issues.append(_issue("error", f"The ID {qid!r} is already used by another question in the survey.", "id.duplicate", q, pos, "id"))
        elif qid in first_seen:
            issues.append(_issue("error", f"Question IDs must be unique: {qid!r} is also used by question {first_seen[qid] + 1}.", "id.duplicate", q, pos, "id"))
        else:
            first_seen[qid] = pos
    for q in questions:
        for column in _draft_columns(q):
            columns.setdefault(column, str(q.id).strip())
    for idx, q in enumerate(questions):
        qid = str(q.id).strip()
        if qid == "TWIN_ID" or qid.lower().startswith("demo_"):
            issues.append(_issue("error", f"The ID {qid!r} is the name of a column every export has (TWIN_ID, demo_...); use another ID.", "id.reserved", q, idx + index_offset, "id"))
    all_ids = taken_set | set(first_seen)
    for idx, q in enumerate(questions):
        qid = str(q.id).strip()
        owner = columns.get(qid)
        if qid and owner and owner != qid:
            issues.append(
                _issue("error", f"The ID {qid!r} would repeat a column name of question {owner!r}; choose another ID.", "id.column_clash", q, idx + index_offset, "id")
            )
        # the other way round: one of this question's columns is another question's ID
        for column in _draft_columns(q):
            if column != qid and column in all_ids:
                issues.append(
                    _issue("error", f"This question's column {column!r} would repeat the ID of another question; choose another ID.", "id.column_clash", q, idx + index_offset, "id")
                )
    return issues


def validate_draft(draft: SurveyDraft | dict[str, Any], *, policy: BuilderPolicy | None = None) -> list[DraftIssue]:
    """Every problem ``build_survey`` would raise for this draft, and its warnings,
    errors first in question order. Blank IDs are filled first, as ``build_survey``
    fills them, and a problem is reported under the ID the question gets."""
    policy = policy or DEFAULT_POLICY
    shape = payload_shape_issues(draft)
    if shape:
        return shape
    d = assign_ids(draft)
    issues: list[DraftIssue] = []
    for key in d.unknown_fields:
        issues.append(DraftIssue("warning", f"Unknown field {key!r} is ignored.", "field.unknown", field=key))
    issues.extend(survey_context_issues(d))
    if policy.require_title and not str(d.title).strip():
        issues.append(DraftIssue("error", "Survey title is required.", "title.required", field="title"))
    if not d.questions:
        issues.append(DraftIssue("error", "The survey has no questions.", "survey.empty"))
        return issues
    if policy.max_questions is not None and len(d.questions) > policy.max_questions:
        issues.append(DraftIssue("error", f"The survey exceeds {policy.max_questions} questions.", "survey.too_many"))
    for idx, q in enumerate(d.questions):
        issues.extend(validate_question(q, idx, policy=policy))
    issues.extend(id_issues(d.questions))
    qid_problems = qid_issues(d.questions)
    issues.extend(qid_problems)
    if not qid_problems and not any(i.field == "qid" for i in issues):
        issues.extend(draft_piping_issues(d))
    if next_qid_floor(d) and not any(isinstance(q.qid, str) and q.qid.strip() for q in d.questions) and any(
        _PIPED_QID.search(_json([q.text, q.options, q.rows])) for q in d.questions
    ):
        issues.append(
            DraftIssue(
                "warning",
                "This draft came from a survey (it has next_qid) but its questions carry no internal IDs (qid), so they "
                "are numbered anew and piped text may read another question. Send each question's qid back, or use "
                "builder.apply_draft(survey, draft), which matches questions by their IDs.",
                "text.piping_by_position",
            )
        )
    if not any(q.type in ANSWERABLE_TYPES for q in d.questions):
        issues.append(DraftIssue("error", "The survey has no answerable questions.", "survey.no_answerable"))
    return issues


def next_qid_floor(draft: SurveyDraft) -> int:
    """The draft's ``next_qid`` when it is a usable number, else 0."""
    value = draft.next_qid
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else 0


def survey_context_issues(draft: SurveyDraft) -> list[DraftIssue]:
    """The fields ``to_draft`` carries must hold what it writes there."""
    issues: list[DraftIssue] = []
    if draft.next_qid is not None and not next_qid_floor(draft):
        issues.append(DraftIssue("error", "'next_qid' must be a whole number above 0.", "field.invalid", field="next_qid"))
    for name in ("block_id", "block_name"):
        value = getattr(draft, name)
        if value not in (None, "") and not isinstance(value, str):
            issues.append(DraftIssue("error", f"'{name}' must be text.", "field.invalid", field=name))
    if draft.metadata not in (None, {}) and not isinstance(draft.metadata, dict):
        issues.append(DraftIssue("error", "'metadata' must be an object.", "field.invalid", field="metadata"))
    floor = next_qid_floor(draft)
    if floor:
        for idx, q in enumerate(draft.questions):
            m = _QID_FORM.match(q.qid.strip()) if isinstance(q.qid, str) else None
            if m and int(m.group(1)) >= floor:
                issues.append(_issue("error", f"The internal ID {q.qid} was never given out by this survey (its next is QID{floor}); leave 'qid' blank for a new question.", "qid.unknown", q, idx, "qid"))
    return issues


def qid_issues(questions: list[DraftQuestion]) -> list[DraftIssue]:
    """Two questions cannot keep the same internal ID."""
    issues: list[DraftIssue] = []
    first_seen: dict[str, int] = {}
    for idx, q in enumerate(questions):
        qid = q.qid.strip() if isinstance(q.qid, str) else ""
        if not qid:
            continue
        if qid in first_seen:
            issues.append(_issue("error", f"Internal IDs must be unique: {qid!r} is also the qid of question {first_seen[qid] + 1}.", "qid.duplicate", q, idx, "qid"))
        else:
            first_seen[qid] = idx
    return issues


def planned_qids(draft: SurveyDraft | dict[str, Any]) -> list[str]:
    """The internal ID each question gets when the draft is built.

    A question keeps its ``qid`` (a draft from ``to_draft``). A new one gets the
    next number above every internal ID the draft has or pipes and at least the
    draft's ``next_qid``, so it never takes the ID that another question's piped
    text reads or that the survey gave a question before. A draft with neither
    ``qid`` nor ``next_qid`` numbers its questions by position (QID1, QID2, ...),
    as always.
    """
    d = as_draft(draft)
    given = [q.qid.strip() if isinstance(q.qid, str) else "" for q in d.questions]
    floor = next_qid_floor(d)
    if not any(given) and not floor:
        return [f"QID{n}" for n in range(1, len(d.questions) + 1)]
    used = {int(m.group(1)) for g in given if (m := _QID_FORM.match(g))}
    for q in d.questions:
        used |= {int(m[3:]) for m in _PIPED_QID.findall(_json([q.text, q.options, q.rows]))}
    # ``next_qid`` (from to_draft) is one past every ID the survey ever used, so a
    # removed or dropped question's ID is never given to a new one.
    next_number = max(max(used, default=0) + 1, floor)
    planned: list[str] = []
    for g in given:
        if g:
            planned.append(g)
        else:
            planned.append(f"QID{next_number}")
            next_number += 1
    return planned


def draft_piping_issues(draft: SurveyDraft | dict[str, Any]) -> list[DraftIssue]:
    """Piped text (``${q://QID2/...}``) must read a question of the draft that comes
    earlier, by the internal IDs the draft will be built with."""
    d = as_draft(draft)
    qids = planned_qids(d)
    position = {qid: idx for idx, qid in enumerate(qids)}
    issues: list[DraftIssue] = []
    for idx, q in enumerate(d.questions):
        for field_name in ("text", "options", "rows"):
            for other in sorted(set(_PIPED_QID.findall(_json(getattr(q, field_name))))):
                if other == qids[idx]:
                    issues.append(_issue("error", f"The question pipes {other}, its own internal ID; a question cannot show its own answer.", "text.piping_self", q, idx, field_name))
                    continue
                if other not in position:
                    issues.append(_issue("error", f"The question pipes {other}, which is not in this draft.", "text.piping_unknown", q, idx, field_name))
                elif position[other] > idx:
                    issues.append(_issue("error", f"The question pipes {other}, which comes later in the draft.", "text.piping_later", q, idx, field_name))
    return issues


def _json(value: Any) -> str:
    import json

    return json.dumps(value, ensure_ascii=False, default=str)


def payload_shape_issues(draft: Any) -> list[DraftIssue]:
    """Problems with the payload's shape itself (not a dict, questions not a list,
    a question that is not an object), which stop it from being read at all."""
    if isinstance(draft, SurveyDraft):
        return []
    if not isinstance(draft, dict):
        return [DraftIssue("error", f"A survey draft must be a JSON object, not {type(draft).__name__}.", "draft.invalid")]
    questions = draft.get("questions")
    if questions is not None and not isinstance(questions, list):
        return [DraftIssue("error", "The draft's 'questions' must be a list.", "draft.invalid", field="questions")]
    return [
        DraftIssue("error", f"Question {idx + 1} must be a JSON object, not {type(q).__name__}.", "question.invalid", question_index=idx)
        for idx, q in enumerate(questions or [])
        if not isinstance(q, (dict, DraftQuestion))
    ]


def errors_only(issues: list[DraftIssue]) -> list[DraftIssue]:
    return [i for i in issues if i.severity == "error"]


def next_free_q_id(taken: Iterable[str], *, question: DraftQuestion | None = None) -> str:
    """The lowest ``Q<n>`` not in ``taken`` whose own column names (for
    ``question``'s type: ``Q<n>_1``, ...) repeat no ID in ``taken`` either."""
    used = {str(t) for t in taken}
    n = 1
    while f"Q{n}" in used or (question is not None and any(column in used for column in _draft_columns_as(question, f"Q{n}"))):
        n += 1
    return f"Q{n}"


def _draft_columns_as(question: DraftQuestion, tag: str) -> list[str]:
    return expanded_column_names(tag, question.type, options=len(_nonblank(question.options)), rows=len(_nonblank(question.rows)))


def assign_ids(draft: SurveyDraft | dict[str, Any], *, taken: Iterable[str] = ()) -> SurveyDraft:
    """A copy of the draft with blank IDs filled by the lowest free ``Q<n>``.

    IDs already given are kept as they are (trimmed), including duplicates,
    which ``validate_draft`` reports. Unlike the website, a blank question never
    takes an ID a later question already has.
    """
    d = as_draft(draft)
    reserved = {str(t) for t in taken}
    for q in d.questions:
        if isinstance(q.id, str) and q.id.strip():
            q.id = q.id.strip()
            reserved.add(q.id)
    for q in d.questions:
        if isinstance(q.id, str) and not q.id.strip():  # an ID that is not text stays, for validation to name
            q.id = next_free_q_id(reserved, question=q)
            reserved.add(q.id)
    return d
