"""Asking every twin its survey: static (one call) and staged (a call per stage).

Ported from the website's ``services/v3/tools/simulation_tool.py``. The answer
helpers (fallbacks, late display logic, cascade taint) are copied unchanged.
The two loops keep the website's order of work and warnings; they take a model
caller and each twin's persona text instead of the website's global client,
database and progress events.
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Any, Callable

from ..runtime.choice_identity import (
    answer_selects_qsf_choice,
    choice_label_for_qsf_key,
    split_answer_is_selected,
)
from ..runtime.logic_compare import boolean_condition_value, is_boolean_condition
from ..engine.definition import Definition
from ..engine.staged import StagedRuntime, prior_answer_context as staged_prior_answer_context, sequence_with_prior_context as staged_survey_sequence_with_prior_context
from ..providers.context import call_context
from .executor import (
    ANSWER_STATUS_CASCADE_HIDDEN,
    ANSWER_STATUS_SKIPPED,
    CASCADE_TRIGGER_STATUSES,
    NO_USABLE_ANSWERS_NOTE,
    _is_participant_id_question,
    _normalize_constant_sum_groups,
    simulate_persona,
)


def _gave_no_usable_answers(notes: list[str]) -> bool:
    return any(str(note).startswith(NO_USABLE_ANSWERS_NOTE) for note in notes)


def _asks_the_model(questions: list[dict[str, Any]]) -> bool:
    """Whether a call has a question for the model (a prefilled box and a
    participant ID box are filled without one)."""
    return any(not q.get("prefill_only") and not _is_participant_id_question(q) for q in questions)

RUN_BATCH_SIZE = 100
STAGED_QSF_LLM_CONCURRENCY = 25
logger = logging.getLogger(__name__)

ProgressCallback = Callable[[int, int], None]


def _fallback_answer(question: dict[str, Any]) -> dict[str, Any]:
    prefilled = str(question.get("prefilled_answer", "") or "").strip()

    if bool(question.get("prefill_only")):
        return {
            "question_id": question.get("id", "Q1"),
            "answer_value": prefilled,
            "answer_label": prefilled,
            "answer_raw": prefilled,
        }

    if question.get("multi_answer_option_id"):
        # Blank multi-select option = "not selected" — a real (negative)
        # answer, NOT a skip; deliberately unflagged.
        return {
            "question_id": question.get("id", "Q1"),
            "answer_value": "",
            "answer_label": "",
            "answer_raw": "",
        }

    # A question the model omitted is recorded as SKIPPED — never backfilled
    # with a fabricated value (scale midpoint, first option, "50"). The
    # needs_validation marker lets the report surface it and the post-hoc
    # rerun re-ask the whole call. Mirrors v2 simulation_executor.
    return {
        "question_id": question.get("id", "Q1"),
        "answer_value": "",
        "answer_label": "",
        "answer_raw": "",
        "answer_status": ANSWER_STATUS_SKIPPED,
    }


def _answers_with_fallbacks(
    *,
    answers: list[dict[str, Any]],
    questions: list[dict[str, Any]],
    embedded_data: dict[str, Any] | None = None,
    displayed_qids: set[str] | None = None,
    apply_display_logic: bool = True,
) -> list[dict[str, Any]]:
    """The answers for ``questions``, with a skipped answer for any the model left
    out. ``apply_display_logic``: blank the questions whose display logic this
    call's answers rule out (a static run asks every question at once). A staged
    run passes False: its runtime shows a question only after deciding its display
    logic with every answer it reads, so checking again here, with this stage's
    answers (or with a second evaluator), could only blank what the twin was
    shown and answered."""
    embedded = embedded_data if isinstance(embedded_data, dict) else {}
    by_qid = {str(a.get("question_id", "")): a for a in answers if isinstance(a, dict)}
    complete_answers = [
        dict(by_qid.get(str(question.get("id", "Q1")), _fallback_answer(question)))
        for question in questions
    ]
    normalized = _normalize_constant_sum_groups(complete_answers, questions)
    if not apply_display_logic:
        return normalized
    return _apply_question_display_logic(
        answers=normalized,
        questions=questions,
        embedded_data=embedded,
        displayed_qids=displayed_qids,
    )


def _blank_answer(answer: dict[str, Any]) -> dict[str, Any]:
    """Structural blank: the question was hidden by display logic, so the twin
    never saw it. Deliberately DROPS any needs_validation skip flag — a
    never-shown question is not a skip and must not look like one."""
    blanked = {**answer, "answer_value": "", "answer_label": "", "answer_raw": ""}
    blanked.pop("answer_status", None)
    # Library addition: remember that the twin never saw it. Kept outside
    # answer_status so every export file stays identical to the website's;
    # Results reports it as the status "not_shown".
    blanked["not_shown"] = True
    return blanked


def _cascade_blank(answer: dict[str, Any]) -> dict[str, Any]:
    """Cascade-tainted blank: the question was hidden by display logic whose
    condition depended on a FLAGGED upstream answer, so we cannot trust that the
    twin would truly not have seen it. Blank the value like a structural hide but
    mark it cascade_hidden so the report counts it as a tainted cell, not a clean
    not-shown one (PR-C review C14)."""
    blanked = {**answer, "answer_value": "", "answer_label": "", "answer_raw": ""}
    blanked["answer_status"] = ANSWER_STATUS_CASCADE_HIDDEN
    return blanked


def _display_logic_dependency_qids(logic: Any, acc: set[str] | None = None) -> set[str]:
    """Collect the question ids a display-logic tree reads (its LeftOperand
    `q://<qid>/...` locators), recursively — the upstream answers whose flagged
    state would taint this question's hide."""
    if acc is None:
        acc = set()
    if not isinstance(logic, dict):
        return acc
    if str(logic.get("LogicType", "") or "").strip() == "Question":
        qid, _, _ = _qualtrics_locator_parts(logic.get("LeftOperand"))
        if qid:
            acc.add(qid)
    for key, value in logic.items():
        if isinstance(value, dict):
            _display_logic_dependency_qids(value, acc)
    return acc


def _clean_token(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _clean_token_lower(value: Any) -> str:
    return _clean_token(value).lower()


def _qualtrics_locator_parts(locator: Any) -> tuple[str, str, str]:
    raw = str(locator or "").strip()
    match = re.match(r"q://([^/]+)/([^/]+)(?:/([^/]+))?", raw)
    if not match:
        return raw, "", ""
    return match.group(1).strip(), match.group(2).strip(), (match.group(3) or "").strip()


def _answer_tokens(answer: dict[str, Any] | None) -> set[str]:
    if not answer:
        return set()
    tokens: set[str] = set()
    for key in ("answer_value", "answer_label", "answer_raw"):
        value = _clean_token(answer.get(key))
        if not value:
            continue
        tokens.add(value.lower())
        for part in re.split(r"[,;|]", value):
            part = _clean_token_lower(part)
            if part:
                tokens.add(part)
    return tokens


def _is_answer_present(answer: dict[str, Any] | None) -> bool:
    return bool(_answer_tokens(answer))


def _choice_label_for_id(question: dict[str, Any] | None, choice_id: str) -> str:
    return choice_label_for_qsf_key(question, choice_id)


def _is_selected_choice(
    *,
    qid: str,
    choice_id: str,
    answers_by_id: dict[str, dict[str, Any]],
    questions_by_id: dict[str, dict[str, Any]],
) -> bool:
    split_answer = answers_by_id.get(f"{qid}_{choice_id}")
    if split_answer is not None:
        return split_answer_is_selected(split_answer)

    base_answer = answers_by_id.get(qid)
    return answer_selects_qsf_choice(
        questions_by_id.get(qid),
        choice_id,
        base_answer,
    )


def _numeric_answer_value(answer: dict[str, Any] | None) -> float | None:
    if not answer:
        return None
    for key in ("answer_value", "answer_label", "answer_raw"):
        match = re.search(r"-?\d+(?:\.\d+)?", str(answer.get(key, "")))
        if match:
            try:
                return float(match.group(0))
            except ValueError:
                return None
    return None


def _logic_actual_value(
    condition: dict[str, Any],
    *,
    answers_by_id: dict[str, dict[str, Any]],
    questions_by_id: dict[str, dict[str, Any]],
    embedded_data: dict[str, Any],
    displayed_qids: set[str] | None = None,
) -> Any:
    logic_type = str(condition.get("LogicType", "") or "").strip()
    left_operand = condition.get("LeftOperand")

    if logic_type == "EmbeddedField":
        return embedded_data.get(str(left_operand or "").strip())
    if logic_type != "Question":
        return True

    qid, locator_kind, locator_value = _qualtrics_locator_parts(left_operand)
    if locator_kind == "QuestionDisplayed":
        # Staged runs pass the runtime's displayed set. Without one (static
        # runs, where the pre-filter never ran), treat the target as displayed
        # so this post-hoc pass does not blank answers it cannot reason about.
        if displayed_qids is None:
            return True
        return qid in displayed_qids
    if locator_kind == "SelectableChoice":
        return _is_selected_choice(
            qid=qid,
            choice_id=locator_value,
            answers_by_id=answers_by_id,
            questions_by_id=questions_by_id,
        )
    if locator_kind in {"ChoiceNumericEntryValue", "NumericEntryValue"}:
        return _numeric_answer_value(answers_by_id.get(qid))
    if locator_kind == "SelectedChoicesCount":
        prefix = f"{qid}_"
        return sum(
            1
            for answer_id, answer in answers_by_id.items()
            if answer_id.startswith(prefix) and _is_answer_present(answer)
        )

    answer = answers_by_id.get(qid)
    tokens = _answer_tokens(answer)
    if not tokens:
        return ""
    return next(iter(tokens))


def _compare_logic_value(actual: Any, expected: Any, operator: str) -> bool:
    op = str(operator or "").strip()
    if isinstance(actual, bool):
        if op in {"Selected", "Displayed"}:
            return actual
        if op in {"NotSelected", "NotDisplayed"}:
            return not actual

    actual_str = _clean_token(actual)
    expected_str = _clean_token(expected)

    def as_number(value: Any) -> float | None:
        try:
            return float(str(value).strip())
        except (TypeError, ValueError):
            return None

    actual_num = as_number(actual)
    expected_num = as_number(expected)

    if op == "EqualTo":
        if actual_num is not None and expected_num is not None:
            return actual_num == expected_num
        return actual_str == expected_str
    if op == "NotEqualTo":
        if actual_num is not None and expected_num is not None:
            return actual_num != expected_num
        return actual_str != expected_str
    if op in {"GreaterThan", "GreaterThanOrEqual", "GreaterThanOrEqualTo"}:
        if actual_num is not None and expected_num is not None:
            return actual_num >= expected_num if "Equal" in op else actual_num > expected_num
        return actual_str >= expected_str if "Equal" in op else actual_str > expected_str
    if op in {"LessThan", "LessThanOrEqual", "LessThanOrEqualTo"}:
        if actual_num is not None and expected_num is not None:
            return actual_num <= expected_num if "Equal" in op else actual_num < expected_num
        return actual_str <= expected_str if "Equal" in op else actual_str < expected_str
    if op == "Contains":
        return expected_str.lower() in actual_str.lower()
    if op == "DoesNotContain":
        return expected_str.lower() not in actual_str.lower()
    if op == "IsEmpty":
        return not actual_str
    if op == "IsNotEmpty":
        return bool(actual_str)
    if op == "Selected":
        return bool(actual)
    if op == "NotSelected":
        return not bool(actual)
    return True


def _evaluate_display_logic(
    logic: Any,
    *,
    answers_by_id: dict[str, dict[str, Any]],
    questions_by_id: dict[str, dict[str, Any]],
    embedded_data: dict[str, Any],
    displayed_qids: set[str] | None = None,
) -> bool:
    if not isinstance(logic, dict) or not logic:
        return True

    if is_boolean_condition(logic):
        # "If False" / "If True": decided by its own value (it has no Operator).
        return boolean_condition_value(logic)

    if "LogicType" in logic and "Operator" in logic:
        actual = _logic_actual_value(
            logic,
            answers_by_id=answers_by_id,
            questions_by_id=questions_by_id,
            embedded_data=embedded_data,
            displayed_qids=displayed_qids,
        )
        return _compare_logic_value(
            actual,
            logic.get("RightOperand"),
            str(logic.get("Operator", "")),
        )

    results: list[bool] = []
    conjunction = str(logic.get("Conjunction") or logic.get("Conjuction") or "").strip()
    logic_type = str(logic.get("Type", "") or "").strip()

    for key, value in logic.items():
        if key in {"Type", "Conjunction", "Conjuction"} or not str(key).isdigit():
            continue
        if not isinstance(value, dict):
            continue
        child_conjunction = str(
            value.get("Conjunction") or value.get("Conjuction") or ""
        ).strip()
        if child_conjunction:
            conjunction = child_conjunction
        results.append(
            _evaluate_display_logic(
                value,
                answers_by_id=answers_by_id,
                questions_by_id=questions_by_id,
                embedded_data=embedded_data,
                displayed_qids=displayed_qids,
            )
        )

    if not results:
        return True
    if conjunction == "Or" or logic_type == "Or":
        return any(results)
    return all(results)


def _apply_question_display_logic(
    *,
    answers: list[dict[str, Any]],
    questions: list[dict[str, Any]],
    embedded_data: dict[str, Any],
    displayed_qids: set[str] | None = None,
) -> list[dict[str, Any]]:
    output = [dict(answer) for answer in answers]
    answers_by_id = {
        str(question.get("id", "") or "").strip(): output[idx]
        for idx, question in enumerate(questions)
        if idx < len(output) and str(question.get("id", "") or "").strip()
    }
    questions_by_id = {
        str(question.get("id", "") or "").strip(): question
        for question in questions
        if str(question.get("id", "") or "").strip()
    }

    for idx, question in enumerate(questions):
        if idx >= len(output):
            continue
        display_logic = question.get("display_logic")
        if not isinstance(display_logic, dict) or not display_logic:
            continue
        if _evaluate_display_logic(
            display_logic,
            answers_by_id=answers_by_id,
            questions_by_id=questions_by_id,
            embedded_data=embedded_data,
            displayed_qids=displayed_qids,
        ):
            continue
        # The question is hidden. If its condition read a FLAGGED (or already-
        # tainted) upstream answer, the hide is unreliable — taint it as a
        # cascade rather than record a clean structural blank.
        dep_qids = _display_logic_dependency_qids(display_logic)
        tainted = any(
            str(answers_by_id.get(dep, {}).get("answer_status") or "").strip()
            in CASCADE_TRIGGER_STATUSES
            for dep in dep_qids
        )
        output[idx] = _cascade_blank(output[idx]) if tainted else _blank_answer(output[idx])
        qid = str(question.get("id", "") or "").strip()
        if qid:
            answers_by_id[qid] = output[idx]

    return output




# Answer statuses that hold no answer in the data.
_NO_ANSWER = frozenset({"skipped", "out_of_range", "needs_validation"})


def chosen_option_code(question: dict[str, Any], row: dict[str, Any]) -> str:
    """The values-file code of the option a twin chose, from the options it was
    shown: the option's QSF ID, recoded as the survey recodes it.
    Blank when there is none to record: no answer, a question without options,
    one Yes/No of a multi-select (coded 1 by its column), a side-by-side cell, or
    a label that two options share. Options that pipe an answer or embedded data
    read differently for each twin, so matching the label to the options as
    written can miss; this code does not."""
    label = str(row.get("answer_label") or "").strip()
    if not label or str(row.get("answer_status") or "") in _NO_ANSWER:
        return ""
    if question.get("multi_answer_option_id") or "#" in str(question.get("id", "")):
        return ""
    if str(question.get("type", "")).lower() not in {"single_choice", "matrix"}:
        return ""
    options = [str(option).strip() for option in question.get("options") or []]
    ids = [str(option_id).strip() for option_id in question.get("option_ids") or []]
    if not options or len(options) != len(ids) or options.count(label) != 1:
        return ""
    option_id = ids[options.index(label)]
    recodes = question.get("recode_values") if isinstance(question.get("recode_values"), dict) else {}
    return str(recodes.get(option_id, option_id)).strip()


def _rows_for(
    persona_id: str,
    questions: list[dict[str, Any]],
    answers: list[dict[str, Any]],
    embedded_data: dict[str, Any],
    warnings: list[str],
) -> list[dict[str, Any]]:
    """The website's long rows for one twin (one per question)."""
    rows: list[dict[str, Any]] = []
    flagged_groups: set[str] = set()
    for question, answer in zip(questions, answers, strict=False):
        qid = str(question.get("id", "Q1"))
        violation = answer.get("choice_count_violation")
        row: dict[str, Any] = {
            "persona_id": persona_id,
            "question_id": qid,
            "question_text": str(question.get("text", "")),
            "question_type": str(question.get("type", "open_text")),
            "answer_raw": str(answer.get("answer_raw", "")),
            "answer_value": str(answer.get("answer_value", "")),
            "answer_label": str(answer.get("answer_label", "")),
            "embedded_data": embedded_data,
        }
        if answer.get("answer_status"):
            row["answer_status"] = str(answer["answer_status"])
        code = chosen_option_code(question, row)
        if code:
            row["answer_code"] = code
        if answer.get("not_shown"):
            row["not_shown"] = True
        if isinstance(violation, dict):
            row["choice_count_violation"] = violation
            group_key = str(question.get("parent_question_id", "") or qid)
            if group_key not in flagged_groups:
                flagged_groups.add(group_key)
                warnings.append(
                    f"{persona_id} {group_key}: selected "
                    f"{violation.get('selected')} option(s) but at least "
                    f"{violation.get('required_min')} required (kept as-is)."
                )
        rows.append(row)
    return rows


async def simulate_static(
    *,
    caller: Any,
    twins: list[dict[str, Any]],
    instances: list[dict[str, Any]],
    concurrency: int = 8,
    precomputed: dict[int, str] | None = None,
    blocked_image_urls: list[str] | None = None,
    on_progress: ProgressCallback | None = None,
) -> tuple[list[dict[str, Any]], list[str], list[dict[str, Any]]]:
    """One call per twin, as the website's ``_simulate_batch``.

    ``twins`` are ``{"persona_id", "persona_text"}`` in sample order; twin i
    takes survey copy ``instances[i % len(instances)]``, as on the website.
    Returns rows, warnings and per-twin records.
    """
    rows: list[dict[str, Any]] = []
    warnings: list[str] = []
    records: list[dict[str, Any]] = []
    total_count = len(twins)
    completed_count = 0
    semaphore = asyncio.Semaphore(max(1, int(concurrency)))
    precomputed = precomputed or {}

    async def one(global_idx: int, twin: dict[str, Any], notes: list[str]):
        instance = instances[global_idx % len(instances)]
        questions = instance.get("questions", []) if isinstance(instance, dict) else []
        try:
            async with semaphore:
                with call_context(twin_id=twin["persona_id"], stage=None, purpose="answer"):
                    answers = await simulate_persona(
                        caller=caller,
                        persona_text=twin["persona_text"],
                        persona_id=twin["persona_id"],
                        questions=questions,
                        survey_sequence_text=str(instance.get("prompt_sequence_text", "")) or None,
                        survey_sequence_media=instance.get("prompt_sequence_media") or None,
                        precomputed_answer_text=precomputed.get(global_idx),
                        blocked_image_urls=blocked_image_urls or None,
                        notes=notes,
                    )
            return global_idx, questions, answers, None
        except Exception as exc:  # noqa: BLE001 - recorded as a skip, as on the website
            return global_idx, questions, None, exc

    for start in range(0, len(twins), RUN_BATCH_SIZE):
        batch = twins[start : start + RUN_BATCH_SIZE]
        call_notes: dict[int, list[str]] = {}
        tasks = [
            asyncio.create_task(one(start + offset, twin, call_notes.setdefault(start + offset, [])))
            for offset, twin in enumerate(batch)
        ]
        done: dict[int, tuple[list[dict[str, Any]], list[dict[str, Any]], Exception | None]] = {}
        for task in asyncio.as_completed(tasks):
            global_idx, questions, answers, error = await task
            persona_id = twins[global_idx]["persona_id"]
            for note in call_notes.get(global_idx, []):
                warnings.append(f"{persona_id}: {note}")
            if error is not None:
                warnings.append(f"Used deterministic fallback for {persona_id}: {error}")
                answers = [_fallback_answer(q) for q in questions]
            done[global_idx] = (questions, answers or [], error)
            completed_count += 1
            if on_progress is not None:
                on_progress(completed_count, total_count)
        for offset, twin in enumerate(batch):
            global_idx = start + offset
            persona_id = twin["persona_id"]
            instance = instances[global_idx % len(instances)]
            questions, answers, error = done.get(global_idx, ([], [], None))
            embedded_data = instance.get("embedded_data", {}) if isinstance(instance, dict) else {}
            if not isinstance(embedded_data, dict):
                embedded_data = {}
            complete_answers = _answers_with_fallbacks(
                answers=answers,
                questions=questions,
                embedded_data=embedded_data,
            )
            twin_rows = _rows_for(persona_id, questions, complete_answers, embedded_data, warnings)
            rows.extend(twin_rows)
            records.append(
                {
                    "persona_id": persona_id,
                    "persona_index": global_idx,
                    "survey_pid": instance.get("survey_pid", ""),
                    "execution_mode": "static",
                    "status": "failed" if error is not None else "complete",
                    "error": str(error) if error is not None else "",
                    "notes": list(call_notes.get(global_idx, [])),
                    # every attempt failed or came back unusable: nothing the twin
                    # "answered" came from the model
                    "no_usable_answers": error is None
                    and _asks_the_model(questions)
                    and _gave_no_usable_answers(call_notes.get(global_idx, [])),
                }
            )
    return rows, warnings, records


def _unique_media(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Each question's images once: text carried into a stage brings its
    images, which the earlier stages' images already hold."""
    seen: set[tuple[str, tuple[str, ...]]] = set()
    unique: list[dict[str, Any]] = []
    for item in items:
        sources = tuple(str(entry.get("src") or entry.get("url") or "") for entry in item.get("media") or [] if isinstance(entry, dict))
        key = (str(item.get("question_id", "")), sources)
        if key in seen:
            continue
        seen.add(key)
        unique.append(item)
    return unique


async def simulate_staged_twin(
    *,
    caller: Any,
    persona_id: str,
    persona_text: str,
    global_idx: int,
    persona_count: int,
    semaphore: asyncio.Semaphore,
    seed: int,
    max_stages: int = 25,
    definition: dict[str, Any] | None = None,
    initial_embedded_data: dict[str, str] | None = None,
    carry_stage_images: bool = False,
    blocked_image_urls: list[str] | None = None,
    state: Any = None,
    template: dict[str, Any] | None = None,
) -> tuple[int, str, list[dict[str, Any]], list[str], dict[str, Any], Any]:
    """The website's ``_simulate_staged_qsf_persona`` for one twin."""
    runtime = StagedRuntime(_definition(template, definition), seed=seed, persona_count=persona_count, max_stages=max_stages)
    if state is None:
        state = runtime.new_state(persona_id=persona_id, persona_index=global_idx)
        if initial_embedded_data:
            state.outside_embedded_data = {str(k): str(v) for k, v in initial_embedded_data.items()}
    warnings: list[str] = []
    carried_media: list[dict[str, Any]] = []
    from .rerun import shown_questions

    shown: dict[str, dict[str, Any]] = {}
    # stages that asked the model, and those of them that got no usable answer
    model_stages = 0
    failed_stages = 0

    while True:
        stage = runtime.render_next_stage(state)
        if not stage.questions:
            break
        # What this stage asks, as asked: a block shown again later fills pipes of
        # its later questions with answers given after these were asked.
        asked = {str(question.get("id", "")) for question in stage.questions}
        shown.update({qid: seen for qid, seen in shown_questions(stage.blocks).items() if qid in asked and qid not in shown})
        prior_answer_context = staged_prior_answer_context(state)
        stage_survey_sequence_text = staged_survey_sequence_with_prior_context(
            prior_answer_context=prior_answer_context,
            survey_sequence_text=stage.survey_sequence_text,
        )
        stage.audit_event["prior_answer_count"] = len(state.rendered_answers)
        stage.audit_event["prior_answer_context_included"] = bool(prior_answer_context)
        stage_media = list(stage.survey_sequence_media or [])
        if carry_stage_images and carried_media:
            stage_media = _unique_media(carried_media + stage_media)
        stage_index = stage.audit_event.get("stage_index")
        try:
            stage_notes: list[str] = []
            async with semaphore:
                with call_context(twin_id=persona_id, stage=stage_index, purpose="answer"):
                    raw_answers = await simulate_persona(
                        caller=caller,
                        persona_text=persona_text,
                        persona_id=persona_id,
                        questions=stage.questions,
                        survey_sequence_text=stage_survey_sequence_text or None,
                        survey_sequence_media=stage_media or None,
                        blocked_image_urls=blocked_image_urls or None,
                        notes=stage_notes,
                    ) or []
            for note in stage_notes:
                warnings.append(f"{persona_id} stage {stage_index}: {note}")
            stage_failed = _gave_no_usable_answers(stage_notes)
        except Exception as exc:  # noqa: BLE001
            warnings.append(f"Used deterministic fallback for {persona_id} stage {stage_index}: {exc}")
            raw_answers = [_fallback_answer(question) for question in stage.questions]
            stage_failed = True
        if _asks_the_model(stage.questions):
            model_stages += 1
            failed_stages += int(stage_failed)
        if carry_stage_images:
            carried_media.extend(stage.survey_sequence_media or [])

        complete_answers = _answers_with_fallbacks(
            answers=raw_answers,
            questions=stage.questions,
            embedded_data=stage.embedded_data,
            displayed_qids=state.displayed_qids,
            apply_display_logic=False,  # the runtime already decided what this twin is shown
        )
        runtime.apply_answers(state, questions=stage.questions, answers=complete_answers)
        if state.done:
            break

    final_embedded_data = dict(state.embedded_data)
    rows = _rows_for(persona_id, state.rendered_questions, state.rendered_answers, final_embedded_data, warnings)
    warnings.extend(state.warnings)
    audit = {
        "persona_id": persona_id,
        "persona_index": global_idx,
        "stage_count": len(state.audit_events),
        "question_ids": [str(question.get("id", "")) for question in state.rendered_questions],
        "embedded_data": final_embedded_data,
        "randomizer_choices": dict(state.randomizer_choices),
        "assignments": [dict(a) for a in getattr(state, "assignments", [])],
        "shown": shown,
        # each conjoint or MaxDiff task the twin was asked, as drawn for it
        **(
            {"experiments": _asked_tasks(state)}
            if getattr(state, "experiments", None)
            else {}
        ),
        "events": state.audit_events,
        "warnings": list(state.warnings),
        "stopped_at_stage_limit": bool(getattr(state, "stopped_at_limit", False)),
        "model_stages": model_stages,
        "no_usable_answer_stages": failed_stages,
    }
    return global_idx, persona_id, rows, warnings, audit, state


def _asked_tasks(state: Any) -> list[dict[str, Any]]:
    """The records of the conjoint and MaxDiff tasks a twin was asked (a task the run
    stopped before, at the stage limit, is left out)."""
    asked = set(state.answers_by_id)
    out = []
    for entry in state.experiments.values():
        for record in entry.get("records") or []:
            ids = [record.get("question_id")] if record.get("question_id") else list((record.get("question_ids") or {}).values())
            if any(str(i) in asked for i in ids):
                out.append(dict(record))
    return out


def _definition(template: dict[str, Any] | None, definition: dict[str, Any] | None) -> Definition:
    """The survey definition a staged run walks (a template read into the standard form
    when a caller gives only that)."""
    if definition is None:
        from ..spec.read_template import survey_from_template

        definition = survey_from_template(template)
    return Definition(definition)


async def simulate_staged(
    *,
    caller: Any,
    twins: list[dict[str, Any]],
    seed: int,
    max_stages: int = 25,
    initial_embedded_data: dict[str, str] | None = None,
    carry_stage_images: bool = False,
    concurrency: int = 8,
    blocked_image_urls: list[str] | None = None,
    on_progress: ProgressCallback | None = None,
    persona_count: int | None = None,
    definition: dict[str, Any] | None = None,
    template: dict[str, Any] | None = None,
) -> tuple[list[dict[str, Any]], list[str], list[dict[str, Any]]]:
    """The website's ``_simulate_staged_qsf_batch``: every twin walks its own stages
    of ``definition`` (the survey in the standard format; a script holding only a
    template may pass ``template``, read into that form first).

    ``persona_count`` is how many twins the randomization is laid out for
    (default: these twins); ``Plan.head`` passes the full plan's number.
    """
    rows: list[dict[str, Any]] = []
    warnings: list[str] = []
    audits: list[dict[str, Any]] = []
    total_count = len(twins)
    layout_count = int(persona_count or total_count)
    completed_count = 0
    semaphore = asyncio.Semaphore(max(1, int(concurrency)))
    for start in range(0, len(twins), RUN_BATCH_SIZE):
        batch = twins[start : start + RUN_BATCH_SIZE]
        tasks = [
            asyncio.create_task(
                simulate_staged_twin(
                    caller=caller,
                    persona_id=str(twin["persona_id"]),
                    persona_text=twin["persona_text"],
                    global_idx=start + offset,
                    persona_count=layout_count,
                    template=template,
                    definition=definition,
                    semaphore=semaphore,
                    seed=seed,
                    max_stages=max_stages,
                    initial_embedded_data=initial_embedded_data,
                    carry_stage_images=carry_stage_images,
                    blocked_image_urls=blocked_image_urls,
                )
            )
            for offset, twin in enumerate(batch)
        ]
        by_idx: dict[int, tuple[list[dict[str, Any]], list[str], dict[str, Any]]] = {}
        for task in asyncio.as_completed(tasks):
            global_idx, _pid, twin_rows, twin_warnings, audit, _state = await task
            by_idx[global_idx] = (twin_rows, twin_warnings, audit)
            completed_count += 1
            if on_progress is not None:
                on_progress(completed_count, total_count)
        for offset, _twin in enumerate(batch):
            global_idx = start + offset
            twin_rows, twin_warnings, audit = by_idx.get(
                global_idx,
                ([], [f"Staged QSF simulation did not return results for index {global_idx}."], {}),
            )
            rows.extend(twin_rows)
            warnings.extend(twin_warnings)
            audits.append(audit)
    return rows, warnings, audits
