"""Text with piped values: Qualtrics ``${...}`` tokens to standard references and back.

A text value in the format is plain text, or ``{"parts": [...]}`` where each part
is ``{"text": ...}`` or ``{"ref": ...}``. Reading a QSF splits its text around the
tokens; compiling joins the parts back, each reference as the token it came
from. A token with no standard form becomes a ``source`` reference holding it as
written, so nothing is lost or changed.
"""

from __future__ import annotations

import re
from typing import Any

PIPE_RE = re.compile(r"\$\{[^{}]*\}")

# Answer fields and the Qualtrics locator each stands for.
_ANSWER_FORMS: dict[str, str] = {
    "selected_choices": "ChoiceGroup/SelectedChoices",
    "selected_choices_recode": "ChoiceGroup/SelectedChoicesRecode",
    "selected_choices_text_entry": "ChoiceGroup/SelectedChoicesTextEntry",
    "unselected_choices": "ChoiceGroup/UnselectedChoices",
    "displayed_choices": "ChoiceGroup/DisplayedChoices",
    "undisplayed_choices": "ChoiceGroup/UndisplayedChoices",
    "all_choices": "ChoiceGroup/AllChoices",
    "selected_count": "SelectedChoicesCount",
    "choice_label": "ChoiceDescription",
    "choice_recode": "ChoiceRecode",
    "text_entry": "ChoiceTextEntryValue",
    "number_entry": "ChoiceNumericEntryValue",
    "question_text": "QuestionText",
}
_FORM_FIELDS = {form: name for name, form in _ANSWER_FORMS.items()}
# Fields that name an item ("ChoiceDescription/3"); the others never do.
_ITEM_FIELDS = {"choice_label", "choice_recode", "text_entry", "number_entry"}
_ITEM_REQUIRED = {"choice_label", "choice_recode"}

_NUMBER_RE = re.compile(r"-?\d+(?:\.\d+)?")


def _number(text: str) -> int | float | None:
    if not _NUMBER_RE.fullmatch(text.strip()):
        return None
    value = float(text)
    return int(value) if value.is_integer() and "." not in text else value


def _format_number(value: Any) -> str:
    if isinstance(value, bool):
        return str(value)
    if isinstance(value, int) or (isinstance(value, float) and value.is_integer() and not isinstance(value, bool)):
        return str(int(value)) if isinstance(value, int) else repr(value)
    return repr(value) if isinstance(value, float) else str(value)


def parse_pipe(token: str) -> dict[str, Any]:
    """The standard reference for one ``${...}`` token."""
    inner = token[2:-1]
    scheme, _, rest = inner.partition("://")
    if scheme == "e" and rest.startswith("Field/") and len(rest) > len("Field/"):
        return {"scope": "variable", "variable": rest[len("Field/"):]}
    if scheme == "lm":
        if rest.startswith("Field/") and len(rest) > len("Field/"):
            return {"scope": "loop", "field": rest[len("Field/"):]}
        if rest == "CurrentLoopNumber":
            return {"scope": "loop", "value": "current_index"}
        if rest == "TotalLoops":
            return {"scope": "loop", "value": "total"}
    if scheme == "rand":
        kind, _, spec = rest.partition("/")
        low, sep, high = spec.partition(":")
        if kind in {"int", "float"} and sep:
            lo, hi = _number(low), _number(high)
            if lo is not None and hi is not None and token == random_token(kind, lo, hi):
                return {"scope": "random", "kind": kind, "min": lo, "max": hi}
    if scheme == "q":
        question, _, locator = rest.partition("/")
        if question and locator:
            for form, field in sorted(_FORM_FIELDS.items(), key=lambda item: -len(item[0])):
                if locator == form and field not in _ITEM_REQUIRED:
                    return {"scope": "answer", "question": question, "field": field}
                if field in _ITEM_FIELDS and locator.startswith(form + "/"):
                    item = locator[len(form) + 1:]
                    if item and "/" not in item:
                        return {"scope": "answer", "question": question, "field": field, "item": item}
    return {"scope": "source", "expression": token}


def random_token(kind: str, low: Any, high: Any) -> str:
    return "${rand://" + kind + "/" + _format_number(low) + ":" + _format_number(high) + "}"


def pipe_token(ref: dict[str, Any]) -> str:
    """The ``${...}`` token a standard reference compiles to."""
    scope = ref.get("scope")
    if scope == "variable":
        return "${e://Field/" + str(ref["variable"]) + "}"
    if scope == "loop":
        if ref.get("value") == "current_index":
            return "${lm://CurrentLoopNumber}"
        if ref.get("value") == "total":
            return "${lm://TotalLoops}"
        return "${lm://Field/" + str(ref.get("field", "")) + "}"
    if scope == "random":
        return random_token(str(ref["kind"]), ref["min"], ref["max"])
    if scope == "answer":
        form = _ANSWER_FORMS[str(ref["field"])]
        item = ref.get("item")
        return "${q://" + str(ref["question"]) + "/" + form + (f"/{item}" if item not in (None, "") else "") + "}"
    if scope == "source":
        return str(ref["expression"])
    raise ValueError(f"Unknown reference scope: {scope!r}")


def split_text(text: str) -> str | dict[str, Any]:
    """A source text as a text value: plain when it pipes nothing, else its parts."""
    text = str(text or "")
    if "${" not in text:
        return text
    parts: list[dict[str, Any]] = []
    position = 0
    for match in PIPE_RE.finditer(text):
        if match.start() > position:
            parts.append({"text": text[position:match.start()]})
        token = match.group(0)
        ref = parse_pipe(token)
        if pipe_token(ref) != token:  # never change a token: keep it as written
            ref = {"scope": "source", "expression": token}
        parts.append({"ref": ref})
        position = match.end()
    if position < len(text):
        parts.append({"text": text[position:]})
    if not any("ref" in part for part in parts):
        return text
    return {"parts": parts}


def join_text(value: Any) -> str:
    """A text value as source text, each reference as its token."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, dict) and isinstance(value.get("parts"), list):
        out: list[str] = []
        for part in value["parts"]:
            if "ref" in part:
                out.append(pipe_token(part["ref"]))
            else:
                out.append(str(part.get("text", "")))
        return "".join(out)
    return str(value)


def text_refs(value: Any) -> list[dict[str, Any]]:
    """The references a text value pipes, in order."""
    if isinstance(value, dict) and isinstance(value.get("parts"), list):
        return [part["ref"] for part in value["parts"] if isinstance(part, dict) and "ref" in part]
    return []


def plain_text(value: Any) -> str:
    """A text value's literal text, references left out (for labels in messages)."""
    if isinstance(value, dict) and isinstance(value.get("parts"), list):
        return "".join(str(part.get("text", "")) for part in value["parts"] if isinstance(part, dict) and "text" in part)
    return str(value or "")
