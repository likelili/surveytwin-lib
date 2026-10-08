"""Qualtrics branch and display logic, decided one way in static and staged runs.

``evaluate_logic_tree`` folds a BranchLogic or DisplayLogic tree: the conditions of
one set are joined left to right, each by its own conjunction, and the sets by
their type (``OrIf`` and ``ElseIf`` join with "or"). ``compare_logic_value``
applies one condition's operator, with Qualtrics' operator names (``EqualTo``
compares numbers as numbers, ``GreaterThanOrEqual``, ``Empty``,
``MatchesRegex``, ...). The staged runtime decides each condition from the
respondent's answers and embedded data; a static survey has only embedded data.
"""

from __future__ import annotations

import re
import urllib.parse
from typing import Any, Callable

_EMBEDDED_FIELD_TOKEN = re.compile(r"\$\{e://Field/([^}]+)\}")


def clean_token(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def compare_logic_value(actual: Any, expected: Any, operator: str) -> bool:
    op = str(operator or "").strip()
    if isinstance(actual, bool):
        if op in {"Selected", "Displayed"}:
            return actual
        if op in {"NotSelected", "NotDisplayed"}:
            return not actual

    actual_str = clean_token(actual)
    expected_str = clean_token(expected)

    def as_number(value: Any) -> float | None:
        try:
            return float(str(value).strip())
        except (TypeError, ValueError):
            return None

    actual_num = as_number(actual)
    expected_num = as_number(expected)

    if op == "EqualTo":
        if actual_num is not None and expected_num is not None:
            return actual_num == expected_num
        return actual_str == expected_str
    if op == "NotEqualTo":
        if actual_num is not None and expected_num is not None:
            return actual_num != expected_num
        return actual_str != expected_str
    if op in {"GreaterThan", "GreaterThanOrEqual", "GreaterThanOrEqualTo"}:
        if actual_num is not None and expected_num is not None:
            return actual_num >= expected_num if "Equal" in op else actual_num > expected_num
        return actual_str >= expected_str if "Equal" in op else actual_str > expected_str
    if op in {"LessThan", "LessThanOrEqual", "LessThanOrEqualTo"}:
        if actual_num is not None and expected_num is not None:
            return actual_num <= expected_num if "Equal" in op else actual_num < expected_num
        return actual_str <= expected_str if "Equal" in op else actual_str < expected_str
    if op == "Contains":
        return expected_str.lower() in actual_str.lower()
    if op == "DoesNotContain":
        return expected_str.lower() not in actual_str.lower()
    if op in {"Empty", "IsEmpty"}:
        return not actual_str
    if op in {"NotEmpty", "IsNotEmpty"}:
        return bool(actual_str)
    if op in {"MatchesRegex", "DoesNotMatchRegex"}:
        try:
            matches = re.search(expected_str, actual_str) is not None
        except re.error:
            matches = False
        return not matches if op == "DoesNotMatchRegex" else matches
    if op == "Selected":
        return bool(actual)
    if op == "NotSelected":
        return not bool(actual)
    return True


def is_boolean_condition(condition: Any) -> bool:
    """A literal true/false condition (``LogicType: BooleanValue``). Qualtrics
    writes "If False" this way to switch a question or branch off for every
    respondent; it carries a ``Value`` and no ``Operator``."""
    return isinstance(condition, dict) and str(condition.get("LogicType", "") or "").strip() == "BooleanValue"


def boolean_condition_value(condition: dict[str, Any]) -> bool:
    value = condition.get("Value")
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() == "true"


def logic_always_false(logic: Any) -> bool:
    """True when ``logic`` is decided by literal true/false conditions alone and
    comes out false: whatever is gated by it is shown to nobody."""
    conditions: list[dict[str, Any]] = []

    def collect(node: Any) -> None:
        if isinstance(node, dict):
            if "LogicType" in node:
                conditions.append(node)
                return
            for value in node.values():
                collect(value)
        elif isinstance(node, list):
            for value in node:
                collect(value)

    collect(logic)
    # conditions a twin cannot meet are left out: "Device is mobile Or False"
    # is decided by its "False"
    literal = [condition for condition in conditions if not is_ignored_condition(condition)]
    if not literal or not all(is_boolean_condition(condition) for condition in literal):
        return False
    return not evaluate_logic_tree(logic, lambda _condition: True)


# Conditions a twin can be judged on: its answers, its embedded data, and literal
# true/false. Qualtrics' other built-in conditions (device type, location, quota,
# panel, ...) describe a real respondent's browser or account; a twin has none, so
# they are ignored, as if the logic did not have them.
DECIDED_LOGIC_TYPES = frozenset({"Question", "EmbeddedField", "BooleanValue"})


def is_ignored_condition(condition: Any) -> bool:
    return (
        isinstance(condition, dict)
        and "LogicType" in condition
        and str(condition.get("LogicType", "") or "").strip() not in DECIDED_LOGIC_TYPES
    )


def _conditions(logic: Any) -> list[dict[str, Any]]:
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

    collect(logic)
    return found


def logic_only_ignored(logic: Any) -> bool:
    """Logic whose conditions (literal true/false aside) are all ignored ones."""
    conditions = [c for c in _conditions(logic) if not is_boolean_condition(c)]
    return bool(conditions) and all(is_ignored_condition(c) for c in conditions)


def ends_survey_on_ignored_conditions(element: Any) -> bool:
    """A branch gated only on ignored conditions that ends the survey anywhere
    inside it ("If Device Type is Mobile: show a sorry message, end the survey"):
    it is skipped whole, message and embedded data included, as for a respondent
    it does not screen out, since ignoring its condition must not end everyone's
    survey."""
    if not isinstance(element, dict) or str(element.get("ElementType", "") or "").strip() != "Branch":
        return False
    if not logic_only_ignored(element.get("BranchLogic")):
        return False

    def ends(nodes: Any) -> bool:
        return any(
            isinstance(node, dict)
            and (str(node.get("ElementType", "") or "").strip() == "EndSurvey" or ends(node.get("Elements")))
            for node in (nodes if isinstance(nodes, list) else [])
        )

    return ends(element.get("Elements"))


def evaluate_logic_tree(logic: Any, decide: Callable[[dict[str, Any]], bool]) -> bool:
    """Whether ``logic`` holds, with ``decide(condition)`` deciding each condition
    (a dict with ``LogicType`` and ``Operator``). Empty logic holds. A literal
    true/false condition is decided by its own value, never by ``decide``. An
    ignored condition (``is_ignored_condition``) is left out: the rest decides,
    and logic made only of ignored conditions holds."""
    result = _evaluate_tree(logic, decide)
    return True if result is None else result


def _evaluate_tree(logic: Any, decide: Callable[[dict[str, Any]], bool]) -> bool | None:
    """``evaluate_logic_tree`` with None for "no condition left to decide"."""
    if not isinstance(logic, dict) or not logic:
        return None

    if is_boolean_condition(logic):
        # Before this, "If False" has no Operator, fell through to the
        # numbered-group walk, found nothing and held: a question the author
        # switched off was asked of every twin.
        return boolean_condition_value(logic)

    if "LogicType" in logic and "Operator" in logic:
        if is_ignored_condition(logic):
            return None
        return decide(logic)

    numbered = [
        logic[key]
        for key in sorted((key for key in logic if str(key).isdigit()), key=lambda key: int(str(key)))
        if isinstance(logic[key], dict)
    ]
    if not numbered:
        return None

    def fold(items: list[tuple[bool | None, str]]) -> bool | None:
        kept = [(value, join) for value, join in items if value is not None]
        if not kept:
            return None
        result = kept[0][0]
        for value, join in kept[1:]:
            result = (result or value) if join == "Or" else (result and value)
        return result

    def condition_join(node: dict[str, Any], default: str = "And") -> str:
        conjunction = str(node.get("Conjuction") or node.get("Conjunction") or "").strip()
        return "Or" if conjunction == "Or" else default

    # Some QSF payloads pass a single condition group directly rather than
    # wrapping it in a BooleanExpression. Preserve the conditions' own joins.
    if all("LogicType" in node for node in numbered):
        parent_default = "Or" if str(logic.get("Type", "")).strip() == "Or" else "And"
        return fold([(_evaluate_tree(node, decide), condition_join(node, parent_default)) for node in numbered])

    group_results: list[tuple[bool | None, str]] = []
    for index, group in enumerate(numbered):
        value = _evaluate_tree(group, decide)
        group_type = str(group.get("Type", "") or "").strip()
        join = "Or" if group_type in {"OrIf", "ElseIf"} else "And"
        group_results.append((value, "And" if index == 0 else join))
    return fold(group_results)


def embedded_field_value(name: str, embedded_data: dict[str, Any]) -> Any:
    """The value of the field a ${e://Field/<name>} token names, or None when it is
    not set. Qualtrics writes a space in a field name as %20."""
    if name in embedded_data:
        return embedded_data[name]
    decoded = urllib.parse.unquote(name)
    if decoded in embedded_data:
        return embedded_data[decoded]
    stripped = name.strip()
    return embedded_data.get(stripped)


def fill_embedded_fields(text: Any, embedded_data: dict[str, Any]) -> Any:
    """``text`` with each ${e://Field/...} replaced by the field's value (blank when
    the field is not set); anything that is not text is returned as it is."""
    if not isinstance(text, str) or "${e://" not in text:
        return text
    return _EMBEDDED_FIELD_TOKEN.sub(lambda match: str(embedded_field_value(match.group(1), embedded_data) or ""), text)


def evaluate_embedded_logic(logic: Any, embedded_data: dict[str, Any]) -> bool:
    """Logic decided from embedded data alone (a static survey has no answers):
    an embedded-field condition compares the field's current value with the
    condition's value (a field piped into that value is filled first); a
    condition on an answer, or on anything else a simulated respondent cannot
    have, does not hold."""

    def decide(condition: dict[str, Any]) -> bool:
        if str(condition.get("LogicType", "") or "").strip() != "EmbeddedField":
            return False
        return compare_logic_value(
            embedded_data.get(str(condition.get("LeftOperand", "") or "").strip()),
            fill_embedded_fields(condition.get("RightOperand"), embedded_data),
            str(condition.get("Operator", "")),
        )

    return evaluate_logic_tree(logic, decide)
