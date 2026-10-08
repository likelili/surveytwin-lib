"""Display logic on single choices: an option, a matrix statement or a scale point
that Qualtrics shows only when its condition holds.

The reader keeps each item's logic by its QSF ID: ``ChoiceDisplayLogic`` for the
QSF's ``Choices`` (options, matrix and slider statements, constant-sum rows) and
``AnswerDisplayLogic`` for its ``Answers`` (matrix scale points). Both runtimes
remove the items whose logic does not hold before the twin sees the question. The
items left keep their own IDs, so the values file codes an answer as the option
written. A question left with no option or no statement is not shown, as in
Qualtrics.
"""

from __future__ import annotations

import copy
from typing import Any, Callable

# The lists that hold the QSF's "Choices" and "Answers" groups, by question type:
# (items, their IDs, lists kept in step with them).
_CHOICE_LISTS: dict[str, tuple[str, str, tuple[str, ...]]] = {
    "MC": ("Options", "OptionsID", ("OptionMedia",)),
    "RANK": ("Options", "OptionsID", ("OptionMedia",)),
    "MATRIX": ("Rows", "RowsID", ("RowsMedia",)),
    "CS": ("Rows", "RowsID", ("RowsMedia",)),
    "SLIDER": ("Statements", "StatementsID", ("StatementsMedia",)),
}
_ANSWER_LISTS: dict[str, tuple[str, str, tuple[str, ...]]] = {
    "MATRIX": ("Columns", "ColumnsID", ("ColumnsMedia",)),
}
# Answers keyed by an option's ID, filtered with it.
_BY_OPTION_ID = ("TextEntryOptions", "DefaultChoices", "DefaultRanks")

LOGIC_KEYS = ("ChoiceDisplayLogic", "AnswerDisplayLogic")


def item_logic(payload: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """A QSF question payload's per-item display logic, as the reader keeps it."""
    kept: dict[str, dict[str, Any]] = {}
    for group, key in (("Choices", "ChoiceDisplayLogic"), ("Answers", "AnswerDisplayLogic")):
        items = payload.get(group)
        if not isinstance(items, dict):
            continue
        logic = {
            str(item_id): copy.deepcopy(item["DisplayLogic"])
            for item_id, item in items.items()
            if isinstance(item, dict) and isinstance(item.get("DisplayLogic"), dict) and item["DisplayLogic"]
        }
        if logic:
            kept[key] = logic
    return kept


def logic_of(question: Any) -> list[dict[str, Any]]:
    """Every item's display logic in a parsed question."""
    if not isinstance(question, dict):
        return []
    found: list[dict[str, Any]] = []
    for key in LOGIC_KEYS:
        by_id = question.get(key)
        if isinstance(by_id, dict):
            found += [logic for logic in by_id.values() if isinstance(logic, dict) and logic]
    return found


def _selector(question: dict[str, Any]) -> str:
    settings = question.get("Settings") if isinstance(question.get("Settings"), dict) else {}
    return str(question.get("Selector") or settings.get("Selector") or "").strip().upper()


def _choice_lists(question: dict[str, Any]) -> tuple[str, str, tuple[str, ...]] | None:
    qtype = str(question.get("QuestionType", "") or "").strip().upper()
    if qtype == "TE":
        # the fields of a text-entry form are its QSF Choices
        return ("Rows", "RowsID", ("RowsMedia",)) if _selector(question) == "FORM" else None
    return _CHOICE_LISTS.get(qtype)


def applies(question: Any) -> bool:
    """Whether the runtimes apply a question's per-item display logic (not for a
    side-by-side question, whose rows belong to several sub-questions)."""
    if not isinstance(question, dict):
        return False
    qtype = str(question.get("QuestionType", "") or "").strip().upper()
    return _choice_lists(question) is not None or qtype in _ANSWER_LISTS


def _filter_group(question: dict[str, Any], lists: tuple[str, str, tuple[str, ...]], hidden: set[str]) -> None:
    items_key, ids_key, parallel = lists
    items = question.get(items_key)
    if not isinstance(items, list):
        return
    ids = question.get(ids_key)
    # IDs left out mean the default order 1..n: written out before any item goes,
    # so the items kept keep their codes.
    ids = [str(i) for i in ids] if isinstance(ids, list) and len(ids) == len(items) else [str(i) for i in range(1, len(items) + 1)]
    keep = [position for position, item_id in enumerate(ids) if item_id not in hidden]
    question[items_key] = [items[position] for position in keep]
    question[ids_key] = [ids[position] for position in keep]
    for key in parallel:
        values = question.get(key)
        if isinstance(values, list) and len(values) == len(items):
            question[key] = [values[position] for position in keep]
    defaults = question.get("DefaultChoices")
    if isinstance(defaults, list) and len(defaults) == len(items) and not any(isinstance(entry, dict) for entry in defaults):
        # defaults listed by place, one per row
        question["DefaultChoices"] = [defaults[position] for position in keep]


def hidden_items(question: dict[str, Any], holds: Callable[[dict[str, Any]], bool]) -> dict[str, list[str]]:
    """The IDs of the items whose display logic does not hold (``holds`` decides
    a logic tree, as for a question's own display logic), by logic key."""
    qtype = str(question.get("QuestionType", "") or "").strip().upper()
    hidden: dict[str, list[str]] = {}
    for key, table in (("ChoiceDisplayLogic", _CHOICE_LISTS), ("AnswerDisplayLogic", _ANSWER_LISTS)):
        by_id = question.get(key)
        lists = _choice_lists(question) if key == "ChoiceDisplayLogic" else table.get(qtype)
        if not isinstance(by_id, dict) or not lists:
            continue
        ids = sorted(
            (str(item_id) for item_id, logic in by_id.items() if isinstance(logic, dict) and logic and not holds(logic)),
            key=lambda item_id: (len(item_id), item_id),
        )
        if ids:
            hidden[key] = ids
    return hidden


def remove_items(question: dict[str, Any], hidden: dict[str, list[str]]) -> dict[str, Any] | None:
    """``question`` without the items ``hidden`` names (and without its per-item
    logic); None when no option or statement (or no scale point) is left to
    show. The IDs removed are kept in ``HiddenChoiceIDs``."""
    qtype = str(question.get("QuestionType", "") or "").strip().upper()
    shown = copy.deepcopy(question)
    for key in LOGIC_KEYS:
        shown.pop(key, None)
    removed: list[str] = []
    for key, table in (("ChoiceDisplayLogic", _CHOICE_LISTS), ("AnswerDisplayLogic", _ANSWER_LISTS)):
        ids = set(hidden.get(key) or [])
        lists = _choice_lists(shown) if key == "ChoiceDisplayLogic" else table.get(qtype)
        if not ids or not lists:
            continue
        _filter_group(shown, lists, ids)
        if not shown.get(lists[0]):
            return None
        removed += sorted(ids, key=lambda item_id: (len(item_id), item_id))
        # default answers on a hidden item go with it
        entries = shown.get("DefaultEntries")
        if isinstance(entries, list):
            which = "ItemID" if key == "ChoiceDisplayLogic" else "SubID"
            kept = [e for e in entries if not (isinstance(e, dict) and str(e.get(which, "")) in ids)]
            if kept:
                shown["DefaultEntries"] = kept
            else:
                shown.pop("DefaultEntries", None)
        if key == "ChoiceDisplayLogic":
            for by_option in _BY_OPTION_ID:
                entries = shown.get(by_option)
                if isinstance(entries, list):
                    kept = [e for e in entries if not (isinstance(e, dict) and str(e.get("OptionID", "")) in ids)]
                    if kept:
                        shown[by_option] = kept
                    else:
                        shown.pop(by_option, None)
    if removed:
        shown["HiddenChoiceIDs"] = removed
    return shown


def hide_choices(question: dict[str, Any], holds: Callable[[dict[str, Any]], bool]) -> dict[str, Any] | None:
    """``question`` without the items whose display logic does not hold; None
    when no option or statement (or no scale point) is left to show."""
    if not any(isinstance(question.get(key), dict) and question.get(key) for key in LOGIC_KEYS):
        return question
    return remove_items(question, hidden_items(question, holds))
