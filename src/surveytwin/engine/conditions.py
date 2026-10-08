"""Deciding a condition of the standard format for one twin.

A condition is a tree (``and``, ``or``, ``not``, comparisons, whether an option
was selected or a question shown, ``true``/``false``). It is decided as
Qualtrics logic always was: combined conditions left to right; a condition no
twin can meet (a device, location or quota test, kept as a ``source``
condition) is left out and the rest decides; a condition with nothing left to
decide holds. ``Decider`` says how one condition is decided: from variables
alone in a survey copy made before any answer, or from the answers so far in a
staged run.
"""

from __future__ import annotations

from typing import Any

from ..runtime.logic_compare import (
    DECIDED_LOGIC_TYPES,
    _evaluate_tree,
    compare_logic_value,
    fill_embedded_fields,
    is_boolean_condition,
    is_ignored_condition,
)
from ..spec.text import join_text

# Each comparison as the Qualtrics operator it is decided by.
OPERATOR_NAMES = {
    "eq": "EqualTo",
    "ne": "NotEqualTo",
    "gt": "GreaterThan",
    "gte": "GreaterThanOrEqual",
    "lt": "LessThan",
    "lte": "LessThanOrEqual",
    "contains": "Contains",
    "not_contains": "DoesNotContain",
    "matches": "MatchesRegex",
    "empty": "Empty",
    "not_empty": "NotEmpty",
}
ANSWER_OPS = frozenset({"selected", "not_selected", "displayed", "not_displayed"})


class Decider:
    """How one condition is decided. ``atom`` decides a condition of the standard
    form (a comparison, an option selected, a question shown); ``source`` decides
    a condition kept as the source platform wrote it."""

    def atom(self, condition: dict[str, Any]) -> bool:
        raise NotImplementedError

    def source(self, condition: dict[str, Any]) -> bool:
        raise NotImplementedError


class VariablesOnly(Decider):
    """A survey copy made before any answer: a comparison of a variable is decided
    from the variables set so far (a variable piped into the value it is compared
    with is filled first); a condition on an answer does not hold."""

    def __init__(self, variables: dict[str, Any]):
        self.variables = variables

    def atom(self, condition: dict[str, Any]) -> bool:
        left = condition.get("left") or {}
        if condition.get("op") in OPERATOR_NAMES and left.get("scope") == "variable":
            return compare_logic_value(
                self.variables.get(str(left.get("variable", "") or "").strip()),
                fill_embedded_fields(right_value(condition), self.variables),
                OPERATOR_NAMES[condition["op"]],
            )
        return False

    def source(self, condition: dict[str, Any]) -> bool:
        if str(condition.get("LogicType", "") or "").strip() != "EmbeddedField":
            return False
        return compare_logic_value(
            self.variables.get(str(condition.get("LeftOperand", "") or "").strip()),
            fill_embedded_fields(condition.get("RightOperand"), self.variables),
            str(condition.get("Operator", "")),
        )


def right_value(condition: dict[str, Any]) -> Any:
    """The value a comparison compares with: its text (pipes as tokens), a number
    as written, or None when it has none."""
    right = condition.get("right")
    if isinstance(right, dict):
        return join_text(right)
    return right


def holds(condition: Any, decider: Decider) -> bool:
    """Whether ``condition`` holds (no condition, or nothing left to decide, holds)."""
    result = decide(condition, decider)
    return True if result is None else result


def decide(condition: Any, decider: Decider) -> bool | None:
    """``holds`` with None for "nothing left to decide"."""
    if not isinstance(condition, dict) or not condition:
        return None
    op = condition.get("op")
    if op in {"and", "or"}:
        # every part is decided (as before), then folded left to right
        values = [decide(arg, decider) for arg in condition.get("args") or []]
        kept = [value for value in values if value is not None]
        if not kept:
            return None
        result = kept[0]
        for value in kept[1:]:
            result = (result or value) if op == "or" else (result and value)
        return result
    if op == "not":
        value = decide(condition.get("arg"), decider)
        return None if value is None else not value
    if op in {"true", "false"}:
        return op == "true"
    if op == "source":
        return _evaluate_tree(condition.get("qualtrics"), decider.source)
    return decider.atom(condition)


_NEGATED = {
    "selected": "not_selected",
    "not_selected": "selected",
    "displayed": "not_displayed",
    "not_displayed": "displayed",
    "eq": "ne",
    "ne": "eq",
    "lt": "gte",
    "gte": "lt",
    "gt": "lte",
    "lte": "gt",
    "contains": "not_contains",
    "not_contains": "contains",
    "empty": "not_empty",
    "not_empty": "empty",
    "true": "false",
    "false": "true",
}


def negate(condition: dict[str, Any]) -> dict[str, Any]:
    """The opposite condition, with the negation moved into each part (De Morgan),
    as an ``else`` branch's condition has always been written: "not selected" for
    "selected", "at most" for "more than", and so on."""
    op = condition.get("op")
    if op in {"and", "or"}:
        return {"op": "or" if op == "and" else "and", "args": [negate(arg) for arg in condition.get("args") or []]}
    if op == "not":
        return _push_not(condition.get("arg") or {})
    if op in _NEGATED:
        return {**condition, "op": _NEGATED[op]}
    raise ValueError(f"A condition with op {op!r} cannot be negated")


def _push_not(condition: dict[str, Any]) -> dict[str, Any]:
    op = condition.get("op")
    if op in {"and", "or"}:
        return {**condition, "args": [_push_not(arg) for arg in condition.get("args") or []]}
    if op == "not":
        return negate(condition.get("arg") or {})
    return condition


def _atoms(condition: Any) -> list[dict[str, Any]]:
    if not isinstance(condition, dict):
        return []
    if condition.get("op") in {"and", "or"}:
        return [atom for arg in condition.get("args") or [] for atom in _atoms(arg)]
    if condition.get("op") == "not":
        return _atoms(condition.get("arg"))
    return [condition]


def _source_conditions(condition: dict[str, Any]) -> list[dict[str, Any]]:
    """The conditions inside a ``source`` condition, as the platform wrote them."""
    found: list[dict[str, Any]] = []

    def collect(node: Any) -> None:
        if isinstance(node, dict):
            if "LogicType" in node:
                found.append(node)
                return
            for value in node.values():
                collect(value)
        elif isinstance(node, list):
            for value in node:
                collect(value)

    collect(condition.get("qualtrics"))
    return found


def only_ignored(condition: Any) -> bool:
    """A condition decided by nothing a twin has: every part (true/false aside) is a
    test of a device, location, quota and so on."""
    parts: list[dict[str, Any]] = []
    for atom in _atoms(condition):
        if atom.get("op") in {"true", "false"}:
            continue
        if atom.get("op") == "source":
            parts += [c for c in _source_conditions(atom) if not is_boolean_condition(c)]
        else:
            parts.append(atom)
    return bool(parts) and all(isinstance(p, dict) and "LogicType" in p and is_ignored_condition(p) for p in parts)


def reads_answers(condition: Any) -> bool:
    """Whether a condition reads an answer (an option selected, a question shown,
    an answer's value)."""
    for atom in _atoms(condition):
        op = atom.get("op")
        if op in ANSWER_OPS:
            return True
        if op in OPERATOR_NAMES and (atom.get("left") or {}).get("scope") == "answer":
            return True
        if op == "source" and _source_reads_answers(atom.get("qualtrics")):
            return True
    return False


def _source_reads_answers(logic: Any) -> bool:
    if isinstance(logic, dict):
        if str(logic.get("LogicType", "")).strip() == "Question":
            return True
        return any(_source_reads_answers(value) for value in logic.values())
    if isinstance(logic, list):
        return any(_source_reads_answers(item) for item in logic)
    return False


__all__ = ["DECIDED_LOGIC_TYPES", "Decider", "VariablesOnly", "decide", "holds", "negate", "only_ignored", "reads_answers", "right_value"]
