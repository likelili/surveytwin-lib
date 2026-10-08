"""Write a Qualtrics QSF by hand, in the payload shapes real exports use (taken from
the fixtures), for questions a survey-builder draft can also express. Tests use
it to check that a built survey and the equivalent QSF behave the same."""

from __future__ import annotations

from typing import Any


def _choices(labels: list[str]) -> tuple[dict[str, Any], list[int]]:
    return {str(i): {"Display": label} for i, label in enumerate(labels, start=1)}, list(range(1, len(labels) + 1))


def _payload(q: dict[str, Any], qid: str) -> dict[str, Any]:
    required = "ON" if q.get("required") else "OFF"
    base = {
        "QuestionText": q.get("text", ""),
        "DefaultChoices": False,
        "DataExportTag": q["id"],
        "Configuration": {"QuestionDescriptionOption": "UseText"},
        "QuestionID": qid,
    }
    kind = q["type"]
    if kind == "instruction":
        return {**base, "QuestionType": "DB", "Selector": "TB", "ChoiceOrder": [], "Validation": {"Settings": {"Type": "None"}}}
    if kind in {"single_choice", "scale"}:
        choices, order = _choices(q["options"])
        return {**base, "QuestionType": "MC", "Selector": "SAVR", "SubSelector": "TX", "Choices": choices, "ChoiceOrder": order,
                "Validation": {"Settings": {"ForceResponse": required, "Type": "None"}}}
    if kind == "multi_select":
        choices, order = _choices(q["options"])
        settings: dict[str, Any] = {"ForceResponse": required, "Type": "None"}
        if q.get("min_selections") or q.get("max_selections"):
            settings["Type"] = "ChoiceRange"
            if q.get("min_selections"):
                settings["MinChoices"] = str(q["min_selections"])
            if q.get("max_selections"):
                settings["MaxChoices"] = str(q["max_selections"])
        return {**base, "QuestionType": "MC", "Selector": "MAVR", "SubSelector": "TX", "Choices": choices, "ChoiceOrder": order,
                "Validation": {"Settings": settings}}
    if kind == "grid":
        rows, row_order = _choices(q["rows"])
        answers, answer_order = _choices(q["options"])
        return {**base, "QuestionType": "Matrix", "Selector": "Likert", "SubSelector": "SingleAnswer", "Choices": rows, "ChoiceOrder": row_order,
                "Answers": answers, "AnswerOrder": answer_order, "Validation": {"Settings": {"ForceResponse": required, "Type": "None"}}}
    if kind == "open_text":
        settings = {"ForceResponse": required, "Type": "None"}
        if q.get("min_chars") or q.get("max_chars"):
            settings["Type"] = "CharRange"
            if q.get("min_chars"):
                settings["MinChars"] = str(q["min_chars"])
            if q.get("max_chars"):
                settings["TotalChars"] = str(q["max_chars"])
        return {**base, "QuestionType": "TE", "Selector": "ML", "Validation": {"Settings": settings}}
    if kind == "number":
        config = {"QuestionDescriptionOption": "UseText", "CSSliderMin": q.get("min_value", 0), "CSSliderMax": q.get("max_value", 100),
                  "GridLines": 10, "NumDecimals": q.get("decimals", 0), "ShowValue": True, "SnapToGrid": False, "CustomStart": False, "NotApplicable": False}
        return {**base, "QuestionType": "Slider", "Selector": "HSLIDER", "Configuration": config, "Choices": {"1": {"Display": ""}},
                "ChoiceOrder": ["1"], "Validation": {"Settings": {"ForceResponse": required, "Type": "None"}}}
    raise ValueError(kind)


def write_qsf(title: str, questions: list[dict[str, Any]], *, block_id: str = "BL_BUILDER") -> dict[str, Any]:
    """A QSF dict with one block holding these draft-style questions (QID1, QID2, ... in order)."""
    elements: list[dict[str, Any]] = [
        {"SurveyID": "SV_EXAMPLE", "Element": "BL", "PrimaryAttribute": "Survey Blocks", "SecondaryAttribute": None, "TertiaryAttribute": None,
         "Payload": {"1": {"Type": "Standard", "SubType": "", "Description": title, "ID": block_id,
                           "BlockElements": [{"Type": "Question", "QuestionID": f"QID{i}"} for i in range(1, len(questions) + 1)]}}},
        {"SurveyID": "SV_EXAMPLE", "Element": "FL", "PrimaryAttribute": "Survey Flow", "SecondaryAttribute": None, "TertiaryAttribute": None,
         "Payload": {"Flow": [{"ID": block_id, "Type": "Standard", "FlowID": "FL_2"}], "Properties": {"Count": 2}, "FlowID": "FL_1", "Type": "Root"}},
    ]
    for i, q in enumerate(questions, start=1):
        elements.append({"SurveyID": "SV_EXAMPLE", "Element": "SQ", "PrimaryAttribute": f"QID{i}", "SecondaryAttribute": q.get("text", ""),
                         "TertiaryAttribute": None, "Payload": _payload(q, f"QID{i}")})
    return {"SurveyEntry": {"SurveyID": "SV_EXAMPLE", "SurveyName": title, "SurveyLanguage": "EN"}, "SurveyElements": elements}
