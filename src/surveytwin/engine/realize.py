"""A question as one twin is shown it, made from its definition.

``shown_question`` copies a question of the standard definition and writes
every text a twin reads as plain text, the way the model reads it: HTML loses
its markup (a table keeps its rows), an unlabeled option with a picture is
called "[Image option 2]", a bipolar statement names both ends, and so on (the
rules of ``engine.text``). Piped values are still tokens (``${e://Field/x}``);
the planner and the staged runtime fill them in for each twin.

``map_text`` applies a function to every text a twin reads, with the place of
that text. The places are named as the survey template named them
(``QuestionText``, ``Options/2``, ``sub_questions/0/Rows/1``...): a random value
(``${rand://int/1:10}``) is drawn from the run's seed, the twin and that place,
so a twin keeps the same draws as before the engine read the standard form.
"""

from __future__ import annotations

import copy
from typing import Any, Callable

from ..spec.text import join_text
from .text import IMAGE_STIMULUS, item_labels, picture_text, question_text

TextFunction = Callable[[str, str], str]


def _labels(items: list[dict[str, Any]], image_label: str, empty: Callable[[int, dict[str, Any]], str] | None = None) -> None:
    for item, label in zip(items, item_labels(items, image_label, empty)):
        item["label"] = label


def _plain(value: Any) -> Any:
    """Every text value inside ``value`` as plain text (references as their tokens)."""
    if isinstance(value, dict) and isinstance(value.get("parts"), list):
        return join_text(value)
    if isinstance(value, dict):
        return {key: _plain(child) for key, child in value.items()}
    if isinstance(value, list):
        return [_plain(child) for child in value]
    return value


def _bipolar(response: dict[str, Any], control: Any) -> None:
    """A bipolar grid's statements, named by both ends and the scale (as the QSF reader names them)."""
    columns = (response.get("row_response") or {}).get("options") or []
    if control != "bipolar" or not columns:
        return
    first, last = columns[0]["label"].strip(), columns[-1]["label"].strip()
    for row in response.get("rows") or []:
        if "left_label" in row and "right_label" in row:
            left, right = row.pop("left_label"), row.pop("right_label")
            row["label"] = f"{left} to {right} (scale: {first} = more {left}; {last} = more {right})" if first and last else f"{left} to {right}"


def _response_labels(response: dict[str, Any], control: Any) -> None:
    rtype = response.get("type")
    if rtype == "choice":
        _labels(response.get("options") or [], "Image option", lambda position, item: str(position))
    elif rtype == "form":
        _labels(response.get("fields") or [], "Image field")
    elif rtype in {"allocation", "ranking"}:
        _labels(response.get("items") or [], "Image option")
    elif rtype == "matrix":
        rows = response.get("rows") or []
        if response.get("mode") == "per_row" and (response.get("row_response") or {}).get("type") == "number":
            _labels(rows, "Image statement")
            return
        columns = response.get("columns") or [] if response.get("mode") == "per_cell" else (response.get("row_response") or {}).get("options") or []
        _labels(columns, "Image option", lambda position, item: str(item["id"]))
        _labels(rows, "Image row")
        _bipolar(response, control)
    elif rtype == "group":
        for field in response.get("fields") or []:
            matrix = field.get("response") or {}
            row_response = matrix.get("row_response") or {}
            if row_response.get("type") != "text":
                _labels(row_response.get("options") or [], "Image option")
            _labels(matrix.get("rows") or [], "Image row")


def shown_question(question: dict[str, Any], *, native: bool, brand: str = "") -> dict[str, Any]:
    """The shown question for a question of the definition: its text as the model
    reads it, tokens kept; ``native`` when its survey was written in the standard
    format (its text keeps its lines, its types are exact). When the text shows
    pictures, the same text with "[[picture N]]" at their places is kept beside
    it (``with_pictures``) for the survey text."""
    text, lines = question_text(question, native=native)
    pictured = picture_text(question, native=native, brand=brand)
    source = question.get("source") if isinstance(question.get("source"), dict) else {}
    shown = _plain(copy.deepcopy({key: value for key, value in question.items() if key != "content"}))
    images = [copy.deepcopy(item) for item in question.get("content") or [] if item.get("type") == "image"]
    if text == IMAGE_STIMULUS and images:
        text = ""
    shown["content"] = [{"type": "text", "text": text, **({"with_pictures": pictured} if pictured else {})}] + images
    response = shown.get("response")
    if isinstance(response, dict):
        _response_labels(response, (shown.get("presentation") or {}).get("control"))
    return {"question": shown, "lines": bool(lines or source.get("keep_lines")), "native": bool(native or source.get("exact_type"))}


# ---------------------------------------------------------------- every text a twin reads


def _media(items: list[dict[str, Any]], path: str, fn: TextFunction) -> None:
    for index, item in enumerate(items):
        for key, name in (("source", "src"), ("alt", "alt"), ("title", "title")):
            if isinstance(item.get(key), str):
                item[key] = fn(f"{path}/{index}/{name}", item[key])


def _items(items: list[dict[str, Any]], labels: str, media: str, fn: TextFunction) -> None:
    for index, item in enumerate(items):
        if isinstance(item.get("label"), str):
            item["label"] = fn(f"{labels}/{index}", item["label"])
        if item.get("media"):
            _media(item["media"], f"{media}/{index}", fn)


def _number_settings(number: dict[str, Any], fn: TextFunction) -> None:
    not_applicable = number.get("not_applicable")
    if isinstance(not_applicable, dict) and isinstance(not_applicable.get("label"), str):
        not_applicable["label"] = fn("Settings/NotApplicableLabel", not_applicable["label"])


def _defaults(rows: list[dict[str, Any]], path: str, fn: TextFunction) -> None:
    """Preset values written as text, one place per row that has one, in row order."""
    index = 0
    for row in rows:
        default = row.get("default")
        if default is None or isinstance(default, list):
            continue
        if isinstance(default, str):
            row["default"] = fn(f"{path}/{index}/Value", default)
        index += 1


def map_text(question: dict[str, Any], fn: TextFunction) -> None:
    """Apply ``fn(place, text)`` to every text of a shown question a twin reads, in
    place. Preset answers are at places that start with ``Default``."""
    images = [item for item in question.get("content") or [] if item.get("type") == "image"]
    for item in question.get("content") or []:
        if item.get("type") == "text" and isinstance(item.get("text"), str):
            item["text"] = fn("QuestionText", item["text"])
            if isinstance(item.get("with_pictures"), str):
                # the same text with its pictures' places: the same values (draws are
                # keyed by place and position, so a random value is drawn the same)
                item["with_pictures"] = fn("QuestionText", item["with_pictures"])
    _media(images, "QuestionMedia", fn)
    response = question.get("response")
    if not isinstance(response, dict):
        return
    rtype = response.get("type")
    if rtype == "choice":
        _items(response.get("options") or [], "Options", "OptionMedia", fn)
    elif rtype == "ranking":
        _items(response.get("items") or [], "Options", "OptionMedia", fn)
    elif rtype == "allocation":
        _items(response.get("items") or [], "Rows", "RowsMedia", fn)
        defaults = response.get("default") or {}
        for index, (item_id, value) in enumerate(list(defaults.items())):
            if isinstance(value, str):
                defaults[item_id] = fn(f"DefaultEntries/{index}/Value", value)
    elif rtype == "form":
        fields = response.get("fields") or []
        _items(fields, "Rows", "RowsMedia", fn)
        for index, field in enumerate(fields):
            if isinstance(field.get("default"), str):
                field["default"] = fn(f"DefaultChoices/{index}", field["default"])
    elif rtype == "text":
        if isinstance(response.get("default"), str):
            response["default"] = fn("DefaultChoices/0", response["default"])
    elif rtype == "number":
        _number_settings(response, fn)
    elif rtype == "matrix":
        rows = response.get("rows") or []
        if response.get("mode") == "per_row" and (response.get("row_response") or {}).get("type") == "number":
            _items(rows, "Statements", "StatementsMedia", fn)
            _number_settings(response.get("row_response") or {}, fn)
            _defaults(rows, "DefaultChoices", fn)
        else:
            _items(rows, "Rows", "RowsMedia", fn)
            columns = response.get("columns") if response.get("mode") == "per_cell" else (response.get("row_response") or {}).get("options")
            _items(columns or [], "Columns", "ColumnsMedia", fn)
            _defaults(rows, "DefaultEntries", fn)
    elif rtype == "group":
        for index, field in enumerate(response.get("fields") or []):
            base = f"sub_questions/{index}"
            if isinstance(field.get("label"), str):
                field["label"] = fn(f"{base}/text", field["label"])
            if field.get("media"):
                _media(field["media"], f"{base}/QuestionMedia", fn)
            matrix = field.get("response") or {}
            _items(matrix.get("rows") or [], f"{base}/Rows", f"{base}/RowsMedia", fn)
            row_response = matrix.get("row_response") or {}
            if row_response.get("type") != "text":
                _items(row_response.get("options") or [], f"{base}/Options", f"{base}/OptionsMedia", fn)


def map_condition_text(condition: Any, fn: Callable[[str], str]) -> Any:
    """A condition with ``fn`` applied to the values it compares with."""
    if isinstance(condition, dict):
        out = {}
        for key, value in condition.items():
            if key == "right" and isinstance(value, str):
                out[key] = fn(value)
            elif key == "qualtrics":
                out[key] = _map_strings(value, fn)
            else:
                out[key] = map_condition_text(value, fn)
        return out
    if isinstance(condition, list):
        return [map_condition_text(item, fn) for item in condition]
    return condition


def _map_strings(value: Any, fn: Callable[[str], str]) -> Any:
    if isinstance(value, str):
        return fn(value)
    if isinstance(value, list):
        return [_map_strings(item, fn) for item in value]
    if isinstance(value, dict):
        return {key: _map_strings(item, fn) for key, item in value.items()}
    return value


def every_text(question: dict[str, Any]) -> list[str]:
    """Every text of a shown question a twin reads."""
    found: list[str] = []
    map_text(question, lambda _place, text: found.append(text) or text)
    return found
