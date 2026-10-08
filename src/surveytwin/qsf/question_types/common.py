"""Type-agnostic extraction of Qualtrics question constraints and options.

Qualtrics stores every "Constraints & options" payload at the same location on
every SQ payload regardless of question type: ``Validation.Settings``,
``Randomization``, ``RecodeValues``, and ``DefaultChoices``. Historically each
per-type extractor cherry-picked its own subset of these, which is how
asymmetries crept in (matrix randomization dropped, EnforceRange ignored,
ValidNumber ignored, ...).

This module extracts them uniformly for EVERY question. It is applied by
``parse_qsf.extract_question_data`` after the per-type extractor runs, and is
strictly additive: per-type extractors keep their existing keys (they win on
conflicts), and the common pass fills in whatever they did not extract.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

# Renames applied when copying Validation.Settings so downstream code sees one
# canonical name (matches what matrix.py/text_entry.py already emit).
_SETTINGS_ALIASES = {"Type": "ValidationType"}

def _is_truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def extract_max_choices_from_custom_validation(validation_settings: Dict[str, Any]) -> str:
    """Extract Qualtrics max-selected-choice custom validation when present."""
    custom_validation = validation_settings.get("CustomValidation")
    logic = custom_validation.get("Logic") if isinstance(custom_validation, dict) else None
    if not isinstance(logic, dict):
        return ""

    def walk(node: Any) -> str:
        if not isinstance(node, dict):
            return ""
        left_operand = str(node.get("LeftOperand", "") or "")
        operator = str(node.get("Operator", "") or "")
        if "SelectedChoicesCount" in left_operand and operator in {
            "LessThanOrEqual",
            "LessThanOrEqualTo",
        }:
            return str(node.get("RightOperand", "") or "").strip()
        for value in node.values():
            found = walk(value)
            if found:
                return found
        return ""

    return walk(logic)


def extract_validation_settings(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Copy the whole Validation.Settings dict (scalars) with canonical names.

    Defensive against the non-dict Validation payloads observed in exports
    (plain strings / null). Complex sub-payloads are reduced to what the
    pipeline can use: ``ValidNumber`` keeps its Min/Max/NumDecimals scalars,
    ``CustomValidation`` becomes a ``HasCustomValidation`` flag plus a
    best-effort ``MaxChoices``.
    """
    validation = payload.get("Validation")
    source = validation.get("Settings") if isinstance(validation, dict) else None
    if not isinstance(source, dict):
        return {}

    output: Dict[str, Any] = {}
    for key, value in source.items():
        if isinstance(value, (str, int, float, bool)):
            output[_SETTINGS_ALIASES.get(key, key)] = value

    valid_number = source.get("ValidNumber")
    if isinstance(valid_number, dict):
        scalars = {
            key: value
            for key, value in valid_number.items()
            if isinstance(value, (str, int, float)) and str(value).strip() != ""
        }
        if scalars:
            output["ValidNumber"] = scalars

    if isinstance(source.get("CustomValidation"), dict):
        output["HasCustomValidation"] = True
        max_choices = extract_max_choices_from_custom_validation(source)
        if max_choices:
            output.setdefault("MaxChoices", max_choices)

    return output


def normalize_default_choices(raw: Any) -> List[Dict[str, Any]]:
    """Normalize every Qualtrics DefaultChoices shape into uniform records.

    Observed shapes and their records ({Kind, Value, ItemID, SubID}):
      {"TEXT": {"Text": "..."}}              -> text (question level)
      {"4": {"Selected": true}}              -> selected for item 4
      {"1": {"Value": "15"}}                 -> value for item 1
      {"2": {"TEXT": {"Text": "..."}}}       -> text for item 2 (form field)
      {"row": {"col": {"Selected": true}}}   -> selected for item row / sub col
    """
    if not isinstance(raw, dict) or not raw:
        return []

    entries: List[Dict[str, Any]] = []

    def emit(kind: str, value: Any = None, item_id: str | None = None, sub_id: str | None = None) -> None:
        record: Dict[str, Any] = {"Kind": kind}
        if item_id is not None:
            record["ItemID"] = str(item_id)
        if sub_id is not None:
            record["SubID"] = str(sub_id)
        if value is not None:
            record["Value"] = str(value)
        entries.append(record)

    for key, value in raw.items():
        if key == "TEXT" and isinstance(value, dict) and value.get("Text") not in (None, ""):
            emit("text", value.get("Text"))
            continue
        if not isinstance(value, dict):
            continue

        handled = False
        if _is_truthy(value.get("Selected")):
            emit("selected", item_id=key)
            handled = True
        if value.get("Value") not in (None, ""):
            emit("value", value.get("Value"), item_id=key)
            handled = True
        nested_text = value.get("TEXT")
        if isinstance(nested_text, dict) and nested_text.get("Text") not in (None, ""):
            emit("text", nested_text.get("Text"), item_id=key)
            handled = True
        if handled:
            continue

        # Nested matrix/SBS shape: {row_id: {column_id: {Selected/Value}}}.
        for sub_key, sub_value in value.items():
            if not isinstance(sub_value, dict):
                continue
            if _is_truthy(sub_value.get("Selected")):
                emit("selected", item_id=key, sub_id=sub_key)
            elif sub_value.get("Value") not in (None, ""):
                emit("value", sub_value.get("Value"), item_id=key, sub_id=sub_key)

    return entries


def apply_common_question_features(
    question_data: Dict[str, Any],
    payload: Dict[str, Any],
    question_type: str,
) -> Dict[str, Any]:
    """Merge the type-agnostic constraint/option features into a parsed question.

    Additive by design: keys already produced by the per-type extractor are
    never overwritten, so existing behavior is preserved and the common pass
    only fills the gaps (e.g. matrix Randomization, CS EnforceRange, TE
    ValidNumber, matrix/CS DefaultEntries).
    """
    settings = question_data.get("Settings")
    if not isinstance(settings, dict):
        settings = {}
        question_data["Settings"] = settings
    for key, value in extract_validation_settings(payload).items():
        settings.setdefault(key, value)

    randomization = payload.get("Randomization")
    if (
        "Randomization" not in question_data
        and isinstance(randomization, dict)
        and randomization
    ):
        question_data["Randomization"] = randomization

    recode_values = payload.get("RecodeValues")
    # Choice identifiers and export recodes serve different purposes in a QSF.
    # Keep the authored choice/answer keys in OptionsID/ColumnsID/RowsID for
    # flow logic, randomization, defaults, and stable internal question IDs.
    # Preserve recodes separately and apply them only at the export boundary.
    if (
        isinstance(recode_values, dict)
        and recode_values
        and "RecodeValues" not in question_data
    ):
        question_data["RecodeValues"] = {str(k): v for k, v in recode_values.items()}

    if "DefaultEntries" not in question_data:
        default_entries = normalize_default_choices(payload.get("DefaultChoices"))
        if default_entries:
            question_data["DefaultEntries"] = default_entries

    return question_data
