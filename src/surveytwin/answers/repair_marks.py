"""Which answers a repaired answer had shaped.

A twin answers "Which fruit?" with Grape, which is no option, and the next
question is asked as "Why do you like ?". A rerun (or a manual repair) later
repairs the first answer to Banana; the second was still answered to what the
original answer made of the survey. These answers are marked, not re-asked. The
marks are worked out from the repairs stored and the survey, whenever answers or
files are built, so an undone repair or a second rerun needs nothing more.
"""

from __future__ import annotations

import json
import re
from collections import defaultdict
from typing import Any
from urllib.parse import unquote

_QID_RE = re.compile(r"QID\d+")
# a pipe to a question, or to one Loop & Merge row's copy of it ("1_QID5")
_Q_PIPE_RE = re.compile(r"\$\{q://((?:\d+_)?[^/}]+)")
_E_PIPE_RE = re.compile(r"\$\{e://Field/([^}]+)\}")
_LOOP_ROW_RE = re.compile(r"^(\d+)_(.+)$")
_SOURCE_LOGIC = ("display_logic", "skip_logic", "choice_logic", "answer_logic")

# the survey's question IDs, longest first (set by shaped_by_repaired / answers_shaping)
_QUESTION_IDS: list[str] = []


def _base(qid: str) -> str:
    """The question an answer ID belongs to: a loop row's copy (1_QID5), a row or
    option (QID5_2), a side-by-side column (QID5#1) or a write-in (QID5_2_TEXT)
    names its question."""
    core = re.sub(r"^\d+_", "", str(qid or ""))
    for question_id in _QUESTION_IDS:
        if core == question_id or core.startswith(question_id + "_") or core.startswith(question_id + "#"):
            return question_id
    return (_QID_RE.findall(str(qid or "")) or [""])[0]


def _use_ids(definition: dict[str, Any] | None) -> None:
    global _QUESTION_IDS
    _QUESTION_IDS = sorted((str(q["id"]) for q in (definition or {}).get("questions") or []), key=len, reverse=True)


def _field_name(name: Any) -> str:
    return unquote(str(name or "")).strip()


def _strings(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [text for item in value for text in _strings(item)]
    if isinstance(value, dict):
        return [text for item in value.values() for text in _strings(item)]
    return []


def _source_question_reads(logic: Any) -> set[str]:
    found: set[str] = set()

    def walk(value: Any) -> None:
        if isinstance(value, dict):
            if str(value.get("LogicType", "") or "") == "Question":
                found.update(_base(text) for text in (str(value.get(k, "")) for k in ("QuestionID", "LeftOperand", "ChoiceLocator")) if _qid_in(text))
            for child in value.values():
                walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)

    walk(logic)
    return {read for read in found if read}


def _qid_in(text: str) -> bool:
    match = re.match(r"^q://([^/]+)", text)
    return bool(_base(match.group(1) if match else text))


def _atoms(condition: Any) -> list[dict[str, Any]]:
    if not isinstance(condition, dict):
        return []
    if condition.get("op") in {"and", "or"}:
        return [atom for arg in condition.get("args") or [] for atom in _atoms(arg)]
    if condition.get("op") == "not":
        return _atoms(condition.get("arg"))
    return [condition]


def _question_reads(condition: Any) -> set[str]:
    """Question IDs a condition reads."""
    found: set[str] = set()
    for atom in _atoms(condition):
        op = atom.get("op")
        if op in {"selected", "not_selected", "displayed", "not_displayed"}:
            found.add(_base(str(atom.get("question", ""))))
        elif op == "source":
            found |= _source_question_reads(atom.get("qualtrics"))
        elif isinstance(atom.get("left"), dict) and atom["left"].get("scope") == "answer":
            found.add(_base(str(atom["left"].get("question", ""))))
    return {read for read in found if read}


def _field_reads(condition: Any) -> set[str]:
    """Variables a condition reads, and those piped into the values it compares with."""
    found: set[str] = set()
    for atom in _atoms(condition):
        left = atom.get("left") if isinstance(atom.get("left"), dict) else {}
        if left.get("scope") == "variable" and str(left.get("variable", "") or "").strip():
            found.add(_field_name(left["variable"]))
        if atom.get("op") == "source":
            for value in _dicts(atom.get("qualtrics")):
                if str(value.get("LogicType", "") or "") == "EmbeddedField" and str(value.get("LeftOperand", "") or "").strip():
                    found.add(_field_name(value["LeftOperand"]))
        for text in _strings(atom.get("right")) + (_strings(atom.get("qualtrics")) if atom.get("op") == "source" else []):
            found.update(_field_name(name) for name in _E_PIPE_RE.findall(text))
    return found


def _dicts(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, dict):
        return [value] + [d for child in value.values() for d in _dicts(child)]
    if isinstance(value, list):
        return [d for child in value for d in _dicts(child)]
    return []


def _ends_survey(nodes: Any) -> bool:
    return any(
        isinstance(node, dict) and (node.get("type") == "end" or _ends_survey(node.get("then")) or _ends_survey(node.get("else")) or _ends_survey(node.get("children")))
        for node in (nodes if isinstance(nodes, list) else [])
    )


def _content(question: dict[str, Any]) -> dict[str, Any]:
    """A question without its conditions (what it shows, and its settings)."""
    out = {key: value for key, value in question.items() if key not in {"show_if", "skip"}}
    source = out.get("source")
    if isinstance(source, dict):
        out["source"] = {key: value for key, value in source.items() if key not in _SOURCE_LOGIC}
    response = out.get("response")
    if isinstance(response, dict):
        out["response"] = _without_item_conditions(response)
    return out


def _without_item_conditions(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _without_item_conditions(child) for key, child in value.items() if key != "show_if"}
    if isinstance(value, list):
        return [_without_item_conditions(child) for child in value]
    return value


def _item_conditions(question: dict[str, Any]) -> list[Any]:
    found: list[Any] = []

    def walk(value: Any) -> None:
        if isinstance(value, dict):
            if value.get("show_if"):
                found.append(value["show_if"])
            for key, child in value.items():
                if key != "show_if":
                    walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)

    walk(question.get("response"))
    return found


def answers_shaping(definition: dict[str, Any] | None) -> dict[str, set[str]]:
    """Question ID -> the answers that decide what a twin is shown there:
    a question ID, or one Loop & Merge row's copy ("1_QID5") for
    a pipe to that row. Walked in flow order, so only answers given before a
    question count. What counts:

    - its piped text and options, and descriptive text above it in its block;
    - its display condition, and its options';
    - the branches around it, and a screen-out or a skip to the end of the
      survey earlier in the flow (whether the twin got this far);
    - a skip earlier in its block that could jump past it;
    - variables it reads, set from an answer (or copied from a variable that
      was), or set inside a branch on an answer, as the variable stood there.

    A question in no such relation is left out."""
    from ..engine.definition import assignment_value
    from ..engine.static import branch_parts
    from ..results.layout import Layout

    _use_ids(definition)
    shaped: dict[str, set[str]] = defaultdict(set)
    seen: set[str] = set()  # questions reached so far
    layout = Layout(definition)

    def via(fields: dict[str, set[str]], names: set[str]) -> set[str]:
        return set().union(*(fields.get(name, set()) for name in names)) if names else set()

    def pipes(value: Any, fields: dict[str, set[str]]) -> set[str]:
        text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
        return set(_Q_PIPE_RE.findall(text)) | via(fields, {_field_name(name) for name in _E_PIPE_RE.findall(text)})

    def logic_reads(condition: Any, fields: dict[str, set[str]]) -> set[str]:
        return _question_reads(condition) | via(fields, _field_reads(condition)) if condition else set()

    def block(questions: list[dict[str, Any]], around: set[str], fields: dict[str, set[str]], ended: set[str]) -> set[str]:
        from ..engine import render

        context: set[str] = set()  # what descriptive text above in the block reads
        rest_of_block: set[str] = set()  # skips that can end the block
        until: dict[str, set[str]] = defaultdict(set)  # destination -> skips that can jump to it
        for shown in questions:
            question = shown["question"]
            qid = str(question.get("id", "") or "")
            until.pop(qid, None)  # a skip to this question lands here: it is shown either way
            jumping = set().union(*until.values()) if until else set()
            reads = pipes(_content(question), fields)
            reads |= logic_reads(question.get("show_if"), fields)
            for condition in _item_conditions(question):
                reads |= logic_reads(condition, fields)
            kind = render.Shown(shown).kind
            if kind == "instruction":
                context = (set() if question.get("show_if") else context) | reads
                continue
            reads |= context | around | ended | rest_of_block | jumping
            reads = {r for r in reads if _base(r) != qid and _base(r) in seen}  # only answers given before it
            if reads and question.get("kind") not in {"timing", "meta_info", "captcha"}:
                shaped[qid] |= reads
            seen.add(qid)
            for rule in question.get("skip") or []:
                destination = str((rule or {}).get("to", "") or "").strip()
                if destination == "end_of_survey":
                    ended = ended | {qid}  # every later question in the survey
                elif destination == "end_of_block":
                    rest_of_block.add(qid)
                elif destination:
                    until[destination].add(qid)  # the questions it can jump past
        return ended

    questions_of = {str(b["id"]): questions for b, questions in layout.placements()}

    def walk(nodes: Any, around: set[str], fields: dict[str, set[str]], ended: set[str]) -> tuple[dict[str, set[str]], set[str]]:
        for node in nodes if isinstance(nodes, list) else []:
            if not isinstance(node, dict):
                continue
            kind = node.get("type")
            if kind == "set_variables":
                fields = dict(fields)
                for assignment in node.get("assignments") or []:
                    if assignment.get("from_outside") or not _field_name(assignment.get("variable")):
                        continue
                    # set here: from the answers and variables its value pipes, or a
                    # constant whose being set depends on the branches around it
                    fields[_field_name(assignment.get("variable"))] = pipes(str(assignment_value(assignment) or ""), fields) | around
            elif kind == "branch":
                for condition, steps in branch_parts(node):
                    decides = logic_reads(condition, fields)
                    inner, ended_inside = walk(steps, around | decides, dict(fields), set(ended))
                    # the branch may not run: a variable it sets holds either value after it
                    fields = {name: fields.get(name, set()) | inner.get(name, set()) for name in set(fields) | set(inner)}
                    ended = ended | ended_inside
                    if _ends_survey(steps):
                        ended = ended | decides | around  # a screen-out: whether the twin got past it
            elif kind == "show_block":
                if str(node.get("block")) in questions_of:
                    ended = block(questions_of[str(node.get("block"))], around, fields, ended)
            else:  # groups and randomizer arms
                fields, ended = walk(node.get("children"), around, fields, ended)
        return fields, ended

    walk((definition or {}).get("flow") or [], set(), {}, set())
    return dict(shaped)


def _matches(read: str, repaired_id: str, row_id: str) -> bool:
    """Whether a read names this repaired answer: a pipe to one loop row's copy
    names that row; a plain read names the question, and inside a loop row the
    row's own copy."""
    if _base(read) != _base(repaired_id):
        return False
    read_row = _LOOP_ROW_RE.match(read)
    repaired_row = _LOOP_ROW_RE.match(repaired_id)
    if read_row:
        return bool(repaired_row) and repaired_row.group(1) == read_row.group(1)
    row_loop = _LOOP_ROW_RE.match(row_id)
    return not (row_loop and repaired_row and row_loop.group(1) != repaired_row.group(1))


def shaped_by_repaired(rows: list[dict[str, Any]], definition: dict[str, Any] | None) -> dict[tuple[str, str], list[str]]:
    """(twin, answer ID) -> the twin's repaired answers that decided that
    question's text, options or visibility: it was answered as the
    original, flagged answer made the survey. Worked out from the repairs
    stored, so an undone repair or a second rerun needs nothing more. A question
    the twin was not asked (hidden by the flagged answer, say) passes on what
    decides it: "shown if Q2 is not X", with Q2 hidden because of Q1, is decided
    by Q1."""
    _use_ids(definition)
    repaired: dict[str, list[str]] = defaultdict(list)
    answered: dict[str, set[str]] = defaultdict(set)
    for row in rows:
        persona_id = str(row.get("persona_id", ""))
        answered[persona_id].add(_base(str(row.get("question_id", ""))))
        if str(row.get("answer_repaired") or "").strip():
            repaired[persona_id].append(str(row.get("question_id", "")))
    if not repaired:
        return {}
    shaping = answers_shaping(definition)

    def reads_of(base: str, twin_answered: set[str]) -> set[str]:
        found: set[str] = set()
        todo = list(shaping.get(base) or ())
        visited: set[str] = set()
        while todo:
            read = todo.pop()
            if read in visited:
                continue
            visited.add(read)
            found.add(read)
            if _base(read) not in twin_answered:
                todo.extend(shaping.get(_base(read)) or ())
        return found

    marks: dict[tuple[str, str], list[str]] = {}
    for row in rows:
        persona_id = str(row.get("persona_id", ""))
        sources = repaired.get(persona_id)
        if not sources:
            continue
        qid = str(row.get("question_id", ""))
        reads = reads_of(_base(qid), answered[persona_id])
        hits = sorted(
            source for source in sources
            if _base(source) != _base(qid) and any(_matches(read, source, qid) for read in reads)
        )
        if hits:
            marks[(persona_id, qid)] = hits
    return marks
