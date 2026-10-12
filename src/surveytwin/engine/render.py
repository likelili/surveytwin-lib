"""The prompt, from the questions a twin is shown.

Each shown question is a question in the standard format, as this twin sees it
(piped text filled in, items in the order shown), with a few facts about how it
reads (see ``shown``). From a list of them this module makes what a model call
needs: the questions the model answers, each with what the answer checks need
(``prompt_questions``); the survey text before them (``sequence_text``); and
the pictures in that text (``sequence_media``). The survey text carries a marker
where each picture appears (``prompts.picture_markers``): where the survey puts it
in a question's text, before the text when it has no place there, or right after
its option, row or statement; the request sends the picture there.

The rules are the template prompt builder's (``prompts/instrument.py``), read
off the standard form: a question asking for several options becomes one
yes/no question per option, a grid one question per row, and so on. The text
and every field are the same as before, character for character
(``scripts/engine_diff.py`` checks this on every test and study survey).
"""

from __future__ import annotations

import re
from functools import cached_property
from typing import Any

from ..prompts import layout as prompt_layout
from ..prompts.picture_markers import picture_marker
from ..qsf.html_text import keep_lines
from ..spec.qualtrics import (
    SLIDER_SELECTOR_NAMES,
    number_text,
    positive_int,
    text_checks_settings,
)
from ..spec.text import join_text
from .text import IMAGE_STIMULUS, item_labels, media_items, question_images, question_text

_LM_PLACEHOLDER_RE = re.compile(r"\$\{lm://[^}]+\}")
_ED_PLACEHOLDER_RE = re.compile(r"\$\{e://[^}]+\}")
# Where a question's text shows its Nth picture (engine.text.picture_text).
_PICTURE_PLACE_RE = re.compile(r"\[\[picture (\d+)\]\]")
_NUMERIC_HINTS = [
    re.compile(r"\bhow\s+many\b"),
    re.compile(r"\bnumber\s+of\b"),
    re.compile(r"\bcount\b"),
    re.compile(r"\b0\s*-\s*10\b"),
    re.compile(r"\b0\s+to\s+10\b"),
    re.compile(r"\bfrom\s+0\s+to\b"),
]
_TEXT_SELECTORS = {"single_line": "SL", "multi_line": "ML", "essay": "ESTB", "password": "PW"}
_NUMERIC_CONTENT = {"numeric", "number", "integer", "float", "validnumber"}
NOT_ASKED_KINDS = frozenset({"timing", "meta_info", "captcha", "unsupported"})


# ---------------------------------------------------------------- small helpers


def strip_html(text: Any) -> str:
    """Tags out, spaces collapsed: the template prompt builder's second pass over
    text that is not laid out in lines."""
    if text is None:
        return ""
    text = re.sub(r"<[^>]*>", " ", str(text))
    text = text.replace("&nbsp;", " ")
    return re.sub(r"\s+", " ", text).strip()


def _sanitize_prefilled_text(value: Any) -> str:
    cleaned = str(value or "")
    cleaned = _LM_PLACEHOLDER_RE.sub("", cleaned)
    cleaned = _ED_PLACEHOLDER_RE.sub("", cleaned)
    return re.sub(r"\s+", " ", cleaned).strip()


def _has_placeholder(value: Any) -> bool:
    raw = str(value or "")
    return bool(_LM_PLACEHOLDER_RE.search(raw) or _ED_PLACEHOLDER_RE.search(raw))


def sanitize_rendered_text(rendered: str) -> str:
    """The survey text of one question without its answer markers and unfilled
    variables, with runs of blank lines collapsed."""
    lines: list[str] = []
    previous_blank = False
    for raw_line in str(rendered or "").splitlines():
        line = raw_line
        if "[Masked]" in line:
            line = line.replace("[Masked]", "").rstrip()
        if "[No Answer Provided]" in line:
            line = line.replace("[No Answer Provided]", "").rstrip()
        line = _ED_PLACEHOLDER_RE.sub("", line)
        line = re.sub(r"\s{2,}", " ", line).rstrip()
        stripped = line.strip()
        if stripped in {"Answer:", "Default:"}:
            continue
        if not stripped:
            if lines and not previous_blank:
                lines.append("")
            previous_blank = True
            continue
        lines.append(line)
        previous_blank = False
    return "\n".join(lines).strip()


def _merge_row_text(question_text: str, row_text: str) -> str:
    if not row_text:
        return question_text
    qt = question_text.rstrip()
    rt = row_text.lstrip()
    if qt.endswith("...") and rt.startswith("..."):
        return re.sub(r"[,\s.]+$", "", qt) + ", " + re.sub(r"^\.+\s*", "", rt)
    return f"{question_text} - {row_text}"


def _parse_float(value: Any) -> float | None:
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return None


def _parse_nonnegative_int(value: Any) -> int | None:
    try:
        parsed = int(float(str(value).strip()))
    except (TypeError, ValueError):
        return None
    return parsed if parsed >= 0 else None


def _required(value: Any) -> str:
    """The prompt's response requirement for a ``required`` setting."""
    if value is True:
        return "force_response"
    if value == "request":
        return "request_response"
    return ""


def _number(value: Any) -> str:
    """A number as the survey text prints it."""
    return number_text(value) if not isinstance(value, str) else value


class Item:
    """An option, statement, scale point, field or ranked item, as the prompt names it."""

    __slots__ = ("id", "label", "media", "code", "raw")

    def __init__(self, raw: dict[str, Any], label: str):
        self.raw = raw
        self.id = str(raw.get("id", ""))
        self.label = label
        self.media = media_items(raw.get("media"))
        self.code = raw.get("code")


# ---------------------------------------------------------------- one shown question


class Shown:
    """What the prompt reads of one shown question, from its standard form.

    ``kind`` is one of: ``instruction`` (text only), ``choice``, ``text``,
    ``form``, ``grid`` (a statement per row, one or several scale points each),
    ``grid_text`` and ``grid_sum`` (a box per row and column), ``slider`` (a
    number per statement), ``allocation``, ``ranking``, ``group`` (grids side by
    side), or ``None`` for a question never asked (timing, page metadata, a
    captcha, a type not simulated)."""

    def __init__(self, shown: dict[str, Any]):
        self.record = shown
        self.q: dict[str, Any] = shown["question"]
        self.response: dict[str, Any] = self.q.get("response") or {}
        self.id = str(self.q.get("id", ""))
        self.export_name = str(self.q.get("export_name") or "")
        self.lines = bool(shown.get("lines"))
        self.native = bool(shown.get("native"))
        self.control = (self.q.get("presentation") or {}).get("control")
        self.kind = self._kind()

    def _kind(self) -> str | None:
        if self.q.get("kind") in NOT_ASKED_KINDS:
            return None
        response = self.q.get("response")
        if not response:
            return "instruction"
        rtype = response.get("type")
        if rtype == "matrix":
            if response.get("mode") == "per_cell":
                return "grid_text" if (response.get("cell_response") or {}).get("type") == "text" else "grid_sum"
            return "slider" if (response.get("row_response") or {}).get("type") == "number" else "grid"
        if rtype == "number":
            return "slider"
        return {
            "choice": "choice",
            "text": "text",
            "form": "form",
            "allocation": "allocation",
            "ranking": "ranking",
            "group": "group",
        }.get(rtype)

    # ---- text

    @cached_property
    def text(self) -> str:
        """The question's text as written for the twin (before the prompt's own pass)."""
        return question_text(self.q, native=False)[0]

    @cached_property
    def media(self) -> list[dict[str, str]]:
        return question_images(self.q)

    @cached_property
    def prompt_text(self) -> str:
        """The question's words in a prompt question: unfilled variables out,
        spaces collapsed (lines kept when they count); a picture alone reads
        "[Image stimulus attached]"."""
        text = _LM_PLACEHOLDER_RE.sub("", self.text.strip()).strip()
        text = _ED_PLACEHOLDER_RE.sub("", text).strip()
        text = keep_lines(text) if self.lines else re.sub(r"\s{2,}", " ", text).strip()
        if text:
            return text
        return IMAGE_STIMULUS if self.media else ""

    def survey_text(self) -> str:
        """The question's words in the survey text."""
        return keep_lines(self.text) if self.lines else strip_html(self.text)

    @cached_property
    def picture_text(self) -> str:
        """The question's text with "[[picture N]]" where its pictures sit; empty
        when none has a place in it."""
        for item in self.q.get("content") or []:
            if item.get("type") == "text":
                return str(item.get("with_pictures") or "")
        return ""

    # ---- items

    @cached_property
    def options(self) -> list[Item]:
        """A choice question's options (an unlabeled one is called by its position)."""
        options = self.response.get("options") or [] if self.kind == "choice" else []
        labels = item_labels(options, "Image option", lambda position, item: str(position))
        return [Item(o, label) for o, label in zip(options, labels)]

    @cached_property
    def rows(self) -> list[Item]:
        """A grid's statements, a form's fields or an allocation's items."""
        if self.kind in {"grid", "grid_text", "grid_sum"}:
            rows = self.response.get("rows") or []
            labels = item_labels(rows, "Image row")
            if self.control == "bipolar" and self.columns:
                first, last = self.columns[0].label.strip(), self.columns[-1].label.strip()
                for index, row in enumerate(rows):
                    if "left_label" in row and "right_label" in row:
                        left, right = join_text(row["left_label"]), join_text(row["right_label"])
                        labels[index] = (
                            f"{left} to {right} (scale: {first} = more {left}; {last} = more {right})" if first and last else f"{left} to {right}"
                        )
            return [Item(r, label) for r, label in zip(rows, labels)]
        if self.kind == "form":
            fields = self.response.get("fields") or []
            return [Item(f, label) for f, label in zip(fields, item_labels(fields, "Image field"))]
        if self.kind == "allocation":
            items = self.response.get("items") or []
            return [Item(i, label) for i, label in zip(items, item_labels(items, "Image option"))]
        return []

    @cached_property
    def columns(self) -> list[Item]:
        """A grid's scale points (or a box grid's columns)."""
        if self.kind == "grid":
            columns = (self.response.get("row_response") or {}).get("options") or []
        elif self.kind in {"grid_text", "grid_sum"}:
            columns = self.response.get("columns") or []
        else:
            return []
        labels = item_labels(columns, "Image option", lambda position, item: str(item["id"]))
        return [Item(c, label) for c, label in zip(columns, labels)]

    @cached_property
    def statements(self) -> list[Item]:
        """A slider's statements (a lone number has one, unlabeled, with ID 1)."""
        if self.kind != "slider":
            return []
        if self.response.get("type") == "number":
            return [Item({"id": "1", "label": ""}, "")]
        rows = self.response.get("rows") or []
        return [Item(r, label) for r, label in zip(rows, item_labels(rows, "Image statement"))]

    @cached_property
    def rank_items(self) -> list[Item]:
        if self.kind != "ranking":
            return []
        items = self.response.get("items") or []
        return [Item(i, label) for i, label in zip(items, item_labels(items, "Image option"))]

    # ---- settings

    @cached_property
    def number(self) -> dict[str, Any]:
        """A slider's number settings."""
        if self.kind != "slider":
            return {}
        if self.response.get("type") == "number":
            return self.response
        return self.response.get("row_response") or {}

    @property
    def required(self) -> Any:
        return self.response.get("required", False)

    @property
    def force_response(self) -> bool:
        return self.required is True

    def limits(self) -> tuple[int | None, int | None]:
        """At least and at most how many options (or ranked items) to pick."""
        if self.kind == "choice":
            return positive_int(self.response.get("min_selected")), positive_int(self.response.get("max_selected"))
        if self.kind == "grid":
            row_response = self.response.get("row_response") or {}
            return positive_int(row_response.get("min_selected")), positive_int(row_response.get("max_selected"))
        if self.kind == "ranking":
            return positive_int(self.response.get("min_ranked")), positive_int(self.response.get("max_ranked"))
        return None, None

    @property
    def multiple(self) -> bool:
        if self.kind == "choice":
            return self.response.get("mode") == "multiple"
        if self.kind == "grid":
            return (self.response.get("row_response") or {}).get("mode") == "multiple"
        return False

    @cached_property
    def text_checks(self) -> dict[str, Any]:
        """The Qualtrics names of a text box's checks (``text_checks_settings``)."""
        if self.kind in {"text"}:
            checks = self.response
        elif self.kind == "form":
            checks = self.response.get("field_response") or {}
        elif self.kind == "grid_text":
            checks = self.response.get("cell_response") or {}
        else:
            return {}
        wanted = {key: checks[key] for key in ("min_length", "max_length", "format", "number") if key in checks}
        return text_checks_settings(wanted)

    @property
    def text_selector(self) -> str:
        if self.kind == "form":
            return "FORM"
        if self.kind == "text":
            return _TEXT_SELECTORS.get(self.control or "multi_line", "ML")
        return ""

    @property
    def slider_selector(self) -> str:
        return SLIDER_SELECTOR_NAMES.get(self.control or "slider", "HSLIDER")

    @cached_property
    def stars(self) -> tuple[int | None, str | None]:
        """A star rating's star count and kind (None, None for other sliders)."""
        if self.kind != "slider" or self.control != "stars":
            return None, None
        number = self.number
        count = int(number["max"]) if number.get("max") is not None else None
        step = number.get("step")
        return count, "half" if step == 0.5 else ("discrete" if step == 1 else "continuous")

    @property
    def choice_total(self) -> Any:
        """An allocation's total, as the survey text prints it ("" when none is set)."""
        if self.kind == "allocation":
            total = self.response.get("total")
        elif self.kind == "grid_sum":
            sums = [c for c in self.response.get("constraints") or [] if c.get("type") == "sum_equals"]
            total = sums[0]["total"] if sums else None
        else:
            return ""
        return str(total) if total is not None else ""

    @cached_property
    def value_range(self) -> tuple[Any, Any] | None:
        """(min, max) of a slider, a box grid's sums or an allocation's amounts; None
        when the question has no range."""
        if self.kind == "slider":
            number = self.number
            return (number.get("min") if number.get("min") is not None else 0, number.get("max") if number.get("max") is not None else 100)
        if self.kind == "grid_sum":
            cell = self.response.get("cell_response") or {}
            if cell.get("min") is None and cell.get("max") is None:
                return None
            return (cell.get("min") if cell.get("min") is not None else 0, cell.get("max") if cell.get("max") is not None else 100)
        if self.kind == "allocation":
            if self.response.get("min") is None and self.response.get("max") is None:
                return None
            return (
                self.response.get("min") if self.response.get("min") is not None else 0,
                self.response.get("max") if self.response.get("max") is not None else 100,
            )
        return None

    @cached_property
    def codes(self) -> dict[str, Any]:
        """Recodes: the value exported for each option, by option ID."""
        if self.kind == "choice":
            items = self.response.get("options") or []
        elif self.kind in {"allocation", "ranking"}:
            items = self.response.get("items") or []
        elif self.kind in {"grid", "grid_text", "grid_sum"}:
            items = [c.raw for c in self.columns]
        else:
            return {}
        return {str(item["id"]): item["code"] for item in items if "code" in item}

    # ---- defaults

    def default_choices(self) -> list[tuple[str, str]]:
        """A choice question's preselected options: (ID, label)."""
        if self.kind != "choice":
            return []
        chosen = set(self.response.get("default") or [])
        return [(o.id, o.label.strip() or f"Option {o.id}") for o in self.options if o.id in chosen]

    def grid_defaults(self) -> dict[str, list[tuple[str, str]]]:
        """A grid's preselected scale points by row ID: [(column ID, label)]."""
        if self.kind != "grid":
            return {}
        labels = {c.id: c.label.strip() or f"Scale point {c.id}" for c in self.columns}
        out: dict[str, list[tuple[str, str]]] = {}
        for row in self.rows:
            default = row.raw.get("default")
            if isinstance(default, list):
                for column_id in default:
                    out.setdefault(row.id, []).append((str(column_id), labels.get(str(column_id), f"Option {column_id}")))
        return out

    def value_defaults(self) -> dict[tuple[str, str], str]:
        """Preset amounts by (row ID, column ID) (column "" for a whole row)."""
        out: dict[tuple[str, str], str] = {}
        if self.kind == "allocation":
            for item_id, value in (self.response.get("default") or {}).items():
                if str(value).strip():
                    out[(str(item_id), "")] = str(value).strip()
        elif self.kind in {"grid_sum", "grid_text", "slider"}:
            rows = self.statements if self.kind == "slider" else self.rows
            for row in rows:
                default = row.raw.get("default")
                if default is not None and not isinstance(default, list) and str(default).strip():
                    out[(row.id, "")] = str(default).strip()
        return out

    @cached_property
    def _slider_defaults(self) -> list[tuple[str, str]]:
        return [
            (s.id, str(s.raw["default"]))
            for s in self.statements
            if s.raw.get("default") is not None and not isinstance(s.raw.get("default"), list)
        ]

    def slider_default(self, statement: Item, index: int) -> str:
        """A statement's preset value: the one set for its ID, else (as the template
        prompt builder read the list) the one at its position."""
        defaults = self._slider_defaults
        value = next((v for statement_id, v in defaults if statement_id == statement.id), None)
        if value is None and index < len(defaults):
            value = defaults[index][1]
        return str(value).strip() if value not in (None, "") else ""

    def slider_shown_default(self, statement: Item, position: int) -> str:
        """The preset value the survey text shows: by statement ID, else by the
        statement's number."""
        defaults = dict(self._slider_defaults)
        return defaults.get(statement.id) or defaults.get(str(position)) or ""

    def text_default(self) -> Any:
        """A text box's preset text, as written (with its pipes); None when none."""
        if self.kind != "text" or "default" not in self.response:
            return None
        return join_text(self.response["default"])

    def rank_default(self, item_id: str) -> str:
        for entry in self.response.get("default") or [] if self.kind == "ranking" else []:
            if str(entry.get("item", "")) == item_id and entry.get("rank"):
                return str(entry["rank"])
        return ""


# ---------------------------------------------------------------- prompt questions


def _prefilled(shown: Shown) -> tuple[bool, str]:
    """A text box filled from outside the survey (it pipes a variable): not asked."""
    default = shown.text_default()
    if shown.kind != "text" or shown.text_selector not in _TEXT_SELECTORS.values() or default is None:
        return False, ""
    if not _has_placeholder(default):
        return False, ""
    return True, _sanitize_prefilled_text(default)


def _default_text(shown: Shown) -> str:
    default = shown.text_default()
    if shown.kind != "text" or default in (None, "") or _has_placeholder(default):
        return ""
    return _sanitize_prefilled_text(default)


def _form_prefilled(shown: Shown, field: Item, index: int) -> tuple[bool, str]:
    """A form field with preset text: kept as written, not asked."""
    if "default" not in field.raw:
        return False, ""
    raw = join_text(field.raw["default"])
    match = re.search(r"\$\{lm://Field/([^}]+)\}", raw)
    if match:
        anchor = _loop_anchor(shown.text, match.group(1).strip())
        if anchor:
            return True, _sanitize_prefilled_text(anchor)
    return True, _sanitize_prefilled_text(raw)


def _loop_anchor(question_text: str, field_id: str) -> str:
    """A word a loop row sets, read back from the question's own text (as the
    template prompt builder did for one study's word-chain task)."""
    text = re.sub(r"\s+", " ", question_text).strip()
    if not text:
        return ""
    if field_id == "1":
        match = re.search(r"\bfirst word\b.*?\bset to\s+(.+?)\s*(?:\.|$)", text, re.I)
        if match:
            return match.group(1).strip(" .")
        match = re.search(r"\bconnect these two words:\s*(.+?)\s+and\s+(.+?)\s*(?:\.|$)", text, re.I)
        if match:
            return match.group(1).strip(" .")
    if field_id == "2":
        match = re.search(r"\blast word\b.*?\bset to\s+(.+?)\s*(?:\.|$)", text, re.I)
        if match:
            return match.group(1).strip(" .")
        match = re.search(r"\bconnect these two words:\s*(.+?)\s+and\s+(.+?)\s*(?:\.|$)", text, re.I)
        if match:
            return match.group(2).strip(" .")
    return ""


def _attach_defaults(entry: dict[str, Any], defaults: list[tuple[str, str]], *, option_id: str | None = None) -> None:
    if not defaults:
        return
    entry["default_choice_ids"] = [choice_id for choice_id, _ in defaults]
    entry["default_choice_labels"] = [label for _, label in defaults]
    if option_id is not None:
        entry["default_selected"] = option_id in entry["default_choice_ids"]


def _attach_text_checks(entry: dict[str, Any], settings: dict[str, Any]) -> None:
    validation_type = str(settings.get("ValidationType") or settings.get("Type") or "").strip()
    if validation_type and validation_type.upper() != "NONE":
        entry["text_validation_type"] = validation_type
    content_type = str(settings.get("ContentType") or "").strip()
    if content_type:
        entry["text_content_type"] = content_type
    low = positive_int(settings.get("MinChars"))
    high = positive_int(settings.get("TotalChars") or settings.get("MaxChars"))
    kind = validation_type.upper()
    if kind == "TOTALCHAR":
        low = None
    elif kind == "MINCHAR":
        high = None
    elif kind == "CONTENTTYPE":
        low = high = None
    if low is not None:
        entry["text_min_chars"] = low
    if high is not None:
        entry["text_max_chars"] = high


def _attach_common(entry: dict[str, Any], shown: Shown) -> dict[str, Any]:
    """What every prompt question of ``shown`` carries for the answer checks."""
    if shown.kind in {"choice", "ranking"}:
        items = shown.options if shown.kind == "choice" else shown.rank_items
        entry.setdefault("option_ids", [i.id for i in items])
    elif shown.kind in {"grid", "grid_text", "grid_sum"}:
        entry.setdefault("option_ids", [c.id for c in shown.columns])
    elif shown.kind == "group":
        entry.setdefault("option_ids", [o.id for o in _group_options(shown)])
    if shown.codes:
        entry.setdefault("recode_values", dict(shown.codes))
    if shown.kind in {"text", "form", "grid_text"}:
        _attach_text_checks(entry, shown.text_checks)
    best_worst = shown.record.get("best_worst")
    if isinstance(best_worst, dict) and best_worst.get("group"):
        # a MaxDiff task's best and worst picks: answered together, two different items
        entry["best_worst_group_id"] = str(best_worst["group"])
        entry["best_worst_role"] = str(best_worst.get("role", ""))
    requirement = _required(shown.required)
    if requirement:
        entry["response_requirement"] = requirement
        if requirement == "force_response":
            entry["force_response"] = True
        elif "force_response" not in entry:
            entry["force_response"] = False
    value_range = shown.value_range
    if value_range is not None:
        entry["numeric_min"], entry["numeric_max"] = value_range
    number = shown.text_checks.get("ValidNumber") if shown.text_checks else None
    if isinstance(number, dict):
        for source_key, entry_key in (("Min", "numeric_min"), ("Max", "numeric_max"), ("NumDecimals", "numeric_decimals")):
            value = number.get(source_key)
            if value not in (None, "") and entry_key not in entry:
                entry[entry_key] = value
    return entry


def _entry_type(shown: Shown) -> str:
    """The prompt's type name for a question asked as one entry."""
    if shown.kind == "choice":
        return "single_choice"
    if shown.kind in {"grid", "grid_text", "grid_sum"}:
        return "matrix"
    if shown.kind in {"slider", "allocation"}:
        return "numeric"
    if shown.kind == "ranking":
        return "rank_order"
    if shown.kind in {"text", "form"}:
        content_type = str(shown.text_checks.get("ContentType", "") or "").strip().lower()
        if content_type in _NUMERIC_CONTENT:
            return "numeric"
        # a survey written in the standard format says what it asks: its text
        # question is never taken for a number question from its wording
        if not shown.native and shown.text_selector in {"SL", "FORM"}:
            text = shown.text.strip().lower()
            if any(pattern.search(text) for pattern in _NUMERIC_HINTS):
                return "numeric"
        return "open_text"
    if shown.kind == "group":
        return "sbs"
    return "open_text"


def _write_in_entry(shown: Shown, question_text: str, option: Item) -> dict[str, Any]:
    label = option.label.strip() or f"Option {option.id}"
    entry: dict[str, Any] = {
        "id": f"{shown.id}_{option.id}_TEXT",
        "text": f"{question_text} If you selected '{label}', specify the text. Otherwise leave blank.",
        "type": "open_text",
        "options": [],
        "parent_question_id": shown.id,
        "choice_text_entry_option_id": option.id,
        "choice_text_entry_option_label": label,
        "force_response": False,
    }
    if shown.media:
        entry["question_media"] = shown.media
    return _attach_common(entry, shown)


def _write_ins(shown: Shown) -> list[Item]:
    return [o for o in shown.options if o.raw.get("write_in")]


def _multi_choice_entries(shown: Shown, qtext: str) -> list[dict[str, Any]]:
    low, high = shown.limits()
    defaults = shown.default_choices()
    out = []
    for position, option in enumerate(shown.options, start=1):
        label = option.label.strip() or f"Option {option.id}"
        entry: dict[str, Any] = {
            "id": f"{shown.id}_{option.id}",
            "text": f"{qtext} Option: {label}. Should this option be selected?",
            "type": "single_choice",
            "options": ["No", "Yes"],
            "parent_question_id": shown.id,
            "multi_answer_option_id": option.id,
            "multi_answer_option_label": label,
            "multi_answer_option_position": position,
            "force_response": shown.force_response,
        }
        if low is not None:
            entry["multi_answer_min_choices"] = low
        if high is not None:
            entry["multi_answer_max_choices"] = high
        if shown.media:
            entry["question_media"] = shown.media
        _attach_defaults(entry, defaults, option_id=option.id)
        out.append(_attach_common(entry, shown))
    out += [_write_in_entry(shown, qtext, option) for option in _write_ins(shown)]
    return out


def _ranking_entries(shown: Shown, qtext: str) -> list[dict[str, Any]]:
    low, high = shown.limits()
    count = len(shown.rank_items)
    out = []
    for position, item in enumerate(shown.rank_items, start=1):
        label = item.label.strip() or f"Item {item.id}"
        entry: dict[str, Any] = {
            "id": f"{shown.id}_{item.id}",
            "text": f"{qtext} - Rank for {label}",
            "type": "numeric",
            "options": [],
            "parent_question_id": shown.id,
            "rank_order_item_id": item.id,
            "rank_order_item_label": label,
            "rank_order_item_position": position,
            "rank_order_count": count,
            "numeric_min": 1,
            "numeric_max": count,
            "numeric_decimals": 0,
            "numeric_step": 1,
            "force_response": shown.force_response,
        }
        if low is not None:
            entry["rank_order_min_items"] = low
        if high is not None:
            entry["rank_order_max_items"] = high
        default = shown.rank_default(item.id)
        if default:
            entry["default_rank"] = default
            entry["default_numeric_value"] = default
        media = [*shown.media, *item.media]
        if media:
            entry["question_media"] = media
        out.append(_attach_common(entry, shown))
    return out


def _grid_entries(shown: Shown, qtext: str) -> list[dict[str, Any]]:
    rows = [(r.id, r.label.strip() or f"Statement {r.id}") for r in shown.rows]
    columns = [(c.id, c.label.strip() or f"Scale point {c.id}") for c in shown.columns]
    out: list[dict[str, Any]] = []
    if shown.kind in {"grid_text", "grid_sum"}:
        # one box per row (per row and column when there are several columns)
        expand = len(columns) > 1
        cells = columns if expand else [("", "")]
        defaults = shown.value_defaults() if shown.kind == "grid_sum" else {}
        total = shown.choice_total if shown.kind == "grid_sum" else ""
        for row_id, row_label in rows:
            row_text = _merge_row_text(qtext, row_label)
            for column_id, column_label in cells:
                entry: dict[str, Any] = {
                    "id": f"{shown.id}_{row_id}_{column_id}" if expand else f"{shown.id}_{row_id}",
                    "text": f"{row_text} - {column_label}" if expand and column_label else row_text,
                    "type": "open_text" if shown.kind == "grid_text" else "numeric",
                    "options": [],
                    "parent_question_id": shown.id,
                    "matrix_row_id": row_id,
                    "matrix_row_label": row_label,
                }
                if shown.kind == "grid_sum":
                    entry["constant_sum_row_id"] = row_id
                    entry["constant_sum_row_label"] = row_label
                entry["force_response"] = shown.force_response
                if expand:
                    entry["matrix_column_id"] = column_id
                    entry["matrix_column_label"] = column_label
                    if shown.kind == "grid_sum":
                        entry["constant_sum_group_id"] = f"{shown.id}_col_{column_id}"
                        entry["constant_sum_group_label"] = column_label
                if shown.kind == "grid_sum":
                    default = defaults.get((row_id, column_id if expand else "")) or defaults.get((row_id, ""))
                    if default:
                        entry["default_numeric_value"] = default
                    if total:
                        entry["constant_sum_total"] = total
                if shown.media:
                    entry["question_media"] = shown.media
                out.append(_attach_common(entry, shown))
        return out
    defaults = shown.grid_defaults()
    if shown.multiple:
        low, high = shown.limits()
        for row_id, row_label in rows:
            row_qid = f"{shown.id}_{row_id}"
            row_text = _merge_row_text(qtext, row_label)
            for position, (column_id, column_label) in enumerate(columns, start=1):
                entry = {
                    "id": f"{row_qid}_{column_id}",
                    "text": f"{row_text} Option: {column_label}. Should this option be selected?",
                    "type": "single_choice",
                    "options": ["No", "Yes"],
                    "parent_question_id": row_qid,
                    "matrix_question_id": shown.id,
                    "matrix_row_id": row_id,
                    "matrix_row_label": row_label,
                    "matrix_column_id": column_id,
                    "matrix_column_label": column_label,
                    "matrix_multi_answer": True,
                    "multi_answer_option_id": column_id,
                    "multi_answer_option_label": column_label,
                    "multi_answer_option_position": position,
                    "force_response": shown.force_response,
                }
                if low is not None:
                    entry["multi_answer_min_choices"] = low
                if high is not None:
                    entry["multi_answer_max_choices"] = high
                _attach_defaults(entry, defaults.get(row_id, []), option_id=column_id)
                if shown.media:
                    entry["question_media"] = shown.media
                out.append(_attach_common(entry, shown))
        return out
    options = [c.label.strip() for c in shown.columns if c.label.strip()]
    option_media = [c.media for c in shown.columns]
    for row_id, row_label in rows:
        entry = {
            "id": f"{shown.id}_{row_id}",
            "text": _merge_row_text(qtext, row_label),
            "type": "matrix",
            "options": options,
            "parent_question_id": shown.id,
            "matrix_row_id": row_id,
            "matrix_row_label": row_label,
            "force_response": shown.force_response,
        }
        _attach_defaults(entry, defaults.get(row_id, []))
        if shown.media:
            entry["question_media"] = shown.media
        if any(option_media):
            entry["option_media"] = option_media
        out.append(_attach_common(entry, shown))
    return out


def _slider_entries(shown: Shown, qtext: str) -> list[dict[str, Any]]:
    statements = shown.statements or [Item({"id": "1", "label": ""}, "")]
    single = len(statements) == 1
    number = shown.number
    out = []
    for index, statement in enumerate(statements):
        label = statement.label.strip()
        meaningful = label not in {"", "[No Statement Needed]"}
        if single and not meaningful:
            entry_id, text = shown.id, qtext
        else:
            entry_id = f"{shown.id}_{statement.id}"
            text = f"{qtext} - {label}" if meaningful else qtext
        entry: dict[str, Any] = {"id": entry_id, "text": text, "type": "numeric", "options": [], "force_response": shown.force_response}
        if not single:
            entry["parent_question_id"] = shown.id
        entry["slider_selector"] = shown.slider_selector.upper()
        entry["slider_statement_id"] = statement.id
        if label:
            entry["slider_statement_label"] = label
        if number.get("decimals") is not None:
            entry["numeric_decimals"] = int(number["decimals"])
        step = _parse_float(number.get("step")) if number.get("step") is not None else None
        if step is not None and step > 0:
            entry["numeric_step"] = step
        not_applicable = number.get("not_applicable")
        if not_applicable:
            entry["allow_not_applicable"] = True
            entry["not_applicable_label"] = join_text(not_applicable.get("label", "")).strip() or "Not Applicable"
        default = shown.slider_default(statement, index)
        if default:
            entry["default_numeric_value"] = default
        star_count, star_type = shown.stars
        if star_count is not None:
            entry["star_count"] = star_count
        if star_type:
            entry["star_type"] = star_type
        media = [*shown.media, *statement.media]
        if media:
            entry["question_media"] = media
        out.append(_attach_common(entry, shown))
    return out


def _form_entries(shown: Shown, qtext: str) -> list[dict[str, Any]]:
    entry_type = _entry_type(shown)
    out = []
    for index, field in enumerate(shown.rows):
        label = field.label.strip() or f"Field {field.id}"
        entry: dict[str, Any] = {
            "id": f"{shown.id}_{field.id}",
            "text": f"{qtext} - {label}" if label else qtext,
            "type": entry_type,
            "options": [],
            "parent_question_id": shown.id,
            "form_field_id": field.id,
            "form_field_label": label,
            "force_response": shown.force_response,
        }
        prefilled, value = _form_prefilled(shown, field, index)
        if prefilled:
            entry["prefill_only"] = True
            entry["prefilled_answer"] = value
        if shown.media:
            entry["question_media"] = shown.media
        out.append(_attach_common(entry, shown))
    return out


def _allocation_entries(shown: Shown, qtext: str) -> list[dict[str, Any]]:
    defaults = shown.value_defaults()
    total = shown.choice_total
    out = []
    for item in shown.rows:
        label = item.label.strip() or f"Choice {item.id}"
        entry: dict[str, Any] = {
            "id": f"{shown.id}_{item.id}",
            "text": f"{qtext} - {label}" if label else qtext,
            "type": "numeric",
            "options": [],
            "parent_question_id": shown.id,
            "constant_sum_row_id": item.id,
            "constant_sum_row_label": label,
            "force_response": shown.force_response,
        }
        default = defaults.get((item.id, ""))
        if default:
            entry["default_numeric_value"] = default
        if total:
            entry["constant_sum_total"] = total
        if shown.media:
            entry["question_media"] = shown.media
        out.append(_attach_common(entry, shown))
    return out


# ---- side by side


def _group_options(shown: Shown) -> list[Item]:
    """The options the question as a whole lists: the first column's with options."""
    for field in shown.response.get("fields") or []:
        row_response = (field.get("response") or {}).get("row_response") or {}
        if row_response.get("type") != "text" and row_response.get("options"):
            options = row_response["options"]
            return [Item(o, label) for o, label in zip(options, item_labels(options, "Image option"))]
    return []


def _group_columns(shown: Shown) -> list[dict[str, Any]]:
    """Each column of a side-by-side question: its label, statements and scale."""
    out = []
    for index, field in enumerate(shown.response.get("fields") or [], start=1):
        matrix = field.get("response") or {}
        row_response = matrix.get("row_response") or {}
        is_text = row_response.get("type") == "text"
        rows = matrix.get("rows") or []
        options = [] if is_text else (row_response.get("options") or [])
        field_id = str(field.get("id", "") or index)
        source = field.get("source") if isinstance(field.get("source"), dict) else {}
        out.append(
            {
                "id": field_id,
                "qid": str(source.get("QuestionID") or f"{shown.id}#{field_id}"),
                "label": join_text(field.get("label", "")).strip() or str(field.get("export_name", "")).strip(),
                "text": join_text(field.get("label", "")),
                "is_text": is_text,
                "rows": [Item(r, label) for r, label in zip(rows, item_labels(rows, "Image row"))],
                "options": [Item(o, label) for o, label in zip(options, item_labels(options, "Image option"))],
                "media": media_items(field.get("media")),
                "required": matrix.get("required", False),
                "checks": text_checks_settings({k: row_response[k] for k in ("min_length", "max_length", "format", "number") if k in row_response})
                if is_text
                else {},
                "has_settings": "Settings" in source or bool(matrix.get("required")) or (is_text and len(row_response) > 1),
            }
        )
    return out


def _group_entries(shown: Shown, qtext: str) -> list[dict[str, Any]]:
    out = []
    for column in _group_columns(shown):
        sub_label = column["label"] or f"Column {column['id']}"
        options = [] if column["is_text"] else [o.label.strip() for o in column["options"]]
        option_ids = [] if column["is_text"] else [o.id for o in column["options"]]
        codes = {} if column["is_text"] else {o.id: o.code for o in column["options"] if "code" in o.raw}
        option_media = [] if column["is_text"] else [o.media for o in column["options"]]
        labels = {o.id: o.label for o in column["options"]}
        for row in column["rows"]:
            row_label = row.label.strip() or f"Row {row.id}"
            defaults = [
                (str(option_id), (labels.get(str(option_id), str(option_id)) or "").strip() or f"Option {option_id}")
                for option_id in (row.raw.get("default") if isinstance(row.raw.get("default"), list) else [])
            ]
            entry: dict[str, Any] = {
                "id": f"{column['qid']}_{row.id}",
                "text": " - ".join(part for part in (qtext, sub_label, row_label) if part),
                "type": "open_text" if column["is_text"] else "matrix",
                "options": options,
                "option_ids": option_ids,
                "recode_values": codes,
                "parent_question_id": shown.id,
                "sbs_sub_question_id": column["id"],
                "sbs_sub_question_qid": column["qid"],
                "sbs_sub_question_label": sub_label,
                "sbs_row_id": row.id,
                "sbs_row_label": row_label,
                "force_response": shown.force_response or column["required"] is True,
            }
            if column["is_text"] and column["has_settings"]:
                # per-column text limits live on the column, not the question
                _attach_text_checks(entry, column["checks"])
            media = [*shown.media, *column["media"], *row.media]
            if media:
                entry["question_media"] = media
            if any(option_media):
                entry["option_media"] = option_media
            _attach_defaults(entry, defaults)
            out.append(_attach_common(entry, shown))
    return out


# ---- every question


def question_entries(shown_record: dict[str, Any], *, index: int = 0) -> list[dict[str, Any]]:
    """The prompt questions one shown question becomes (none for text only)."""
    shown = Shown(shown_record)
    if shown.kind in {None, "instruction"}:
        return []
    qid = shown.id or f"Q{index + 1}"
    shown.id = qid
    qtext = shown.prompt_text or shown.export_name or qid
    if shown.kind == "choice" and shown.multiple and shown.options:
        return _multi_choice_entries(shown, qtext)
    if shown.kind == "ranking":
        return _ranking_entries(shown, qtext)
    if shown.kind in {"grid", "grid_text", "grid_sum"} and shown.rows:
        return _grid_entries(shown, qtext)
    if shown.kind == "slider":
        return _slider_entries(shown, qtext)
    if shown.kind == "form" and shown.rows:
        return _form_entries(shown, qtext)
    if shown.kind == "allocation" and shown.rows:
        return _allocation_entries(shown, qtext)
    if shown.kind == "group":
        cells = _group_entries(shown, qtext)
        if cells:
            return cells
    entry: dict[str, Any] = {"id": qid, "text": qtext, "type": _entry_type(shown), "options": _entry_options(shown)}
    entry["force_response"] = shown.force_response
    if shown.media:
        entry["question_media"] = shown.media
    option_media = _entry_option_media(shown)
    if any(option_media):
        entry["option_media"] = option_media
    if shown.kind == "choice":
        _attach_defaults(entry, shown.default_choices())
    default_text = _default_text(shown)
    if default_text:
        entry["default_text"] = default_text
    prefilled, value = _prefilled(shown)
    if prefilled:
        entry["prefill_only"] = True
        entry["prefilled_answer"] = value
    out = [_attach_common(entry, shown)]
    out += [_write_in_entry(shown, qtext, option) for option in _write_ins(shown)]
    return out


def _entry_options(shown: Shown) -> list[str]:
    if shown.kind == "choice":
        return [o.label.strip() for o in shown.options]
    if shown.kind == "ranking":
        return [i.label.strip() for i in shown.rank_items]
    if shown.kind == "group":
        return [o.label.strip() for o in _group_options(shown)]
    if shown.kind in {"grid", "grid_text", "grid_sum"}:
        return [c.label.strip() for c in shown.columns if c.label.strip()]
    return []


def _entry_option_media(shown: Shown) -> list[list[dict[str, str]]]:
    if shown.kind == "choice":
        return [o.media for o in shown.options]
    if shown.kind == "ranking":
        return [i.media for i in shown.rank_items]
    if shown.kind in {"grid", "grid_text", "grid_sum"}:
        return [c.media for c in shown.columns]
    return []


def prompt_questions(questions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Every prompt question the shown questions become, in order."""
    out: list[dict[str, Any]] = []
    for shown in questions:
        out += question_entries(shown, index=len(out))
    return out


# ---------------------------------------------------------------- the survey text


def _marked(label: str, markers: dict[tuple[Any, ...], list[str]], *key: Any) -> str:
    """An item's label with the markers of its pictures after it."""
    placed = markers.get(key)
    return f"{str(label or '').strip()} {' '.join(placed)}".strip() if placed else label


def _format_choice(shown: Shown, markers: dict[tuple[Any, ...], list[str]]) -> str:
    out = ["Question Type: Multiple Choice\n" if shown.multiple else "Question Type: Single Choice\n"]
    if shown.options:
        out.append("Options:\n")
        for position, option in enumerate(shown.options, 1):
            out.append(f"  {position} - {strip_html(_marked(option.label, markers, 'option', position))}\n")
        out.append("Answer: [Masked]\n")
    out.append("\n")
    return "".join(out)


def _format_grid(shown: Shown, markers: dict[tuple[Any, ...], list[str]]) -> str:
    out = ["Question Type: Matrix\n"]
    if shown.columns:
        out.append("Options:\n")
        for position, column in enumerate(shown.columns, 1):
            out.append(f"  {position} = {strip_html(_marked(column.label, markers, 'option', position))}\n")
        for position, row in enumerate(shown.rows, 1):
            out.append(f"{position}. {strip_html(_marked(row.label, markers, 'row', position))}\n")
            out.append("Answer: [Masked]\n")
        out.append("\n")
    return "".join(out)


def _format_text(shown: Shown, markers: dict[tuple[Any, ...], list[str]]) -> str:
    selector = shown.text_selector
    if selector == "FORM":
        out = ["Question Type: Text Entry (Form)\n"]
        for position, field in enumerate(shown.rows, 1):
            default = _clean_default(join_text(field.raw["default"])) if "default" in field.raw else ""
            label = strip_html(_marked(field.label, markers, "row", position))
            out.append(f"{label}: {default}\n" if default else f"{label}:\n")
    else:
        out = ["Question Type: Text Entry (Password)\n" if selector == "PW" else "Question Type: Text Entry\n"]
        default = shown.text_default()
        cleaned = _clean_default(default) if default is not None else ""
        if cleaned:
            out.append(f"Default: {cleaned}\n")
    out.append("\n")
    return "".join(out)


def _clean_default(value: Any) -> str:
    cleaned = strip_html(str(value))
    cleaned = _ED_PLACEHOLDER_RE.sub("", cleaned)
    return re.sub(r"\s+", " ", cleaned).strip()


def _format_slider(shown: Shown, markers: dict[tuple[Any, ...], list[str]]) -> str:
    number = shown.number
    out = ["Question Type: Slider\n", f"Slider selector: {shown.slider_selector}\n"]
    low, high = shown.value_range or (0, 100)
    out.append(f"Range: {low} to {high}\n")
    star_count, star_type = shown.stars
    if star_count:
        out.append(f"Stars: {star_count}\n")
    if star_type:
        out.append(f"Star type: {star_type}\n")
    if number.get("decimals") is not None:
        out.append(f"Decimal places: {number['decimals']}\n")
    if number.get("step") is not None:
        out.append(f"Step: {_number(number['step'])}\n")
    if number.get("not_applicable"):
        out.append(f"Not applicable option: {join_text(number['not_applicable'].get('label', '')).strip() or 'Not Applicable'}\n")
    statements = shown.statements
    if statements:
        for position, statement in enumerate(statements, 1):
            label = statement.label if statement.label != "" else "[No Statement Needed]"
            out.append(f"{position}. {strip_html(_marked(label, markers, 'statement', position))}\n")
            default = shown.slider_shown_default(statement, position)
            if default not in (None, ""):
                out.append(f"Default shown: {strip_html(default)}\n")
            out.append("Answer: [Masked]\n")
    else:
        out.append("Answer: [No Answer Provided]\n")
    out.append("\n")
    return "".join(out)


def _format_allocation(shown: Shown, markers: dict[tuple[Any, ...], list[str]]) -> str:
    total = shown.choice_total or "100"
    out = ["Question Type: Constant Sum\n", f"Instructions: Please assign values that sum to {total}\n"]
    if shown.rows:
        out.append("Options:\n")
        for position, item in enumerate(shown.rows, 1):
            out.append(f"  {position}. {strip_html(_marked(item.label, markers, 'row', position))}\n")
        out.append("Answer: [Masked]\n")
    out.append("\n")
    return "".join(out)


def _format_ranking(shown: Shown, markers: dict[tuple[Any, ...], list[str]]) -> str:
    items = [(i.id, strip_html(_marked(i.label, markers, "option", position)) or f"Item {i.id}") for position, i in enumerate(shown.rank_items, 1)]
    out = [
        "Question Type: Rank Order\n",
        f"Instructions: Assign each item a unique rank from 1 to {len(items)}. Rank 1 means highest preference.\n",
    ]
    if items:
        out.append("Items:\n")
        for position, (item_id, label) in enumerate(items, 1):
            default = shown.rank_default(item_id)
            out.append(f"  {position}. {label}{f' (default rank: {default})' if default else ''}\n")
    out.append("Answer: [Masked]\n")
    out.append("\n")
    return "".join(out)


def _format_group(shown: Shown, markers: dict[tuple[Any, ...], list[str]]) -> str:
    out = ["Question Type: Side by Side\n"]
    columns = _group_columns(shown)
    for position, column in enumerate(columns, 1):
        heading = _marked(column["text"].strip() or f"Column {position}", markers, "column", position)
        out.append(f"Column {position}: {strip_html(heading)}\n")
        if column["is_text"]:
            out.append("  Response type: Text Entry\n")
        elif column["options"]:
            out.append("  Options:\n")
            for option_position, option in enumerate(column["options"], 1):
                out.append(f"    {option_position} = {strip_html(_marked(option.label, markers, 'column option', position, option_position))}\n")
    rows = _group_rows(shown)
    if rows:
        out.append("Rows:\n")
        for position, label in enumerate(rows, 1):
            out.append(f"{position}. {strip_html(label)}\n")
            out.append("Answer: [Masked]\n")
    out.append("\n")
    return "".join(out)


def _group_rows(shown: Shown) -> list[str]:
    """The statements a side-by-side question lists once for all its columns."""
    source = shown.q.get("source") if isinstance(shown.q.get("source"), dict) else {}
    sbs = source.get("sbs") if isinstance(source.get("sbs"), dict) else None
    if sbs is not None:
        return [str(row) for row in sbs.get("Rows") or []]
    return []


def _formatted(shown: Shown, places: "Places | None" = None) -> str:
    """The question in the survey text, before the answer markers come out, with
    the markers of its pictures at their places."""
    places = places or Places("", {})
    body = {
        "instruction": lambda s, m: "[Descriptive Information]\n\n",
        "choice": _format_choice,
        "grid": _format_grid,
        "grid_text": _format_grid,
        "grid_sum": _format_grid,
        "text": _format_text,
        "form": _format_text,
        "slider": _format_slider,
        "allocation": _format_allocation,
        "ranking": _format_ranking,
        "group": _format_group,
    }[shown.kind](shown, places.items)
    text = places.text if places.text is not None else shown.survey_text()
    return text + "\n" + body


class Places:
    """Where one question's picture markers go in the survey text: ``text``, the
    question's text with its own pictures' markers (None: the text as it is, no
    picture of its own), and ``items``, the markers after an option, row,
    statement or side-by-side column (keyed as the formatters read them)."""

    def __init__(self, text: str | None, items: dict[tuple[Any, ...], list[str]]):
        self.text = text
        self.items = items


def _picture_label(text: str, entry: dict[str, Any]) -> str:
    """A picture's label, with the alt text and title the survey gives it."""
    alt = str(entry.get("alt", "") or "").strip()
    title = str(entry.get("title", "") or "").strip()
    if alt:
        text += f". Alt text: {alt}"
    if title and title != alt:
        text += f". Title: {title}"
    return text


def _places(shown: Shown, entries: list[dict[str, Any]]) -> Places:
    """Where each of the question's pictures (``entries``: its media entries) is
    marked: its own pictures where its text shows them (``Shown.picture_text``),
    or before the text when it has no place there; an option, row or statement
    picture right after its line; a side-by-side column's after its heading or
    option. A side-by-side column's row pictures have no place here: they are
    sent after the survey text with the question's text."""
    if not entries:
        return Places(None, {})
    prompt_label = str(entries[0].get("prompt_label", "") or "").strip() or "the survey"
    own = [entry for entry in entries if entry.get("context") == "question" and not entry.get("sub_index")]
    text: str | None = None
    if own:
        markers = {
            number: picture_marker(
                entry["key"],
                _picture_label(f"Image {number} of {len(own)} attached to {prompt_label}" if len(own) > 1 else f"Image attached to {prompt_label}", entry),
            )
            for number, entry in enumerate(own, start=1)
        }
        placed: set[int] = set()

        def place(match: re.Match[str]) -> str:
            number = int(match.group(1))
            if number in markers:
                placed.add(number)
                return markers[number]
            return ""

        written = shown.picture_text or shown.text
        written = _PICTURE_PLACE_RE.sub(place, written)
        written = keep_lines(written) if shown.lines else strip_html(written)
        before = [marker for number, marker in markers.items() if number not in placed]
        if before:
            if written.strip() == IMAGE_STIMULUS:
                written = ""
            written = "\n".join([*before, written]).strip()
        text = written
    items: dict[tuple[Any, ...], list[str]] = {}
    for entry in entries:
        context = str(entry.get("context", "") or "")
        label = str(entry.get("label", "") or "").strip()
        if entry.get("sub_index"):
            column = int(entry["sub_index"])
            if context == "question":
                name = next((c["label"] for i, c in enumerate(_group_columns(shown), 1) if i == column), "") or f"Column {column}"
                key: tuple[Any, ...] = ("column", column)
                marker = picture_marker(entry["key"], _picture_label(f"Image for column '{name}' in {prompt_label}", entry))
            elif context == "option":
                key = ("column option", column, int(entry["option_index"]))
                marker = picture_marker(entry["key"], _picture_label(f"Image for option '{label}' in {prompt_label}", entry))
            else:
                continue
        elif context in {"option", "row", "statement"}:
            index = int(entry.get(f"{context}_index") or 0)
            key = (context, index)
            marker = picture_marker(entry["key"], _picture_label(f"Image for {context} '{label}' in {prompt_label}", entry))
        else:
            continue
        items.setdefault(key, []).append(marker)
    return Places(text, items)


def _option_labels_and_media(shown: Shown) -> tuple[list[str], list[list[dict[str, str]]]]:
    if shown.kind == "choice":
        return [o.label.strip() for o in shown.options], [o.media for o in shown.options]
    if shown.kind == "ranking":
        return [i.label.strip() for i in shown.rank_items], [i.media for i in shown.rank_items]
    if shown.kind in {"grid", "grid_text", "grid_sum"}:
        return [c.label.strip() for c in shown.columns if c.label.strip()], [c.media for c in shown.columns]
    return [], []


def _row_labels_and_media(shown: Shown) -> tuple[list[str], list[list[dict[str, str]]]]:
    rows = shown.rows if shown.kind in {"grid", "grid_text", "grid_sum", "form", "allocation"} else []
    media = [r.media for r in rows]
    if not any(media):
        return [], media
    return [r.label.strip() or f"Row {index}" for index, r in enumerate(rows, start=1)], media


def _statement_labels_and_media(shown: Shown) -> tuple[list[str], list[list[dict[str, str]]]]:
    statements = shown.statements if shown.kind == "slider" and shown.response.get("type") != "number" else []
    media = [s.media for s in statements]
    if not any(media):
        return [], media
    return [s.label.strip() or f"Statement {index}" for index, s in enumerate(statements, start=1)], media


def sequence_text(questions: list[dict[str, Any]]) -> str:
    """The survey text of a prompt: each question with its options, numbered
    Q1, Q2, ... (text only is not numbered), and after each question the marker
    where its answer details go (``prompts.layout``)."""
    chunks: list[str] = []
    number = 0
    for record in questions:
        shown = Shown(record)
        if shown.kind is None:
            continue
        index = None if shown.kind == "instruction" else number + 1
        rendered = sanitize_rendered_text(_formatted(shown, _places(shown, _media_entries(shown, index))))
        if not rendered and shown.media:
            rendered = IMAGE_STIMULUS
        if shown.kind == "instruction":
            chunks.append(rendered)
        else:
            number += 1
            chunk = f"Q{number}:\n{rendered}"
            if prompt_layout.markers_wanted():
                # where this question's answer details go (prompts.layout)
                chunk += "\n" + prompt_layout.answer_marker(shown.id, f"Q{number}")
            chunks.append(chunk)
    return "\n\n".join(chunk for chunk in chunks if chunk).strip()


def question_media_entries(record: dict[str, Any], number: int | None = None) -> list[dict[str, str]]:
    """The pictures one shown question brings to the survey text, each with where
    it appears (``number``: its Q number, None for text only)."""
    return _media_entries(Shown(record), number)


def _media_entries(shown: Shown, number: int | None) -> list[dict[str, str]]:
    label_base = f"Q{number}" if number is not None else (shown.id or "Descriptive block")
    text = shown.prompt_text
    entries: list[dict[str, str]] = []
    qid = shown.id
    for index, media in enumerate(shown.media, start=1):
        entries.append({**media, "context": "question", "question_id": qid, "prompt_label": label_base, "label": f"{label_base} question image {index}", "question_text": text, "key": f"{qid}~q{index}"})
    if shown.kind != "group":
        labels, option_media = _option_labels_and_media(shown)
        for position, items in enumerate(option_media, start=1):
            label = labels[position - 1] if position - 1 < len(labels) else f"[Image option {position}]"
            for media_index, media in enumerate(items, start=1):
                entries.append(
                    {**media, "context": "option", "question_id": qid, "prompt_label": label_base, "label": label, "option_index": str(position), "media_index": str(media_index), "question_text": text,
                     "key": f"{qid}~o{position}.{media_index}"}
                )
    labels, row_media = _row_labels_and_media(shown)
    for position, items in enumerate(row_media, start=1):
        label = labels[position - 1] if position - 1 < len(labels) else f"[Image row {position}]"
        for media_index, media in enumerate(items, start=1):
            entries.append({**media, "context": "row", "question_id": qid, "prompt_label": label_base, "label": label, "row_index": str(position), "media_index": str(media_index), "question_text": text,
                            "key": f"{qid}~r{position}.{media_index}"})
    labels, statement_media = _statement_labels_and_media(shown)
    for position, items in enumerate(statement_media, start=1):
        label = labels[position - 1] if position - 1 < len(labels) else f"[Image statement {position}]"
        for media_index, media in enumerate(items, start=1):
            entries.append(
                {**media, "context": "statement", "question_id": qid, "prompt_label": label_base, "label": label, "statement_index": str(position), "media_index": str(media_index), "question_text": text,
                 "key": f"{qid}~s{position}.{media_index}"}
            )
    if shown.kind == "group":
        for sub, column in enumerate(_group_columns(shown), start=1):
            sub_label = column["label"] or f"Column {column['id']}"
            for media_index, media in enumerate(column["media"], start=1):
                entries.append(
                    {**media, "context": "question", "question_id": qid, "prompt_label": label_base, "label": f"{sub_label} question image {media_index}", "media_index": str(media_index), "question_text": text,
                     "sub_index": str(sub), "key": f"{qid}~c{sub}.q{media_index}"}
                )
            for position, row in enumerate(column["rows"], start=1):
                label = " - ".join(part for part in (sub_label, row.label.strip()) if part)
                for media_index, media in enumerate(row.media, start=1):
                    entries.append(
                        {**media, "context": "row", "question_id": qid, "prompt_label": label_base, "label": label, "row_index": str(position), "media_index": str(media_index), "question_text": text,
                         "sub_index": str(sub), "key": f"{qid}~c{sub}.r{position}.{media_index}"}
                    )
            if not column["is_text"]:
                for position, option in enumerate(column["options"], start=1):
                    label = " - ".join(part for part in (sub_label, option.label.strip()) if part)
                    for media_index, media in enumerate(option.media, start=1):
                        entries.append(
                            {**media, "context": "option", "question_id": qid, "prompt_label": label_base, "label": label, "option_index": str(position), "media_index": str(media_index), "question_text": text,
                             "sub_index": str(sub), "key": f"{qid}~c{sub}.o{position}.{media_index}"}
                        )
    return entries


def text_with_pictures(shown: Shown, entries: list[dict[str, Any]]) -> str:
    """The question's own text with the markers of its pictures (as the survey text
    shows it), for a reader of its pictures away from the survey text: a picture
    of a question answered at an earlier stage, or a rerun. Empty when it marks
    none."""
    text = _places(shown, entries).text or ""
    text = _ED_PLACEHOLDER_RE.sub("", _LM_PLACEHOLDER_RE.sub("", text)).strip()
    return text if "[[image " in text else ""


def sequence_media(questions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The pictures in the survey text, in order, each with where it appears."""
    out: list[dict[str, Any]] = []
    number = 0
    for record in questions:
        shown = Shown(record)
        if shown.kind is None:
            continue
        index = None if shown.kind == "instruction" else number + 1
        entries = _media_entries(shown, index)
        if entries:
            with_pictures = text_with_pictures(shown, entries)
            out.append(
                {
                    "question_id": shown.id,
                    "question_type": _qualtrics_type(shown),
                    "question_text": shown.prompt_text,
                    **({"question_text_with_pictures": with_pictures} if with_pictures else {}),
                    "prompt_label": f"Q{index}" if index is not None else (shown.id or "Descriptive block"),
                    "media": entries,
                }
            )
        if shown.kind != "instruction":
            number += 1
    return out


def _qualtrics_type(shown: Shown) -> str:
    """The question's type as the media list names it (the Qualtrics names)."""
    return {
        "instruction": "DB",
        "choice": "MC",
        "text": "TE",
        "form": "TE",
        "grid": "MATRIX",
        "grid_text": "MATRIX",
        "grid_sum": "MATRIX",
        "slider": "SLIDER",
        "allocation": "CS",
        "ranking": "RANK",
        "group": "SBS",
    }.get(shown.kind or "", "")
