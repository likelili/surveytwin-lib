"""Draft questions to survey template questions (the shape every other step reads).

The encoding follows the website's ``draft_to_template``, with one change: a
question's own ID (``Q1``) becomes its export tag (``QuestionName``, which names
its columns in the CSV files) and the question gets an internal ``QID<n>`` ID,
as in a Qualtrics export. The website uses ``Q1`` for both, and those IDs
collide with the ``Q1:`` labels the prompt gives questions.

| draft type | template question |
|---|---|
| instruction | ``DB``, descriptive text; an image-only instruction reads "[Image stimulus attached]" |
| single_choice, scale | ``MC``, selector ``SAVR`` (a scale is a labeled single choice) |
| multi_select | ``MC``, selector ``MAVR``; ``MinChoices``/``MaxChoices`` when set above 0 |
| grid | ``Matrix``, ``Likert``/``SingleAnswer``; rows are statements, options the scale points |
| open_text | ``TE``, selector ``ML``; ``MinChars``/``TotalChars`` when set above 0 |
| number | ``Slider``, ``HSLIDER``, one unlabeled statement; range 0 to 100 unless set |

``required`` becomes ``ForceResponse`` ``ON``/``OFF``, and each question records
its validation type as a Qualtrics export does (``ChoiceRange``, ``CharRange``,
... or ``None``), so a built survey behaves exactly like the equivalent QSF.
Blank options and rows are dropped. ``rationale`` never enters the template.
"""

from __future__ import annotations

from typing import Any

from .draft import DraftQuestion
from .images import ImageResolver, resolve_image
from .validate import DEFAULT_MAX_VALUE, DEFAULT_MIN_VALUE

SURVEY_SOURCE = "survey_builder"


def _number(value: Any) -> int | float:
    """Whole numbers as ints, so a range prints "0 to 100" as a parsed QSF's does."""
    number = float(value)
    return int(number) if number.is_integer() else number


def _validation_type(low: bool, high: bool, low_name: str, high_name: str, both_name: str) -> str:
    """Qualtrics' validation type for a minimum, a maximum, or both."""
    if low and high:
        return both_name
    if low:
        return low_name
    if high:
        return high_name
    return "None"


def question_to_template(
    question: DraftQuestion,
    qid: str,
    *,
    image_resolver: ImageResolver | None = None,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """(template question, image references that did not resolve)."""
    q = question
    text = str(q.text).strip()
    tag = str(q.id).strip()
    base: dict[str, Any] = {"QuestionID": qid, "QuestionName": tag, "QuestionText": text}
    unresolved: list[dict[str, Any]] = []
    reference = str(q.image_id).strip()
    if reference:
        src, why = resolve_image(reference, image_resolver)
        if src:
            # The uploaded file's name stays in the draft: a respondent never sees
            # it, and a name like "high_price_version.png" would tell the twin what
            # the picture is meant to show.
            base["QuestionMedia"] = [{"kind": "image", "src": src}]
        else:
            unresolved.append(
                {"question_id": qid, "question": tag, "reference": reference, "name": str(q.image_name).strip(), "detail": why}
            )
    force = "ON" if q.required is True else "OFF"

    if q.type == "instruction":
        if not text and base.get("QuestionMedia"):
            base["QuestionText"] = "[Image stimulus attached]"
        return {**base, "QuestionType": "DB", "is_descriptive": True, "Settings": {"Selector": "TB", "SubSelector": "", "ValidationType": "None"}}, unresolved

    if q.type in {"single_choice", "scale"}:
        options = [o.strip() for o in q.options if o.strip()]
        settings = {"Selector": "SAVR", "SubSelector": "TX", "ForceResponse": force, "ValidationType": "None"}
        return {**base, "QuestionType": "MC", "Options": options, "Settings": settings}, unresolved

    if q.type == "multi_select":
        options = [o.strip() for o in q.options if o.strip()]
        settings = {"Selector": "MAVR", "SubSelector": "TX", "ForceResponse": force}
        if q.min_selections:
            settings["MinChoices"] = str(q.min_selections)
        if q.max_selections:
            settings["MaxChoices"] = str(q.max_selections)
        settings["ValidationType"] = _validation_type(bool(q.min_selections), bool(q.max_selections), "MinChoices", "MaxChoices", "ChoiceRange")
        return {**base, "QuestionType": "MC", "Options": options, "Settings": settings}, unresolved

    if q.type == "grid":
        rows = [r.strip() for r in q.rows if r.strip()]
        columns = [o.strip() for o in q.options if o.strip()]
        settings = {"Selector": "Likert", "SubSelector": "SingleAnswer", "ForceResponse": force, "ValidationType": "None"}
        return {**base, "QuestionType": "Matrix", "Rows": rows, "Columns": columns, "Settings": settings}, unresolved

    if q.type == "open_text":
        settings = {"Selector": "ML", "ForceResponse": force, "ContentType": ""}
        settings["ValidationType"] = _validation_type(bool(q.min_chars), bool(q.max_chars), "MinChar", "TotalChar", "CharRange")
        if q.min_chars:
            settings["MinChars"] = str(q.min_chars)
        if q.max_chars:
            settings["TotalChars"] = str(q.max_chars)
        return {**base, "QuestionType": "TE", "Settings": settings}, unresolved

    if q.type == "number":
        low = _number(q.min_value if q.min_value is not None else DEFAULT_MIN_VALUE)
        high = _number(q.max_value if q.max_value is not None else DEFAULT_MAX_VALUE)
        settings = {"Selector": "HSLIDER", "ForceResponse": force, "ValidationType": "None"}
        if q.decimals is not None:
            settings["NumericDecimals"] = int(q.decimals)
        if q.step is not None:
            settings["NumericStep"] = _number(q.step)
        return (
            {
                **base,
                "QuestionType": "Slider",
                # One unlabeled statement collapses to a single numeric answer
                # whose ID is the question's own.
                "Statements": [""],
                "StatementsID": ["1"],
                "Range": {"Min": low, "Max": high, "Ticks": 10},
                "Settings": settings,
            },
            unresolved,
        )

    raise ValueError(f"Unsupported draft question type: {q.type}")


def block_element(block_id: str, block_name: str, questions: list[dict[str, Any]]) -> dict[str, Any]:
    return {"ElementType": "Block", "BlockID": block_id, "BlockName": block_name, "BlockType": "Standard", "Questions": questions}


def survey_template(title: str, blocks: list[dict[str, Any]]) -> dict[str, Any]:
    return {"Metadata": {"SurveyName": str(title).strip() or "Untitled survey", "SurveySource": SURVEY_SOURCE}, "Elements": blocks}
