"""Editing a survey's definition in the standard format.

``new_survey`` starts an empty survey; the Survey methods ``add_question``,
``update_question``, ``move_question``, ``swap_questions``, ``remove_question``,
``add_block``, ``remove_block`` and ``group_into_block`` call the functions here.

Each edit changes the definition (``Survey.standard``) and returns a new Survey;
the survey it was called on, and any plan made from it, never change. Only what
the edit names changes: every other question, block and flow step stays as it
was, field for field. The edited definition is checked as ``read_survey`` checks
a file, and the edit is refused, with nothing changed, when it would

- leave an error in the definition (``SurveyFormatError``, each problem with the
  path of its field), name a picture file that cannot be read, or give two CSV
  columns the same name;
- leave logic or piped text reading a question that is gone, or one asked only
  after it (``UnsupportedEdit``);
- touch a dropped block, or add questions to a conjoint or MaxDiff block, whose
  tasks are drawn from its definition (``SurveyError``).

A survey read from a QSF is edited the same way: its definition is the QSF read
into the standard format, and everything the reader kept stays.
"""

from __future__ import annotations

import copy
import json
import re
from collections import Counter
from pathlib import Path
from typing import Any, Iterable

from ..errors import SurveyError, SurveyFormatError, UnsupportedEdit
from ..spec import model
from ..spec.validate import Finding, check_survey
from ..survey import _UNCHANGED, Survey
from .draft import SCALE_PRESETS


# The default of update_question's parameters: leave that part as it is.
UNCHANGED: Any = _UNCHANGED

# The question types the builder writes, and the response type of each.
TYPES: dict[str, str | None] = {
    "instruction": None,
    "single_choice": "choice",
    "multiple_choice": "choice",
    "text": "text",
    "number": "number",
    "grid": "matrix",
    "number_grid": "matrix",
    "text_grid": "matrix",
    "sum_grid": "matrix",
    "form": "form",
    "ranking": "ranking",
    "allocation": "allocation",
    "side_by_side": "group",
}

# The lists each type is made of; the first ones listed are required.
_PARTS: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {
    "instruction": ((), ()),
    "single_choice": (("options",), ()),
    "multiple_choice": (("options",), ()),
    "text": ((), ()),
    "number": ((), ()),
    "grid": (("rows", "scale"), ()),
    "number_grid": (("rows",), ()),
    "text_grid": (("rows", "columns"), ()),
    "sum_grid": (("rows", "columns"), ()),
    "form": (("fields",), ()),
    "ranking": (("items",), ()),
    "allocation": (("items",), ()),
    "side_by_side": (("rows", "columns"), ()),
}

# Question fields a setting may set directly (the rest belong to the builder).
_QUESTION_SETTINGS = tuple(k for k in model.Question.fields if k not in {"id", "content", "response", "kind", "source"})

# The block a question goes to when the survey has none.
DEFAULT_BLOCK = "main"
_SKIP_TARGETS = {"end_of_block", "end_of_survey"}
_AUTO_ID = re.compile(r"^Q(\d+)$")


# ---------------------------------------------------------------------------
# a new survey
# ---------------------------------------------------------------------------


def new_survey(
    title: str = "",
    *,
    id: str | None = None,
    description: str | None = None,
    language: str | None = None,
    folder: str | Path | None = None,
) -> Survey:
    """An empty survey in the standard format, to fill with ``add_question``."""
    from ..spec.compile import compile_survey

    survey_id = id if id is not None else (_slug(title) or "survey")
    definition: dict[str, Any] = {"schema_version": model.SCHEMA_VERSION, "id": survey_id}
    for key, value in (("title", title), ("description", description), ("language", language)):
        if value:
            definition[key] = value
    definition.update({"questions": [], "blocks": [], "flow": []})
    errors = [f for f in check_survey(definition) if f.level == "error"]
    if errors:
        raise SurveyFormatError(errors)
    survey = Survey(
        compile_survey(definition),
        kind="standard",
        raw_bytes=json.dumps(definition, ensure_ascii=False, sort_keys=True).encode("utf-8"),
        standard=definition,
    )
    survey._picture_folder = (Path(folder).expanduser() if folder is not None else Path.cwd()).resolve()
    return survey


def _slug(text: str) -> str:
    return re.sub(r"[^0-9a-z]+", "_", str(text or "").lower()).strip("_")


# ---------------------------------------------------------------------------
# questions
# ---------------------------------------------------------------------------


def add_question(
    survey: Survey,
    type: str,
    text: Any = None,
    *,
    id: str | None = None,
    block: str | None = None,
    position: int | None = None,
    after: str | None = None,
    before: str | None = None,
    image: Any = None,
    options: Any = None,
    rows: Any = None,
    columns: Any = None,
    scale: Any = None,
    items: Any = None,
    fields: Any = None,
    **settings: Any,
) -> Survey:
    definition = _definition(survey)
    btype = _builder_type_name(type)
    qid = _new_question_id(survey, definition, id)
    after, before = _anchors(survey, definition, after, before)
    parts = {k: v for k, v in dict(options=options, rows=rows, columns=columns, scale=scale, items=items, fields=fields).items() if v is not None}
    settings = {k: v for k, v in settings.items() if v is not None}
    question: dict[str, Any] = {"id": qid}
    content = _content(text, image, settings.pop("content", None))
    if not content:
        raise SurveyError(f"Question {qid!r} needs text or a picture.")
    question["content"] = content
    response = _build_response(btype, parts, settings, where=f"add_question({btype!r})")
    if response is not None:
        question["response"] = response
    _apply_question_settings(question, settings, where=f"add_question({btype!r})")
    _refuse_unknown(settings, btype, f"add_question({btype!r})")
    block_id, made_block = _target_block(survey, definition, block, after, before, question_id=qid)
    if made_block:
        definition["blocks"].append({"id": block_id, "kind": "survey", "questions": []})
        definition["flow"].append({"type": "show_block", "block": block_id})
    order = _block(definition, block_id)["questions"]
    index = _place(order, block_id, position, after, before)
    order.insert(index, qid)
    definition["questions"].insert(_question_slot(definition, block_id, index, qid), question)
    entry = {"op": "add_question", "on": "definition", "question": qid, "type": btype, "block": block_id, "position": index + 1}
    return _finish(survey, definition, entry)


def update_question(
    survey: Survey,
    question_id: str,
    *,
    type: Any = UNCHANGED,
    text: Any = UNCHANGED,
    image: Any = UNCHANGED,
    options: Any = UNCHANGED,
    rows: Any = UNCHANGED,
    columns: Any = UNCHANGED,
    scale: Any = UNCHANGED,
    items: Any = UNCHANGED,
    fields: Any = UNCHANGED,
    **settings: Any,
) -> Survey:
    definition = _definition(survey)
    qid = _question_id(survey, definition, question_id)
    _check_editable(survey, definition, _block_of(definition, qid), qid)
    question = _question(definition, qid)
    where = f"update_question({qid!r})"
    if "id" in settings:
        raise SurveyError(f"{where}: a question's ID cannot change (logic and answers name it); add a new question instead.")
    parts = {k: v for k, v in dict(options=options, rows=rows, columns=columns, scale=scale, items=items, fields=fields).items() if v is not UNCHANGED}
    if any(v is None for v in parts.values()):
        raise SurveyError(f"{where}: {', '.join(k for k, v in parts.items() if v is None)} cannot be removed, only replaced.")
    changed = sorted(set(parts) | set(settings) | {k for k, v in dict(type=type, text=text, image=image).items() if v is not UNCHANGED})
    if not changed:
        raise SurveyError(f"{where}: nothing to change.")
    old_type = builder_type(question)
    if question.get("kind") and (parts or (type is not UNCHANGED) or set(settings) - set(_QUESTION_SETTINGS) - {"control", "content"}):
        raise SurveyError(f"{where}: this is a {question['kind'].replace('_', ' ')} question, which takes no answer; only its text and display settings can change.")
    if "content" in settings:
        if text is not UNCHANGED or image is not UNCHANGED:
            raise SurveyError(f"{where}: give content, or text and image, not both.")
        content = settings.pop("content")
        question["content"] = _content(None, None, content)
    else:
        if text is not UNCHANGED:
            question["content"] = _replace_items(question.get("content") or [], "text", _text_items(text))
        if image is not UNCHANGED:
            question["content"] = _replace_items(question.get("content") or [], "image", _image_items(image), after="text")
    if not question.get("content"):
        raise SurveyError(f"{where}: a question needs text or a picture.")
    if type is not UNCHANGED and _builder_type_name(type) != old_type:
        new_type = _builder_type_name(type)
        family_changed = TYPES[new_type] != TYPES.get(old_type or "")
        if family_changed and _reads_of(survey, definition, {qid}, exclude_self=qid):
            raise UnsupportedEdit(
                f"{where}: other parts of the survey read this question, and a {new_type} question would answer them "
                "differently:\n" + "\n".join(f"  {line}" for line in _read_lines(_reads_of(survey, definition, {qid}, exclude_self=qid)))
            )
        if family_changed and question.get("skip") and "skip" not in settings:
            raise UnsupportedEdit(f"{where}: the question's skip rules test its options; give skip=None, or new rules, with the new type.")
        response = _build_response(new_type, {**_carried_parts(question.get("response"), new_type), **parts}, settings, where=where, base=_carried_settings(question.get("response"), new_type))
        if response is None:
            question.pop("response", None)
        else:
            question["response"] = response
        if family_changed and "control" not in settings and "presentation" not in settings:
            question.pop("presentation", None)  # it described the old answer
        _forget_source(question, keep=_QUESTION_SOURCE)
        old_type = new_type
    elif old_type is None:
        if parts or set(settings) - set(_QUESTION_SETTINGS) - {"control"}:
            raise SurveyError(f"{where}: this question takes no answer; give type= to make it one.")
    elif old_type != "instruction":
        question["response"] = _update_response(old_type, question["response"], parts, settings, where=where)
        if parts:
            _forget_source(question, drop=_LIST_SOURCE)
    elif parts:
        raise SurveyError(f"{where}: an instruction takes no answer; give type= to make it a question.")
    if "export_name" in settings:
        _forget_source(question, drop=frozenset({"no_export_name"}))
    _apply_question_settings(question, settings, where=where)
    _refuse_unknown(settings, old_type or "instruction", where)
    entry = {"op": "update_question", "on": "definition", "question": qid, "changed": changed}
    return _finish(survey, definition, entry)


def move_question(
    survey: Survey,
    question_id: str,
    *,
    block: str | None = None,
    position: int | None = None,
    after: str | None = None,
    before: str | None = None,
) -> Survey:
    definition = _definition(survey)
    qid = _question_id(survey, definition, question_id)
    if block is None and position is None and after is None and before is None:
        raise SurveyError(f"move_question({qid!r}): say where to: block=, position=, after= or before=.")
    after, before = _anchors(survey, definition, after, before)
    if qid in {after, before}:
        raise SurveyError(f"move_question({qid!r}): a question cannot go next to itself.")
    source = _block_of(definition, qid)
    _check_editable(survey, definition, source, qid)
    if block is None and after is None and before is None:
        if source is None:
            raise SurveyError(f"move_question({qid!r}): the question is in no block; say which with block=.")
        block = source
    target, _made = _target_block(survey, definition, block, after, before, question_id=qid, may_make=False)
    if source is not None:
        _block(definition, source)["questions"].remove(qid)
    order = _block(definition, target)["questions"]
    index = _place(order, target, position, after, before)
    order.insert(index, qid)
    entry = {"op": "move_question", "on": "definition", "question": qid, "block": target, "position": index + 1}
    return _finish(survey, definition, entry)


def swap_questions(survey: Survey, first: str, second: str) -> Survey:
    definition = _definition(survey)
    a = _question_id(survey, definition, first)
    b = _question_id(survey, definition, second)
    if a == b:
        raise SurveyError(f"swap_questions: {a!r} is named twice.")
    block_a, block_b = _block_of(definition, a), _block_of(definition, b)
    for block_id, qid in ((block_a, a), (block_b, b)):
        _check_editable(survey, definition, block_id, qid)
        if block_id is None:
            raise SurveyError(f"swap_questions: question {qid!r} is in no block; place it with move_question.")
    order_a = _block(definition, block_a)["questions"]
    order_b = _block(definition, block_b)["questions"]
    i, j = order_a.index(a), order_b.index(b)
    order_a[i], order_b[j] = b, a
    entry = {"op": "swap_questions", "on": "definition", "questions": [a, b]}
    return _finish(survey, definition, entry)


def remove_question(survey: Survey, question_id: str) -> Survey:
    definition = _definition(survey)
    qid = _question_id(survey, definition, question_id)
    block_id = _block_of(definition, qid)
    _check_editable(survey, definition, block_id, qid)
    readers = _reads_of(survey, definition, {qid}, exclude_self=qid)
    if readers:
        raise UnsupportedEdit(
            f"remove_question({qid!r}): other parts of the survey read this question; change or remove them first:\n"
            + "\n".join(f"  {line}" for line in _read_lines(readers))
        )
    for block in definition.get("blocks") or []:
        if isinstance(block, dict) and block.get("kind", "survey") == "survey":
            block["questions"] = [q for q in block.get("questions") or [] if str(q) != qid]
    definition["questions"] = [q for q in definition["questions"] if str(q.get("id")) != qid]
    entry = {"op": "remove_question", "on": "definition", "question": qid, "block": block_id}
    return _finish(survey, definition, entry)


# ---------------------------------------------------------------------------
# blocks
# ---------------------------------------------------------------------------


def add_block(
    survey: Survey,
    block_id: str,
    *,
    title: str | None = None,
    position: int | None = None,
    after: str | None = None,
    before: str | None = None,
) -> Survey:
    definition = _definition(survey)
    block_id = _new_block_id(definition, block_id)
    block: dict[str, Any] = {"id": block_id, "kind": "survey"}
    if title:
        block["title"] = title
    block["questions"] = []
    _insert_block_step(survey, definition, block_id, position=position, after=after, before=before, where=f"add_block({block_id!r})")
    definition.setdefault("blocks", []).append(block)
    entry = {"op": "add_block", "on": "definition", "block": block_id}
    return _finish(survey, definition, entry)


def remove_block(survey: Survey, block_id: str) -> Survey:
    definition = _definition(survey)
    block_id = _block_id(definition, block_id)
    if block_id in set(survey.exclude_blocks):
        raise SurveyError(f"Block {block_id!r} is dropped from this survey; edit the survey before dropping blocks.")
    from ..engine.drops import block_questions, reads

    block = _block(definition, block_id)
    gone = set(block_questions(block))
    readers = [r for r in reads(definition, gone, {block_id})] if gone else []
    if readers:
        raise UnsupportedEdit(
            f"remove_block({block_id!r}): other parts of the survey read its questions; change or remove them first:\n"
            + "\n".join(f"  {line}" for line in _read_lines(readers))
        )
    own = set(str(q) for q in block.get("questions") or []) if block.get("kind", "survey") == "survey" else set()
    definition["blocks"] = [b for b in definition["blocks"] if str(b.get("id")) != block_id]
    definition["questions"] = [q for q in definition.get("questions") or [] if str(q.get("id")) not in own]
    definition["flow"] = _without_block_steps(definition.get("flow") or [], block_id)
    entry = {"op": "remove_block", "on": "definition", "block": block_id, "questions": sorted(own)}
    return _finish(survey, definition, entry)


def group_into_block(
    survey: Survey,
    question_ids: Iterable[str],
    block_id: str,
    *,
    title: str | None = None,
    after: str | None = None,
    before: str | None = None,
) -> Survey:
    definition = _definition(survey)
    if isinstance(question_ids, str):
        question_ids = [question_ids]
    qids = [_question_id(survey, definition, q) for q in question_ids]
    where = f"group_into_block({block_id!r})"
    if not qids:
        raise SurveyError(f"{where}: name the questions to gather.")
    repeated = sorted(q for q, n in Counter(qids).items() if n > 1)
    if repeated:
        raise SurveyError(f"{where}: {', '.join(repr(q) for q in repeated)} named twice.")
    block_id = _new_block_id(definition, block_id)
    sources: list[str] = []
    for qid in qids:
        source = _block_of(definition, qid)
        _check_editable(survey, definition, source, qid)
        if source is not None and source not in sources:
            sources.append(source)
        if source is not None and isinstance(_block(definition, source).get("repeat"), dict):
            raise UnsupportedEdit(
                f"{where}: question {qid!r} is in block {source!r}, which repeats (loop and merge); in a new block it would be asked once. "
                "Gather questions from blocks that do not repeat."
            )
    if after is None and before is None:
        if not sources:
            raise SurveyError(f"{where}: the questions are in no block; place the new block with after= or before=.")
        after = sources[0]
        steps = _block_steps(definition.get("flow") or [], after)
        if len(steps) == 1 and steps[0][1] == "randomize":
            raise SurveyError(
                f"{where}: block {after!r} is an arm of a randomizer, so a block placed next to it would be an arm of its own. "
                "Place the new block with after= or before=."
            )
    for source in sources:
        _block(definition, source)["questions"] = [q for q in _block(definition, source)["questions"] if str(q) not in set(qids)]
    block: dict[str, Any] = {"id": block_id, "kind": "survey"}
    if title:
        block["title"] = title
    block["questions"] = list(qids)
    _insert_block_step(survey, definition, block_id, after=after, before=before, where=where)
    definition.setdefault("blocks", []).append(block)
    entry = {"op": "group_into_block", "on": "definition", "block": block_id, "questions": list(qids)}
    return _finish(survey, definition, entry)


# ---------------------------------------------------------------------------
# finding things
# ---------------------------------------------------------------------------


def _definition(survey: Survey) -> dict[str, Any]:
    """The whole definition, dropped blocks included, as a copy to edit."""
    definition = copy.deepcopy(survey._standard_full)
    for key in ("questions", "blocks", "flow"):
        if not isinstance(definition.get(key), list):
            definition[key] = []
    return definition


def _question(definition: dict[str, Any], qid: str) -> dict[str, Any]:
    return next(q for q in definition["questions"] if str(q.get("id")) == qid)


def _block(definition: dict[str, Any], block_id: str) -> dict[str, Any]:
    return next(b for b in definition["blocks"] if str(b.get("id")) == block_id)


def _survey_blocks(definition: dict[str, Any]) -> list[str]:
    return [str(b.get("id")) for b in definition.get("blocks") or [] if isinstance(b, dict) and b.get("kind", "survey") == "survey"]


def _block_of(definition: dict[str, Any], qid: str) -> str | None:
    for block in definition.get("blocks") or []:
        if isinstance(block, dict) and block.get("kind", "survey") == "survey" and qid in [str(q) for q in block.get("questions") or []]:
            return str(block.get("id"))
    return None


def _question_id(survey: Survey, definition: dict[str, Any], name: Any) -> str:
    """A question's ID, given as written (an export name naming exactly one question is accepted too)."""
    import difflib

    text = str(name if name is not None else "").strip()
    ids = [str(q.get("id")) for q in definition["questions"] if isinstance(q, dict)]
    if text in ids:
        return text
    by_export = [str(q.get("id")) for q in definition["questions"] if isinstance(q, dict) and str(q.get("export_name") or "").strip() == text]
    if len(by_export) == 1:
        return by_export[0]
    if len(by_export) > 1:
        raise SurveyError(f"{text!r} is the export name of {', '.join(by_export)}; name the question by its ID.")
    if text in {str(q) for q in survey.exclude_questions}:
        raise SurveyError(f"Question {text!r} was excluded when the survey was read.")
    close = difflib.get_close_matches(text, ids, n=3, cutoff=0.5)
    hint = f" Did you mean: {', '.join(close)}?" if close else ""
    raise SurveyError(f"No question {text!r} in this survey.{hint}")


def _block_id(definition: dict[str, Any], name: Any) -> str:
    import difflib

    text = str(name if name is not None else "").strip()
    ids = [str(b.get("id")) for b in definition.get("blocks") or [] if isinstance(b, dict)]
    if text in ids:
        return text
    close = difflib.get_close_matches(text, ids, n=3, cutoff=0.5)
    hint = f" Did you mean: {', '.join(close)}?" if close else f" Its blocks: {', '.join(ids) or 'none'}."
    raise SurveyError(f"No block {text!r} in this survey.{hint}")


def _check_editable(survey: Survey, definition: dict[str, Any], block_id: str | None, qid: str = "") -> None:
    if block_id is None:
        return
    if block_id in set(survey.exclude_blocks):
        what = f"Question {qid!r} is in block {block_id!r}, which" if qid else f"Block {block_id!r}"
        raise SurveyError(f"{what} is dropped from this survey; edit the survey before dropping blocks.")


def _new_question_id(survey: Survey, definition: dict[str, Any], wanted: str | None) -> str:
    taken = {str(q.get("id")) for q in definition["questions"] if isinstance(q, dict)}
    if wanted is not None:
        qid = str(wanted).strip()
        if not qid or re.search(r"\s", qid):
            raise SurveyError(f"A question ID is text without spaces, not {wanted!r}.")
        if qid in _SKIP_TARGETS:
            raise SurveyError(f"{qid!r} names the end of a block or survey in skip rules; choose another question ID.")
        if qid in taken:
            raise SurveyError(f"The survey already has a question {qid!r}.")
        return qid
    # Q<n>, one past every Q<n> the survey has as an ID or export name, or had
    # before an edit removed it
    named = set(taken) | {str(q.get("export_name") or "").strip() for q in definition["questions"] if isinstance(q, dict)}
    named |= {str(e.get("question", "")) for e in survey.edits} | {str(q) for e in survey.edits for q in e.get("questions") or [] if isinstance(q, str)}
    numbers = [int(m.group(1)) for m in (_AUTO_ID.match(name) for name in named) if m]
    return f"Q{max(numbers, default=0) + 1}"


def _new_block_id(definition: dict[str, Any], wanted: Any) -> str:
    block_id = str(wanted if wanted is not None else "").strip()
    if not block_id or re.search(r"\s", block_id):
        raise SurveyError(f"A block ID is text without spaces, not {wanted!r}.")
    if block_id in {str(b.get("id")) for b in definition.get("blocks") or [] if isinstance(b, dict)}:
        raise SurveyError(f"The survey already has a block {block_id!r}.")
    return block_id


def _target_block(
    survey: Survey,
    definition: dict[str, Any],
    block: str | None,
    after: str | None,
    before: str | None,
    *,
    question_id: str,
    may_make: bool = True,
) -> tuple[str, bool]:
    """(the block a question goes to, whether it must be made): ``block``, or the
    block of the question ``after``/``before`` (IDs) names, or the survey's only
    block, or a new block when the survey has none."""
    if after is not None and before is not None:
        raise SurveyError("Give after= or before=, not both.")
    anchor = after if after is not None else before
    anchor_block = None
    if anchor is not None:
        anchor_block = _block_of(definition, anchor)
        if anchor_block is None:
            raise SurveyError(f"Question {anchor!r} is in no block, so nothing can be placed next to it.")
    if block is not None:
        found = _block_id(definition, block)
        target = _block(definition, found)
        if target.get("kind", "survey") != "survey":
            raise SurveyError(
                f"Block {found!r} is a {target.get('kind')} block: its tasks are drawn from its definition and it lists no questions."
            )
        if anchor_block is not None and anchor_block != found:
            raise SurveyError(f"Question {anchor!r} is in block {anchor_block!r}, not {found!r}.")
        _check_editable(survey, definition, found)
        return found, False
    if anchor_block is not None:
        _check_editable(survey, definition, anchor_block)
        return anchor_block, False
    candidates = [b for b in _survey_blocks(definition) if b not in set(survey.exclude_blocks)]
    if len(candidates) == 1:
        return candidates[0], False
    if candidates:
        raise SurveyError(
            f"This survey has {len(candidates)} blocks ({', '.join(candidates[:8])}{', ...' if len(candidates) > 8 else ''}); "
            f"say where question {question_id!r} goes with block=, after= or before=."
        )
    if not may_make:
        raise SurveyError("This survey has no block to put the question in; add one with add_block.")
    taken = {str(b.get("id")) for b in definition.get("blocks") or [] if isinstance(b, dict)}
    name, n = DEFAULT_BLOCK, 1
    while name in taken:
        n += 1
        name = f"{DEFAULT_BLOCK}_{n}"
    return name, True


def _place(order: list[str], block_id: str, position: int | None, after: str | None, before: str | None) -> int:
    """Where in a block's question list a question goes (an index)."""
    given = [name for name, value in (("position", position), ("after", after), ("before", before)) if value is not None]
    if len(given) > 1:
        raise SurveyError(f"Give one of position=, after= and before=, not {' and '.join(given)}.")
    order_ids = [str(q) for q in order]
    if after is not None or before is not None:
        # (an ID of a question in this block: see _anchors and _target_block)
        return order_ids.index(str(after if after is not None else before)) + (1 if after is not None else 0)
    if position is None:
        return len(order_ids)
    if isinstance(position, bool) or not isinstance(position, int) or not 1 <= position <= len(order_ids) + 1:
        raise SurveyError(f"position is a number from 1 (first) to {len(order_ids) + 1} (last) in block {block_id!r}, not {position!r}.")
    return position - 1


def _anchors(survey: Survey, definition: dict[str, Any], after: Any, before: Any) -> tuple[str | None, str | None]:
    """``after`` and ``before`` as question IDs (each may be given by export name)."""
    if after is not None and before is not None:
        raise SurveyError("Give after= or before=, not both.")
    return (
        _question_id(survey, definition, after) if after is not None else None,
        _question_id(survey, definition, before) if before is not None else None,
    )


def _question_slot(definition: dict[str, Any], block_id: str, index: int, qid: str) -> int:
    """Where a new question goes in the survey's question list: after the question
    before it in its block, else before the one after it, else at the end."""
    order = [str(q) for q in _block(definition, block_id)["questions"]]
    ids = [str(q.get("id")) for q in definition["questions"]]
    for neighbor in reversed(order[:index]):
        if neighbor in ids:
            return ids.index(neighbor) + 1
    for neighbor in order[index + 1:]:
        if neighbor in ids:
            return ids.index(neighbor)
    return len(ids)


# ---------------------------------------------------------------------------
# the flow
# ---------------------------------------------------------------------------


def _block_steps(nodes: list[Any], block_id: str, parent: str = "") -> list[tuple[list[Any], str, int]]:
    """Every step that shows the block: (the list holding it, the kind of step
    that list belongs to: "flow", "then", "else", "randomize" or "group", its index)."""
    found: list[tuple[list[Any], str, int]] = []
    for index, node in enumerate(nodes):
        if not isinstance(node, dict):
            continue
        if node.get("type") == "show_block" and str(node.get("block")) == block_id:
            found.append((nodes, parent or "flow", index))
        for key in ("then", "else", "children"):
            if isinstance(node.get(key), list):
                kind = key if key != "children" else str(node.get("type"))
                found.extend(_block_steps(node[key], block_id, kind))
    return found


def _insert_block_step(
    survey: Survey,
    definition: dict[str, Any],
    block_id: str,
    *,
    position: int | None = None,
    after: str | None = None,
    before: str | None = None,
    where: str,
) -> None:
    """Add a step showing the new block: next to the step showing ``after`` or
    ``before`` (a block), at ``position`` in the top level of the flow, or last."""
    flow = definition["flow"]
    given = [name for name, value in (("position", position), ("after", after), ("before", before)) if value is not None]
    if len(given) > 1:
        raise SurveyError(f"{where}: give one of position=, after= and before=, not {' and '.join(given)}.")
    step = {"type": "show_block", "block": block_id}
    if after is not None or before is not None:
        anchor = _block_id(definition, after if after is not None else before)
        steps = _block_steps(flow, anchor)
        if not steps:
            raise SurveyError(f"{where}: block {anchor!r} is not shown in the flow, so nothing can be placed next to it.")
        if len(steps) > 1:
            raise SurveyError(f"{where}: block {anchor!r} is shown {len(steps)} times in the flow; place the new block with position=.")
        nodes, _kind, index = steps[0]
        nodes.insert(index + (1 if after is not None else 0), step)
        return
    if position is None:
        flow.append(step)
        return
    if isinstance(position, bool) or not isinstance(position, int) or not 1 <= position <= len(flow) + 1:
        raise SurveyError(f"{where}: position is a number from 1 (first) to {len(flow) + 1} (last) in the flow, not {position!r}.")
    flow.insert(position - 1, step)


def _without_block_steps(nodes: list[Any], block_id: str) -> list[Any]:
    out = []
    for node in nodes:
        if isinstance(node, dict) and node.get("type") == "show_block" and str(node.get("block")) == block_id:
            continue
        if isinstance(node, dict):
            node = dict(node)
            for key in ("then", "else", "children"):
                if isinstance(node.get(key), list):
                    node[key] = _without_block_steps(node[key], block_id)
        out.append(node)
    return out


# ---------------------------------------------------------------------------
# content and responses
# ---------------------------------------------------------------------------


def _builder_type_name(name: Any) -> str:
    key = str(name or "").strip().lower().replace("-", "_").replace(" ", "_")
    if key not in TYPES:
        raise SurveyError(f"{name!r} is not a question type; the types are: {', '.join(TYPES)}.")
    return key


def builder_type(question: dict[str, Any]) -> str | None:
    """The builder's type name for a question in the standard format ("instruction"
    for one without a response; None for one the builder does not write)."""
    if question.get("kind"):
        return None
    response = question.get("response")
    if not isinstance(response, dict):
        return "instruction"
    rtype = response.get("type")
    if rtype == "choice":
        return "multiple_choice" if response.get("mode") == "multiple" else "single_choice"
    if rtype in {"text", "number", "form", "ranking", "allocation"}:
        return str(rtype)
    if rtype == "group":
        return "side_by_side"
    if rtype == "matrix":
        if response.get("mode") == "per_row":
            return "number_grid" if (response.get("row_response") or {}).get("type") == "number" else "grid"
        return "sum_grid" if (response.get("cell_response") or {}).get("type") == "number" else "text_grid"
    return None


def _text_items(text: Any) -> list[dict[str, Any]]:
    if text is None:
        return []
    if isinstance(text, str):
        return [{"type": "text", "text": text}]
    if isinstance(text, dict) and isinstance(text.get("parts"), list):
        return [{"type": "text", "text": copy.deepcopy(text)}]
    if isinstance(text, dict) and text.get("type") in {"text", "table"}:
        return [copy.deepcopy(text)]
    if isinstance(text, (list, tuple)) and all(isinstance(t, (str, dict)) for t in text):
        return [item for t in text for item in _text_items(t)]
    raise SurveyError(
        "text is a string, {\"parts\": [...]} for text that pipes values, a text or table content item, or a list of these; "
        f"not {text!r}."
    )


def _image_items(image: Any) -> list[dict[str, Any]]:
    if image is None:
        return []
    images = list(image) if isinstance(image, (list, tuple)) else [image]
    out: list[dict[str, Any]] = []
    for item in images:
        if isinstance(item, (str, Path)) and str(item).strip():
            out.append({"type": "image", "source": str(item)})
        elif isinstance(item, dict) and item.get("type", "image") == "image" and item.get("source"):
            out.append({"type": "image", **{k: copy.deepcopy(v) for k, v in item.items() if k != "type"}})
        else:
            raise SurveyError(f"image is a picture's path or web link, {{\"source\": ..., \"alt\": ...}}, or a list of these; not {item!r}.")
    return out


def _content(text: Any, image: Any, content: Any) -> list[dict[str, Any]]:
    if content is not None:
        if text is not None or image is not None:
            raise SurveyError("Give content, or text and image, not both.")
        if not isinstance(content, list) or not all(isinstance(c, dict) for c in content):
            raise SurveyError("content is a list of content items (text, image or table).")
        return copy.deepcopy(content)
    return _text_items(text) + _image_items(image)


def _replace_items(content: list[dict[str, Any]], kind: str, new: list[dict[str, Any]], after: str = "") -> list[dict[str, Any]]:
    """The content with its ``kind`` items replaced by ``new``, where the first one
    was (else after the last ``after`` item, else at the end)."""
    kinds = {"text": {"text", "table"}, "image": {"image"}}[kind]
    first = next((i for i, item in enumerate(content) if item.get("type") in kinds), None)
    kept = [item for item in content if item.get("type") not in kinds]
    if first is None:
        others = {"text": {"text", "table"}, "image": {"image"}}.get(after, set())
        last = max((i for i, item in enumerate(kept) if item.get("type") in others), default=len(kept) - 1)
        index = last + 1 if after else len(kept)
    else:
        index = len([item for item in content[:first] if item.get("type") not in kinds])
    return kept[:index] + new + kept[index:]


def _list_of(values: Any, name: str, *, where: str, presets: bool = False) -> list[dict[str, Any]]:
    """Options, rows, columns, items or fields from labels or objects; an entry
    without an ID gets the number of its place ("1", "2", ...) if free."""
    if isinstance(values, str):
        if presets and values in SCALE_PRESETS:
            values = SCALE_PRESETS[values]
        else:
            named = f" (or the name of a scale: {', '.join(SCALE_PRESETS)})" if presets else ""
            raise SurveyError(f"{where}: {name} is a list of labels or objects{named}, not {values!r}.")
    if not isinstance(values, (list, tuple)) or not values:
        raise SurveyError(f"{where}: {name} is a non-empty list of labels or objects.")
    taken = {str(v["id"]) for v in values if isinstance(v, dict) and v.get("id") not in (None, "")}
    out: list[dict[str, Any]] = []
    for place, value in enumerate(values, start=1):
        if isinstance(value, dict):
            item = copy.deepcopy(value)
        elif isinstance(value, (str, int, float)) and not isinstance(value, bool):
            item = {"label": str(value)}
        else:
            raise SurveyError(f"{where}: each entry of {name} is a label or an object, not {value!r}.")
        if item.get("id") in (None, ""):
            number = place
            while str(number) in taken:
                number += 1
            item = {"id": str(number), **item}
            taken.add(str(number))
        else:
            item["id"] = str(item["id"])
        out.append(item)
    return out


def _sbs_columns(columns: Any, rows: list[dict[str, Any]], *, where: str) -> list[dict[str, Any]]:
    """Side-by-side columns: each ``{"label": ..., "scale": [...]}`` becomes a grid
    of the shared statements on that scale; one with its own ``response`` stays."""
    fields = _list_of(columns, "columns", where=where)
    out = []
    for field in fields:
        if "response" not in field:
            if "scale" not in field:
                raise SurveyError(f"{where}: each side-by-side column needs a scale (or its own response), as {{\"label\": ..., \"scale\": [...]}}.")
            scale = _list_of(field.pop("scale"), "scale", where=where, presets=True)
            field["response"] = {
                "type": "matrix",
                "mode": "per_row",
                "rows": copy.deepcopy(rows),
                "row_response": {"type": "choice", "mode": "single", "options": scale},
            }
        out.append(field)
    return out


# Where the response settings of each type go: the response, then the response
# its rows, cells or fields take.
_TARGETS: dict[str, tuple[tuple[str | None, Any], ...]] = {
    "single_choice": ((None, model.ChoiceResponse),),
    "multiple_choice": ((None, model.ChoiceResponse),),
    "text": ((None, model.TextResponse),),
    "number": ((None, model.NumberResponse),),
    "grid": ((None, model.MatrixResponse), ("row_response", model.ChoiceResponse)),
    "number_grid": ((None, model.MatrixResponse), ("row_response", model.NumberResponse)),
    "text_grid": ((None, model.MatrixResponse), ("cell_response", model.TextResponse)),
    "sum_grid": ((None, model.MatrixResponse), ("cell_response", model.NumberResponse)),
    "form": ((None, model.FormResponse), ("field_response", model.TextResponse)),
    "ranking": ((None, model.RankingResponse),),
    "allocation": ((None, model.AllocationResponse),),
    "side_by_side": ((None, model.GroupResponse),),
    "instruction": (),
}
# Settings the builder reads itself, by type.
_OWN_SETTINGS = {"sum_grid": ("total", "axis")}
# Fields fixed by the type (set by the builder, never by a setting).
_FIXED = {"type", "mode", "layout"}


def _build_response(
    btype: str,
    parts: dict[str, Any],
    settings: dict[str, Any],
    *,
    where: str,
    base: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """A new response of the builder type, from its lists and settings (the settings
    it uses are taken out of ``settings``)."""
    if TYPES[btype] is None:
        stray = sorted(parts)
        if stray:
            raise SurveyError(f"{where}: an instruction takes no answer, so no {', '.join(stray)}.")
        return None
    required, _optional = _PARTS[btype]
    allowed = set(required) | set(_optional)
    stray = sorted(set(parts) - allowed)
    if stray:
        raise SurveyError(f"{where}: a {btype} question has no {', '.join(stray)} (it takes {', '.join(required) or 'no lists'}).")
    response: dict[str, Any] = dict(base or {})
    if btype in {"single_choice", "multiple_choice"}:
        response = {"type": "choice", "mode": "single" if btype == "single_choice" else "multiple", **response}
    elif btype in {"grid", "number_grid", "text_grid", "sum_grid"}:
        response = {"type": "matrix", "mode": "per_row" if btype in {"grid", "number_grid"} else "per_cell", **response}
    elif btype == "side_by_side":
        response = {"type": "group", "layout": "side_by_side", **response}
    else:
        response = {"type": TYPES[btype], **response}
    sub_given = {key for key, _spec in _TARGETS[btype][1:] if key in settings}
    missing = [name for name in required if name not in parts and not (name == "scale" and "row_response" in sub_given)]
    if missing:
        raise SurveyError(f"{where}: a {btype} question needs {' and '.join(missing)}.")
    _set_parts(btype, response, parts, where=where)
    if btype == "number_grid":
        response.setdefault("row_response", {"type": "number"})
    elif btype == "text_grid":
        response.setdefault("cell_response", {"type": "text"})
    elif btype == "sum_grid":
        response.setdefault("cell_response", {"type": "number"})
        response.setdefault("constraints", [{"type": "sum_equals", "axis": "row", "total": 100}])
    elif btype == "allocation":
        response.setdefault("total", 100)
    _set_settings(btype, response, settings)
    return response


def _set_parts(btype: str, response: dict[str, Any], parts: dict[str, Any], *, where: str) -> None:
    """Put the given lists where the type keeps them."""
    if "options" in parts:
        response["options"] = _list_of(parts["options"], "options", where=where, presets=True)
    if "items" in parts:
        response["items"] = _list_of(parts["items"], "items", where=where)
    if btype == "side_by_side":
        if "rows" in parts or "columns" in parts:
            if "columns" not in parts:
                rows = _list_of(parts["rows"], "rows", where=where)
                for field in response.get("fields") or []:
                    if isinstance(field.get("response"), dict):
                        field["response"]["rows"] = copy.deepcopy(rows)
            else:
                rows = _list_of(parts["rows"], "rows", where=where) if "rows" in parts else _sbs_rows(response)
                response["fields"] = _sbs_columns(parts["columns"], rows, where=where)
        return
    if "rows" in parts:
        response["rows"] = _list_of(parts["rows"], "rows", where=where)
    if "columns" in parts:
        response["columns"] = _list_of(parts["columns"], "columns", where=where)
    if "fields" in parts:
        response["fields"] = _list_of(parts["fields"], "fields", where=where)
    if "scale" in parts:
        row = response.get("row_response") if isinstance(response.get("row_response"), dict) else {"type": "choice", "mode": "single"}
        row["options"] = _list_of(parts["scale"], "scale", where=where, presets=True)
        response["row_response"] = row


def _sbs_rows(response: dict[str, Any]) -> list[dict[str, Any]]:
    for field in response.get("fields") or []:
        rows = (field.get("response") or {}).get("rows")
        if rows:
            return copy.deepcopy(rows)
    raise SurveyError("A side-by-side question needs rows (its statements).")


def _set_settings(btype: str, response: dict[str, Any], settings: dict[str, Any]) -> None:
    """Move each setting the type takes from ``settings`` into the response (into
    the response its rows, cells or fields take when only that has the field). A
    value of None removes the setting."""
    targets = _TARGETS[btype]
    for name in list(settings):
        if name in _FIXED:
            continue
        if name in _OWN_SETTINGS.get(btype, ()):
            continue
        for key, spec in targets:
            if name not in spec.fields:
                continue
            holder = response if key is None else response.setdefault(key, _default_sub(btype))
            value = settings.pop(name)
            if value is None:
                holder.pop(name, None)
            else:
                holder[name] = copy.deepcopy(value)
            break
    if btype == "sum_grid" and ("total" in settings or "axis" in settings):
        total, axis = settings.pop("total", UNCHANGED), settings.pop("axis", UNCHANGED)
        constraints = [c for c in response.get("constraints") or [] if c.get("type") != "sum_equals"]
        current = next((c for c in response.get("constraints") or [] if c.get("type") == "sum_equals"), {"type": "sum_equals", "axis": "row", "total": 100})
        if total is None:
            response["constraints"] = constraints
        else:
            current = dict(current)
            if total is not UNCHANGED:
                current["total"] = total
            if axis is not UNCHANGED and axis is not None:
                current["axis"] = axis
            response["constraints"] = constraints + [current]
        if not response["constraints"]:
            response.pop("constraints")


def _default_sub(btype: str) -> dict[str, Any]:
    return {
        "grid": {"type": "choice", "mode": "single"},
        "number_grid": {"type": "number"},
        "text_grid": {"type": "text"},
        "sum_grid": {"type": "number"},
        "form": {"type": "text"},
    }[btype]


def _update_response(btype: str, response: dict[str, Any], parts: dict[str, Any], settings: dict[str, Any], *, where: str) -> dict[str, Any]:
    required, optional = _PARTS[btype]
    stray = sorted(set(parts) - set(required) - set(optional))
    if stray:
        raise SurveyError(f"{where}: a {btype} question has no {', '.join(stray)} (it takes {', '.join(required) or 'no lists'}).")
    response = copy.deepcopy(response)
    _set_parts(btype, response, parts, where=where)
    _set_settings(btype, response, settings)
    return response


def _carried_parts(response: Any, new_type: str) -> dict[str, Any]:
    """The lists of the old response that a new type takes, by the new type's names."""
    if not isinstance(response, dict):
        return {}
    wanted = set(_PARTS[new_type][0])
    out: dict[str, Any] = {}
    listed = response.get("options") or response.get("items")
    if listed and "options" in wanted:
        out["options"] = listed
    if listed and "items" in wanted:
        out["items"] = listed
    if response.get("rows") and "rows" in wanted:
        out["rows"] = response["rows"]
    if response.get("type") == "matrix" and response.get("columns") and "columns" in wanted and new_type != "side_by_side":
        out["columns"] = response["columns"]
    scale = (response.get("row_response") or {}).get("options") if response.get("type") == "matrix" else None
    if scale and "scale" in wanted:
        out["scale"] = scale
    if response.get("type") == "form" and "fields" in wanted:
        out["fields"] = response.get("fields")
    return {k: copy.deepcopy(v) for k, v in out.items() if v}


def _carried_settings(response: Any, new_type: str) -> dict[str, Any]:
    """``required``, and the order of a list the new type takes over, when the new
    response has these fields."""
    if not isinstance(response, dict) or TYPES[new_type] is None:
        return {}
    spec = _TARGETS[new_type][0][1]
    out: dict[str, Any] = {}
    if "required" in response:
        out["required"] = response["required"]
    lists_kept = bool(set(_carried_parts(response, new_type)) & {"options", "items", "rows"})
    if lists_kept and "randomization" in response and "randomization" in spec.fields:
        out["randomization"] = copy.deepcopy(response["randomization"])
    return out


# What a question read from a QSF keeps of its source when it gets another type
# (the rest described the old answer), and what goes when its options, rows,
# columns or items are replaced (copies of the old lists).
_QUESTION_SOURCE = frozenset({"format", "display_logic", "skip_logic", "keep_lines", "question_media", "no_export_name", "flags"})
_LIST_SOURCE = frozenset({"sbs", "id_lists", "no_rows"})


def _forget_source(question: dict[str, Any], *, keep: frozenset[str] | None = None, drop: frozenset[str] = frozenset()) -> None:
    source = question.get("source")
    if not isinstance(source, dict):
        return
    source = {k: v for k, v in source.items() if (keep is None or k in keep) and k not in drop}
    if source:
        question["source"] = source
    else:
        question.pop("source", None)


def _apply_question_settings(question: dict[str, Any], settings: dict[str, Any], *, where: str) -> None:
    """Question fields (export name, display condition, skips, presentation)."""
    if "control" in settings:
        control = settings.pop("control")
        if control is None:
            presentation = dict(question.get("presentation") or {})
            presentation.pop("control", None)
            if presentation:
                question["presentation"] = presentation
            else:
                question.pop("presentation", None)
        else:
            question["presentation"] = {**(question.get("presentation") or {}), "control": control}
    for name in _QUESTION_SETTINGS:
        if name not in settings:
            continue
        value = settings.pop(name)
        if value is None:
            question.pop(name, None)
        else:
            question[name] = copy.deepcopy(value)
    # the key order of a question in the format
    order = list(model.Question.fields)
    extra = [k for k in question if k not in order]
    reordered = {k: question[k] for k in order + extra if k in question}
    question.clear()
    question.update(reordered)


def _refuse_unknown(settings: dict[str, Any], btype: str, where: str) -> None:
    if not settings:
        return
    names: list[str] = []
    for _key, spec in _TARGETS.get(btype, ()):
        names += [n for n in spec.fields if n not in _FIXED and n not in names and n not in {"options", "rows", "columns", "items", "fields"}]
    names += list(_OWN_SETTINGS.get(btype, ()))
    names += list(_QUESTION_SETTINGS) + ["control", "content"]
    raise SurveyError(
        f"{where}: {', '.join(repr(k) for k in sorted(settings))} {'is not a setting' if len(settings) == 1 else 'are not settings'} "
        f"of a {btype} question; its settings are: {', '.join(names)}."
    )


# ---------------------------------------------------------------------------
# what reads a question
# ---------------------------------------------------------------------------


def _reads_of(survey: Survey, definition: dict[str, Any], questions: set[str], *, exclude_self: str = "") -> list[tuple[str, str, str]]:
    from ..engine.drops import reads

    found = reads(definition, questions)
    if exclude_self:
        found = [r for r in found if r[1] != f"SQ {exclude_self}" and not r[1].startswith(f"SQ {exclude_self} ")]
    return found


def _order_problems(definition: dict[str, Any]) -> set[tuple[str, str, str]]:
    """(kind, location, question) for each piece of logic or piped text that reads a
    question not asked before it, in the flow as written (every branch and arm in
    the order written; a question's own skip rules after it is answered)."""
    from ..engine.definition import Definition, flow_id
    from ..engine.drops import _item_conditions, _question_content, block_questions, condition_questions, text_questions

    d = Definition(definition)
    known = {str(q.get("id")) for q in definition.get("questions") or [] if isinstance(q, dict)}
    asked: set[str] = set()
    problems: set[tuple[str, str, str]] = set()

    def check(kind: str, where: str, read: set[str]) -> None:
        for qid in read:
            if qid in known and qid not in asked:
                problems.add((kind, where, qid))

    def walk(nodes: list[Any]) -> None:
        for node in nodes:
            if not isinstance(node, dict):
                continue
            kind = node.get("type")
            if kind == "branch":
                check("branch", f"FL {flow_id(node)}", condition_questions(node.get("when")))
            elif kind == "set_variables":
                for assignment in node.get("assignments") or []:
                    if isinstance(assignment, dict) and not assignment.get("from_outside"):
                        check("embedded_data", f"FL {flow_id(node)}", text_questions(assignment.get("value")))
            elif kind == "show_block":
                block_id = str(node.get("block"))
                block = d.blocks.get(block_id)
                if block is None:
                    continue
                if block.get("kind") in {"conjoint", "maxdiff"}:
                    asked.update(block_questions(block))
                    continue
                if isinstance(block.get("repeat"), dict):
                    rows = [row.get("values") for row in block["repeat"].get("rows") or [] if isinstance(row, dict)]
                    check("loop_fields", f"BL {block_id}", text_questions(rows))
                for qid in block.get("questions") or []:
                    question = d.questions.get(str(qid))
                    if question is None:
                        continue
                    where = f"SQ {qid}"
                    check("display_logic", where, condition_questions(question.get("show_if")))
                    for place, item_id, condition in _item_conditions(question):
                        check("choice_display_logic", f"{where} {place} {item_id}", condition_questions(condition))
                    check("piping", where, text_questions(_question_content(question)))
                    asked.add(str(qid))
                    for rule in question.get("skip") or []:
                        if isinstance(rule, dict):
                            check("skip_logic", where, condition_questions(rule.get("when")))
            for key in ("then", "else", "children"):
                if isinstance(node.get(key), list):
                    walk(node[key])

    walk(d.flow)
    return problems


_READ_KINDS = {
    "branch": "the condition of branch {name!r}",
    "embedded_data": "a value set in step {name!r}",
    "loop_fields": "the repeat rows of block {name!r}",
    "display_logic": "the display condition of question {name!r}",
    "skip_logic": "a skip rule of question {name!r}",
    "piping": "the text of question {name!r}",
}


def _read_line(kind: str, where: str, qid: str, *, later: bool = False) -> str:
    """One line saying what reads a question, in words."""
    _prefix, _, name = where.partition(" ")
    if kind == "choice_display_logic":
        question, _, item = name.partition(" ")
        place, _, item_id = item.rpartition(" ")
        what = f"the display condition of {place or 'item'} {item_id!r} of question {question!r}"
    else:
        what = _READ_KINDS.get(kind, f"{kind.replace('_', ' ')} at {where}").format(name=name)
    if kind == "skip_logic" and not later:
        return f"{what} jumps to question {qid!r}"
    if later:
        return f"{what} reads question {qid!r}, which would be asked after it"
    return f"{what} reads question {qid!r}"


def _read_lines(readers: list[tuple[str, str, str]], *, later: bool = False) -> list[str]:
    return [_read_line(kind, where, qid, later=later) for kind, where, qid in readers]


# ---------------------------------------------------------------------------
# checking and making the edited survey
# ---------------------------------------------------------------------------


def _repeated_columns(definition: dict[str, Any]) -> set[str]:
    """The CSV column names that appear more than once for this definition."""
    from ..results import exports as ex
    from ..results.layout import Layout

    header, _desc, _imports, _rows = ex._build_public_surveytwin_csv(
        [], {}, {}, [], answer_resolver=ex._resolve_label_answer, layout=Layout(definition)
    )
    seen: set[str] = set()
    repeated: set[str] = set()
    for column in ["TWIN_ID"] + [str(h) for h in header[1:]]:
        if column in seen:
            repeated.add(column)
        seen.add(column)
    return repeated


def _visible(survey: Survey, definition: dict[str, Any]) -> dict[str, Any]:
    if not survey.exclude_blocks:
        return definition
    from ..engine.drops import without_blocks

    return without_blocks(definition, survey.exclude_blocks, gone=survey.exclude_questions)[0]


def _new_only(after: list[Finding], before: list[Finding]) -> list[Finding]:
    """The findings of ``after`` that ``before`` does not have (by code and message)."""
    left = Counter((f.code, f.message) for f in before)
    out = []
    for finding in after:
        key = (finding.code, finding.message)
        if left[key]:
            left[key] -= 1
        else:
            out.append(finding)
    return out


def _finish(survey: Survey, after: dict[str, Any], entry: dict[str, Any]) -> Survey:
    """The survey with the edited definition, once it is checked (see the module
    docstring); nothing changes when a check fails."""
    from ..spec.compile import CompileError, compile_survey
    from ..spec.pictures import picture_places, read_pictures

    before = survey._standard_full
    before_findings = check_survey(before)
    findings = check_survey(after)
    errors = _new_only([f for f in findings if f.level == "error"], [f for f in before_findings if f.level == "error"])
    if errors:
        raise SurveyFormatError(errors)
    later = sorted(_order_problems(after) - _order_problems(before))
    if later:
        raise UnsupportedEdit(
            "This edit would leave logic or piped text reading a question asked only after it; reorder or change them first:\n"
            + "\n".join(f"  {line}" for line in _read_lines(later, later=True))
        )
    try:
        template = compile_survey(after)
    except CompileError as exc:
        raise SurveyFormatError([], f"The edited survey cannot be laid out: {exc}") from exc
    clashes = sorted(_repeated_columns(_visible(survey, after)) - _repeated_columns(_visible(survey, before)))
    if clashes:
        raise SurveyFormatError([
            Finding("$.questions", f"two CSV columns would be named {column!r}; give one of the questions another ID or export_name", "export.column_clash")
            for column in clashes
        ])
    folder = survey._picture_folder or Path.cwd()
    pictures, problems = read_pictures(after, Path(folder), known=survey._pictures)
    if problems:
        raise SurveyFormatError(problems)
    named = {item["source"] for _place, item in picture_places(after)}
    pictures = {source: address for source, address in pictures.items() if source in named}
    edited = survey._derive(template=template, standard=after, pictures=pictures, edits=[dict(e) for e in survey.edits] + [entry])
    edited.edit_warnings = _new_only([f for f in findings if f.level == "warning"], [f for f in before_findings if f.level == "warning"])
    return edited
