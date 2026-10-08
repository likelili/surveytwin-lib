from typing import Any, Dict

from .base import default_order, extract_image_media, strip_html

_SIMPLE_TEXT_SELECTORS = {"SL", "ML", "ESTB", "PW"}


def _extract_single_text_default(default_choices: dict[str, Any]) -> list[str] | None:
    """Return a normalized one-item default list for simple text entry."""
    if not isinstance(default_choices, dict):
        return None

    text_value: Any = None
    text_payload = default_choices.get("TEXT")
    if isinstance(text_payload, dict):
        text_value = text_payload.get("Text")

    if text_value is None:
        for choice_value in default_choices.values():
            if not isinstance(choice_value, dict):
                continue
            nested_text = choice_value.get("TEXT")
            if isinstance(nested_text, dict) and nested_text.get("Text") is not None:
                text_value = nested_text.get("Text")
                break

    if text_value is None:
        return None

    return [strip_html(str(text_value))]


def _text_validation_settings(payload: Dict[str, Any], selector: str) -> dict[str, Any]:
    validation_settings = payload.get("Validation", {}).get("Settings", {})
    if not isinstance(validation_settings, dict):
        validation_settings = {}

    settings = {
        "Selector": selector,
        "ForceResponse": validation_settings.get("ForceResponse", ""),
        "ContentType": validation_settings.get("ContentType", ""),
    }
    for source_key, output_key in (
        ("ForceResponseType", "ForceResponseType"),
        ("Type", "ValidationType"),
        ("MinChars", "MinChars"),
        ("TotalChars", "TotalChars"),
        ("MaxChars", "MaxChars"),
    ):
        value = validation_settings.get(source_key)
        if value not in (None, ""):
            settings[output_key] = value
    return settings


def extract_text_question_data(payload: Dict[str, Any], survey_brand_id: str = "") -> Dict[str, Any]:
    """Extract data specific to Text Entry questions"""
    selector = payload.get("Selector", "")
    if selector in _SIMPLE_TEXT_SELECTORS:
        ## SL: Single Line Text Entry
        ## ML: Multi Line Text Entry
        ## ESTB: Essay Text Box
        ## PW: Password text box; simulated as ordinary text.
        question_data = {
            "Settings": _text_validation_settings(payload, selector)
        }
        default_choices = _extract_single_text_default(payload.get("DefaultChoices", {}))
        if default_choices:
            question_data["DefaultChoices"] = default_choices
    elif selector == "FORM":
        question_data = {
            "Rows": [],
            "RowsID": [],
            "RowsMedia": [],
            "Settings": _text_validation_settings(payload, selector),
        }
        if "Choices" in payload:
            choices = payload.get("Choices", {})
            choice_order = payload.get("ChoiceOrder", [])
            for choice_id in choice_order:
                if str(choice_id) in choices:
                    choice_info = choices[str(choice_id)]
                    if isinstance(choice_info, dict):
                        choice_text_raw = choice_info.get("Display", "")
                        choice_text = strip_html(choice_text_raw)
                        choice_media = extract_image_media(choice_info, survey_brand_id=survey_brand_id)
                        if not choice_text and choice_media:
                            choice_text = f"[Image field {len(question_data['Rows']) + 1}]"
                        question_data["Rows"].append(choice_text)
                        question_data["RowsID"].append(str(choice_id))
                        question_data["RowsMedia"].append(choice_media)

            if default_order(question_data["RowsID"]):
                question_data.pop("RowsID")  # if not present, the options are in default order
            if not any(question_data["RowsMedia"]):
                question_data.pop("RowsMedia")

            if "DefaultChoices" in payload and isinstance(payload.get("DefaultChoices"), dict):
                question_data["DefaultChoices"] = []
                default_choices = payload.get("DefaultChoices", [])
                for choice_id in choice_order:
                    if str(choice_id) in choices:
                        question_data["DefaultChoices"].append(None)
                        if str(choice_id) in default_choices:
                            question_data["DefaultChoices"][-1] = default_choices[str(choice_id)][
                                "TEXT"
                            ]["Text"]
                if all(item is None for item in question_data["DefaultChoices"]):
                    question_data.pop("DefaultChoices")
    else:
        raise ValueError(f"Unknown selector for text question: {selector}")
    return question_data
