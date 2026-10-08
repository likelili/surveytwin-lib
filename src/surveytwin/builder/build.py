"""Building a Survey from a draft."""

from __future__ import annotations

from typing import Any

from ..errors import DraftError, SurveyError
from ..survey import Survey
from .convert import block_element, question_to_template, survey_template
from .draft import SurveyDraft
from .images import ImageResolver
from .policy import BuilderPolicy
from .validate import assign_ids, errors_only, planned_qids, validate_draft

DEFAULT_BLOCK_ID = "BL_BUILDER"
# Metadata key holding one past every internal question number a survey has used.
NEXT_QID_KEY = "NextQuestionNumber"
_BUILT_METADATA = {"SurveyName", "SurveySource", NEXT_QID_KEY}


def build_survey(
    draft: SurveyDraft | dict[str, Any],
    *,
    block_id: str | None = None,
    block_name: str | None = None,
    image_resolver: ImageResolver | None = None,
    policy: BuilderPolicy | None = None,
) -> Survey:
    """A ``Survey`` from a draft: one block, the questions in order.

    ``draft`` is a ``SurveyDraft`` or the website's JSON payload
    (``{"title": ..., "questions": [...]}``); it is never changed. Blank
    question IDs get the lowest free ``Q<n>``. A question with a ``qid`` (a draft
    from ``to_draft``) keeps that internal ID; the others get new ones (see
    ``validate.planned_qids``). The block is ``block_id`` / ``block_name`` when
    given, else the draft's own (a draft from ``to_draft`` carries them), else
    ``BL_BUILDER`` named after the title. Any error in the draft raises
    ``DraftError``, whose ``issues`` name each question and field; warnings do not
    stop the build (see them with ``validate_draft``).

    No model call, no persona, no file is written.
    """
    from .validate import next_qid_floor, payload_shape_issues

    shape = payload_shape_issues(draft)
    if shape:
        raise DraftError(shape)
    d = assign_ids(draft)
    issues = validate_draft(d, policy=policy)
    if errors_only(issues):
        raise DraftError(issues)
    if block_id is None:
        block_id = d.block_id if isinstance(d.block_id, str) and d.block_id.strip() else DEFAULT_BLOCK_ID
    if not isinstance(block_id, str) or not block_id.strip():
        raise SurveyError("block_id must be a non-empty string")
    block_id = block_id.strip()
    if block_name is None and isinstance(d.block_name, str) and d.block_name.strip():
        block_name = d.block_name
    questions: list[dict[str, Any]] = []
    unresolved: list[dict[str, Any]] = []
    authoring: dict[str, dict[str, Any]] = {}
    for q, qid in zip(d.questions, planned_qids(d)):
        template_q, missing = question_to_template(q, qid, image_resolver=image_resolver)
        questions.append(template_q)
        unresolved.extend(missing)
        authoring[qid] = {"draft": _payload(q), "block_id": block_id}
    name = block_name if block_name is not None else (str(d.title).strip() or "Survey")
    template = survey_template(d.title, [block_element(block_id, name, questions)])
    if isinstance(d.metadata, dict) and d.metadata:
        # the survey's own metadata (its Qualtrics ID and language, ...), carried by to_draft
        template["Metadata"] = {**{k: v for k, v in d.metadata.items() if k not in _BUILT_METADATA}, **template["Metadata"]}
    floor = next_qid_floor(d)
    if floor:
        # One past every internal ID the survey this draft came from ever used: later
        # edits and drafts of the new survey never give a new question one of them.
        template["Metadata"][NEXT_QID_KEY] = max([floor] + [int(q[3:]) + 1 for q in planned_qids(d) if q[3:].isdigit()])
    from .edit import _check_new_column_clashes

    _check_new_column_clashes({"Elements": []}, template)
    return Survey(
        template,
        kind="builder",
        source_name=str(d.title).strip() or "Untitled survey",
        unresolved_media=unresolved,
        authoring=authoring,
    )


def _payload(q: Any) -> dict[str, Any]:
    """A question's draft in the website's form (its internal ID lives in the template)."""
    data = q.to_dict()
    data.pop("qid", None)
    return data


def draft_of(survey: Survey, question_id: str) -> dict[str, Any] | None:
    """The draft form of a question the builder made (None for a question read from a QSF)."""
    entry = survey._authoring.get(question_id)
    return dict(entry["draft"]) if entry else None


def to_draft(survey: Survey) -> SurveyDraft:
    """The draft form of a survey the builder made (after any edits), for a web
    form to load again.

    It is the survey as it is now: dropped blocks are left out. Each question
    carries its internal ID in ``qid``; the draft carries the next internal number
    (``next_qid``), the block's ID and name and the survey's metadata. Building it
    again keeps every ID and every piped reference, and a question added in the
    form never takes the ID of one removed or dropped before.

    Raises ``UnsupportedEdit`` when the draft form cannot hold the survey
    faithfully: a question read from a QSF, anything besides one plain block of
    questions (more blocks, a block shown more than once, branches, randomizers,
    embedded data, loops, question randomization, other survey parts such as web
    services), a question changed outside the builder (it no longer matches its
    draft), or a draft that could not be built back.
    """
    from ..errors import UnsupportedEdit
    from .draft import DraftQuestion
    from .edit import _block_elements, _next_qid_number

    template = survey.template
    elements = [e for e in template.get("Elements") or [] if isinstance(e, dict)]
    if not elements:
        raise SurveyError("Nothing is left in this survey (every block is dropped).")
    read_from_qsf = list(dict.fromkeys(
        str(q.get("QuestionName") or q.get("QuestionID"))
        for block in _block_elements(template)
        for q in block.get("Questions") or []
        if isinstance(q, dict) and str(q.get("QuestionID", "")) not in survey._authoring
    ))
    if read_from_qsf:
        raise UnsupportedEdit(
            f"{len(read_from_qsf)} question(s) were read from a QSF and have no draft form: {', '.join(read_from_qsf[:10])}"
        )
    blocks = [e for e in elements if e.get("ElementType") == "Block"]
    others = sorted({str(e.get("ElementType")) if e.get("ElementType") else "a flow element the builder cannot represent" for e in elements if e.get("ElementType") != "Block"})
    problems: list[str] = []
    if others:
        problems.append(f"its flow has {', '.join(others)}")
    block_ids = [str(b.get("BlockID", "")) for b in blocks]
    distinct = list(dict.fromkeys(block_ids))
    if len(distinct) > 1:
        problems.append(f"it has {len(distinct)} blocks")
    for block_id in distinct:
        if block_ids.count(block_id) > 1:
            problems.append(f"block {block_id} appears {block_ids.count(block_id)} times in the flow")
    extra = sorted(k for k in template if k not in {"Metadata", "Elements"})
    if extra:
        problems.append(f"the survey has {', '.join(extra)}")
    options = blocks[0].get("Options") if blocks and isinstance(blocks[0].get("Options"), dict) else {}
    if str(options.get("Looping", "None") or "None") != "None":
        problems.append("its block loops (Loop & Merge)")
    if str(options.get("RandomizeQuestions", "") or "").strip().lower() not in {"", "false", "none", "off", "no", "0"}:
        problems.append("its block randomizes its questions")
    if problems:
        raise UnsupportedEdit(
            "A draft holds one plain block of questions; this survey cannot become one without losing "
            + "; ".join(problems) + ". Edit it with the builder's edit functions instead."
        )
    questions: list[DraftQuestion] = []
    changed: list[str] = []
    for q in blocks[0].get("Questions") or []:
        if not isinstance(q, dict):
            continue
        qid = str(q.get("QuestionID", ""))
        draft_question = DraftQuestion.from_dict(survey._authoring[qid]["draft"])
        draft_question.qid = qid
        differs = differs_from_draft(q, draft_question, qid)
        if differs:
            changed.append(f"{draft_question.id} ({qid}): {', '.join(differs)}")
        questions.append(draft_question)
    if changed:
        raise UnsupportedEdit(
            "Some questions no longer match their draft form (they were changed outside the builder), so a draft would "
            "lose that: " + "; ".join(changed)
        )
    metadata = {k: v for k, v in (survey._base_template.get("Metadata") or {}).items() if k not in _BUILT_METADATA}
    draft = SurveyDraft(
        title=survey.name,
        questions=questions,
        next_qid=_next_qid_number(survey, survey._base_template),
        block_id=str(blocks[0].get("BlockID", "")),
        block_name=str(blocks[0].get("BlockName") or ""),
        metadata=metadata,
    )
    errors = errors_only(validate_draft(draft))
    if errors:
        raise UnsupportedEdit(
            "This survey's draft could not be built back, so it is not offered as one: "
            + "; ".join(f"{e.question_id or 'survey'} {e.field}: {e.message}".replace(" : ", ": ") for e in errors)
        )
    return draft


def differs_from_draft(template_question: dict[str, Any], draft_question: Any, qid: str) -> list[str]:
    """The keys where a builder question's template differs from what its draft
    builds (images aside: a resolved image is the template's own). Empty when the
    template is still the draft's."""
    expected, _missing = question_to_template(draft_question, qid, image_resolver=lambda _ref: None)
    ignore = {"QuestionMedia"}
    keys = sorted((set(expected) | set(template_question)) - ignore)
    differs = []
    for key in keys:
        mine, theirs = template_question.get(key), expected.get(key)
        if key == "QuestionText" and mine != theirs and template_question.get("QuestionMedia") and theirs in ("", "[Image stimulus attached]"):
            continue  # an image-only instruction reads "[Image stimulus attached]" once its image resolves
        if mine != theirs:
            differs.append(key)
    return differs
