"""Conditions: Qualtrics logic to the standard condition tree, and back.

Qualtrics logic is a list of condition groups, each a list of conditions joined
by "And"/"Or". The engine reads both lists left to right with no precedence
(``a Or b And c`` is ``(a or b) and c``); the tree read here has exactly that
meaning. A condition with no standard form (a device, quota or location test)
becomes a ``source`` condition holding it as written; the engine treats those as
before (an ignored source leaves the decision to the rest).

Compiling a tree gives logic the engine decides the same way. ``to_qualtrics``
takes the logic it was read from, when there is one: if the tree is unchanged,
that logic is returned as written (its descriptions and source-only keys
included); otherwise the logic is built from the tree.
"""

from __future__ import annotations

import copy
import re
from typing import Any

from ..runtime.logic_compare import is_boolean_condition

_OPERATORS = {
    "EqualTo": "eq",
    "NotEqualTo": "ne",
    "GreaterThan": "gt",
    "GreaterThanOrEqual": "gte",
    "LessThan": "lt",
    "LessThanOrEqual": "lte",
    "Contains": "contains",
    "DoesNotContain": "not_contains",
    "MatchesRegex": "matches",
    "Empty": "empty",
    "NotEmpty": "not_empty",
}
_QUALTRICS_OPERATORS = {op: name for name, op in _OPERATORS.items()}
# Answer locators that compare as values, and the answer field each reads.
_VALUE_LOCATORS = {
    "ChoiceTextEntryValue": "text_entry",
    "ChoiceNumericEntryValue": "number_entry",
    "SelectedChoicesCount": "selected_count",
}
_LOCATOR_OF_FIELD = {field: locator for locator, field in _VALUE_LOCATORS.items()}
_LOOP_FUNCTIONS = {"evaluateAllInArrayFunction": "all", "evaluateAnyInArrayFunction": "any"}
_LOCATOR_RE = re.compile(r"^q://([^/]+)/([A-Za-z]+)(?:/(.*))?$")
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


# ------------------------------------------------------------------ reading


def from_qualtrics(logic: Any) -> dict[str, Any] | None:
    """The condition tree for Qualtrics logic; None for empty logic."""
    if not isinstance(logic, dict) or not logic:
        return None
    return _tree(logic)


def _numbered(node: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        node[key]
        for key in sorted((key for key in node if str(key).isdigit()), key=lambda key: int(str(key)))
        if isinstance(node[key], dict)
    ]


def _join(node: dict[str, Any], default: str) -> str:
    conjunction = str(node.get("Conjuction") or node.get("Conjunction") or "").strip()
    return "or" if conjunction == "Or" else ("and" if conjunction == "And" else default)


def _tree(node: dict[str, Any]) -> dict[str, Any] | None:
    if is_boolean_condition(node):
        value = node.get("Value")
        truth = value if isinstance(value, bool) else str(value or "").strip().lower() == "true"
        out: dict[str, Any] = {"op": "true" if truth else "false"}
        if node.get("Description"):
            out["description"] = str(node["Description"])
        return out
    if "LogicType" in node and "Operator" in node:
        return _atom(node)
    if "LogicType" in node:
        return {"op": "source", "qualtrics": copy.deepcopy(node)}
    items = _numbered(node)
    if not items:
        return None
    if all("LogicType" in item for item in items):
        parent = "or" if str(node.get("Type", "")).strip() == "Or" else "and"
        return _fold([(_tree(item), _join(item, parent)) for item in items])
    folded: list[tuple[dict[str, Any] | None, str]] = []
    for index, group in enumerate(items):
        group_type = str(group.get("Type", "") or "").strip()
        folded.append((_tree(group), "and" if index == 0 else ("or" if group_type in {"OrIf", "ElseIf"} else "and")))
    return _fold(folded)


def _fold(items: list[tuple[dict[str, Any] | None, str]]) -> dict[str, Any] | None:
    kept = [(tree, join) for tree, join in items if tree is not None]
    if not kept:
        return None
    result = kept[0][0]
    for tree, join in kept[1:]:
        if result.get("op") == join and isinstance(result.get("args"), list) and result.get("_folded"):
            result["args"].append(tree)
        else:
            result = {"op": join, "args": [result, tree], "_folded": True}
    return _unmark(result)


def _unmark(tree: dict[str, Any]) -> dict[str, Any]:
    tree.pop("_folded", None)
    for child in tree.get("args", []) if isinstance(tree.get("args"), list) else []:
        if isinstance(child, dict):
            _unmark(child)
    return tree


def _atom(condition: dict[str, Any]) -> dict[str, Any]:
    logic_type = str(condition.get("LogicType", "") or "").strip()
    operator = str(condition.get("Operator", "") or "").strip()
    left = str(condition.get("LeftOperand", "") or "")
    description = str(condition.get("Description", "") or "")

    def described(out: dict[str, Any]) -> dict[str, Any]:
        if description:
            out["description"] = description
        return out

    if logic_type == "Question":
        match = _LOCATOR_RE.match(left)
        if match:
            qid, kind, rest = match.group(1), match.group(2), match.group(3) or ""
            loop = _LOOP_FUNCTIONS.get(str(condition.get("LoopTarget") or condition.get("FunctionName") or ""))
            if operator in {"Selected", "NotSelected"} and kind == "SelectableChoice" and rest and "/" not in rest:
                out: dict[str, Any] = {"op": "selected" if operator == "Selected" else "not_selected", "question": qid, "option": rest}
                if loop:
                    out["loop"] = loop
                return described(out)
            if operator in {"Displayed", "NotDisplayed"} and kind == "QuestionDisplayed" and not rest and not loop:
                return described({"op": "displayed" if operator == "Displayed" else "not_displayed", "question": qid})
            if operator in _OPERATORS and kind in _VALUE_LOCATORS and not loop and "/" not in rest:
                ref: dict[str, Any] = {"scope": "answer", "question": qid, "field": _VALUE_LOCATORS[kind]}
                if rest:
                    ref["item"] = rest
                return described(_comparison(_OPERATORS[operator], ref, condition))
    if logic_type == "EmbeddedField" and operator in _OPERATORS and left:
        return described(_comparison(_OPERATORS[operator], {"scope": "variable", "variable": left}, condition))
    return described({"op": "source", "qualtrics": copy.deepcopy(condition)})


def _comparison(op: str, left: dict[str, Any], condition: dict[str, Any]) -> dict[str, Any]:
    from .text import split_text

    out: dict[str, Any] = {"op": op, "left": left}
    if "RightOperand" in condition:
        right = condition.get("RightOperand")
        out["right"] = split_text(right) if isinstance(right, str) else right
    ignore_case = condition.get("IgnoreCase")
    if ignore_case not in (None, "", 0, "0", False):
        out["ignore_case"] = True
    return out


# ------------------------------------------------------------------ compiling


def to_qualtrics(
    tree: dict[str, Any] | None,
    *,
    original: Any = None,
    wrapper: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Qualtrics logic for a condition tree.

    ``original``: the logic the tree was read from; returned as written when the
    tree is unchanged. ``wrapper``: keys of the outer object (``inPage``) for logic
    built from the tree."""
    if tree is None:
        return None
    if isinstance(original, dict) and original and from_qualtrics(original) == tree:
        return copy.deepcopy(original)
    tree = _push_not(tree)
    out = _groups(tree)
    out["Type"] = "BooleanExpression"
    for key, value in (wrapper or {}).items():
        out[key] = value
    return out


def _push_not(tree: dict[str, Any]) -> dict[str, Any]:
    """Qualtrics logic has no "not": negations move into the atoms (De Morgan)."""
    op = tree.get("op")
    if op in {"and", "or"}:
        return {**tree, "args": [_push_not(arg) for arg in tree["args"]]}
    if op != "not":
        return tree
    inner = tree["arg"]
    inner_op = inner.get("op")
    if inner_op == "not":
        return _push_not(inner["arg"])
    if inner_op in {"and", "or"}:
        return {"op": "or" if inner_op == "and" else "and", "args": [_push_not({"op": "not", "arg": arg}) for arg in inner["args"]]}
    if inner_op in _NEGATED:
        return {**inner, "op": _NEGATED[inner_op]}
    raise ValueError(f"A condition with op {inner_op!r} cannot be negated in this format version")


def _is_atom(tree: dict[str, Any]) -> bool:
    return tree.get("op") not in {"and", "or"}


def _chain(tree: dict[str, Any]) -> list[tuple[dict[str, Any], str]] | None:
    """The tree as one left-to-right list of atoms and joins, when it is one."""
    if _is_atom(tree):
        return [(tree, "")]
    args = tree["args"]
    head = _chain(args[0])
    if head is None or not all(_is_atom(arg) for arg in args[1:]):
        return None
    return head + [(arg, tree["op"]) for arg in args[1:]]


def _groups(tree: dict[str, Any]) -> dict[str, Any]:
    chain = _chain(tree)
    if chain is not None:
        return {"0": _condition_group(chain)}
    # Not one chain: one group per argument, joined as the tree says.
    out: dict[str, Any] = {}
    for index, arg in enumerate(tree["args"]):
        group = _condition_group(_chain(arg)) if _chain(arg) is not None else _nested(arg)
        group["Type"] = "If" if index == 0 or tree["op"] == "and" else "OrIf"
        out[str(index)] = group
    return out


def _nested(tree: dict[str, Any]) -> dict[str, Any]:
    nested = _groups(tree)
    return nested


def _condition_group(chain: list[tuple[dict[str, Any], str]]) -> dict[str, Any]:
    group: dict[str, Any] = {}
    for index, (atom, join) in enumerate(chain):
        expression = _expression(atom)
        if index:
            expression["Conjuction"] = "Or" if join == "or" else "And"
        group[str(index)] = expression
    group["Type"] = "If"
    return group


def _expression(atom: dict[str, Any]) -> dict[str, Any]:
    from .text import join_text

    op = atom["op"]
    description = atom.get("description")
    if op == "source":
        out = copy.deepcopy(atom["qualtrics"])
        if description and "Description" not in out:
            out["Description"] = description
        return out
    if op in {"true", "false"}:
        out = {"LogicType": "BooleanValue", "Value": op == "true", "Type": "Expression"}
    elif op in {"selected", "not_selected"}:
        qid, option = atom["question"], atom["option"]
        locator = f"q://{qid}/SelectableChoice/{option}"
        out = {
            "LogicType": "Question",
            "QuestionID": qid,
            "QuestionIsInLoop": "no",
            "ChoiceLocator": locator,
            "Operator": "Selected" if op == "selected" else "NotSelected",
            "QuestionIDFromLocator": qid,
            "LeftOperand": locator,
            "Type": "Expression",
        }
        if atom.get("loop"):
            function = next(name for name, value in _LOOP_FUNCTIONS.items() if value == atom["loop"])
            out["QuestionIsInLoop"] = "yes"
            out["LoopTarget"] = function
            out["FunctionName"] = function
    elif op in {"displayed", "not_displayed"}:
        qid = atom["question"]
        out = {
            "LogicType": "Question",
            "QuestionID": qid,
            "QuestionIsInLoop": "no",
            "ChoiceLocator": f"q://{qid}/DisplayableQuestion/1",
            "Operator": "Displayed" if op == "displayed" else "NotDisplayed",
            "QuestionIDFromLocator": qid,
            "LeftOperand": f"q://{qid}/QuestionDisplayed",
            "Type": "Expression",
        }
    elif op in _QUALTRICS_OPERATORS:
        left = atom["left"]
        if left.get("scope") == "variable":
            out = {"LogicType": "EmbeddedField", "LeftOperand": left["variable"], "Operator": _QUALTRICS_OPERATORS[op], "Type": "Expression"}
        elif left.get("scope") == "answer" and left.get("field") in _LOCATOR_OF_FIELD:
            qid = left["question"]
            locator = f"q://{qid}/{_LOCATOR_OF_FIELD[left['field']]}" + (f"/{left['item']}" if left.get("item") else "")
            out = {
                "LogicType": "Question",
                "QuestionID": qid,
                "QuestionIsInLoop": "no",
                "ChoiceLocator": locator,
                "Operator": _QUALTRICS_OPERATORS[op],
                "QuestionIDFromLocator": qid,
                "LeftOperand": locator,
                "Type": "Expression",
            }
        else:
            raise ValueError(f"A comparison of {left.get('scope')!r} values cannot be compiled in this format version")
        if "right" in atom:
            right = atom["right"]
            out["RightOperand"] = join_text(right) if isinstance(right, (str, dict)) else right
        if atom.get("ignore_case"):
            out["IgnoreCase"] = 1
    else:
        raise ValueError(f"Unknown condition op: {op!r}")
    if description:
        out["Description"] = description
    return out


# ------------------------------------------------------------------ reading a tree


def condition_questions(tree: Any) -> set[str]:
    """The question IDs a condition reads."""
    found: set[str] = set()

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            if node.get("op") in {"selected", "not_selected", "displayed", "not_displayed"} and node.get("question"):
                found.add(str(node["question"]))
            left = node.get("left")
            if isinstance(left, dict) and left.get("scope") == "answer":
                found.add(str(left.get("question")))
            if node.get("op") == "source":
                text = str(node.get("qualtrics"))
                found.update(re.findall(r"q://(QID[^/'\"]+)/", text))
            for value in node.values():
                if isinstance(value, (dict, list)):
                    walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(tree)
    return found


def condition_variables(tree: Any) -> set[str]:
    """The variables a condition reads."""
    found: set[str] = set()

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            if node.get("scope") == "variable" and node.get("variable"):
                found.add(str(node["variable"]))
            for value in node.values():
                if isinstance(value, (dict, list)):
                    walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(tree)
    return found
