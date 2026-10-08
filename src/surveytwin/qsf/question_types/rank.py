from typing import Any, Dict

from .base import extract_image_media, get_common_settings, strip_html


def _ordered_choice_ids(choices: dict[str, Any], choice_order: list[Any]) -> list[str]:
    if choice_order:
        return [str(choice_id) for choice_id in choice_order if str(choice_id) in choices]
    try:
        return sorted(choices.keys(), key=lambda item: int(str(item)))
    except (TypeError, ValueError):
        return sorted(choices.keys(), key=str)


def _extract_default_ranks(
    default_choices: Any,
    *,
    options_by_id: dict[str, str],
) -> list[dict[str, str]]:
    if not isinstance(default_choices, dict):
        return []

    defaults: list[dict[str, str]] = []
    for option_id, payload in default_choices.items():
        if not isinstance(payload, dict):
            continue
        rank_value = str(payload.get("Value", "") or "").strip()
        option_id_text = str(option_id).strip()
        if not option_id_text or not rank_value:
            continue
        defaults.append(
            {
                "OptionID": option_id_text,
                "OptionText": options_by_id.get(option_id_text, option_id_text),
                "Rank": rank_value,
            }
        )

    def sort_key(item: dict[str, str]) -> tuple[int, str]:
        try:
            return (int(item["Rank"]), item["OptionID"])
        except ValueError:
            return (10**9, item["OptionID"])

    return sorted(defaults, key=sort_key)


def extract_rank_question_data(payload: Dict[str, Any], survey_brand_id: str = "") -> Dict[str, Any]:
    """Extract data specific to Ranking questions"""
    question_data = {
        "Options": [],
        "OptionsID": [],
        "OptionMedia": [],
        "Settings": get_common_settings(payload),
    }

    choices = payload.get("Choices", {}) if isinstance(payload.get("Choices"), dict) else {}
    for choice_id in _ordered_choice_ids(choices, payload.get("ChoiceOrder", [])):
        choice_info = choices.get(str(choice_id))
        if not isinstance(choice_info, dict):
            continue
        choice_text_raw = choice_info.get("Display", "")
        choice_text = strip_html(choice_text_raw)
        choice_media = extract_image_media(choice_info, survey_brand_id=survey_brand_id)
        if not choice_text and choice_media:
            choice_text = f"[Image option {len(question_data['Options']) + 1}]"
        question_data["Options"].append(choice_text)
        question_data["OptionsID"].append(str(choice_id))
        question_data["OptionMedia"].append(choice_media)

    default_ranks = _extract_default_ranks(
        payload.get("DefaultChoices"),
        options_by_id={
            option_id: option_text
            for option_id, option_text in zip(question_data["OptionsID"], question_data["Options"])
        },
    )

    if default_ranks:
        question_data["DefaultRanks"] = default_ranks

    randomization = payload.get("Randomization")
    if isinstance(randomization, dict):
        question_data["Randomization"] = randomization

    if not any(question_data["OptionMedia"]):
        question_data.pop("OptionMedia")
    return question_data
