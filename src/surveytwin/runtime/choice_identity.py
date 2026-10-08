"""Canonical Qualtrics choice identity helpers.

Qualtrics uses three related but distinct values for a choice: the authored QSF
key used by survey logic, an optional recode used in exported value files, and
the display label shown to respondents.  Runtime logic must resolve the first,
while exports apply the second and prompts present the third.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ChoiceIdentity:
    qsf_key: str
    recode_value: str
    display_label: str


def _clean(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _question_choices(question: dict[str, Any] | None) -> list[ChoiceIdentity]:
    if not isinstance(question, dict):
        return []

    labels = question.get("Options")
    if not isinstance(labels, list):
        labels = question.get("Columns")
    if not isinstance(labels, list):
        labels = question.get("options")
    if not isinstance(labels, list):
        return []

    keys = question.get("OptionsID")
    if not isinstance(keys, list):
        keys = question.get("ColumnsID")
    if not isinstance(keys, list):
        keys = question.get("option_ids")
    if not isinstance(keys, list) or len(keys) != len(labels):
        keys = [str(index + 1) for index in range(len(labels))]

    recodes_raw = question.get("RecodeValues")
    if not isinstance(recodes_raw, dict):
        recodes_raw = question.get("recode_values")
    recodes = (
        {str(key): _clean(value) for key, value in recodes_raw.items()}
        if isinstance(recodes_raw, dict)
        else {}
    )

    output: list[ChoiceIdentity] = []
    for index, label in enumerate(labels):
        qsf_key = _clean(keys[index])
        if not qsf_key:
            continue
        output.append(
            ChoiceIdentity(
                qsf_key=qsf_key,
                recode_value=recodes.get(qsf_key, qsf_key),
                display_label=_clean(label),
            )
        )
    return output


def choice_for_qsf_key(
    question: dict[str, Any] | None,
    qsf_key: Any,
) -> ChoiceIdentity | None:
    """Resolve an authored QSF choice key without confusing it with a recode."""
    target = _clean(qsf_key)
    for choice in _question_choices(question):
        if choice.qsf_key == target:
            return choice
    return None


def choice_label_for_qsf_key(question: dict[str, Any] | None, qsf_key: Any) -> str:
    choice = choice_for_qsf_key(question, qsf_key)
    return choice.display_label if choice else ""


def selected_choice_label(
    question: dict[str, Any] | None,
    answer: dict[str, Any] | None,
) -> str:
    """Return the display label for one stored single-choice answer."""
    if not isinstance(answer, dict):
        return ""
    label = _clean(answer.get("answer_label"))
    if label:
        return label

    raw = _clean(answer.get("answer_raw"))
    choices = _question_choices(question)
    for choice in choices:
        if raw and raw.casefold() == choice.display_label.casefold():
            return choice.display_label

    value = _clean(answer.get("answer_value"))
    recode_matches = [choice for choice in choices if value and value == choice.recode_value]
    if len(recode_matches) == 1:
        return recode_matches[0].display_label
    return raw or value


def answer_selects_qsf_choice(
    question: dict[str, Any] | None,
    qsf_key: Any,
    answer: dict[str, Any] | None,
) -> bool:
    """Evaluate a SelectableChoice locator against a stored answer.

    ``answer_value`` is interpreted as an exported recode, whereas label/raw
    fields are compared with display text.  Keeping those comparisons separate
    avoids the ambiguous case where one choice's recode equals another choice's
    authored QSF key.
    """
    if not isinstance(answer, dict):
        return False
    choice = choice_for_qsf_key(question, qsf_key)
    if choice is None:
        return False

    expected_label = choice.display_label.casefold()
    for field in ("answer_label", "answer_raw"):
        value = _clean(answer.get(field))
        if not value:
            continue
        tokens = {_clean(part).casefold() for part in re.split(r"[,;|]", value)}
        tokens.add(value.casefold())
        if expected_label and expected_label in tokens:
            return True

    answer_value = _clean(answer.get("answer_value"))
    if answer_value:
        recode_matches = [
            candidate
            for candidate in _question_choices(question)
            if candidate.recode_value == answer_value
        ]
        if len(recode_matches) == 1 and recode_matches[0].qsf_key == choice.qsf_key:
            return True
        # Some execution paths keep the display label in answer_value rather
        # than a coded value.  Accept that representation without treating a
        # different choice's raw key as this choice's recode.
        if expected_label and answer_value.casefold() == expected_label:
            return True
    return False


def split_answer_is_selected(answer: dict[str, Any] | None) -> bool:
    """Interpret the Yes/No answer unit used for multi-select choices."""
    if not isinstance(answer, dict):
        return False
    tokens: set[str] = set()
    for field in ("answer_value", "answer_label", "answer_raw"):
        value = _clean(answer.get(field))
        if value:
            tokens.add(value.casefold())
    if tokens & {"yes", "true", "1", "selected", "select", "checked"}:
        return True
    if tokens & {"no", "false", "0", "not selected", "unselected", "unchecked"}:
        return False
    # An unrecognized token is not evidence that the choice was selected.
    # Failing closed prevents values such as the string "unknown" from taking
    # a branch merely because non-empty strings are truthy in Python.
    return False


def export_value_for_qsf_key(question: dict[str, Any] | None, qsf_key: Any) -> str:
    target = _clean(qsf_key)
    if not isinstance(question, dict):
        return target
    recodes = question.get("RecodeValues")
    if not isinstance(recodes, dict):
        recodes = question.get("recode_values")
    if not isinstance(recodes, dict):
        return target
    return _clean(recodes.get(target, target))
