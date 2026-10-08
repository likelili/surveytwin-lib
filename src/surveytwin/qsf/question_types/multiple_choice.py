from typing import Any, Dict

from .base import default_order, extract_image_media, get_common_settings, strip_html
from .common import extract_max_choices_from_custom_validation


def extract_choice_question_data(
    payload: Dict[str, Any], survey_brand_id: str = ""
) -> Dict[str, Any]:
    """Extract data specific to Multiple Choice questions"""
    settings = get_common_settings(payload)
    validation_settings = payload.get("Validation", {}).get("Settings", {})
    if isinstance(validation_settings, dict):
        min_choices = str(validation_settings.get("MinChoices", "") or "").strip()
        if min_choices:
            settings["MinChoices"] = min_choices
        max_choices = str(validation_settings.get("MaxChoices", "") or "").strip()
        if not max_choices:
            max_choices = _extract_max_choices(validation_settings)
        if max_choices:
            settings["MaxChoices"] = max_choices

    question_data = {
        "Options": [],
        "OptionsID": [],
        "OptionMedia": [],
        "TextEntryOptions": [],
        "Settings": settings,
    }

    if "Choices" in payload:
        choices = payload.get("Choices", {})
        choice_order = payload.get("ChoiceOrder", [])
        analyze_choices = payload.get("AnalyzeChoices", {})
        if not isinstance(analyze_choices, dict):
            analyze_choices = {}
        default_choice_ids = _extract_default_choice_ids(payload.get("DefaultChoices"))
        included_raw_choice_ids = []

        for choice_id in choice_order:
            raw_choice_id = str(choice_id)
            if raw_choice_id in choices:
                if _is_non_analyzed_choice(analyze_choices.get(raw_choice_id)):
                    continue
                choice_info = choices[raw_choice_id]
                if isinstance(choice_info, dict):
                    choice_text_raw = choice_info.get("Display", "")
                    choice_text = strip_html(choice_text_raw)
                    choice_media = extract_image_media(choice_info, survey_brand_id=survey_brand_id)
                    if not choice_text and choice_media:
                        choice_text = f"[Image option {len(question_data['Options']) + 1}]"
                    if not choice_text:
                        # An unlabeled scale point. Qualtrics leaves the middle
                        # points of an endpoint-anchored Likert scale blank
                        # ("1 Strongly disagree", "", "", ..., "7 Strongly
                        # agree"), which would reach the model as an EMPTY
                        # option string. Since a respondent is instructed to
                        # copy an option verbatim, an empty option cannot be
                        # chosen: the model can only ever answer at the
                        # endpoints, and everything else is normalized away as
                        # out_of_range. Fall back to the scale position so the
                        # midpoint is expressible. Export values are unaffected —
                        # they come from OptionsID/recode, not from this label.
                        choice_text = str(len(question_data["Options"]) + 1)
                    question_data["Options"].append(choice_text)
                    question_data["OptionsID"].append(raw_choice_id)
                    question_data["OptionMedia"].append(choice_media)
                    included_raw_choice_ids.append(raw_choice_id)
                    if _is_truthy(choice_info.get("TextEntry")):
                        question_data["TextEntryOptions"].append(
                            {"OptionID": raw_choice_id, "OptionText": choice_text}
                        )

        default_choices = []
        for idx, raw_option_id in enumerate(included_raw_choice_ids):
            if raw_option_id not in default_choice_ids or idx >= len(question_data["Options"]):
                continue
            default_choices.append(
                {
                    "OptionID": raw_option_id,
                    "OptionText": question_data["Options"][idx],
                }
            )
        if default_choices:
            question_data["DefaultChoices"] = default_choices

        randomization = payload.get("Randomization")
        if isinstance(randomization, dict) and randomization:
            question_data["Randomization"] = randomization

        if default_order(question_data["OptionsID"]) and not payload.get("RecodeValues"):
            question_data.pop("OptionsID")  # if not present, the options are in default order
        if not any(question_data["OptionMedia"]):
            question_data.pop("OptionMedia")
        if not question_data["TextEntryOptions"]:
            question_data.pop("TextEntryOptions")
    return question_data


def _is_truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _is_non_analyzed_choice(value: Any) -> bool:
    return str(value or "").strip().lower() in {"0", "false", "no", "off"}


def _extract_default_choice_ids(value: Any) -> set[str]:
    if not isinstance(value, dict):
        return set()
    selected: set[str] = set()
    for option_id, payload in value.items():
        if isinstance(payload, dict) and _is_truthy(payload.get("Selected")):
            selected.add(str(option_id))
    return selected


def _extract_max_choices(validation_settings: Dict[str, Any]) -> str:
    """Extract Qualtrics max-selected-choice validation when present."""
    return extract_max_choices_from_custom_validation(validation_settings)
