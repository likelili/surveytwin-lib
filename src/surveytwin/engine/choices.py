"""The options a twin is shown, and in what order.

``order_items`` applies a question's randomization to one twin's copy: all
options in a random order, a random few of them, a random half reversed, or
positions pinned and filled by draws (Qualtrics' advanced randomization). A
grid's randomization orders its statements; a side-by-side question's orders the
options of every column alike. The random generator is the one the planner
passes, used exactly as before, so a twin keeps its orders.

``hidden_items`` and ``remove_items`` apply display conditions on single items
(an option, a statement, a scale point): the items whose
condition does not hold are left out, and a question left with none is not
shown.
"""

from __future__ import annotations

import hashlib
import random
from typing import Any, Callable

from ..spec.qualtrics import positive_int


def _list_key(response: dict[str, Any]) -> tuple[dict[str, Any], str] | None:
    """The list a question's randomization orders: (the dict holding it, its key)."""
    rtype = response.get("type")
    if rtype == "choice":
        return response, "options"
    if rtype in {"ranking", "allocation"}:
        return response, "items"
    if rtype == "form":
        return response, "fields"
    if rtype == "matrix":
        return response, "rows"
    return None


def consistent_reversal(participant_index: int) -> bool:
    key = f"choice-scale-reversal::{participant_index}".encode("utf-8")
    return int.from_bytes(hashlib.sha256(key).digest()[:8], "big") % 2 == 1


def _basic_order(count: int, randomization: dict[str, Any], rng: random.Random, participant_index: int) -> list[int] | None:
    order = list(range(count))
    kind = randomization.get("order")
    if kind == "shuffle" and not randomization.get("take"):
        rng.shuffle(order)
    elif kind == "shuffle":
        order = rng.sample(order, min(int(randomization["take"]), count))
        rng.shuffle(order)
    elif kind == "random_reverse":
        reverse = consistent_reversal(participant_index) if randomization.get("consistent") else bool(rng.getrandbits(1))
        if not reverse:
            return None
        order.reverse()
    else:
        return None
    return order


def _positions_order(ids: list[str], randomization: dict[str, Any], rng: random.Random) -> list[str] | None:
    """Pinned positions, the others filled from shuffled (or sampled) pools."""
    positions = randomization.get("positions") or []
    if not positions:
        return None
    available = set(ids)
    hidden = {str(item) for item in randomization.get("hidden") or [] if str(item).strip()}

    def pool(name: str) -> list[str]:
        return [str(item) for item in randomization.get(name) or [] if str(item) in available and str(item) not in hidden]

    shuffled = pool("shuffled")
    rng.shuffle(shuffled)
    subset = pool("subset")
    if subset:
        take = min(positive_int(randomization.get("subset_take")) or len(subset), len(subset))
        subset = rng.sample(subset, take)
    shuffled_iter, subset_iter = iter(shuffled), iter(subset)
    out: list[str] = []
    seen: set[str] = set()
    for slot in positions:
        if isinstance(slot, dict):
            primary, fallback = (subset_iter, shuffled_iter) if slot.get("draw") == "subset" else (shuffled_iter, subset_iter)
            candidate = next(primary, None)
            if candidate is None:
                candidate = next(fallback, None)
            if candidate is not None and candidate not in seen:
                seen.add(candidate)
                out.append(candidate)
            continue
        token = str(slot)
        if token in available and token not in hidden and token not in seen:
            seen.add(token)
            out.append(token)
    return out or None


def order_items(question: dict[str, Any], rng: random.Random, participant_index: int) -> dict[str, Any] | None:
    """Order a shown question's items for one twin, in place. Returns what was done
    (``{"type": ..., "ids": [...]}``, or ``{"warning": ...}`` for a randomization
    kept but not run), or None when nothing changed."""
    response = question.get("response")
    if not isinstance(response, dict) or not response.get("randomization"):
        return None
    randomization = response["randomization"]
    if response.get("type") == "group":
        return _order_group(response, randomization, rng, participant_index)
    located = _list_key(response)
    if located is None:
        return None
    holder, key = located
    items = holder.get(key) or []
    if len(items) <= 1:
        return None
    ids = [str(item["id"]) for item in items]
    kind = randomization.get("order")
    if kind in {"positions", "source"}:
        shown_ids = _positions_order(ids, randomization, rng) if kind == "positions" else None
        if shown_ids is None:
            return {"warning": "Advanced choice randomization was preserved but not executed.", "list": key}
        index = {item_id: position for position, item_id in enumerate(ids)}
        order = [index[item_id] for item_id in shown_ids if item_id in index]
        if not order:
            return None
        applied = "Advanced"
    else:
        order = _basic_order(len(items), randomization, rng, participant_index)
        if order is None:
            return None
        applied = {"shuffle": "SubSet" if randomization.get("take") else "All", "random_reverse": "ScaleReversal"}[kind]
    holder[key] = [items[position] for position in order]
    _keep_defaults(response, {str(items[position]["id"]) for position in order})
    return {"type": applied, "ids": [str(items[position]["id"]) for position in order], "list": key}


def _keep_defaults(response: dict[str, Any], visible: set[str]) -> None:
    """Preset answers on options no longer shown go with them."""
    if response.get("type") == "choice" and response.get("default"):
        kept = [item for item in response["default"] if str(item) in visible]
        if kept:
            response["default"] = kept
        else:
            response.pop("default")
    if response.get("type") == "ranking" and response.get("default"):
        kept = [entry for entry in response["default"] if str(entry.get("item")) in visible]
        if kept:
            response["default"] = kept
        else:
            response.pop("default")


def _order_group(response: dict[str, Any], randomization: dict[str, Any], rng: random.Random, participant_index: int) -> dict[str, Any] | None:
    """A side-by-side question: every column's options in the same random order
    (a column with a different number of options gets its own)."""
    if randomization.get("order") in {"positions", "source"}:
        return {"warning": "Advanced SBS choice randomization was preserved but not executed.", "list": "group"}
    columns = []
    for field in response.get("fields") or []:
        row_response = (field.get("response") or {}).get("row_response") or {}
        if row_response.get("type") != "text" and len(row_response.get("options") or []) > 1:
            columns.append((field, row_response))
    if not columns:
        return None
    base = _basic_order(len(columns[0][1]["options"]), randomization, rng, participant_index)
    if not base:
        return None
    base_count = len(columns[0][1]["options"])
    applied = {"shuffle": "SubSet" if randomization.get("take") else "All", "random_reverse": "ScaleReversal"}[randomization["order"]]
    done = []
    for field, row_response in columns:
        options = row_response["options"]
        order = base if len(options) == base_count else _basic_order(len(options), randomization, rng, participant_index)
        if not order:
            continue
        row_response["options"] = [options[position] for position in order]
        visible = {str(o["id"]) for o in row_response["options"]}
        for row in (field.get("response") or {}).get("rows") or []:
            if isinstance(row.get("default"), list):
                row["default"] = [d for d in row["default"] if str(d) in visible]
                if not row["default"]:
                    row.pop("default")
        done.append({"field": str(field.get("id")), "ids": [str(o["id"]) for o in row_response["options"]]})
    return {"type": applied, "fields": done, "list": "group"} if done else None


# ---------------------------------------------------------------- display conditions on items


def _choice_items(response: dict[str, Any]) -> list[dict[str, Any]] | None:
    """The items the QSF calls a question's "Choices": options, statements, fields
    or allocation items (not a side-by-side question's)."""
    rtype = response.get("type")
    if rtype == "choice":
        return response.get("options") or []
    if rtype in {"ranking", "allocation"}:
        return response.get("items") or []
    if rtype == "form":
        return response.get("fields") or []
    if rtype == "matrix":
        return response.get("rows") or []
    return None


def _answer_items(response: dict[str, Any]) -> list[dict[str, Any]] | None:
    """A grid's scale points (the QSF's "Answers"); none for a slider."""
    if response.get("type") != "matrix":
        return None
    if response.get("mode") == "per_cell":
        return response.get("columns") or []
    row_response = response.get("row_response") or {}
    if row_response.get("type") == "number":
        return None
    return row_response.get("options") or []


def has_item_conditions(question: dict[str, Any]) -> bool:
    response = question.get("response")
    if not isinstance(response, dict):
        return False
    return any(item.get("show_if") for items in (_choice_items(response) or [], _answer_items(response) or []) for item in items)


def item_conditions(question: dict[str, Any]) -> list[dict[str, Any]]:
    response = question.get("response") if isinstance(question.get("response"), dict) else {}
    return [item["show_if"] for items in (_choice_items(response) or [], _answer_items(response) or []) for item in items if item.get("show_if")]


def hidden_items(question: dict[str, Any], holds: Callable[[dict[str, Any]], bool]) -> dict[str, list[str]]:
    """The IDs of the items whose display condition does not hold: ``{"choices":
    [...], "answers": [...]}`` (scale points apart from options and statements),
    each sorted as before."""
    response = question.get("response") if isinstance(question.get("response"), dict) else {}
    hidden: dict[str, list[str]] = {}
    for name, items in (("choices", _choice_items(response)), ("answers", _answer_items(response))):
        if not items:
            continue
        ids = sorted(
            (str(item["id"]) for item in items if item.get("show_if") and not holds(item["show_if"])),
            key=lambda item_id: (len(item_id), item_id),
        )
        if ids:
            hidden[name] = ids
    return hidden


def drop_item_conditions(question: dict[str, Any]) -> None:
    response = question.get("response") if isinstance(question.get("response"), dict) else {}
    for items in (_choice_items(response) or [], _answer_items(response) or []):
        for item in items:
            item.pop("show_if", None)


def remove_items(question: dict[str, Any], hidden: dict[str, list[str]]) -> bool:
    """Leave out the items ``hidden`` names, in place. False when no option or
    statement (or no scale point) is left: the question is then not shown."""
    response = question.get("response") if isinstance(question.get("response"), dict) else {}
    for name, finder in (("choices", _choice_items), ("answers", _answer_items)):
        ids = set(hidden.get(name) or [])
        items = finder(response)
        if not ids or items is None:
            continue
        kept = [item for item in items if str(item["id"]) not in ids]
        _put_items(response, name, kept)
        if not kept:
            return False
        if name == "choices":
            _keep_defaults(response, {str(item["id"]) for item in kept})
            if response.get("type") == "allocation" and isinstance(response.get("default"), dict):
                response["default"] = {k: v for k, v in response["default"].items() if str(k) not in ids}
                if not response["default"]:
                    response.pop("default")
            if response.get("type") == "matrix":
                pass  # a statement's own preset goes with the statement
        else:
            for row in response.get("rows") or []:
                if isinstance(row.get("default"), list):
                    row["default"] = [d for d in row["default"] if str(d) not in ids]
                    if not row["default"]:
                        row.pop("default")
    return True


def _put_items(response: dict[str, Any], name: str, items: list[dict[str, Any]]) -> None:
    rtype = response.get("type")
    if name == "choices":
        key = {"choice": "options", "ranking": "items", "allocation": "items", "form": "fields", "matrix": "rows"}[rtype]
        response[key] = items
    elif response.get("mode") == "per_cell":
        response["columns"] = items
    else:
        response["row_response"]["options"] = items
