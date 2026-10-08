from typing import Any, Dict

from .base import default_order, extract_image_media, get_common_settings, strip_html


def _is_truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _numeric_value(value: Any) -> float | None:
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return None


def _integer_value(value: Any) -> int | None:
    try:
        return int(float(str(value).strip()))
    except (TypeError, ValueError):
        return None


def _format_number(value: Any) -> Any:
    parsed = _numeric_value(value)
    if parsed is None:
        return value
    if parsed.is_integer():
        return int(parsed)
    return parsed


def _not_applicable_label(payload: Dict[str, Any]) -> str:
    labels = payload.get("Labels")
    if not isinstance(labels, dict):
        return "Not Applicable"
    label_payload = labels.get("NA")
    if not isinstance(label_payload, dict):
        return "Not Applicable"
    label = strip_html(str(label_payload.get("Display", "") or "")).strip()
    return label or "Not Applicable"


def _slider_settings(payload: Dict[str, Any]) -> Dict[str, Any]:
    settings = get_common_settings(payload)
    configuration = payload.get("Configuration", {})
    if not isinstance(configuration, dict):
        configuration = {}

    selector = str(payload.get("Selector", "") or "").strip().upper()
    star_type = str(configuration.get("StarType", "") or "").strip().lower()
    star_count = _integer_value(configuration.get("StarCount"))
    decimals = _integer_value(configuration.get("NumDecimals"))
    grid_lines = _numeric_value(configuration.get("GridLines"))
    snap_to_grid = _is_truthy(configuration.get("SnapToGrid"))
    not_applicable = _is_truthy(configuration.get("NotApplicable"))
    min_value = _numeric_value(configuration.get("CSSliderMin", 0))
    max_value = _numeric_value(configuration.get("CSSliderMax", 100))

    for source_key, output_key in (
        ("NumDecimals", "NumericDecimals"),
        ("GridLines", "TickInterval"),
        ("SnapToGrid", "SnapToGrid"),
        ("ShowValue", "ShowValue"),
        ("CustomStart", "CustomStart"),
        ("SliderStartPositions", "SliderStartPositions"),
        ("NotApplicable", "NotApplicable"),
    ):
        value = configuration.get(source_key)
        if value not in (None, ""):
            settings[output_key] = value

    if not_applicable:
        settings["NotApplicableLabel"] = _not_applicable_label(payload)

    if selector == "STAR":
        if star_type == "half":
            settings["NumericStep"] = 0.5
            settings["NumericDecimals"] = max(decimals or 0, 1)
        elif star_type == "discrete":
            settings["NumericStep"] = 1
            settings["NumericDecimals"] = 0
        elif star_type == "continuous":
            settings["NumericDecimals"] = max(decimals or 0, 2)
        if star_count is not None:
            settings["StarCount"] = star_count
        if star_type:
            settings["StarType"] = star_type
    elif selector == "NUMBERSCALE":
        # Number Scale (QuestionType "Slider", Selector "NumberScale"): a row of
        # discrete numbered points per statement — effectively multiple choice
        # with numeric options, so answers are integers on the declared scale.
        settings["NumericStep"] = 1
        settings["NumericDecimals"] = 0
        num_scale_points = _integer_value(configuration.get("NumScalePoints"))
        if num_scale_points is not None:
            settings["NumScalePoints"] = num_scale_points
        starting_value = _integer_value(configuration.get("StartingValue"))
        if starting_value is not None:
            settings["StartingValue"] = starting_value
    elif (
        snap_to_grid
        and grid_lines is not None
        and grid_lines > 0
        and min_value is not None
        and max_value is not None
        and max_value > min_value
    ):
        # Qualtrics stores GridLines as intervals, so step is derived from the range.
        settings["NumericStep"] = _format_number((max_value - min_value) / grid_lines)

    return settings


def _slider_range(payload: Dict[str, Any]) -> Dict[str, Any]:
    configuration = payload.get("Configuration", {})
    if not isinstance(configuration, dict):
        configuration = {}

    selector = str(payload.get("Selector", "") or "").strip().upper()
    star_count = _integer_value(configuration.get("StarCount"))
    if selector == "STAR" and star_count:
        return {
            "Min": 0,
            "Max": star_count,
            "Ticks": star_count,
        }

    num_scale_points = _integer_value(configuration.get("NumScalePoints"))
    if selector == "NUMBERSCALE" and num_scale_points and num_scale_points > 0:
        # The scale runs StartingValue .. StartingValue + NumScalePoints - 1
        # (StartingValue lets authors build e.g. 0-10 scales; defaults to 1).
        starting_value = _integer_value(configuration.get("StartingValue"))
        if starting_value is None:
            starting_value = 1
        return {
            "Min": starting_value,
            "Max": starting_value + num_scale_points - 1,
            "Ticks": num_scale_points,
        }

    return {
        "Min": configuration.get("CSSliderMin", 0),
        "Max": configuration.get("CSSliderMax", 100),
        "Ticks": configuration.get("GridLines", 10),
    }


def _extract_slider_defaults(
    default_choices: Any,
    statement_ids: list[str],
) -> list[dict[str, str]]:
    if not isinstance(default_choices, dict):
        return []

    output: list[dict[str, str]] = []
    for statement_id in statement_ids:
        payload = default_choices.get(str(statement_id))
        if not isinstance(payload, dict):
            continue
        value = payload.get("Value")
        if value in (None, ""):
            continue
        output.append({"StatementID": str(statement_id), "Value": str(value)})
    return output


def extract_slider_question_data(payload: Dict[str, Any], survey_brand_id: str = "") -> Dict[str, Any]:
    """Extract data specific to Slider questions."""
    question_data = {
        "Statements": [],
        "StatementsID": [],
        "StatementsMedia": [],
        "Range": _slider_range(payload),
        "Settings": _slider_settings(payload),
    }

    if "Choices" in payload:
        choices = payload.get("Choices", {})
        choice_order = payload.get("ChoiceOrder", [])
        raw_statement_ids: list[str] = []

        for choice_id in choice_order:
            if str(choice_id) not in choices:
                continue
            choice_info = choices[str(choice_id)]
            if not isinstance(choice_info, dict):
                continue

            choice_text_raw = choice_info.get("Display", "")
            choice_text = strip_html(choice_text_raw)
            choice_media = extract_image_media(choice_info, survey_brand_id=survey_brand_id)
            if not choice_text and choice_media:
                choice_text = f"[Image statement {len(question_data['Statements']) + 1}]"
            question_data["Statements"].append(choice_text)
            question_data["StatementsID"].append(str(choice_id))
            raw_statement_ids.append(str(choice_id))
            question_data["StatementsMedia"].append(choice_media)

        default_choices = _extract_slider_defaults(
            payload.get("DefaultChoices"),
            raw_statement_ids,
        )
        if default_choices:
            question_data["DefaultChoices"] = default_choices

        randomization = payload.get("Randomization")
        if isinstance(randomization, dict) and randomization:
            question_data["Randomization"] = randomization

        if default_order(question_data["StatementsID"]):
            question_data.pop("StatementsID")
        if not any(question_data["StatementsMedia"]):
            question_data.pop("StatementsMedia")

    return question_data
