"""Dropping blocks from a survey definition, and what in a survey reads a question.

``without_blocks`` takes dropped blocks out of a definition: the blocks and their
questions go, and so does a branch that reads only questions that are gone and
shows no block (a screen-out after a dropped consent question). ``reads`` lists
the logic and piped text that read some questions (for ``drop_report`` and the
block previews), ``missing_reads`` the logic that reads a question the survey
does not have, and ``screenout_blocks`` the blocks whose answers can end the
survey. Questions are recognized by the survey's own conditions and references,
whatever their IDs look like (``QID12`` from a QSF, ``consent`` in a survey
written in the standard format); a condition or text kept as the source wrote it
is searched for Qualtrics question IDs, as before.
"""

from __future__ import annotations

import copy
import json
import re
from typing import Any

from .conditions import negate
from .definition import Definition, flow_id

_QID_RE = re.compile(r"QID\d+")
_PIPE_RE = re.compile(r"\$\{q://([^/}]+)")
_LOOP_PREFIX = re.compile(r"^\d+_(?=.)")


def block_questions(block: dict[str, Any]) -> list[str]:
    """The IDs of a block's questions; for a conjoint or MaxDiff block, of its tasks."""
    if block.get("kind") in {"conjoint", "maxdiff"}:
        from ..results.layout import experiment_skeleton

        return [str(shown["question"]["id"]) for shown in experiment_skeleton(block)]
    return [str(q) for q in block.get("questions") or []]


def _ids(question: str) -> set[str]:
    """A question ID as written, and without a Loop & Merge row prefix (``1_QID5``)."""
    question = str(question or "").strip()
    return {question, _LOOP_PREFIX.sub("", question)} - {""}


def _atoms(condition: Any) -> list[dict[str, Any]]:
    if not isinstance(condition, dict):
        return []
    if condition.get("op") in {"and", "or"}:
        return [atom for arg in condition.get("args") or [] for atom in _atoms(arg)]
    if condition.get("op") == "not":
        return _atoms(condition.get("arg"))
    return [condition]


def text_questions(value: Any) -> set[str]:
    """The questions whose answers a value pipes (``${q://...}``), anywhere in it."""
    from .analysis import _token_strings

    return {qid for text in _token_strings(value) for match in _PIPE_RE.findall(text) for qid in _ids(match)}


def condition_questions(condition: Any) -> set[str]:
    """The questions a condition reads: the questions it tests and the answers it
    compares or pipes."""
    from ..spec.text import pipe_token

    found: set[str] = set()
    for atom in _atoms(condition):
        if atom.get("op") == "source":
            found |= set(_QID_RE.findall(json.dumps(atom.get("qualtrics"), ensure_ascii=False)))
            continue
        if atom.get("question"):
            found |= _ids(atom["question"])
        left = atom.get("left")
        if isinstance(left, dict) and left.get("scope") == "answer":
            found |= _ids(left.get("question"))
        elif isinstance(left, dict) and left.get("scope") == "source":
            found |= text_questions(pipe_token(left))
        found |= text_questions(atom.get("right"))
    return found


def _item_conditions(question: dict[str, Any]) -> list[tuple[str, str, Any]]:
    """(place, item ID, condition) for each display condition on a single item, as
    Qualtrics groups them: a choice (options, rows, fields, items) or a scale point
    (a grid's columns); conditions kept as read for items not shown come last."""
    from ..spec.compile import Compiler

    response = question.get("response")
    source = question.get("source") if isinstance(question.get("source"), dict) else {}
    found: list[tuple[str, str, Any]] = []
    for place, name, items in (
        ("choice", "choice_logic", Compiler.choice_items(response)),
        ("scale point", "answer_logic", Compiler.answer_items(response)),
    ):
        logic: dict[str, Any] = {
            str(item["id"]): item["show_if"] for item in items if isinstance(item, dict) and item.get("show_if") is not None
        }
        raw = source.get(name) if isinstance(source.get(name), dict) else {}
        shown = {str(item.get("id")) for item in items if isinstance(item, dict)}
        logic.update({str(item_id): {"op": "source", "qualtrics": value} for item_id, value in raw.items() if str(item_id) not in shown})
        if name in source and logic:
            ordered = {str(item_id): logic[str(item_id)] for item_id in raw if str(item_id) in logic}
            ordered.update({item_id: value for item_id, value in logic.items() if item_id not in ordered})
            logic = ordered
        found += [(place, item_id, condition) for item_id, condition in logic.items()]
    return found


def _question_content(question: dict[str, Any]) -> Any:
    """A question without its conditions: what its text, options and settings show."""

    def strip(value: Any) -> Any:
        if isinstance(value, dict) and not isinstance(value.get("parts"), list):
            return {key: strip(item) for key, item in value.items() if key not in {"show_if", "skip", "choice_logic", "answer_logic", "display_logic", "skip_logic"}}
        if isinstance(value, list):
            return [strip(item) for item in value]
        return value

    return strip(question)


def reads(definition_dict: dict[str, Any], questions: set[str], dropped_blocks: Any = ()) -> list[tuple[str, str, str]]:
    """(kind, location, question) for each piece of logic or piped text outside the
    dropped blocks that reads one of ``questions``, in flow order: ``branch`` (FL
    <flow ID>), ``embedded_data`` (a value set in the flow), ``loop_fields`` (BL
    <block ID>), ``display_logic``, ``choice_display_logic`` (SQ <ID> choice
    <item>, or scale point <item>), ``skip_logic`` (a jump to it) and ``piping``."""
    definition = Definition(definition_dict)
    wanted = {str(q) for q in questions}
    dropped = {str(b) for b in dropped_blocks or ()}
    found: list[tuple[str, str, str]] = []

    def note(kind: str, where: str, read: set[str]) -> None:
        found.extend((kind, where, qid) for qid in sorted(read & wanted))

    def walk(nodes: list[Any]) -> None:
        for node in nodes:
            if not isinstance(node, dict):
                continue
            kind = node.get("type")
            if kind == "branch":
                where = f"FL {flow_id(node)}"
                note("branch", where, condition_questions(node.get("when")))
                walk(node.get("then") or [])
                walk(node.get("else") or [])
                continue
            if kind == "set_variables":
                for assignment in node.get("assignments") or []:
                    if isinstance(assignment, dict) and not assignment.get("from_outside"):
                        note("embedded_data", f"FL {flow_id(node)}", text_questions(assignment.get("value")))
                continue
            if kind == "show_block":
                block_id = str(node.get("block"))
                block = definition.blocks.get(block_id)
                if block is None or block_id in dropped:
                    continue
                if isinstance(block.get("repeat"), dict):
                    rows = [row.get("values") for row in block["repeat"].get("rows") or [] if isinstance(row, dict)]
                    options = (block.get("source") or {}).get("options") if isinstance(block.get("source"), dict) else None
                    looping = options.get("LoopingOptions") if isinstance(options, dict) else None
                    note("loop_fields", f"BL {block_id}", text_questions(rows) | text_questions(looping))
                for qid in block.get("questions") or []:
                    question = definition.questions.get(str(qid))
                    if question is None:
                        continue
                    where = f"SQ {qid}"
                    note("display_logic", where, condition_questions(question.get("show_if")))
                    for place, item_id, condition in _item_conditions(question):
                        note("choice_display_logic", f"{where} {place} {item_id}", condition_questions(condition))
                    for rule in question.get("skip") or []:
                        if isinstance(rule, dict) and str(rule.get("to", "")) in wanted:
                            found.append(("skip_logic", where, str(rule["to"])))
                    note("piping", where, text_questions(_question_content(question)))
                continue
            for key in ("then", "else", "children"):
                if isinstance(node.get(key), list):
                    walk(node[key])

    walk(definition.flow)
    return list(dict.fromkeys(found))


def _all_reads(definition_dict: dict[str, Any]) -> set[str]:
    """Every question ID that some logic or piped text of the survey names."""
    definition = Definition(definition_dict)
    found: set[str] = set()

    def walk(nodes: list[Any]) -> None:
        for node in nodes:
            if not isinstance(node, dict):
                continue
            if node.get("type") == "branch":
                found.update(condition_questions(node.get("when")))
            elif node.get("type") == "set_variables":
                for assignment in node.get("assignments") or []:
                    if isinstance(assignment, dict):
                        found.update(text_questions(assignment.get("value")))
            elif node.get("type") == "show_block":
                block = definition.blocks.get(str(node.get("block"))) or {}
                if isinstance(block.get("repeat"), dict):
                    found.update(text_questions([row.get("values") for row in block["repeat"].get("rows") or [] if isinstance(row, dict)]))
                for qid in block.get("questions") or []:
                    question = definition.questions.get(str(qid))
                    if question is None:
                        continue
                    found.update(condition_questions(question.get("show_if")))
                    for _place, _item, condition in _item_conditions(question):
                        found.update(condition_questions(condition))
                    found.update(str(rule.get("to")) for rule in question.get("skip") or [] if isinstance(rule, dict) and rule.get("to"))
                    found.update(text_questions(_question_content(question)))
            for key in ("then", "else", "children"):
                if isinstance(node.get(key), list):
                    walk(node[key])

    walk(definition.flow)
    return found - {"end_of_block", "end_of_survey"}


def missing_reads(definition_dict: dict[str, Any], known: Any) -> list[tuple[str, str, str]]:
    """``reads`` for the questions that logic or piped text names and that are not
    in ``known`` (the survey's questions, dropped ones included)."""
    candidates = _all_reads(definition_dict) - {str(q) for q in known}
    return reads(definition_dict, candidates) if candidates else []


def _shows_content(definition: Definition, steps: list[Any], dropped: set[str]) -> bool:
    """Whether steps show a block that is kept."""
    for node in steps:
        if not isinstance(node, dict):
            continue
        if node.get("type") == "show_block" and str(node.get("block")) in definition.blocks and str(node.get("block")) not in dropped:
            return True
        for key in ("then", "else", "children"):
            if isinstance(node.get(key), list) and _shows_content(definition, node[key], dropped):
                return True
    return False


def without_blocks(definition_dict: dict[str, Any], block_ids: Any, gone: Any = ()) -> tuple[dict[str, Any], list[str]]:
    """(the survey without these blocks, the flow IDs of the branches removed with
    them). Their questions go too. A branch whose condition reads only questions
    that are gone (in the dropped blocks, or in ``gone``: excluded when the survey
    was read) and that shows no block is removed; a branch that shows a block
    stays, so dropping a question never removes other content. A branch with
    ``else`` is judged part by part, its ``else`` on the opposite condition."""
    dropped = {str(b) for b in block_ids or ()}
    gone_questions = {str(q) for q in gone or ()}
    if not dropped and not gone_questions:
        return definition_dict, []
    definition = Definition(definition_dict)
    for block_id in dropped:
        gone_questions.update(block_questions(definition.blocks.get(block_id) or {}))
    pruned: list[str] = []

    def orphaned(condition: Any, steps: list[Any]) -> bool:
        read = condition_questions(condition)
        return bool(read) and read <= gone_questions and not _shows_content(definition, steps, dropped)

    def keep(nodes: list[Any]) -> list[Any]:
        out: list[Any] = []
        for node in nodes:
            if not isinstance(node, dict):
                continue
            if node.get("type") == "show_block" and str(node.get("block")) in dropped:
                continue
            node = dict(node)
            for key in ("then", "else", "children"):
                if isinstance(node.get(key), list):
                    node[key] = keep(node[key])
            if node.get("type") == "branch":
                when = node.get("when") or {"op": "true"}
                where = flow_id(node)
                has_else = bool(node.get("else"))
                then_goes = orphaned(when, node.get("then") or [])
                else_goes = has_else and orphaned(when, node["else"])
                if then_goes:
                    pruned.append(where)
                if else_goes:
                    pruned.append(f"{where}_else")
                if then_goes and (else_goes or not has_else):
                    continue
                if then_goes:
                    # the else part stays, as a branch of its own on the opposite condition
                    try:
                        opposite = negate(when)
                    except ValueError:
                        opposite = {"op": "not", "arg": when}
                    steps = node["else"]
                    node = {key: value for key, value in node.items() if key not in {"when", "then", "else"}}
                    node.update({"id": f"{node.get('id') or where}_else", "when": opposite, "then": steps})
                elif else_goes:
                    node.pop("else")
            out.append(node)
        return out

    flow = keep(list(definition.flow))
    out = dict(definition_dict)
    out["flow"] = flow
    if not definition.native and pruned and "variables" in out:
        # read from a QSF (or a template), the variables are those its flow sets or
        # declares: a removed branch takes its own with it
        named = _flow_variables(flow)
        out["variables"] = [v for v in out["variables"] if not isinstance(v, dict) or str(v.get("id")) in named]
        if not out["variables"]:
            del out["variables"]
    out["blocks"] = [copy.deepcopy(b) for b in definition_dict.get("blocks") or [] if str(b.get("id")) not in dropped]
    dropped_questions = {str(q) for block_id in dropped for q in (definition.blocks.get(block_id) or {}).get("questions") or []}
    out["questions"] = [copy.deepcopy(q) for q in definition_dict.get("questions") or [] if str(q.get("id")) not in dropped_questions]
    return out, pruned


def _flow_variables(nodes: list[Any]) -> set[str]:
    """Every variable some step sets or declares."""
    found: set[str] = set()
    for node in nodes:
        if not isinstance(node, dict):
            continue
        if node.get("type") == "set_variables":
            found.update(str(a.get("variable")) for a in node.get("assignments") or [] if isinstance(a, dict))
        for key in ("then", "else", "children"):
            if isinstance(node.get(key), list):
                found |= _flow_variables(node[key])
    return found


def screenout_blocks(definition_dict: dict[str, Any]) -> list[str]:
    """The blocks whose answers can end the survey: a question with a skip rule to
    the end of the survey, or read by a branch that only ends it."""
    definition = Definition(definition_dict)
    triggers: set[str] = set()

    def walk(nodes: list[Any]) -> None:
        for node in nodes:
            if not isinstance(node, dict):
                continue
            if node.get("type") == "branch":
                for condition, steps in _branch_parts(node):
                    if steps and all(isinstance(s, dict) and s.get("type") == "end" for s in steps):
                        triggers.update(condition_questions(condition))
            if node.get("type") == "show_block":
                block = definition.blocks.get(str(node.get("block"))) or {}
                for qid in block.get("questions") or []:
                    question = definition.questions.get(str(qid)) or {}
                    if any(isinstance(rule, dict) and rule.get("to") == "end_of_survey" for rule in question.get("skip") or []):
                        triggers.add(str(qid))
            for key in ("then", "else", "children"):
                if isinstance(node.get(key), list):
                    walk(node[key])

    walk(definition.flow)
    hits: list[str] = []
    for block_id, block in definition.blocks.items():
        if any(str(q) in triggers for q in block.get("questions") or []) and block_id not in hits:
            hits.append(block_id)
    return hits


def _branch_parts(node: dict[str, Any]) -> list[tuple[Any, list[Any]]]:
    parts = [(node.get("when") or {"op": "true"}, node.get("then") or [])]
    if node.get("else"):
        parts.append((node.get("when") or {"op": "true"}, node["else"]))
    return parts


def shown_questions(definition_dict: dict[str, Any]) -> set[str]:
    """The questions of every block the flow shows (dropped blocks included, when
    given the whole survey)."""
    definition = Definition(definition_dict)
    found: set[str] = set()

    def walk(nodes: list[Any]) -> None:
        for node in nodes:
            if not isinstance(node, dict):
                continue
            if node.get("type") == "show_block":
                found.update(block_questions(definition.blocks.get(str(node.get("block"))) or {}))
            for key in ("then", "else", "children"):
                if isinstance(node.get(key), list):
                    walk(node[key])

    walk(definition.flow)
    return found
