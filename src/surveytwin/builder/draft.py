"""The survey draft: what a person (or a web form, or a model) writes to author a survey.

A draft is a title and an ordered list of questions, in the same JSON shape the
SurveyTwin website's survey builder sends (``title`` plus ``questions``, each
question with the website's 18 fields). ``SurveyDraft.from_dict`` accepts that
payload; ``to_dict`` returns it with every field, as the website does.

A draft is only an input: ``build_survey`` turns it into a ``Survey``, which is
what everything else runs on.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field, fields
from typing import Any

QUESTION_TYPES = ("single_choice", "multi_select", "scale", "grid", "open_text", "number", "instruction")
ANSWERABLE_TYPES = frozenset(QUESTION_TYPES) - {"instruction"}
CHOICE_TYPES = frozenset({"single_choice", "multi_select", "scale"})

# The website's four scale presets (``survey_draft.SCALE_PRESETS``).
SCALE_PRESETS: dict[str, list[str]] = {
    "agreement_5": [
        "Strongly disagree",
        "Somewhat disagree",
        "Neither agree nor disagree",
        "Somewhat agree",
        "Strongly agree",
    ],
    "agreement_7": [
        "Strongly disagree",
        "Disagree",
        "Somewhat disagree",
        "Neither agree nor disagree",
        "Somewhat agree",
        "Agree",
        "Strongly agree",
    ],
    "satisfaction_5": [
        "Very dissatisfied",
        "Somewhat dissatisfied",
        "Neither satisfied nor dissatisfied",
        "Somewhat satisfied",
        "Very satisfied",
    ],
    "likelihood_5": [
        "Very unlikely",
        "Somewhat unlikely",
        "Neither likely nor unlikely",
        "Somewhat likely",
        "Very likely",
    ],
}

# Field order as the website returns a question (``DraftQuestion.model_dump()``).
DRAFT_FIELDS = (
    "id",
    "type",
    "text",
    "required",
    "options",
    "rows",
    "min_selections",
    "max_selections",
    "min_chars",
    "max_chars",
    "min_value",
    "max_value",
    "step",
    "decimals",
    "image_id",
    "image_name",
    "image_bytes",
    "rationale",
)
# The survey's own internal ID of a question (``QID<n>``). Not one of the website's
# fields: ``to_draft`` fills it so a rebuilt survey keeps every question's ID (and
# so every piped reference its target); a new question leaves it blank.
INTERNAL_FIELDS = ("qid",)
# Survey-level fields a draft from ``to_draft`` carries besides the website's
# title and questions (see ``SurveyDraft``).
SURVEY_CONTEXT_FIELDS = ("next_qid", "block_id", "block_name", "metadata")
INT_FIELDS = ("min_selections", "max_selections", "min_chars", "max_chars", "decimals")
FLOAT_FIELDS = ("min_value", "max_value", "step")


@dataclass
class DraftIssue:
    """One problem with a draft or an edit, placed for a web form.

    ``question_id`` and ``question_index`` locate the question (empty and -1 for
    the whole survey); ``field`` names the draft field (empty for the whole
    question); ``item_index`` points into ``options`` or ``rows`` when one entry
    is at fault. ``code`` is a stable name for the rule.
    """

    severity: str
    message: str
    code: str
    question_id: str = ""
    question_index: int = -1
    field: str = ""
    item_index: int = -1

    def to_dict(self) -> dict[str, Any]:
        return {
            "question_id": self.question_id,
            "question_index": self.question_index,
            "severity": self.severity,
            "message": self.message,
            "field": self.field,
            "item_index": self.item_index,
            "code": self.code,
        }

    def __str__(self) -> str:
        where = []
        if self.question_id:
            where.append(self.question_id)
        elif self.question_index >= 0:
            where.append(f"question {self.question_index + 1}")
        if self.field:
            where.append(self.field + (f"[{self.item_index}]" if self.item_index >= 0 else ""))
        return f"{' '.join(where)}: {self.message}" if where else self.message


@dataclass
class DraftQuestion:
    """One draft question, with the website's fields and defaults.

    Types: ``single_choice``, ``multi_select``, ``scale``, ``grid`` (``rows`` are
    the statements, ``options`` the scale points), ``open_text``, ``number`` and
    ``instruction`` (text or an image shown to the twin, nothing to answer).
    ``rationale`` is authoring metadata and never reaches a twin.
    """

    type: str
    text: str = ""
    id: str = ""
    required: bool = False
    options: list[str] = field(default_factory=list)
    rows: list[str] = field(default_factory=list)
    min_selections: int | None = None
    max_selections: int | None = None
    min_chars: int | None = None
    max_chars: int | None = None
    min_value: float | None = None
    max_value: float | None = None
    step: float | None = None
    decimals: int | None = None
    image_id: str = ""
    image_name: str = ""
    image_bytes: int = 0
    rationale: str = ""
    # The internal ID this question already has in a survey (see INTERNAL_FIELDS).
    qid: str = ""
    # Keys in the payload that are not draft fields; reported by validate_draft, never used.
    unknown_fields: dict[str, Any] = field(default_factory=dict, repr=False, compare=False)

    @classmethod
    def from_dict(cls, data: dict[str, Any] | "DraftQuestion") -> "DraftQuestion":
        """Read one question from a payload. Never raises for bad values:
        ``validate_draft`` reports them with their field."""
        if isinstance(data, DraftQuestion):
            return copy.deepcopy(data)
        if not isinstance(data, dict):
            raise TypeError(f"a draft question must be a dict, got {type(data).__name__}")
        known = {f.name for f in fields(cls)} - {"unknown_fields"}
        values: dict[str, Any] = {}
        for key, value in data.items():
            if key in known:
                values[key] = _coerce(key, value)
        unknown = {k: v for k, v in data.items() if k not in known}
        question = cls(**{"type": values.pop("type", ""), **values})
        question.unknown_fields = unknown
        return question

    def to_dict(self) -> dict[str, Any]:
        """The website's JSON form: all 18 fields, in its order, plus ``qid`` when
        the question has an internal ID already."""
        data = {name: copy.deepcopy(getattr(self, name)) for name in DRAFT_FIELDS}
        if self.qid:
            data["qid"] = copy.deepcopy(self.qid)
        return data


@dataclass
class SurveyDraft:
    """A title and an ordered list of questions.

    ``notes`` holds remarks a caller attaches to the draft; it is not part of
    the payload.
    """

    title: str = "Untitled survey"
    questions: list[DraftQuestion] = field(default_factory=list)
    # Carried from the survey a draft came from (``to_draft``), so building it back
    # gives the same survey: the next internal question number (a new question never
    # takes the ID of one removed or dropped before), and the block's ID and name
    # and the survey's metadata. Not website fields; written only when set.
    next_qid: Any = None
    block_id: Any = ""
    block_name: Any = ""
    metadata: Any = field(default_factory=dict)
    notes: list[DraftIssue] = field(default_factory=list, repr=False, compare=False)
    unknown_fields: dict[str, Any] = field(default_factory=dict, repr=False, compare=False)

    @classmethod
    def from_dict(cls, data: dict[str, Any] | "SurveyDraft") -> "SurveyDraft":
        """Read a draft payload (``{"title": ..., "questions": [...]}``) or copy a draft."""
        if isinstance(data, SurveyDraft):
            return copy.deepcopy(data)
        if not isinstance(data, dict):
            raise TypeError(f"a survey draft must be a dict, got {type(data).__name__}")
        raw_questions = data.get("questions")
        if raw_questions is None:
            raw_questions = []
        if not isinstance(raw_questions, list):
            raise TypeError("the draft's 'questions' must be a list")
        title = data.get("title", "Untitled survey")
        draft = cls(
            title="" if title is None else str(title),
            questions=[DraftQuestion.from_dict(q) for q in raw_questions],
            next_qid=copy.deepcopy(data.get("next_qid")),
            block_id=copy.deepcopy(data.get("block_id", "")),
            block_name=copy.deepcopy(data.get("block_name", "")),
            metadata=copy.deepcopy(data.get("metadata", {})),
        )
        draft.unknown_fields = {k: v for k, v in data.items() if k not in {"title", "questions", *SURVEY_CONTEXT_FIELDS}}
        return draft

    def to_dict(self) -> dict[str, Any]:
        """The website's JSON form, plus the fields ``to_draft`` carries (when set)."""
        data: dict[str, Any] = {"title": self.title, "questions": [q.to_dict() for q in self.questions]}
        for name in SURVEY_CONTEXT_FIELDS:
            value = getattr(self, name)
            if value not in (None, "", {}):
                data[name] = copy.deepcopy(value)
        return data

    def copy(self) -> "SurveyDraft":
        return copy.deepcopy(self)

    def __str__(self) -> str:
        lines = [f"Draft {self.title!r}, {len(self.questions)} question(s):"]
        for q in self.questions:
            extra = ""
            if q.type in CHOICE_TYPES or q.type == "grid":
                extra = f" ({len([o for o in q.options if str(o).strip()])} options)"
            if q.type == "grid":
                extra = f" ({len([r for r in q.rows if str(r).strip()])} rows x {len([o for o in q.options if str(o).strip()])} points)"
            image = f" [image {q.image_id}]" if str(q.image_id).strip() else ""
            lines.append(f"  {q.id or '(no id)':<8} {q.type:<13} {str(q.text)[:70]!r}{extra}{image}")
        lines += [f"  note: {n}" for n in self.notes]
        return "\n".join(lines)


def as_draft(draft: "SurveyDraft | dict[str, Any]") -> SurveyDraft:
    """A copy of ``draft`` as a ``SurveyDraft`` (the caller's object is never changed)."""
    return SurveyDraft.from_dict(draft)


def _coerce(key: str, value: Any) -> Any:
    """The lenient conversions the website's model accepts (numeric strings, whole
    floats as whole numbers, true/false spelled t/f, y/n, on/off, 1/0, a boolean
    as a number); anything else is kept as given so validation can name it.
    Stricter than the website on one point: "inf" and "nan" are not numbers here."""
    if key in INT_FIELDS:
        if value is None or value == "":
            return None
        if isinstance(value, bool):
            return int(value)
        if isinstance(value, int):
            return value
        if isinstance(value, float) and value.is_integer():
            return int(value)
        if isinstance(value, str):
            text = value.strip()
            try:
                return int(text)
            except ValueError:
                try:
                    number = float(text)
                except ValueError:
                    return value
                return int(number) if number.is_integer() else value
        return value
    if key in FLOAT_FIELDS:
        if value is None or value == "":
            return None
        if isinstance(value, bool):
            return float(value)
        if isinstance(value, (int, float)):
            return value
        if isinstance(value, str):
            try:
                return float(value.strip())
            except ValueError:
                return value
        return value
    if key == "image_bytes":
        if value is None or value == "":
            return 0
        if isinstance(value, float) and value.is_integer():
            return int(value)
        if isinstance(value, str) and value.strip().isdigit():
            return int(value.strip())
        return value
    if key == "required":
        truthy = {"true", "t", "1", "yes", "y", "on"}
        falsy = {"false", "f", "0", "no", "n", "off"}
        if isinstance(value, str) and value.strip().lower() in truthy | falsy:
            return value.strip().lower() in truthy
        if isinstance(value, (int, float)) and not isinstance(value, bool) and value in (0, 1):
            return bool(value)
        return value
    if key in ("options", "rows"):
        if value is None:
            return []
        if isinstance(value, (list, tuple)):
            # numbers become text; an object or a list stays as it is, for validation to name
            return ["" if item is None else (str(item) if isinstance(item, (int, float)) and not isinstance(item, bool) else item) for item in value]
        return value
    if key in ("id", "text", "image_id", "image_name", "rationale", "type", "qid"):
        if value is None:
            return ""
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return str(value)
        return value
    return value
