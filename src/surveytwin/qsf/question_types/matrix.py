from typing import Any, Dict

from .base import default_order, extract_image_media, get_common_settings, strip_html


def _matrix_settings(payload: Dict[str, Any]) -> dict[str, Any]:
    settings = get_common_settings(payload)
    validation_settings = payload.get("Validation", {}).get("Settings", {})
    if not isinstance(validation_settings, dict):
        validation_settings = {}

    for source_key, output_key in (
        ("ForceResponseType", "ForceResponseType"),
        ("Type", "ValidationType"),
        ("MinChoices", "MinChoices"),
        ("MaxChoices", "MaxChoices"),
        ("MinChars", "MinChars"),
        ("TotalChars", "TotalChars"),
        ("MaxChars", "MaxChars"),
        ("ChoiceTotal", "ChoiceTotal"),
        ("ContentType", "ContentType"),
    ):
        value = validation_settings.get(source_key)
        if value not in (None, ""):
            settings[output_key] = value

    return settings


def _ordered_items(source: Any, order: Any) -> list[tuple[str, Any]]:
    if not isinstance(source, dict):
        return []
    ordered_ids = [str(item) for item in order] if isinstance(order, list) else []
    if not ordered_ids:
        ordered_ids = [str(item) for item in source.keys()]
    return [(item_id, source[item_id]) for item_id in ordered_ids if item_id in source]


def _normalize_scale_label(answer_id: str, answer_text: str, position: int) -> str:
    if answer_text:
        return answer_text
    return str(answer_id or position)


def _matrix_answer_items(payload: Dict[str, Any]) -> list[tuple[str, Any]]:
    answers = payload.get("Answers", {})
    answer_order = payload.get("AnswerOrder", [])
    ordered_answers = _ordered_items(answers, answer_order)
    if not ordered_answers:
        return []

    # Profile matrix QSF stores the answer scale inside each row-id entry:
    # Answers: {"row_id": {"scale_id": {"Display": "..."}}}. Use the first
    # row's scale so runtime gets the same option list as ordinary Likert.
    first_answer_payload = ordered_answers[0][1]
    if isinstance(first_answer_payload, dict) and "Display" not in first_answer_payload:
        nested_items = _ordered_items(first_answer_payload, answer_order)
        if nested_items:
            return nested_items

    return ordered_answers


def _split_bipolar_endpoints(row_text: str) -> tuple[str, str] | None:
    parts = [part.strip() for part in str(row_text or "").split(":", 1)]
    if len(parts) != 2 or not parts[0] or not parts[1]:
        return None
    return parts[0], parts[1]


def _format_bipolar_row(row_text: str, first_column: str, last_column: str) -> str:
    endpoints = _split_bipolar_endpoints(row_text)
    if endpoints is None:
        return row_text

    one_end, another_end = endpoints
    if first_column and last_column:
        return (
            f"{one_end} to {another_end} "
            f"(scale: {first_column} = more {one_end}; {last_column} = more {another_end})"
        )
    return f"{one_end} to {another_end}"


def extract_matrix_question_data(
    payload: Dict[str, Any], survey_brand_id: str = ""
) -> Dict[str, Any]:
    """Extract data specific to Matrix type questions"""
    question_data = {
        "Rows": [],
        "Columns": [],
        "RowsID": [],
        "ColumnsID": [],
        "RowsMedia": [],
        "ColumnsMedia": [],
        "Settings": _matrix_settings(payload),
    }

    # Extract the statements (rows)
    if "Choices" in payload:
        choices = payload.get("Choices", {})
        choice_order = payload.get("ChoiceOrder", [])
        for choice_id, choice_data in _ordered_items(choices, choice_order):
            if isinstance(choice_data, dict):
                choice_text_raw = choice_data.get("Display", "")
                choice_text = strip_html(choice_text_raw)
                choice_media = extract_image_media(choice_data, survey_brand_id=survey_brand_id)
                if not choice_text and choice_media:
                    choice_text = f"[Image row {len(question_data['Rows']) + 1}]"
                question_data["Rows"].append(choice_text)
                question_data["RowsID"].append(str(choice_id))
                question_data["RowsMedia"].append(choice_media)

    # Extract the answer scale (columns)
    if "Answers" in payload:
        for position, (answer_id, answer_data) in enumerate(
            _matrix_answer_items(payload), start=1
        ):
            if isinstance(answer_data, dict):
                answer_text_raw = answer_data.get("Display", "")
                answer_text = strip_html(answer_text_raw)
                answer_media = extract_image_media(answer_data, survey_brand_id=survey_brand_id)
                if not answer_text and answer_media:
                    answer_text = f"[Image option {len(question_data['Columns']) + 1}]"
                question_data["Columns"].append(
                    _normalize_scale_label(str(answer_id), answer_text, position)
                )
                question_data["ColumnsID"].append(str(answer_id))
                question_data["ColumnsMedia"].append(answer_media)

        if "Selector" in payload and payload["Selector"] == "Bipolar":
            first_column = (
                str(question_data["Columns"][0]).strip() if question_data["Columns"] else ""
            )
            last_column = (
                str(question_data["Columns"][-1]).strip() if question_data["Columns"] else ""
            )
            formatted_rows = [
                _format_bipolar_row(row_text, first_column, last_column)
                for row_text in question_data["Rows"]
            ]
            question_data["Rows"] = formatted_rows

    if payload.get("Selector") == "CS":
        configuration = payload.get("Configuration", {})
        if isinstance(configuration, dict):
            question_data["Range"] = {
                "Min": configuration.get("CSSliderMin", 0),
                "Max": configuration.get("CSSliderMax", 100),
            }

    if default_order(question_data["ColumnsID"]) and not payload.get("RecodeValues"):
        question_data.pop("ColumnsID")  # if not present, the options are in default order

    if default_order(question_data["RowsID"]):
        question_data.pop("RowsID")  # if not present, the options are in default order
    if not any(question_data["RowsMedia"]):
        question_data.pop("RowsMedia")
    if not any(question_data["ColumnsMedia"]):
        question_data.pop("ColumnsMedia")

    return question_data
