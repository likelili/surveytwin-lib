from typing import Any, Dict

from .base import default_order, extract_image_media, strip_html
from .common import extract_validation_settings


def _ordered_keys(values: Dict[str, Any], order: list[Any] | None = None) -> list[str]:
    if order:
        return [str(item) for item in order if str(item) in values]
    try:
        return sorted(values.keys(), key=lambda item: int(str(item)))
    except (TypeError, ValueError):
        return sorted(values.keys(), key=str)


def _display_items(
    values: Any,
    order: list[Any] | None = None,
    *,
    survey_brand_id: str = "",
    image_label_prefix: str = "Image option",
) -> tuple[list[str], list[str]]:
    labels, ids, _media = _display_items_with_media(
        values,
        order,
        survey_brand_id=survey_brand_id,
        image_label_prefix=image_label_prefix,
    )
    return labels, ids


def _display_items_with_media(
    values: Any,
    order: list[Any] | None = None,
    *,
    survey_brand_id: str = "",
    image_label_prefix: str = "Image option",
) -> tuple[list[str], list[str], list[list[dict[str, str]]]]:
    if not isinstance(values, dict):
        return [], [], []

    labels: list[str] = []
    ids: list[str] = []
    media: list[list[dict[str, str]]] = []
    for item_id in _ordered_keys(values, order):
        item_info = values.get(str(item_id))
        if not isinstance(item_info, dict):
            continue
        item_text = strip_html(item_info.get("Display", ""))
        item_media = extract_image_media(item_info, survey_brand_id=survey_brand_id)
        if not item_text and item_media:
            item_text = f"[{image_label_prefix} {len(labels) + 1}]"
        labels.append(item_text)
        ids.append(str(item_id))
        media.append(item_media)
    return labels, ids, media


def _default_choices_for_rows(
    default_choices: Any,
    *,
    options_by_id: dict[str, str],
) -> list[dict[str, str]]:
    if not isinstance(default_choices, dict):
        return []

    output: list[dict[str, str]] = []
    for row_id, row_defaults in default_choices.items():
        if not isinstance(row_defaults, dict):
            continue
        for option_id, option_payload in row_defaults.items():
            if not isinstance(option_payload, dict):
                continue
            if not option_payload.get("Selected"):
                continue
            option_id_text = str(option_id)
            output.append(
                {
                    "RowID": str(row_id),
                    "OptionID": option_id_text,
                    "OptionText": options_by_id.get(option_id_text, option_id_text),
                }
            )
    return output


def extract_sbs_question_data(
    payload: Dict[str, Any], survey_brand_id: str = ""
) -> Dict[str, Any]:
    """Extract data for Side By Side questions"""
    root_options, root_option_ids, root_option_media = _display_items_with_media(
        payload.get("Choices"),
        payload.get("ChoiceOrder"),
        survey_brand_id=survey_brand_id,
        image_label_prefix="Image option",
    )
    is_sbs_matrix = str(payload.get("Selector", "") or "").strip().upper() == "SBSMATRIX"
    question_data = {
        "Rows": [],
        "RowsID": [],
        "RowsMedia": [],
        "Options": root_options,
        "OptionsID": root_option_ids,
        "OptionMedia": root_option_media,
        "sub_questions": [],
        "Settings": {
            "Selector": payload.get("Selector", ""),
            "ForceResponse": payload.get("Validation", {})
            .get("Settings", {})
            .get("ForceResponse", ""),
        },
    }
    if "Randomization" in payload and isinstance(payload.get("Randomization"), dict):
        question_data["Randomization"] = payload["Randomization"]

    # Older hand-built templates in tests used root Choices as rows and
    # AdditionalQuestions.Answers as options. Real SBSMatrix QSFs include
    # per-subquestion Choices, where Choices are the scale and Answers are rows.
    question_data["Rows"] = list(root_options)
    question_data["RowsID"] = list(root_option_ids)
    question_data["RowsMedia"] = list(root_option_media)

    if default_order(question_data["RowsID"]):
        question_data.pop("RowsID")

    sub_questions = payload.get("AdditionalQuestions")
    if not isinstance(sub_questions, dict):
        sub_questions = payload.get("Questions")
    saw_subquestion_rows = False
    if isinstance(sub_questions, dict):
        for sub_q_id in _ordered_keys(sub_questions):
            sub_q_data = sub_questions.get(sub_q_id)
            if not isinstance(sub_q_data, dict):
                continue
            selector = str(sub_q_data.get("Selector", "") or "").strip()
            has_sub_scale = isinstance(sub_q_data.get("Choices"), dict) and bool(
                sub_q_data.get("Choices")
            )
            answer_rows, answer_row_ids, answer_row_media = _display_items_with_media(
                sub_q_data.get("Answers"),
                sub_q_data.get("AnswerOrder"),
                survey_brand_id=survey_brand_id,
                image_label_prefix="Image row",
            )
            sub_question = {
                "id": sub_q_id,
                "text": strip_html(sub_q_data.get("QuestionText", "")),
                "question_type": sub_q_data.get("QuestionType", ""),
                "selector": selector,
                "subselector": sub_q_data.get("SubSelector", ""),
                "QuestionID": sub_q_data.get(
                    "QuestionID", f"{payload.get('QuestionID', '')}#{sub_q_id}"
                ),
                "QuestionName": sub_q_data.get(
                    "DataExportTag",
                    f"{payload.get('DataExportTag', payload.get('QuestionID', ''))}#{sub_q_id}",
                ),
                "Options": [],
                "OptionsID": [],
                "OptionsMedia": [],
                "Rows": [],
                "RowsID": [],
                "RowsMedia": [],
            }
            sub_question_media = extract_image_media(sub_q_data, survey_brand_id=survey_brand_id)
            if sub_question_media:
                sub_question["QuestionMedia"] = sub_question_media

            # Each SBS column carries its own validation (force response,
            # char limits for TE columns, ...). Extract it uniformly.
            sub_settings = extract_validation_settings(sub_q_data)
            if sub_settings:
                sub_question["Settings"] = sub_settings
            sub_recode_values = sub_q_data.get("RecodeValues")
            if isinstance(sub_recode_values, dict) and sub_recode_values:
                sub_question["RecodeValues"] = {
                    str(key): value for key, value in sub_recode_values.items()
                }

            if is_sbs_matrix and answer_rows:
                saw_subquestion_rows = True
                options, option_ids, option_media = _display_items_with_media(
                    sub_q_data.get("Choices"),
                    sub_q_data.get("ChoiceOrder") or payload.get("ChoiceOrder"),
                    survey_brand_id=survey_brand_id,
                    image_label_prefix="Image option",
                )
                sub_question["Rows"] = answer_rows
                sub_question["RowsID"] = answer_row_ids
                sub_question["RowsMedia"] = answer_row_media
                if selector.upper() != "TE":
                    sub_question["Options"] = options
                    sub_question["OptionsID"] = option_ids
                    sub_question["OptionsMedia"] = option_media
            elif has_sub_scale:
                options, option_ids, option_media = _display_items_with_media(
                    sub_q_data.get("Choices"),
                    sub_q_data.get("ChoiceOrder") or payload.get("ChoiceOrder"),
                    survey_brand_id=survey_brand_id,
                    image_label_prefix="Image option",
                )
                if selector.upper() != "TE":
                    sub_question["Options"] = options
                    sub_question["OptionsID"] = option_ids
                    sub_question["OptionsMedia"] = option_media
            else:
                # Compatibility path for pre-existing synthetic templates.
                sub_question["Rows"] = list(question_data["Rows"])
                sub_question["RowsID"] = list(
                    question_data.get("RowsID")
                    or [str(idx + 1) for idx in range(len(question_data["Rows"]))]
                )
                sub_question["RowsMedia"] = list(question_data.get("RowsMedia", []))
                if selector.upper() != "TE":
                    sub_question["Options"] = answer_rows
                    sub_question["OptionsID"] = answer_row_ids
                    sub_question["OptionsMedia"] = answer_row_media

            if default_order(sub_question["RowsID"]):
                sub_question.pop("RowsID")
            if default_order(sub_question["OptionsID"]):
                sub_question.pop("OptionsID")
            if not any(sub_question.get("RowsMedia", [])):
                sub_question.pop("RowsMedia", None)
            if not any(sub_question.get("OptionsMedia", [])):
                sub_question.pop("OptionsMedia", None)

            defaults = _default_choices_for_rows(
                sub_q_data.get("DefaultChoices"),
                options_by_id={
                    str(option_id): option_text
                    for option_id, option_text in zip(
                        sub_question.get("OptionsID")
                        or [str(idx + 1) for idx in range(len(sub_question["Options"]))],
                        sub_question["Options"],
                    )
                },
            )
            if defaults:
                sub_question["DefaultChoices"] = defaults

            question_data["sub_questions"].append(sub_question)

    if saw_subquestion_rows:
        question_data["Rows"] = []
        question_data.pop("RowsID", None)
        question_data.pop("RowsMedia", None)

    if not any(question_data.get("RowsMedia", [])):
        question_data.pop("RowsMedia", None)
    if not any(question_data.get("OptionMedia", [])):
        question_data.pop("OptionMedia", None)

    return question_data
