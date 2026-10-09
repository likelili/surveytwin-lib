"""Prompt building, the model call and answer normalization for one twin.

Ported from the website's ``services/v2/simulation_executor.py``. Prompt text,
the whole-call retry and every answer rule are unchanged. The model is reached
through a caller object (a preset or a defined model) instead of the website's
global client, and the persona text is passed in instead of loaded here.
"""

from __future__ import annotations

import asyncio
import contextvars
import json
import logging
import math
import os
import re
import hashlib
from typing import Any

import httpx

from ..providers.openai_client import extract_json_string
from ..personas.wrapping import CT_SYSTEM_INSTRUCTION
from ..prompts.picture_markers import PICTURE_MARKER_RE, picture_markers_as_text

logger = logging.getLogger(__name__)

# Whole-call retry cap: when a twin's ENTIRE survey call fails or returns
# nonsense (exception / unparseable / zero valid answers), the full call is
# retried with the identical prompt and arm, up to this many attempts. There is
# deliberately NO per-question retry: re-asking a lone question stripped of the
# stimulus/context that preceded it breaks the survey's coherence.
MAX_CALL_RETRIES = max(1, int(os.getenv("SIMULATION_MAX_CALL_RETRIES", "5")))

# Marker recorded on answers we could NOT obtain from the model (skips, parse
# failures after all retries, unmatchable options). Downstream: the report
# surfaces these distinctly and the post-hoc rerun targets them. Fabricating a
# value here (midpoint/first option/"50") is never acceptable.
# answer_status taxonomy (§2b). All three mean "needs validation"; the split
# tells the repair portal which action fits — rematch (out_of_range) vs rerun.
ANSWER_STATUS_NEEDS_VALIDATION = "needs_validation"   # legacy umbrella (pre-split data)
ANSWER_STATUS_SKIPPED = "skipped"                     # twin was SHOWN the question, returned nothing usable
ANSWER_STATUS_OUT_OF_RANGE = "out_of_range"           # twin returned a non-empty answer that maps to no option
FLAGGED_ANSWER_STATUSES = frozenset(
    {ANSWER_STATUS_NEEDS_VALIDATION, ANSWER_STATUS_SKIPPED, ANSWER_STATUS_OUT_OF_RANGE}
)
# Legacy needs_validation is treated as a skip when splitting old data.
SKIP_ANSWER_STATUSES = frozenset({ANSWER_STATUS_NEEDS_VALIDATION, ANSWER_STATUS_SKIPPED})

# How the note for a call that gave no usable answer begins: the run reads it to
# mark the twin failed (its blank multi-select options and prefilled boxes are not
# answers from the model).
NO_USABLE_ANSWERS_NOTE = "no usable answers after"

# A MaxDiff task asks for its best and its worst item together (questions with the
# same best_worst_group_id): they must be two different items of those shown. The
# rule is given with those questions only, so other surveys' prompts are unchanged.
# It is checked after the answers come back, never by asking again during the run:
# a pair that names one item twice is flagged, and a rerun asks it again with the
# run's other flagged answers.
BEST_WORST_RULE = "Answer best and worst together: they must be two different items of the options shown."

# A question hidden by display logic whose condition depended on a FLAGGED
# upstream answer: the hide is unreliable (the twin might have been shown it had
# the upstream answer been real), so it is NOT a clean "structural / not-shown"
# blank — it is a tainted cascade. Distinct from the fielded-flagged statuses
# above: a cascade cell was never shown, so it must NOT count toward exposure/
# fielded, only as its own data-quality bucket. A rerun that repairs the upstream
# answer is what could later un-hide it.
ANSWER_STATUS_CASCADE_HIDDEN = "cascade_hidden"
# The set that TRIGGERS a downstream taint: a fielded-flagged upstream answer, or
# an already-tainted one (chains propagate).
CASCADE_TRIGGER_STATUSES = FLAGGED_ANSWER_STATUSES | {ANSWER_STATUS_CASCADE_HIDDEN}

# Base seconds slept between whole-call retry attempts (grows linearly with the
# attempt number). Keeps a transient provider blip from being hammered by the
# concurrent per-persona fan-out.
RETRY_BACKOFF_BASE_S = max(0.0, float(os.getenv("SIMULATION_RETRY_BACKOFF_S", "1.0")))

# Qualtrics' default ChoiceTotal; single source for the prompt rule AND the
# constant-sum normalizer so the model is told the same target we enforce.
CONSTANT_SUM_DEFAULT_TOTAL = 100.0


_WRAPPED_API_ERROR_STATUS_RE = re.compile(r"API error (\d{3})\b")


def _is_permanent_llm_error(exc: Exception) -> bool:
    """4xx client/config errors (bad key, unknown model, malformed request)
    cannot be fixed by retrying — burning the remaining attempts (times the
    client's provider failover) would only waste money and runner capacity.
    408/429 stay retryable."""
    status: int | None = None
    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
    else:
        # llm_client re-raises HTTP failures as RuntimeError("... API error <status>: ...")
        # ("Responses API error {status}", "Chat completions API error {status}"), so the raw
        # HTTPStatusError never reaches the retry loop — match the wrapped message too, else a
        # bad key / unknown model burns all SIMULATION_MAX_CALL_RETRIES x provider failover.
        match = _WRAPPED_API_ERROR_STATUS_RE.search(str(exc))
        if match:
            status = int(match.group(1))
    if status is None:
        return False
    return 400 <= status < 500 and status not in (408, 429)


_ANSWER_OBJECT_RE = re.compile(r'\{[^{}]*"question_id"\s*:\s*"[^"]+"[^{}]*\}')


def _salvage_answer_objects(raw_text: str) -> list[dict[str, Any]]:
    """Recover complete per-question answer objects from a format-collapsed
    response (typically a truncated JSON envelope, the dominant failure mode
    measured in the capped-token diagnostic). Only INTACT flat objects are
    kept — a partially-written object fails json.loads and is dropped — so
    salvage can only rescue answers the twin actually gave (answered, format
    broken); it can never invent content, and true skips are never restored."""
    if not raw_text:
        return []
    salvaged: list[dict[str, Any]] = []
    seen_qids: set[str] = set()
    for match in _ANSWER_OBJECT_RE.finditer(raw_text):
        try:
            obj = json.loads(match.group(0))
        except Exception:
            continue
        qid = str(obj.get("question_id", "")).strip() if isinstance(obj, dict) else ""
        if qid and qid not in seen_qids:
            seen_qids.add(qid)
            salvaged.append(obj)
    return salvaged

CLOSED_ENDED_TYPES = {"single_choice", "mc", "likert", "matrix"}
MULTI_ANSWER_SELECTED_TOKENS = {"1", "true", "yes", "y", "selected", "select", "checked"}
MULTI_ANSWER_UNSELECTED_TOKENS = {
    "",
    "0",
    "false",
    "no",
    "n",
    "not selected",
    "unselected",
    "unchecked",
}
OPEN_TEXT_INVALID_SENTINELS = {
    "[masked]",
    "masked",
    "[no answer provided]",
    "no answer provided",
    "this persona sees mixed tradeoffs and would want clearer proof of value.",
}
# Explicit refusals on OPTIONAL questions become a skip (blank), matching a
# Qualtrics respondent leaving the question unanswered. Exact matches only, so
# surveys with a literal "Prefer not to say" option still snap to that option
# (option matching runs first).
REFUSAL_ANSWER_TOKENS = {
    "prefer not to answer",
    "prefer not to say",
    "i prefer not to answer",
    "i prefer not to say",
    "rather not say",
    "i'd rather not say",
    "decline to answer",
    "i decline to answer",
    "refuse to answer",
    "no answer",
    "skip",
    "skipped",
    "skip this question",
    "(blank)",
}


def _persona_hash(persona_id: str) -> str:
    return hashlib.sha256(str(persona_id).encode("utf-8")).hexdigest()[:16]


def _numeric_metadata_value(value: Any) -> float | None:
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return None


def _numeric_metadata_int(value: Any) -> int | None:
    try:
        parsed = int(float(str(value).strip()))
    except (TypeError, ValueError):
        return None
    return parsed if parsed >= 0 else None


def _format_numeric_value(value: float, decimals: int | None = None) -> str:
    if decimals is not None:
        if decimals <= 0:
            return str(int(round(value)))
        return f"{value:.{decimals}f}".rstrip("0").rstrip(".")
    if value.is_integer():
        return str(int(value))
    return f"{value:.2f}".rstrip("0").rstrip(".")


def _format_prompt_number(value: Any) -> str:
    parsed = _numeric_metadata_value(value)
    if parsed is None:
        return str(value).strip()
    return _format_numeric_value(parsed)


def _numeric_default_value(question: dict[str, Any]) -> str:
    default_value = str(question.get("default_numeric_value", "") or "").strip()
    if not default_value:
        return ""
    match = re.search(r"-?\d+(?:\.\d+)?", default_value)
    return match.group(0) if match else ""


def _answer_generation_max_tokens(question_count: int) -> int:
    """Size the answer JSON budget to the question count.

    max_tokens is a CAP, not a purchase — completion tokens bill as generated,
    so a generous ceiling costs nothing unless the model genuinely needs it,
    while a tight one truncates real answers (the 19-study raw audit traced
    every fabrication-inducing failure to truncation at the old 3500 floor /
    12000 ceiling; reasoning models spend part of this budget on reasoning
    tokens before any text). SIMULATION_MAX_ANSWER_TOKENS overrides the
    computed value outright when set."""
    override = os.getenv("SIMULATION_MAX_ANSWER_TOKENS", "").strip()
    if override:
        try:
            return max(1, int(override))
        except ValueError:
            pass
    if question_count <= 0:
        return 1200
    return max(8000, min(32000, 900 + question_count * 120))


def _build_question_alias_map(questions: list[dict[str, Any]]) -> dict[str, str]:
    """Map prompt labels like Q1 / Question 1 back onto the real question IDs."""
    alias_map: dict[str, str] = {}

    def register(alias: str, qid: str, *, overwrite: bool = False) -> None:
        cleaned_alias = str(alias or "").strip()
        cleaned_qid = str(qid or "").strip()
        if not cleaned_alias or not cleaned_qid:
            return
        if overwrite:
            alias_map[cleaned_alias] = cleaned_qid
            alias_map[cleaned_alias.lower()] = cleaned_qid
            return
        alias_map.setdefault(cleaned_alias, cleaned_qid)
        alias_map.setdefault(cleaned_alias.lower(), cleaned_qid)

    # Exact question IDs should always win over synthetic prompt labels like Q1.
    for question in questions:
        qid = str(question.get("id", "")).strip()
        if not qid:
            continue
        register(qid, qid, overwrite=True)

    # The prompt labels each question once ("Q2:"), however many answers it
    # takes (a matrix row, a multi-select option, ...). A label names an answer
    # only when its question takes exactly one; otherwise it could mean any of
    # them, and a positional guess would put the answer under another question.
    units_by_label: list[list[str]] = []
    label_bases: list[str] = []
    label_of_base: dict[str, int] = {}
    for question in questions:
        qid = str(question.get("id", "")).strip()
        if not qid:
            continue
        base = str(question.get("matrix_question_id") or question.get("parent_question_id") or qid).strip().split("#", 1)[0]
        if base not in label_of_base:
            label_of_base[base] = len(units_by_label)
            units_by_label.append([])
            label_bases.append(base)
        units_by_label[label_of_base[base]].append(qid)
    for idx, (units, base) in enumerate(zip(units_by_label, label_bases), start=1):
        # one answer, or the question's own answer beside a write-in box
        target = units[0] if len(units) == 1 else (base if base in units else "")
        if not target:
            continue
        register(f"Q{idx}", target)
        register(f"Question {idx}", target)
        register(str(idx), target)

    # Base-id recovery: single-statement expansions carry ids like QID3_1 while
    # the model sometimes answers with the base "QID3" (the 19-study raw audit
    # found whole export columns 100%-missing from exactly this). Map the base
    # onto its expanded id ONLY when the mapping is unambiguous — exactly one
    # question shares that base and the base is not itself a question id.
    base_children: dict[str, set[str]] = {}
    for question in questions:
        qid = str(question.get("id", "")).strip()
        match = re.match(r"^(.*?)(?:_\d+)+$", qid)
        if match and match.group(1):
            base_children.setdefault(match.group(1), set()).add(qid)
    all_ids = {str(q.get("id", "")).strip() for q in questions}
    for base, children in base_children.items():
        if len(children) == 1 and base not in all_ids:
            register(base, next(iter(children)))

    return alias_map


def _resolve_question_id_alias(question_id: Any, alias_map: dict[str, str]) -> str:
    """Resolve prompt labels returned by the model back to the real question ID."""
    raw = str(question_id or "").strip()
    if not raw:
        return ""

    direct = alias_map.get(raw) or alias_map.get(raw.lower())
    if direct:
        return direct

    compact = re.sub(r"\s+", "", raw).lower()
    direct = alias_map.get(compact)
    if direct:
        return direct

    match = re.fullmatch(r"question0*(\d+)", compact) or re.fullmatch(r"q0*(\d+)", compact)
    if match:
        direct = alias_map.get(f"q{int(match.group(1))}")
        if direct:
            return direct

    if compact.isdigit():
        direct = alias_map.get(str(int(compact)))
        if direct:
            return direct

    return raw


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
        # Blank multi-select option = "not selected" — a real (negative) answer,
        # NOT a skip; deliberately unflagged.
        return {
            "question_id": question.get("id", "Q1"),
            "answer_value": "",
            "answer_label": "",
            "answer_raw": "",
        }

    # A question the model omitted is recorded as SKIPPED — never backfilled
    # with a fabricated value (scale midpoint, first option, "50", or even the
    # QSF-configured field default: a default the twin never touched is the
    # survey's value, not the twin's answer). The needs_validation marker lets
    # the report surface it and the post-hoc rerun re-ask the whole call.
    return {
        "question_id": question.get("id", "Q1"),
        "answer_value": "",
        "answer_label": "",
        "answer_raw": "",
        "answer_status": ANSWER_STATUS_SKIPPED,
    }


def _picture_unavailable_answer(question: dict[str, Any]) -> dict[str, Any]:
    """A question not asked because the model could not load its picture: every
    part is recorded as skipped, multi-select options included."""
    return {
        "question_id": question.get("id", "Q1"),
        "answer_value": "",
        "answer_label": "",
        "answer_raw": "",
        "answer_status": ANSWER_STATUS_SKIPPED,
    }


def _prefilled_answer(question: dict[str, Any]) -> dict[str, Any]:
    value = str(question.get("prefilled_answer", "") or "").strip()
    return {
        "question_id": question.get("id", "Q1"),
        "answer_value": value,
        "answer_label": value,
        "answer_raw": value,
    }


def _is_participant_id_question(question: dict[str, Any]) -> bool:
    text = re.sub(r"\s+", " ", str(question.get("text", "") or "")).strip().lower()
    q_type = str(question.get("type", "open_text")).lower()
    return q_type == "open_text" and "prolific" in text and "id" in text


def _participant_id_answer(question: dict[str, Any], persona_id: str) -> dict[str, Any]:
    value = str(persona_id or "").strip()
    return {
        "question_id": str(question.get("id", "Q1")),
        "answer_value": value,
        "answer_label": value,
        "answer_raw": value,
    }


def _normalize_open_text_answer(answer: dict[str, Any], question: dict[str, Any]) -> dict[str, Any]:
    candidate = str(
        answer.get("answer_raw") or answer.get("answer_label") or answer.get("answer_value") or ""
    ).strip()
    if candidate.lower() in OPEN_TEXT_INVALID_SENTINELS:
        candidate = ""
    # Qualtrics blocks typing beyond the character limit, so an over-length
    # simulated answer is truncated to the limit.
    max_chars = _positive_int_metadata(question, "text_max_chars")
    if max_chars and len(candidate) > max_chars:
        candidate = candidate[:max_chars].rstrip()
    result = {
        "question_id": str(question.get("id", answer.get("question_id", "Q1"))),
        "answer_value": candidate,
        "answer_label": candidate,
        "answer_raw": candidate,
    }
    if not candidate and (
        bool(question.get("force_response"))
        or str(question.get("response_requirement", "") or "").strip().lower() == "force_response"
    ):
        # The model responded but supplied nothing usable for a REQUIRED
        # open-text — a state Qualtrics validation would block. Flag it exactly
        # like an omission, otherwise a present-but-blank skip is invisible to
        # the report and the post-hoc rerun. Optional blanks stay unflagged
        # (a legitimate skip-by-choice).
        result["answer_status"] = ANSWER_STATUS_SKIPPED
    return result


def _is_refusal_answer(answer: dict[str, Any], options: list[str]) -> bool:
    """True when the model explicitly declined and no survey option matches."""
    option_tokens = {re.sub(r"\s+", " ", str(opt)).strip().lower() for opt in options}
    for key in ("answer_label", "answer_value", "answer_raw"):
        token = re.sub(r"\s+", " ", str(answer.get(key, "") or "")).strip().lower()
        if not token:
            continue
        if token in option_tokens:
            return False
        if token.rstrip(".") in REFUSAL_ANSWER_TOKENS:
            return True
    return False


def _blank_closed_answer(answer: dict[str, Any]) -> dict[str, Any]:
    """Blank an optional closed answer (explicit refusal): a legitimate
    skip-by-choice, so any stale skip flag is dropped."""
    blanked = {**answer, "answer_value": "", "answer_label": "", "answer_raw": ""}
    blanked.pop("answer_status", None)
    return blanked


def _normalize_answer_for_question(
    answer: dict[str, Any],
    question: dict[str, Any],
) -> dict[str, Any]:
    qid = str(question.get("id", answer.get("question_id", "Q1")))
    normalized = {
        "question_id": qid,
        "answer_value": str(answer.get("answer_value", "")),
        "answer_label": str(answer.get("answer_label", "")),
        "answer_raw": str(answer.get("answer_raw", answer.get("answer_value", ""))),
    }
    q_type = str(question.get("type", "open_text")).lower()
    options = question.get("options") if isinstance(question.get("options"), list) else []
    optional = not bool(question.get("force_response"))
    if question.get("multi_answer_option_id"):
        return _normalize_multi_answer_option(normalized, question)
    if q_type in CLOSED_ENDED_TYPES and options:
        if optional and _is_refusal_answer(normalized, options):
            # An explicit refusal on an optional question is a skip, not a
            # fuzzy match onto the first option.
            return _blank_closed_answer(normalized)
        return _snap_to_option(normalized, options)
    if q_type in {"numeric", "slider"}:
        if (
            optional
            and not question.get("rank_order_item_id")
            and not str(question.get("constant_sum_row_id", "") or "").strip()
            and _is_refusal_answer(normalized, [])
        ):
            return _blank_closed_answer(normalized)
        return _coerce_numeric(normalized, question)
    if q_type == "open_text":
        return _normalize_open_text_answer(normalized, question)
    return normalized


def _normalize_multi_answer_option(
    answer: dict[str, Any],
    question: dict[str, Any],
) -> dict[str, Any]:
    option_label = str(question.get("multi_answer_option_label", "") or "").strip()
    raw_values = [
        str(answer.get("answer_label", "") or "").strip(),
        str(answer.get("answer_value", "") or "").strip(),
        str(answer.get("answer_raw", "") or "").strip(),
    ]
    normalized_values = [re.sub(r"\s+", " ", value).strip().lower() for value in raw_values]
    option_lower = re.sub(r"\s+", " ", option_label).strip().lower()

    selected = False
    for value in normalized_values:
        if value in MULTI_ANSWER_UNSELECTED_TOKENS:
            continue
        if value == option_lower or value in MULTI_ANSWER_SELECTED_TOKENS:
            selected = True
            break

    return {
        "question_id": str(question.get("id", answer.get("question_id", "Q1"))),
        "answer_value": "1" if selected else "",
        "answer_label": option_label if selected else "",
        "answer_raw": option_label if selected else "",
    }


def _is_multi_answer_selected(answer: dict[str, Any]) -> bool:
    return bool(
        str(answer.get("answer_value") or "").strip()
        or str(answer.get("answer_label") or "").strip()
        or str(answer.get("answer_raw") or "").strip()
    )


def _blank_multi_answer(answer: dict[str, Any]) -> dict[str, Any]:
    return {
        **answer,
        "answer_value": "",
        "answer_label": "",
        "answer_raw": "",
    }


def _select_multi_answer(answer: dict[str, Any], question: dict[str, Any]) -> dict[str, Any]:
    option_label = str(question.get("multi_answer_option_label", "") or "").strip()
    return {
        **answer,
        "answer_value": "1",
        "answer_label": option_label,
        "answer_raw": option_label,
    }


def _flag_choice_count_violation(
    answer: dict[str, Any],
    *,
    required_min: int,
    selected: int,
) -> dict[str, Any]:
    """Mark a multi-answer response that selected fewer than MinChoices.

    We preserve the twin's actual selections rather than fabricating extra ones;
    the flag lets the validation report highlight this row as an out-of-range /
    invalid answer so the researcher decides whether to keep or exclude it.
    """
    return {
        **answer,
        "choice_count_violation": {
            "rule": "min_choices",
            "required_min": required_min,
            "selected": selected,
        },
    }


def _choice_limit(question: dict[str, Any], key: str) -> int:
    try:
        value = int(str(question.get(key, "") or "").strip())
    except (TypeError, ValueError):
        return 0
    return max(value, 0)


def _multi_answer_selection_rule(question: dict[str, Any]) -> str:
    if not question.get("multi_answer_option_id"):
        return ""
    min_choices = _choice_limit(question, "multi_answer_min_choices")
    max_choices = _choice_limit(question, "multi_answer_max_choices")
    if min_choices and max_choices:
        if min_choices == max_choices:
            return f"Select exactly {min_choices} option{'s' if min_choices != 1 else ''}."
        return f"Select between {min_choices} and {max_choices} options."
    if min_choices:
        return f"Select at least {min_choices} option{'s' if min_choices != 1 else ''}."
    if max_choices:
        return f"Select at most {max_choices} option{'s' if max_choices != 1 else ''}."
    return ""


def _response_requirement_instruction(question: dict[str, Any]) -> str:
    requirement = str(question.get("response_requirement", "") or "").strip().lower()
    if requirement == "force_response" or bool(question.get("force_response")):
        return "Response required."
    if requirement == "request_response":
        return "Response requested; you may skip only if you would choose not to answer."
    # Optional questions: the anti-over-skip directive lives ONCE in the system
    # prompt ("Answer every question your persona plausibly would; only skip a
    # question if that persona would genuinely leave it blank") rather than
    # being repeated per question — on a 100-question survey the per-question
    # copy costs ~2k tokens per persona for no additional signal.
    return ""


def _default_choice_instruction(question: dict[str, Any]) -> str:
    labels = question.get("default_choice_labels")
    if not isinstance(labels, list):
        labels = []
    labels = [str(label).strip() for label in labels if str(label).strip()]
    if not labels:
        return ""
    if question.get("multi_answer_option_id"):
        joined = "; ".join(labels)
        if bool(question.get("default_selected")):
            return f"Default selected options: {joined}. This option is preselected by default."
        return f"Default selected options: {joined}."
    if len(labels) == 1:
        return f"Default selected option: {labels[0]}."
    return f"Default selected options: {'; '.join(labels)}."


def _default_text_instruction(question: dict[str, Any]) -> str:
    default_text = str(question.get("default_text", "") or "").strip()
    if not default_text:
        return ""
    return f"Default text shown in the field: [{default_text}]."


def _numeric_rule_instruction(question: dict[str, Any]) -> str:
    q_type = str(question.get("type", "") or "").strip().lower()
    has_numeric_metadata = any(
        question.get(key) is not None
        for key in (
            "numeric_min",
            "numeric_max",
            "numeric_decimals",
            "numeric_step",
            "default_numeric_value",
        )
    )
    if q_type not in {"numeric", "slider"} and not has_numeric_metadata:
        return ""

    parts: list[str] = []
    default_value = _numeric_default_value(question)
    if default_value:
        if question.get("rank_order_item_id"):
            parts.append(f"Default rank shown in the field: [{default_value}].")
        else:
            parts.append(f"Default numeric value shown in the field: [{default_value}].")

    min_value = _numeric_metadata_value(question.get("numeric_min"))
    max_value = _numeric_metadata_value(question.get("numeric_max"))
    if min_value is not None and max_value is not None:
        parts.append(
            f"Enter a number between {_format_prompt_number(min_value)} and {_format_prompt_number(max_value)}."
        )
    elif min_value is not None:
        parts.append(f"Enter a number at least {_format_prompt_number(min_value)}.")
    elif max_value is not None:
        parts.append(f"Enter a number no greater than {_format_prompt_number(max_value)}.")

    decimals = _numeric_metadata_int(question.get("numeric_decimals"))
    if decimals is not None:
        if decimals == 0:
            parts.append("Use whole numbers only.")
        else:
            parts.append(f"Use no more than {decimals} decimal place{'s' if decimals != 1 else ''}.")

    step = _numeric_metadata_value(question.get("numeric_step"))
    if step is not None and step > 0:
        parts.append(f"Use increments of {_format_prompt_number(step)}.")

    if bool(question.get("allow_not_applicable")):
        label = str(question.get("not_applicable_label", "") or "").strip() or "Not Applicable"
        parts.append(f"You may answer exactly '{label}' if the statement does not apply.")

    return " ".join(parts)


def _positive_int_metadata(question: dict[str, Any], key: str) -> int:
    try:
        value = int(str(question.get(key, "") or "").strip())
    except (TypeError, ValueError):
        return 0
    return max(value, 0)


def _content_type_rule(question: dict[str, Any]) -> str:
    """Translate Qualtrics ContentType validation into an explicit instruction.
    Without this the LLM is never told a text field must contain a number (or
    email/phone/date), which produces out-of-scope answers Qualtrics itself
    would have rejected."""
    content_type = str(question.get("text_content_type", "") or "").strip().lower()
    if not content_type:
        return ""
    if "number" in content_type or "numeric" in content_type:
        return "This field requires a NUMBER — answer with digits only."
    if "email" in content_type:
        return "This field requires a valid email address."
    if "phone" in content_type:
        return "This field requires a phone number."
    if "date" in content_type:
        return "This field requires a date."
    if "zip" in content_type or "postal" in content_type:
        return "This field requires a ZIP/postal code."
    if "textonly" in content_type or "text_only" in content_type:
        return "This field must contain letters only (no digits)."
    return ""


def _text_validation_rule(question: dict[str, Any]) -> str:
    parts: list[str] = []
    content_rule = _content_type_rule(question)
    if content_rule:
        parts.append(content_rule)
    min_chars = _positive_int_metadata(question, "text_min_chars")
    max_chars = _positive_int_metadata(question, "text_max_chars")
    if min_chars and max_chars:
        if min_chars == max_chars:
            parts.append(f"Enter exactly {min_chars} character{'s' if min_chars != 1 else ''}.")
        else:
            parts.append(f"Enter between {min_chars} and {max_chars} characters.")
    elif min_chars:
        parts.append(f"Enter at least {min_chars} character{'s' if min_chars != 1 else ''}.")
    elif max_chars:
        parts.append(f"Enter no more than {max_chars} character{'s' if max_chars != 1 else ''}.")
    return " ".join(parts)


def _constant_sum_rule(question: dict[str, Any]) -> str:
    if not str(question.get("constant_sum_row_id", "") or "").strip():
        return ""
    total = str(question.get("constant_sum_total", "") or "").strip()
    if not total:
        # Qualtrics enforces a ChoiceTotal even when the QSF omits it from
        # Settings; CONSTANT_SUM_DEFAULT_TOTAL is Qualtrics' default and the
        # SAME constant the normalizer uses, so prompt and enforcement agree.
        # The LLM must ALWAYS be told the target, or it skips/free-forms.
        total = _format_constant_sum_value(CONSTANT_SUM_DEFAULT_TOTAL)
    group_label = str(question.get("constant_sum_group_label", "") or "").strip()
    if group_label:
        return (
            f"Enter a numeric allocation; the '{group_label}' column of this "
            f"constant-sum question should total {total} across its rows."
        )
    return f"Enter a numeric allocation; all rows in this constant-sum group should total {total}."


def _rank_order_rule(question: dict[str, Any]) -> str:
    if not question.get("rank_order_item_id"):
        return ""
    count = _positive_int_metadata(question, "rank_order_count")
    min_items = _positive_int_metadata(question, "rank_order_min_items")
    max_items = _positive_int_metadata(question, "rank_order_max_items")
    count_rule = ""
    if min_items and max_items:
        count_rule = (
            f" Rank between {min_items} and {max_items} items overall."
            if min_items != max_items
            else f" Rank exactly {min_items} items overall."
        )
    elif min_items:
        count_rule = f" Rank at least {min_items} items overall."
    elif max_items:
        count_rule = f" Rank at most {max_items} items overall."
    if count:
        return (
            f"Assign this item a unique rank from 1 to {count}; "
            "1 means highest preference." + count_rule
        )
    return "Assign this item a unique rank; 1 means highest preference." + count_rule


def _prompt_question_text(question: dict[str, Any]) -> str:
    text = str(question.get("text", "") or "")
    instructions = [
        item
        for item in (
            _response_requirement_instruction(question),
            _default_choice_instruction(question),
            _default_text_instruction(question),
            _text_validation_rule(question),
            _numeric_rule_instruction(question),
            _multi_answer_selection_rule(question),
            _constant_sum_rule(question),
            _rank_order_rule(question),
        )
        if item
    ]
    if not instructions:
        return text
    instruction_text = " ".join(instructions)
    marker = " Option:"
    if marker in text:
        return text.replace(marker, f" {instruction_text}{marker}", 1)
    return f"{text.rstrip()} {instruction_text}".strip()


def _normalize_multi_answer_groups(
    answers: list[dict[str, Any]],
    questions: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Enforce Qualtrics MaxChoices and flag MinChoices violations.

    Over-selection is unambiguously invalid (Qualtrics ChoiceRange blocks
    submitting more than MaxChoices), so we deselect the extras, keeping the
    earliest in presented order.

    Under-selection is preserved rather than fabricated: the prompt already
    instructs the twin how many options to pick ("Select between N and M
    options."), so when the twin still under-selects we keep its actual answer
    and attach a `choice_count_violation` flag. The validation report surfaces
    the flag as an out-of-range / invalid response; we never invent selections
    the twin did not make.
    """
    output = [dict(answer) for answer in answers]
    groups: dict[str, list[int]] = {}
    for idx, question in enumerate(questions):
        if idx >= len(output):
            continue
        parent_id = str(question.get("parent_question_id", "") or "").strip()
        if question.get("multi_answer_option_id") and parent_id:
            groups.setdefault(parent_id, []).append(idx)

    for indices in groups.values():
        if not indices:
            continue
        first_question = questions[indices[0]]
        min_choices = _choice_limit(first_question, "multi_answer_min_choices")
        max_choices = _choice_limit(first_question, "multi_answer_max_choices")
        force_response = bool(first_question.get("force_response"))
        selected = [idx for idx in indices if _is_multi_answer_selected(output[idx])]

        if max_choices and len(selected) > max_choices:
            for idx in selected[max_choices:]:
                output[idx] = _blank_multi_answer(output[idx])
            selected = selected[:max_choices]

        if len(selected) == 0 and not force_response:
            # An optional question left fully blank is a legitimate skip.
            continue
        floor = max(min_choices, 1 if force_response else 0)
        if max_choices:
            floor = min(floor, max_choices)
        floor = min(floor, len(indices))
        if len(selected) >= floor:
            continue

        # Under the minimum: preserve the twin's actual selections and flag the
        # group so the validation report can highlight it. We do not fabricate
        # selections the twin never made.
        for idx in indices:
            output[idx] = _flag_choice_count_violation(
                output[idx], required_min=floor, selected=len(selected)
            )

    return output


def _normalized_choice_token(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip().lower()


def _answer_matches_choice_text_entry_parent(
    answer: dict[str, Any],
    question: dict[str, Any],
) -> bool:
    option_id = _normalized_choice_token(question.get("choice_text_entry_option_id"))
    option_label = _normalized_choice_token(question.get("choice_text_entry_option_label"))
    expected = {token for token in {option_id, option_label} if token}
    if not expected:
        return False
    candidates = [
        _normalized_choice_token(answer.get("answer_value")),
        _normalized_choice_token(answer.get("answer_label")),
        _normalized_choice_token(answer.get("answer_raw")),
    ]
    return any(candidate in expected for candidate in candidates if candidate)


def _blank_open_text(answer: dict[str, Any]) -> dict[str, Any]:
    """Structural blank (e.g. the write-in box of an UNSELECTED 'Other'):
    drops any skip flag — a box the twin never had reason to fill is not a
    skip and must not be counted or re-billed as one."""
    blanked = {
        **answer,
        "answer_value": "",
        "answer_label": "",
        "answer_raw": "",
    }
    blanked.pop("answer_status", None)
    return blanked


def _choice_text_entry_placeholder(question: dict[str, Any]) -> str:
    option_label = str(question.get("choice_text_entry_option_label", "") or "").strip()
    if option_label:
        return option_label
    return "Other"


def _normalize_choice_text_entry_groups(
    answers: list[dict[str, Any]],
    questions: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    output = [dict(answer) for answer in answers]
    answer_by_id = {
        str(question.get("id", "") or "").strip(): output[idx]
        for idx, question in enumerate(questions)
        if idx < len(output) and str(question.get("id", "") or "").strip()
    }

    for idx, question in enumerate(questions):
        if idx >= len(output):
            continue
        option_id = str(question.get("choice_text_entry_option_id", "") or "").strip()
        parent_id = str(question.get("parent_question_id", "") or "").strip()
        if not option_id or not parent_id:
            continue

        selected = False
        option_answer = answer_by_id.get(f"{parent_id}_{option_id}")
        if option_answer is not None:
            selected = _is_multi_answer_selected(option_answer)
        else:
            parent_answer = answer_by_id.get(parent_id)
            if parent_answer is not None:
                selected = _answer_matches_choice_text_entry_parent(parent_answer, question)

        if not selected:
            output[idx] = _blank_open_text(output[idx])
            continue

        if not str(
            output[idx].get("answer_raw")
            or output[idx].get("answer_label")
            or output[idx].get("answer_value")
            or ""
        ).strip():
            # The parent option WAS selected (real answer, kept) but the twin
            # left the write-in box empty. Injecting the option label as prose
            # would contaminate the open-text data — record the box as a
            # flagged skip instead.
            output[idx] = {
                **output[idx],
                "answer_value": "",
                "answer_label": "",
                "answer_raw": "",
                "answer_status": ANSWER_STATUS_SKIPPED,
            }

    return output


def build_simulation_prompt_messages(
    persona_text: str,
    questions: list[dict[str, Any]],
    survey_sequence_text: str | None = None,
) -> tuple[str, str]:
    system_prompt, persona_prompt, questions_prompt = build_simulation_prompt_parts(
        persona_text,
        questions,
        survey_sequence_text=survey_sequence_text,
    )
    user_prompt = f"{persona_prompt}\n\n{questions_prompt}"
    return system_prompt, user_prompt


def _normalize_media_item(item: Any) -> dict[str, str] | None:
    if not isinstance(item, dict):
        return None
    src = str(item.get("src", "")).strip()
    if not src:
        return None
    normalized = {"kind": "image", "src": src}
    for key in (
        "alt",
        "title",
        "label",
        "context",
        "question_id",
        "prompt_label",
        "question_text",
        "option_index",
        "row_index",
        "statement_index",
        "media_index",
        "key",
    ):
        value = str(item.get(key, "")).strip()
        if value:
            normalized[key] = value
    return normalized


def _normalize_sequence_media(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []

    output: list[dict[str, Any]] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        raw_media = item.get("media")
        if not isinstance(raw_media, list):
            continue
        normalized_media = []
        for media_item in raw_media:
            normalized = _normalize_media_item(media_item)
            if normalized:
                normalized_media.append(normalized)
        if not normalized_media:
            continue
        text_with_pictures = str(item.get("question_text_with_pictures", "")).strip()
        output.append(
            {
                "question_id": str(item.get("question_id", "")).strip(),
                "question_type": str(item.get("question_type", "")).strip(),
                "question_text": str(item.get("question_text", "")).strip(),
                **({"question_text_with_pictures": text_with_pictures} if text_with_pictures else {}),
                "prompt_label": str(item.get("prompt_label", "")).strip(),
                "media": normalized_media,
            }
        )
    return output


def _fallback_sequence_media_from_questions(
    questions: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    sequence: list[dict[str, Any]] = []
    for idx, question in enumerate(questions, start=1):
        if not isinstance(question, dict):
            continue
        media_entries: list[dict[str, str]] = []
        for media_item in question.get("question_media", []):
            normalized = _normalize_media_item(media_item)
            if not normalized:
                continue
            normalized.setdefault("context", "question")
            normalized.setdefault("label", f"Q{idx} question image")
            normalized.setdefault("prompt_label", f"Q{idx}")
            normalized.setdefault("question_id", str(question.get("id", "")).strip())
            normalized.setdefault("question_text", str(question.get("text", "")).strip())
            media_entries.append(normalized)

        option_media = question.get("option_media")
        if isinstance(option_media, list):
            options = question.get("options") if isinstance(question.get("options"), list) else []
            for option_idx, media_group in enumerate(option_media, start=1):
                if not isinstance(media_group, list):
                    continue
                label = (
                    str(options[option_idx - 1]).strip()
                    if option_idx - 1 < len(options)
                    else f"[Image option {option_idx}]"
                )
                for media_item in media_group:
                    normalized = _normalize_media_item(media_item)
                    if not normalized:
                        continue
                    normalized.setdefault("context", "option")
                    normalized.setdefault("label", label)
                    normalized.setdefault("prompt_label", f"Q{idx}")
                    normalized.setdefault("question_id", str(question.get("id", "")).strip())
                    normalized.setdefault("question_text", str(question.get("text", "")).strip())
                    normalized.setdefault("option_index", str(option_idx))
                    media_entries.append(normalized)

        if media_entries:
            sequence.append(
                {
                    "question_id": str(question.get("id", "")).strip(),
                    "question_type": str(question.get("type", "")).strip(),
                    "question_text": str(question.get("text", "")).strip(),
                    "prompt_label": f"Q{idx}",
                    "media": media_entries,
                }
            )

    return sequence


def _has_sequence_media(sequence_media: list[dict[str, Any]]) -> bool:
    return any(isinstance(item.get("media"), list) and item.get("media") for item in sequence_media)


# OpenAI rejects the WHOLE request when it cannot fetch or decode one image URL
# (HTTP 400 invalid_image_url: "Error while downloading https://..."). That is
# a property of the survey's links, not of the twin, so it must not be treated
# as a permanent failure of the call: the picture is left out and the call made
# again (see _leave_out_unavailable_pictures for what happens to its question).
_IMAGE_INPUT_ERROR_MARKERS = (
    "invalid_image_url",
    "error while downloading",
    "timeout while downloading",
    "failed to download",
    "unsupported image",
    "could not process image",
    "image_url",
)


def _error_chain_texts(exc: BaseException) -> list[str]:
    # With an OpenRouter key, a failed OpenAI call is retried through OpenRouter,
    # and when that fails too, its error is the one that arrives here. OpenAI's
    # complaint about the image stays attached to it (Python keeps the error that
    # was being handled), so read the whole chain.
    texts: list[str] = []
    current: BaseException | None = exc
    seen: set[int] = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        texts.append(str(current))
        current = current.__cause__ or current.__context__
    return texts


def _is_image_input_error(exc: BaseException) -> bool:
    return any(
        marker in text.lower() for text in _error_chain_texts(exc) for marker in _IMAGE_INPUT_ERROR_MARKERS
    )


def _pictures_named_in_error(exc: BaseException, urls: set[str]) -> set[str]:
    """The pictures an image error names. OpenAI quotes the link it could not
    download, inside a JSON body that may escape the link's characters."""
    text = " ".join(_error_chain_texts(exc)).replace("\\/", "/").replace("\\u0026", "&")
    return {url for url in urls if url in text}


def _sequence_media_urls(sequence_media: list[dict[str, Any]]) -> set[str]:
    return {
        str(media.get("src", "")).strip()
        for item in sequence_media
        for media in item.get("media", [])
        if isinstance(media, dict) and str(media.get("src", "")).strip()
    }


def _question_shows_media(question: dict[str, Any], owner_id: str) -> bool:
    """Whether a sequence-media entry, keyed by its QSF question, belongs to this
    question or to one of its parts (matrix rows, options, write-in boxes)."""
    if not owner_id:
        return False
    qid = str(question.get("id", "")).strip()
    owners = {
        qid,
        str(question.get("matrix_question_id") or "").strip(),
        str(question.get("parent_question_id") or "").strip(),
    }
    owners |= {owner.split("#", 1)[0] for owner in owners}
    return owner_id in owners or qid.startswith(f"{owner_id}_")


def _leave_out_unavailable_pictures(
    sequence_media: list[dict[str, Any]],
    questions: list[dict[str, Any]],
    unavailable_urls: set[str],
) -> tuple[list[dict[str, Any]], set[str], list[str]]:
    """Take the pictures the model cannot load out of a call.

    A question that shows one of them is not asked: a twin never answers a
    question without its picture, so the ids of the question's parts come back
    to be recorded as skipped (its other pictures are dropped too, as nothing
    asks for them). A picture that belongs to no question being asked, such as
    one in descriptive text or in an earlier stage, is only left out, and the
    questions after it are still asked.

    Returns (pictures to send, ids not asked, prompt labels of those questions)."""
    if not unavailable_urls:
        return sequence_media, set(), []
    kept: list[dict[str, Any]] = []
    skipped: set[str] = set()
    labels: list[str] = []
    for item in sequence_media:
        media = [media_item for media_item in item.get("media", []) if isinstance(media_item, dict)]
        owner = str(item.get("question_id", "")).strip()
        shown_to = (
            []
            if str(item.get("question_type", "")).strip().upper() == "DB"
            else [q for q in questions if _question_shows_media(q, owner)]
        )
        if shown_to and any(str(m.get("src", "")).strip() in unavailable_urls for m in media):
            skipped.update(str(q.get("id", "")).strip() for q in shown_to)
            labels.append(str(item.get("prompt_label", "")).strip() or owner)
            continue
        rest = [m for m in media if str(m.get("src", "")).strip() not in unavailable_urls]
        if rest:
            kept.append({**item, "media": rest})
    return kept, skipped, labels


def _unloadable_picture_note(failed: set[str], *, named: bool, skipped_labels: list[str]) -> str:
    labels = list(dict.fromkeys(label for label in skipped_labels if label))
    if named:
        note = f"the model could not load {', '.join(sorted(failed))}"
    else:
        note = "the model could not load one of this survey's pictures and did not say which, so none were sent"
    if labels:
        verb = "was" if len(labels) == 1 else "were"
        return f"{note}; {', '.join(labels)} {verb} not asked and recorded as skipped"
    return f"{note}; the questions were asked without {'it' if named and len(failed) == 1 else 'them'}"


def _build_multimodal_input_items(
    *,
    persona_text: str,
    questions: list[dict[str, Any]],
    survey_sequence_text: str | None,
    survey_sequence_media: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    system_prompt, persona_prompt, questions_prompt = build_simulation_prompt_parts(
        persona_text,
        questions,
        survey_sequence_text=None,
    )

    user_content: list[dict[str, Any]] = [
        {"type": "input_text", "text": persona_prompt},
    ]

    # Each picture is sent where the survey shows it (its marker in the survey
    # text, see picture_markers); a picture with no place there (a question
    # answered at an earlier stage) comes after it, with its question's text.
    pictures_by_key = {
        str(media.get("key", "")).strip(): media
        for item in survey_sequence_media
        for media in item.get("media", [])
        if isinstance(media, dict) and str(media.get("key", "")).strip()
    }
    placed: set[str] = set()
    cleaned_sequence = str(survey_sequence_text or "").strip()
    if cleaned_sequence:
        sequence_parts, placed = _picture_parts(f"Survey sequence:\n{cleaned_sequence}", pictures_by_key)
        user_content.extend(sequence_parts)

    rest: list[dict[str, Any]] = []
    for item in survey_sequence_media:
        media = item.get("media", [])
        left = [m for m in media if not (isinstance(m, dict) and str(m.get("key", "")).strip() in placed)]
        if not left:
            continue
        if len(left) < len(media):
            # some of its pictures are already in the survey text: list the
            # others under its plain text
            item = {key: value for key, value in item.items() if key != "question_text_with_pictures"}
        rest.append({**item, "media": left})
    user_content.extend(_media_input_parts(rest))
    user_content.append({"type": "input_text", "text": questions_prompt})

    return [
        {"role": "system", "content": [{"type": "input_text", "text": system_prompt}]},
        {"role": "user", "content": user_content},
    ]


def _picture_parts(
    text: str,
    pictures_by_key: dict[str, dict[str, Any]],
) -> tuple[list[dict[str, Any]], set[str]]:
    """``text`` as request parts with each picture sent at its marker
    (picture_markers): the text up to the marker and the picture's label, then
    the picture. A picture shown at two places is sent at both. A marker whose
    picture is not sent (the model cannot load it) keeps its label, saying so.
    Returns the parts and the keys of the pictures sent."""
    parts: list[dict[str, Any]] = []
    sent: set[str] = set()
    pending = ""
    position = 0
    for match in PICTURE_MARKER_RE.finditer(text):
        pending += text[position : match.start()]
        position = match.end()
        key, label = match.group(1), match.group(2)
        src = str((pictures_by_key.get(key) or {}).get("src", "")).strip()
        if not src:
            pending += f"[{label}: not shown]"
            continue
        parts.append({"type": "input_text", "text": f"{pending}[{label}]"})
        parts.append({"type": "input_image", "image_url": src})
        sent.add(key)
        pending = ""
    pending += text[position:]
    if pending.strip():
        parts.append({"type": "input_text", "text": pending})
    return parts, sent


def _media_input_parts(survey_sequence_media: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Each question's pictures with its text: at their places in the text when
    it marks them (question_text_with_pictures), the others each as a line
    saying what it belongs to, then the picture."""
    parts: list[dict[str, Any]] = []
    for item in survey_sequence_media:
        prompt_label = (
            str(item.get("prompt_label", "")).strip()
            or str(item.get("question_id", "")).strip()
            or "survey context"
        )
        question_text = str(item.get("question_text", "")).strip()
        text_with_pictures = str(item.get("question_text_with_pictures", "")).strip()
        media_items = [media for media in item.get("media", []) if isinstance(media, dict)]
        if text_with_pictures:
            line_parts, sent = _picture_parts(
                f"Visual context for {prompt_label}: {text_with_pictures}",
                {str(m.get("key", "")).strip(): m for m in media_items if str(m.get("key", "")).strip()},
            )
            parts.extend(line_parts)
            media_items = [m for m in media_items if str(m.get("key", "")).strip() not in sent]
        elif question_text:
            parts.append(
                {
                    "type": "input_text",
                    "text": f"Visual context for {prompt_label}: {question_text}",
                }
            )
        for media in media_items:
            if not isinstance(media, dict):
                continue
            src = str(media.get("src", "")).strip()
            if not src:
                continue
            context = str(media.get("context", "question")).strip().lower()
            label = str(media.get("label", "")).strip()
            if context == "option":
                descriptor = f"Image for option '{label}' in {prompt_label}."
            elif context == "row":
                descriptor = f"Image for row '{label}' in {prompt_label}."
            elif context == "statement":
                descriptor = f"Image for statement '{label}' in {prompt_label}."
            else:
                descriptor = f"Image attached to {prompt_label}."
            alt = str(media.get("alt", "")).strip()
            title = str(media.get("title", "")).strip()
            if alt:
                descriptor += f" Alt text: {alt}."
            if title and title != alt:
                descriptor += f" Title: {title}."
            parts.append({"type": "input_text", "text": descriptor})
            parts.append({"type": "input_image", "image_url": src})
    return parts


def _responses_items_to_chat_messages(
    input_items: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Convert Responses-API input items into Chat-Completions messages.

    The batch path standardizes on /v1/chat/completions (Batch-eligible, handles
    text + images + GPT-5), so a multimodal survey's items are converted rather
    than rebuilt — the persona/question/image interleaving stays identical to the
    real-time Responses path (input_text -> text, input_image -> image_url)."""
    messages: list[dict[str, Any]] = []
    for item in input_items:
        role = str(item.get("role", "user"))
        content = item.get("content", [])
        if not isinstance(content, list):
            content = []
        if role == "system":
            text = "\n".join(
                str(part.get("text", ""))
                for part in content
                if isinstance(part, dict) and part.get("type") == "input_text"
            )
            messages.append({"role": "system", "content": text})
            continue
        parts: list[dict[str, Any]] = []
        for part in content:
            if not isinstance(part, dict):
                continue
            if part.get("type") == "input_text":
                parts.append({"type": "text", "text": str(part.get("text", ""))})
            elif part.get("type") == "input_image":
                src = str(part.get("image_url", "")).strip()
                if src:
                    parts.append({"type": "image_url", "image_url": {"url": src}})
        messages.append({"role": role, "content": parts})
    return messages


def build_persona_chat_messages(
    *,
    persona_text: str,
    questions: list[dict[str, Any]],
    survey_sequence_text: str | None = None,
    survey_sequence_media: list[dict[str, Any]] | None = None,
    blocked_image_urls: list[str] | None = None,
) -> tuple[list[dict[str, Any]], int] | None:
    """Build the Chat-Completions (messages, max_tokens) for one twin's primary
    answer call, matching what ``_llm_answer_questions`` would send. Returns None
    when the twin has no answerable questions (no LLM call is needed), which
    includes every question showing a picture the model cannot load."""
    answerable_questions = [
        q
        for q in questions
        if not bool(q.get("prefill_only")) and not _is_participant_id_question(q)
    ]
    normalized_sequence_media = _normalize_sequence_media(survey_sequence_media)
    if not normalized_sequence_media:
        normalized_sequence_media = _fallback_sequence_media_from_questions(questions)
    sendable_media, picture_skipped, _ = _leave_out_unavailable_pictures(
        normalized_sequence_media,
        answerable_questions,
        {str(url).strip() for url in (blocked_image_urls or []) if str(url).strip()},
    )
    if len(answerable_questions) <= len(picture_skipped):
        return None
    max_tokens = _answer_generation_max_tokens(len(answerable_questions) - len(picture_skipped))
    asked_questions = [q for q in questions if str(q.get("id", "")).strip() not in picture_skipped]

    if _has_sequence_media(sendable_media):
        messages = _responses_items_to_chat_messages(
            _build_multimodal_input_items(
                persona_text=persona_text,
                questions=asked_questions,
                survey_sequence_text=survey_sequence_text,
                survey_sequence_media=sendable_media,
            )
        )
    else:
        system_prompt, user_prompt = build_simulation_prompt_messages(
            persona_text,
            asked_questions,
            survey_sequence_text=picture_markers_as_text(survey_sequence_text or "", shown=False),
        )
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ]
    return messages, max_tokens


def build_persona_batch_item(
    *,
    custom_id: str,
    model: str,
    client: Any,
    persona_text: str,
    questions: list[dict[str, Any]],
    survey_sequence_text: str | None = None,
    survey_sequence_media: list[dict[str, Any]] | None = None,
    blocked_image_urls: list[str] | None = None,
) -> dict[str, Any] | None:
    """Build one OpenAI Batch line for a twin, or None when no LLM call is needed."""
    built = build_persona_chat_messages(
        persona_text=persona_text,
        questions=questions,
        survey_sequence_text=survey_sequence_text,
        survey_sequence_media=survey_sequence_media,
        blocked_image_urls=blocked_image_urls,
    )
    if built is None:
        return None
    messages, max_tokens = built
    body = client.build_chat_request_body(
        model=model,
        messages=messages,
        temperature=0.3,
        max_tokens=max_tokens,
        response_format={"type": "json_object"},
    )
    return {"custom_id": custom_id, "body": body}


# The static system prompt sent for every non-CT simulation (a CT run appends
# CT_SYSTEM_INSTRUCTION). Exposed as a module constant so the report/export layer
# can record the exact instruction used, without re-deriving it.
# The behavioral instruction is the researcher-editable half of the system
# prompt (survey mode: config page → preview survey → system prompt). The
# format contract below it is NEVER editable — it is what keeps the answers
# machine-parseable — and is always appended after the instruction.
DEFAULT_SYSTEM_INSTRUCTION = (
    "You are a survey simulation respondent. Stay faithful to the persona context."
)

# Custom system instruction for the current run, set from the run config by the
# simulation tool (and by rerun/preview) via set_system_instruction_override.
# ContextVar so it propagates into per-twin asyncio tasks without threading a
# parameter through every call layer — the same pattern as the CT run context.
_SYSTEM_INSTRUCTION_OVERRIDE: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "simulation_system_instruction_override", default=None
)


def set_system_instruction_override(value: str | None) -> contextvars.Token:
    """Set the researcher's custom system instruction for this async context.
    Pass None/blank to use the default. Returns the token for reset."""
    cleaned = str(value or "").strip() or None
    return _SYSTEM_INSTRUCTION_OVERRIDE.set(cleaned)


def current_system_instruction() -> str:
    return _SYSTEM_INSTRUCTION_OVERRIDE.get() or DEFAULT_SYSTEM_INSTRUCTION


SIMULATION_FORMAT_CONTRACT = (
    "Return valid JSON only.\n"
    "CRITICAL — match the answer format to each question's fields:\n"
    "- If a question provides an `options` list (this includes single_choice, matrix rows, "
    "dropdowns, and every multiple-choice variant), answer_value and answer_label MUST be "
    "EXACTLY one of the provided option strings, copied verbatim. Do NOT invent, abbreviate, "
    "renumber, or reword an option. Pick the option that best fits the persona.\n"
    "- For numeric, slider, scale, or star questions (no options list), answer_value and "
    "answer_label MUST be a number written in digits (never words like 'several' or 'a few'), "
    "inside numeric_min/numeric_max when provided, honoring numeric_step, numeric_decimals, "
    "and default_numeric_value; unless allow_not_applicable is true, in which case the exact "
    "not_applicable_label is also allowed.\n"
    "- For multi-select option groups, honor selection_rule when present: the NUMBER of "
    "options you select must satisfy it.\n"
    "- For constant-sum rows, the allocations across the group MUST add up to the stated total.\n"
    "- For rank-order items, assign each item in the same rank_order_group_id a unique "
    "whole-number rank, where 1 is highest preference.\n"
    "- For open_text questions, follow text_validation_rule when present and treat "
    "default_text as the text shown in the survey field before the respondent edits or "
    "submits it.\n"
    "Answer every question your persona plausibly would; only skip a question if that persona "
    "would genuinely leave it blank."
)

# Backwards-compatible full default prompt (instruction + contract).
SIMULATION_SYSTEM_PROMPT = f"{DEFAULT_SYSTEM_INSTRUCTION} {SIMULATION_FORMAT_CONTRACT}"


def build_simulation_prompt_parts(
    persona_text: str,
    questions: list[dict[str, Any]],
    survey_sequence_text: str | None = None,
) -> tuple[str, str, str]:
    answerable_questions = [q for q in questions if not bool(q.get("prefill_only"))]
    prompt_questions = [
        {
            "id": q.get("id", f"Q{idx + 1}"),
            "text": _prompt_question_text(q),
            "type": q.get("type", "open_text"),
            "options": q.get("options", []),
            **(
                {"response_requirement": q.get("response_requirement")}
                if q.get("response_requirement")
                else {}
            ),
            **(
                {"default_choice_labels": q.get("default_choice_labels")}
                if q.get("default_choice_labels")
                else {}
            ),
            **(
                {"default_text": q.get("default_text")}
                if q.get("default_text")
                else {}
            ),
            **(
                {"text_validation_rule": _text_validation_rule(q)}
                if _text_validation_rule(q)
                else {}
            ),
            **(
                {"numeric_rule": _numeric_rule_instruction(q)}
                if _numeric_rule_instruction(q)
                else {}
            ),
            **(
                {"text_min_chars": q.get("text_min_chars")}
                if q.get("text_min_chars") is not None
                else {}
            ),
            **(
                {"text_max_chars": q.get("text_max_chars")}
                if q.get("text_max_chars") is not None
                else {}
            ),
            **(
                {"default_selected": q.get("default_selected")}
                if q.get("multi_answer_option_id") and "default_selected" in q
                else {}
            ),
            **(
                {"selection_rule": _multi_answer_selection_rule(q)}
                if _multi_answer_selection_rule(q)
                else {}
            ),
            **(
                {"multi_answer_group_id": q.get("parent_question_id")}
                if q.get("multi_answer_option_id") and q.get("parent_question_id")
                else {}
            ),
            **(
                {"multi_answer_option_id": q.get("multi_answer_option_id")}
                if q.get("multi_answer_option_id")
                else {}
            ),
            **(
                {"multi_answer_option_label": q.get("multi_answer_option_label")}
                if q.get("multi_answer_option_id")
                else {}
            ),
            **(
                {"numeric_min": q.get("numeric_min")}
                if q.get("numeric_min") is not None
                else {}
            ),
            **(
                {"numeric_max": q.get("numeric_max")}
                if q.get("numeric_max") is not None
                else {}
            ),
            **(
                {"numeric_decimals": q.get("numeric_decimals")}
                if q.get("numeric_decimals") is not None
                else {}
            ),
            **(
                {"numeric_step": q.get("numeric_step")}
                if q.get("numeric_step") is not None
                else {}
            ),
            **(
                {"default_numeric_value": q.get("default_numeric_value")}
                if q.get("default_numeric_value")
                else {}
            ),
            **(
                {"allow_not_applicable": True}
                if q.get("allow_not_applicable")
                else {}
            ),
            **(
                {"not_applicable_label": q.get("not_applicable_label")}
                if q.get("allow_not_applicable") and q.get("not_applicable_label")
                else {}
            ),
            **(
                {"slider_selector": q.get("slider_selector")}
                if q.get("slider_selector")
                else {}
            ),
            **(
                {"slider_statement_id": q.get("slider_statement_id")}
                if q.get("slider_statement_id")
                else {}
            ),
            **(
                {"slider_statement_label": q.get("slider_statement_label")}
                if q.get("slider_statement_label")
                else {}
            ),
            **(
                {"star_count": q.get("star_count")}
                if q.get("star_count") is not None
                else {}
            ),
            **(
                {"star_type": q.get("star_type")}
                if q.get("star_type")
                else {}
            ),
            **(
                {"constant_sum_group_id": q.get("parent_question_id")}
                if q.get("constant_sum_row_id") and q.get("parent_question_id")
                else {}
            ),
            **(
                {"constant_sum_row_id": q.get("constant_sum_row_id")}
                if q.get("constant_sum_row_id")
                else {}
            ),
            **(
                {"constant_sum_total": q.get("constant_sum_total")}
                if q.get("constant_sum_total")
                else {}
            ),
            **(
                {
                    "best_worst_group_id": q.get("best_worst_group_id"),
                    "best_worst_role": q.get("best_worst_role"),
                    "best_worst_rule": BEST_WORST_RULE,
                }
                if q.get("best_worst_group_id")
                else {}
            ),
            **(
                {"rank_order_group_id": q.get("parent_question_id")}
                if q.get("rank_order_item_id") and q.get("parent_question_id")
                else {}
            ),
            **(
                {"rank_order_item_id": q.get("rank_order_item_id")}
                if q.get("rank_order_item_id")
                else {}
            ),
            **(
                {"rank_order_item_label": q.get("rank_order_item_label")}
                if q.get("rank_order_item_label")
                else {}
            ),
            **(
                {"rank_order_count": q.get("rank_order_count")}
                if q.get("rank_order_count")
                else {}
            ),
            **(
                {"rank_order_rule": _rank_order_rule(q)}
                if _rank_order_rule(q)
                else {}
            ),
            **(
                {"default_rank": q.get("default_rank")}
                if q.get("default_rank")
                else {}
            ),
        }
        for idx, q in enumerate(answerable_questions)
    ]

    # The exact-option rule keys off the PRESENCE of an `options` list, not a
    # hard-coded type list. The pipeline only ever emits four type strings
    # (single_choice, matrix, open_text, numeric); the old prompt named `likert`
    # and `mc` (which never occur) and omitted `matrix` (209/722 questions),
    # leaving every matrix question to infer the rule from `options`. Keying off
    # `options` covers matrix + all future option-bearing types, and "copied
    # verbatim" attacks "7" vs "7 - Strongly Agree" / paraphrase drift at the
    # source. Superset of the prior prompt — every prior rule is preserved.
    # Researcher-editable instruction + fixed format contract. The override (if
    # any) replaces only the behavioral instruction, never the contract.
    system_prompt = f"{current_system_instruction()} {SIMULATION_FORMAT_CONTRACT}"
    # Every persona in the library comes from a Croissant Twins bank (the
    # default Twin-2K-500 included), so the bank safety line is always added,
    # as the website adds it for bank runs.
    system_prompt = f"{system_prompt}\n{CT_SYSTEM_INSTRUCTION}"
    persona_prompt = f"Persona profile:\n{str(persona_text or '').strip()}\n\n"
    sequence_block = ""
    cleaned_sequence = picture_markers_as_text(str(survey_sequence_text or "").strip())
    if cleaned_sequence:
        sequence_block = f"Survey sequence:\n{cleaned_sequence}\n\n"
    questions_prompt = (
        f"{sequence_block}Questions JSON:\n"
        f"{json.dumps(prompt_questions, ensure_ascii=False)}\n\n"
        "Respond using JSON schema: "
        f'{{"answers":[{{"question_id":"{prompt_questions[0]["id"] if prompt_questions else "Q1"}","answer_value":"...","answer_label":"...","answer_raw":"..."}}]}}'
    )

    return system_prompt, persona_prompt.rstrip(), questions_prompt.rstrip()


# Appended to the base system prompt on a rerun. Type-comprehensive on purpose:
# the residual out_of_range answers are format/comprehension errors across every
# question type, so the instruction names the required format for each. It is
# FORMAT + QUESTION-FIDELITY only — it never hints which option to pick and
# preserves a legitimate "Don't know"/refusal choice — so it reveals the twin's
# true intent without manufacturing a fit (the no-fabrication invariant holds).
RERUN_INSTRUCTION = (
    "RE-ANSWER TASK: You already completed this survey; your previous answers are listed below. "
    "A few were missing or did not match the question's required format, so they have been CLEARED "
    'and marked "[NEEDS ANSWER]". Provide an answer for ONLY the [NEEDS ANSWER] questions and do not '
    "change any other answer. For each, answer the EXACT question asked — never a different or related "
    "question — as your persona genuinely would, in that question's required format:\n"
    "- a question with an options list (single_choice, matrix, dropdown, any multiple-choice): exactly "
    "one of the provided option strings, copied verbatim (if a 'Don't know' / 'Prefer not to answer' "
    "option is offered and genuinely fits your persona, choose it);\n"
    "- numeric / slider / scale / star (no options): a number written in digits, within the allowed range;\n"
    "- multi-select option groups: select the number of options its selection_rule requires, each verbatim;\n"
    "- constant-sum rows: allocations across the group that add up to the stated total;\n"
    "- rank-order items: a unique whole-number rank for each item;\n"
    "- open_text: a genuine free-text answer that follows any stated text/length/format rule — never blank.\n"
    'Return JSON whose "answers" array contains ONLY the [NEEDS ANSWER] questions.'
)


def build_rerun_prompt_parts(
    persona_text: str,
    questions: list[dict[str, Any]],
    *,
    prior_answers: dict[str, str],
    flagged_qids: Any,
    survey_sequence_text: str | None = None,
) -> tuple[str, str]:
    """Build the (system, user) prompt for a targeted rerun. Same persona +
    common (§6) instruction + RERUN_INSTRUCTION; the WHOLE survey is shown with
    the twin's prior answers filled in, only the `flagged_qids` masked
    "[NEEDS ANSWER]", and the model is asked to answer ONLY those. Re-asking just
    the broken cells in full context (never re-drawing good answers) is the
    anti-p-hacking design."""
    rerun_system, head, ask = _rerun_prompt_pieces(
        persona_text,
        questions,
        prior_answers=prior_answers,
        flagged_qids=flagged_qids,
        survey_sequence_text=survey_sequence_text,
    )
    return rerun_system, f"{head}\n\n{ask}"


def build_rerun_input_items(
    persona_text: str,
    questions: list[dict[str, Any]],
    *,
    prior_answers: dict[str, str],
    flagged_qids: Any,
    pictures: list[dict[str, Any]],
    survey_sequence_text: str | None = None,
) -> list[dict[str, Any]]:
    """The rerun prompt of ``build_rerun_prompt_parts`` as Responses-API input
    items, with the pictures the twin saw (sequence-media entries) placed
    before the request, so a picture question is never re-asked without its
    picture."""
    rerun_system, head, ask = _rerun_prompt_pieces(
        persona_text,
        questions,
        prior_answers=prior_answers,
        flagged_qids=flagged_qids,
        survey_sequence_text=survey_sequence_text,
    )
    return [
        {"role": "system", "content": [{"type": "input_text", "text": rerun_system}]},
        {
            "role": "user",
            "content": [
                {"type": "input_text", "text": head},
                *_media_input_parts(pictures),
                {"type": "input_text", "text": ask},
            ],
        },
    ]


def _rerun_prompt_pieces(
    persona_text: str,
    questions: list[dict[str, Any]],
    *,
    prior_answers: dict[str, str],
    flagged_qids: Any,
    survey_sequence_text: str | None = None,
) -> tuple[str, str, str]:
    """(system prompt, user prompt without its request, the request)."""
    system_prompt, persona_prompt, questions_prompt = build_simulation_prompt_parts(
        persona_text, questions, survey_sequence_text=survey_sequence_text
    )
    rerun_system = f"{system_prompt}\n{RERUN_INSTRUCTION}"
    flagged = {str(q) for q in (flagged_qids or [])}
    prior_rows: list[dict[str, str]] = []
    for q in questions:
        if bool(q.get("prefill_only")):
            continue
        qid = str(q.get("id", "")).strip()
        if not qid:
            continue
        prior_rows.append(
            {
                "question_id": qid,
                "previous_answer": "[NEEDS ANSWER]" if qid in flagged else str(prior_answers.get(qid, "")),
            }
        )
    prior_block = (
        'Your previous answers (keep every one that is not "[NEEDS ANSWER]" exactly as-is):\n'
        + json.dumps(prior_rows, ensure_ascii=False)
    )
    ask = (
        "Now provide answers for ONLY these questions: "
        + ", ".join(sorted(flagged))
        + '. Return JSON: {"answers":[{"question_id":"...","answer_value":"...",'
        '"answer_label":"...","answer_raw":"..."}]}'
    )
    return rerun_system, f"{persona_prompt}\n\n{questions_prompt}\n\n{prior_block}", ask


async def _llm_answer_questions(
    caller: Any,
    persona_text: str,
    questions: list[dict[str, Any]],
    persona_id: str = "",
    survey_sequence_text: str | None = None,
    survey_sequence_media: list[dict[str, Any]] | None = None,
    precomputed_answer_text: str | None = None,
    blocked_image_urls: list[str] | None = None,
    notes: list[str] | None = None,
) -> list[dict[str, Any]]:
    """``notes`` (optional list) receives plain-language remarks the caller
    should surface as run warnings: the model could not load one of the
    survey's pictures, or no usable answers came back and the twin is recorded
    as skipped. Without it those outcomes were only visible in server logs.

    ``blocked_image_urls`` are pictures the image check found the model cannot
    load: a question showing one is not asked and is recorded as skipped; a
    picture in descriptive text is only left out."""
    answerable_questions = [
        q
        for q in questions
        if not bool(q.get("prefill_only")) and not _is_participant_id_question(q)
    ]
    answers: list[Any] = []
    question_alias_map = _build_question_alias_map(questions)
    answerable_ids = {
        str(q.get("id", "")) for q in answerable_questions if str(q.get("id", ""))
    }
    picture_skipped: set[str] = set()

    def _usable_answers(candidate: Any) -> list[Any] | None:
        """The whole-call validity gate: the response must be a non-empty answers
        list that resolves at least ONE answerable question. ALL-OR-NOTHING —
        partial misses do NOT retry (the answered part is real data; the missing
        part is recorded as skipped, never re-asked in isolation)."""
        if not isinstance(candidate, dict):
            return None
        items = candidate.get("answers", [])
        if not isinstance(items, list) or not items:
            return None
        resolved = {
            _resolve_question_id_alias(item.get("question_id", ""), question_alias_map)
            for item in items
            if isinstance(item, dict)
        }
        if not (resolved & answerable_ids):
            return None
        return items

    if answerable_questions:
        normalized_sequence_media = _normalize_sequence_media(survey_sequence_media)
        if not normalized_sequence_media:
            normalized_sequence_media = _fallback_sequence_media_from_questions(questions)
        unavailable_urls = {str(url).strip() for url in (blocked_image_urls or []) if str(url).strip()}
        sendable_media, picture_skipped, _ = _leave_out_unavailable_pictures(
            normalized_sequence_media, answerable_questions, unavailable_urls
        )
        answerable_ids.difference_update(picture_skipped)
        asked_questions = [q for q in questions if str(q.get("id", "")).strip() not in picture_skipped]
        max_tokens = _answer_generation_max_tokens(len(answerable_questions) - len(picture_skipped))

        usable: list[Any] | None = None if answerable_ids else []
        if usable is None and precomputed_answer_text is not None:
            # Primary answer was produced by the OpenAI Batch job; reuse it as a
            # free "attempt 0". A malformed/nonsense batch line falls through to
            # the live whole-call retry loop below.
            try:
                usable = _usable_answers(
                    json.loads(extract_json_string(precomputed_answer_text))
                )
            except Exception:
                usable = None

        # Whole-call retry: the request uses the SAME prompt each attempt
        # (identical prompt, identical arm, temperature unchanged) so retries
        # never alter the experimental condition — they only recover from
        # transport failures and nonsense outputs that concurrent calling
        # occasionally produces. The prompt is built once, outside the loop.
        multimodal = _has_sequence_media(sendable_media)
        if not multimodal:
            system_prompt, user_prompt = build_simulation_prompt_messages(
                persona_text,
                asked_questions,
                survey_sequence_text=picture_markers_as_text(survey_sequence_text or "", shown=False),
            )
        last_raw_text = ""
        last_error: Exception | None = None
        attempt = 0
        while usable is None and attempt < MAX_CALL_RETRIES:
            attempt += 1
            try:
                if multimodal:
                    raw_text = await caller.responses_input_text(
                        input_items=_build_multimodal_input_items(
                            persona_text=persona_text,
                            questions=asked_questions,
                            survey_sequence_text=survey_sequence_text,
                            survey_sequence_media=sendable_media,
                        ),
                        max_tokens=max_tokens,
                    )
                    last_raw_text = raw_text or last_raw_text
                    parsed = json.loads(extract_json_string(raw_text))
                else:
                    parsed = await caller.chat_json(
                        system_prompt=system_prompt,
                        user_prompt=user_prompt,
                        temperature=0.3,
                        max_tokens=max_tokens,
                    )
                usable = _usable_answers(parsed)
                if usable is None:
                    logger.warning(
                        "Whole-call attempt %s/%s returned no usable answers persona_hash=%s",
                        attempt, MAX_CALL_RETRIES, _persona_hash(persona_id),
                    )
            except Exception as exc:
                last_error = exc
                if multimodal and _is_image_input_error(exc):
                    # The provider could not use one of the survey's pictures.
                    # Leave it out and ask again rather than losing the twin:
                    # the question that shows it is not asked (recorded as
                    # skipped), the other questions are asked as before. When
                    # the error does not name the picture, none is sent.
                    sent = _sequence_media_urls(sendable_media)
                    named = _pictures_named_in_error(exc, sent)
                    failed = named or sent
                    _, _, newly_skipped_labels = _leave_out_unavailable_pictures(
                        sendable_media, answerable_questions, failed
                    )
                    unavailable_urls |= failed
                    sendable_media, picture_skipped, _ = _leave_out_unavailable_pictures(
                        normalized_sequence_media, answerable_questions, unavailable_urls
                    )
                    answerable_ids.difference_update(picture_skipped)
                    asked_questions = [
                        q for q in questions if str(q.get("id", "")).strip() not in picture_skipped
                    ]
                    multimodal = _has_sequence_media(sendable_media)
                    if not multimodal:
                        system_prompt, user_prompt = build_simulation_prompt_messages(
                            persona_text,
                            asked_questions,
                            survey_sequence_text=picture_markers_as_text(survey_sequence_text or "", shown=False),
                        )
                    logger.warning(
                        "Whole-call attempt %s rejected for an image input persona_hash=%s; "
                        "leaving out %s picture(s), %s question(s) not asked: %s",
                        attempt, _persona_hash(persona_id), len(failed),
                        len(newly_skipped_labels), str(exc)[:200],
                    )
                    if notes is not None:
                        notes.append(
                            _unloadable_picture_note(
                                failed, named=bool(named), skipped_labels=newly_skipped_labels
                            )
                        )
                    if not answerable_ids:
                        usable = []
                        break
                    continue
                if _is_permanent_llm_error(exc):
                    logger.warning(
                        "Whole-call attempt %s failed with a permanent client error "
                        "(HTTP %s) persona_hash=%s; not retrying",
                        attempt,
                        getattr(getattr(exc, "response", None), "status_code", "?"),
                        _persona_hash(persona_id),
                    )
                    break
                logger.warning(
                    "Whole-call attempt %s/%s failed persona_hash=%s",
                    attempt, MAX_CALL_RETRIES, _persona_hash(persona_id),
                    exc_info=True,
                )
            if usable is None and attempt < MAX_CALL_RETRIES and RETRY_BACKOFF_BASE_S > 0:
                await asyncio.sleep(RETRY_BACKOFF_BASE_S * attempt)

        if usable is None:
            # LAST RESORT — format-collapse salvage. Live retries recover a
            # complete response when they succeed; only when they are exhausted
            # do we mine the broken raw text (live multimodal output or the
            # batch-precomputed answer) for intact answer objects. Recovers
            # "answered, format broken"; anything truly missing stays a
            # flagged skip below.
            for source in (last_raw_text, precomputed_answer_text or ""):
                if not source:
                    continue
                salvaged = _usable_answers({"answers": _salvage_answer_objects(source)})
                if salvaged:
                    logger.warning(
                        "Salvaged %s intact answers from a format-collapsed response "
                        "persona_hash=%s (remaining questions stay flagged)",
                        len(salvaged), _persona_hash(persona_id),
                    )
                    usable = salvaged
                    break

        # If every attempt AND salvage failed, the twin is recorded as
        # all-skipped (flagged below) — never fabricated. The post-hoc rerun
        # can target it by id. Say so to the caller: a run full of such twins
        # used to finish as "completed" with nothing but silence to explain it.
        if usable is None and notes is not None:
            reason = f" (last error: {str(last_error)[:160]})" if last_error is not None else ""
            notes.append(
                f"{NO_USABLE_ANSWERS_NOTE} {attempt} attempt(s); every question recorded as skipped{reason}"
            )
        answers = usable or []

    normalized = []
    by_qid = {
        _resolve_question_id_alias(item.get("question_id", ""), question_alias_map): item
        for item in answers
        if isinstance(item, dict)
        and _resolve_question_id_alias(item.get("question_id", ""), question_alias_map)
    }

    for question in questions:
        if _is_participant_id_question(question):
            normalized.append(_participant_id_answer(question, persona_id))
            continue
        if bool(question.get("prefill_only")):
            normalized.append(_prefilled_answer(question))
            continue
        found = by_qid.get(str(question.get("id", "")))
        if isinstance(found, dict):
            normalized.append(_normalize_answer_for_question(found, question))
        else:
            normalized.append(_fallback_answer(question))

    normalized = _normalize_multi_answer_groups(normalized, questions)
    normalized = _normalize_choice_text_entry_groups(normalized, questions)
    normalized = _normalize_rank_order_groups(normalized, questions)
    normalized = _normalize_constant_sum_groups(normalized, questions)
    # Best and worst are checked here, after the model answered; a pair that names
    # one item twice is not asked again now: it is flagged for a rerun.
    same_pick = _best_worst_conflicts(normalized, questions)
    if same_pick:
        normalized = _flag_best_worst_conflicts(normalized, questions, same_pick)
        if notes is not None:
            notes.append(
                f"{len(same_pick)} best/worst pair(s) named the same item twice; both answers of each "
                "are recorded as skipped (the item given is kept), for a rerun to ask again"
            )
    # Set last, so no group rule turns a question that was never asked into an
    # answer (a blank multi-select option would read as "not selected").
    return [
        _picture_unavailable_answer(question)
        if str(question.get("id", "")).strip() in picture_skipped
        else answer
        for question, answer in zip(questions, normalized)
    ]


_LEADING_OPTION_CODE_RE = re.compile(r"^\s*\d+\s*[-–:.)]?\s*")


def _normalize_option_text(value: Any) -> str:
    """Lowercase, unicode-fold curly quotes, strip punctuation, collapse
    whitespace — so ' Strongly agree. ' == 'strongly agree' and Don't == Don’t."""
    text = str(value or "")
    for curly, straight in (("’", "'"), ("‘", "'"), ("“", '"'), ("”", '"')):
        text = text.replace(curly, straight)
    text = re.sub(r"[^\w\s]", " ", text.lower())
    return re.sub(r"\s+", " ", text).strip()


def _parse_option_range(option: Any) -> tuple[float, float] | None:
    """Parse an option as a numeric bucket: 'a-b' (with optional trailing
    label), 'a+', or a bare number. Returns None when the option is not a
    range — the range tier only fires when EVERY option parses."""
    text = re.sub(r"\s+", " ", str(option or "").strip())
    m = re.fullmatch(r"(-?\d+(?:\.\d+)?)\s*[-–—]\s*(-?\d+(?:\.\d+)?)(?:\s.*)?", text)
    if m:
        lo, hi = float(m.group(1)), float(m.group(2))
        return (min(lo, hi), max(lo, hi))
    m = re.fullmatch(r"(-?\d+(?:\.\d+)?)\s*\+(?:\s.*)?", text)
    if m:
        return (float(m.group(1)), math.inf)
    if re.fullmatch(r"-?\d+(?:\.\d+)?", text):
        value = float(text)
        return (value, value)
    return None


def _match_option_deterministic(label: str, options: list[str], *, _strip_answer_code: bool = True) -> str | None:
    """Deterministic option matching tiers. Every tier fires ONLY when the
    mapping is unambiguous (exactly one candidate); anything else returns None
    so the caller records a visible flagged blank instead of a guess."""
    if not label:
        return None

    lower = label.lower()
    for opt in options:
        if str(opt).lower() == lower:
            return opt

    norm = _normalize_option_text(label)
    if norm:
        # normalized equality (punctuation/whitespace/unicode variants)
        candidates = [opt for opt in options if _normalize_option_text(opt) == norm]
        if len(candidates) == 1:
            return candidates[0]
        # code-stripped equality: "not at all" == "1 - not at all". Only for
        # non-numeric answers — a bare number must go through the numeric
        # tiers, or stripping "10-" from the range option "10-20" would make
        # "20" a false label match.
        if not re.fullmatch(r"[\d\s.]+", norm):
            candidates = [
                opt
                for opt in options
                if _normalize_option_text(_LEADING_OPTION_CODE_RE.sub("", str(opt))) == norm
            ]
            if len(candidates) == 1:
                return candidates[0]
        # token-order equality: "Agree strongly" == "Strongly agree" (the token
        # multiset must match EXACTLY, so agree/disagree can never cross)
        token_key = " ".join(sorted(norm.split()))
        candidates = [
            opt
            for opt in options
            if " ".join(sorted(_normalize_option_text(opt).split())) == token_key
        ]
        if len(candidates) == 1:
            return candidates[0]

    prefix_match = re.match(r"^(\d+)", label)
    if prefix_match:
        # numeric code prefix: "7" -> "7 - very much so" (unique only)
        prefix = prefix_match.group(1)
        candidates = [
            opt for opt in options if re.match(rf"^{re.escape(prefix)}(?:\s|$)", str(opt))
        ]
        if len(candidates) == 1:
            return candidates[0]

    if re.fullmatch(r"-?\d+(?:\.\d+)?", label):
        # embedded/suffix number: "7" -> "Very likely to share 7" (unique only)
        candidates = [
            opt
            for opt in options
            if re.search(rf"(?<![\d.]){re.escape(label)}(?![\d.])", str(opt))
        ]
        if len(candidates) == 1:
            return candidates[0]
        # range buckets: "2" -> "1-3" — only when EVERY option parses as a
        # range and the number lands in exactly one
        ranges = [_parse_option_range(opt) for opt in options]
        if ranges and all(r is not None for r in ranges):
            number = float(label)
            hits = [
                opt for opt, bounds in zip(options, ranges) if bounds[0] <= number <= bounds[1]
            ]
            if len(hits) == 1:
                return hits[0]

    if _strip_answer_code:
        # The twin prepended the scale code to its OWN answer:
        # "1 - Very unlikely to share 1" for option "Very unlikely to share 1".
        # The tiers strip a leading code from the OPTION but not the ANSWER, so
        # strip it here and re-run ONCE. Requires a separator, so a bare numeric
        # code ("7") is left for the numeric tiers. Cannot flip polarity (only
        # removes a leading code) and stays unique-guarded.
        stripped = _ANSWER_LEADING_CODE_RE.sub("", label).strip()
        if stripped and stripped != label:
            return _match_option_deterministic(stripped, options, _strip_answer_code=False)
    return None


# Leading answer-side code, e.g. "1 - " / "2) " — separator REQUIRED so a bare
# "7" (a valid numeric-code answer) is never stripped to nothing.
_ANSWER_LEADING_CODE_RE = re.compile(r"^\s*\d+\s*[-–—.:)]\s*")


def _snap_to_option(answer: dict[str, Any], options: list[str]) -> dict[str, Any]:
    """Map answer_label/answer_value onto a valid option DETERMINISTICALLY.

    Character-similarity fuzzy matching was removed: SequenceMatcher scores
    'Male' vs 'Female' at 0.60 and 'Somewhat agree' vs 'Somewhat disagree' at
    ~0.87 — polarity flips that would become invisible WRONG answers instead
    of visible blanks. The deterministic tiers (normalized/code-stripped/
    token-order equality, numeric code, embedded number, range bucket) each
    fire only when exactly one option matches; empirically they cover the real
    below-threshold population, and the residual is blanked + flagged for the
    post-hoc rerun. Recover what the twin clearly said; never guess."""
    label = str(answer.get("answer_label", "")).strip()
    if not label or label not in options:
        matched = _match_option_deterministic(label, options)
        if matched is None:
            # Non-empty answer mapping to no option = out_of_range (a rematch/
            # rerun candidate); a truly empty answer is a plain skip.
            verbatim = str(
                answer.get("answer_raw") or answer.get("answer_label")
                or answer.get("answer_value") or ""
            ).strip()
            out_of_range = bool(verbatim)
            answer["answer_status"] = (
                ANSWER_STATUS_OUT_OF_RANGE if out_of_range else ANSWER_STATUS_SKIPPED
            )
            answer["answer_label"] = ""
            answer["answer_value"] = ""
            # Keep the verbatim answer on out_of_range so the twin view and the
            # repair portal can show what the twin actually said (and rematch
            # it); a true skip has nothing to keep. The coded value/label stay
            # blank, so distributions and valid-N are unaffected (flagged rows
            # are excluded from the valid base regardless of answer_raw).
            answer["answer_raw"] = verbatim if out_of_range else ""
            return answer
        label = matched
    answer["answer_label"] = label
    answer["answer_value"] = label
    answer["answer_raw"] = label
    return answer


def _coerce_numeric(answer: dict[str, Any], question: dict[str, Any]) -> dict[str, Any]:
    """Force numeric answers to parseable numeric strings."""
    raw_candidates = [
        str(answer.get("answer_value", "")),
        str(answer.get("answer_label", "")),
        str(answer.get("answer_raw", "")),
    ]

    if bool(question.get("allow_not_applicable")):
        label = str(question.get("not_applicable_label", "") or "").strip() or "Not Applicable"
        accepted = {
            re.sub(r"\s+", " ", label).strip().lower(),
            "not applicable",
            "n/a",
            "na",
            "does not apply",
        }
        for candidate in raw_candidates:
            normalized_candidate = re.sub(r"\s+", " ", candidate).strip().lower()
            if normalized_candidate in accepted:
                # Slider N/A is a valid literal response, not a numeric parse failure.
                answer["answer_value"] = label
                answer["answer_label"] = label
                answer["answer_raw"] = label
                return answer

    value = ""
    for candidate in raw_candidates:
        match = re.search(r"-?\d+(?:\.\d+)?", candidate)
        if match:
            value = match.group(0)
            break

    # "None" is the one word with an unambiguous numeric reading — keep it.
    # Vague magnitudes ("a few"→3, "many"→5) and last-resort defaults (QSF
    # field default, "1", "50") invented the specific number; those cases are
    # now recorded as flagged skips for the post-hoc rerun instead.
    if not value and re.search(r"\bnone\b", " ".join(raw_candidates).lower()):
        value = "0"

    if not value:
        # The twin gave text but no parseable number (e.g. "twenty") =
        # out_of_range; genuinely blank input is a plain skip.
        verbatim = next((str(c).strip() for c in raw_candidates if str(c).strip()), "")
        gave_text = bool(verbatim)
        answer["answer_value"] = ""
        answer["answer_label"] = ""
        # Keep the verbatim text on out_of_range for the twin view / repair
        # portal; a true skip keeps nothing. Coded value/label stay blank.
        answer["answer_raw"] = verbatim if gave_text else ""
        answer["answer_status"] = (
            ANSWER_STATUS_OUT_OF_RANGE if gave_text else ANSWER_STATUS_SKIPPED
        )
        return answer

    try:
        numeric_value = float(value)
    except (TypeError, ValueError):
        numeric_value = None
    if numeric_value is not None:
        try:
            min_value = float(str(question.get("numeric_min", "")).strip())
        except (TypeError, ValueError):
            min_value = None
        try:
            max_value = float(str(question.get("numeric_max", "")).strip())
        except (TypeError, ValueError):
            max_value = None
        step = _numeric_metadata_value(question.get("numeric_step"))
        decimals = _numeric_metadata_int(question.get("numeric_decimals"))
        if _numeric_problem(numeric_value, min_value, max_value, step, decimals):
            # A number the question does not allow is never moved to the nearest
            # allowed one (99.6 on a 0-10 scale is not a 10): it is recorded as the
            # model gave it and flagged, for the report and a rerun after the run.
            answer["answer_value"] = ""
            answer["answer_label"] = ""
            answer["answer_raw"] = next((str(c).strip() for c in raw_candidates if str(c).strip()), value)
            answer["answer_status"] = ANSWER_STATUS_OUT_OF_RANGE
            return answer
        value = _format_numeric_value(numeric_value, decimals)

    answer["answer_value"] = value
    answer["answer_label"] = value
    answer["answer_raw"] = value
    return answer


def _numeric_problem(value: float, low: float | None, high: float | None, step: float | None, decimals: int | None) -> str:
    """Why a number does not fit a numeric question ("" when it fits): below the
    lowest value, above the highest, off the step from the lowest value (or 0), or
    with more decimal places than allowed."""
    tolerance = 1e-9
    if low is not None and value < low - tolerance:
        return "below the lowest value"
    if high is not None and value > high + tolerance:
        return "above the highest value"
    if step is not None and step > 0:
        steps = (value - (low if low is not None else 0.0)) / step
        if abs(steps - round(steps)) > 1e-6:
            return "not on the step"
    if decimals is not None and decimals >= 0 and abs(round(value, decimals) - value) > tolerance:
        return "too many decimal places"
    return ""


def _parse_numeric_answer_value(answer: dict[str, Any]) -> float:
    for key in ("answer_value", "answer_label", "answer_raw"):
        match = re.search(r"-?\d+(?:\.\d+)?", str(answer.get(key, "")))
        if match:
            return max(float(match.group(0)), 0.0)
    return 0.0


def _format_constant_sum_value(value: float) -> str:
    if float(value).is_integer():
        return str(int(value))
    return f"{value:.2f}".rstrip("0").rstrip(".")


def _best_worst_conflicts(answers: list[dict[str, Any]], questions: list[dict[str, Any]]) -> list[tuple[str, str]]:
    """The MaxDiff pairs (best question ID, worst question ID) whose two answers name
    the same item."""
    groups: dict[str, dict[str, int]] = {}
    for idx, question in enumerate(questions):
        group = str(question.get("best_worst_group_id", "") or "").strip()
        role = str(question.get("best_worst_role", "") or "").strip()
        if group and role in {"best", "worst"} and idx < len(answers):
            groups.setdefault(group, {})[role] = idx
    out = []
    for roles in groups.values():
        if "best" not in roles or "worst" not in roles:
            continue
        best, worst = answers[roles["best"]], answers[roles["worst"]]
        if best.get("answer_status") or worst.get("answer_status"):
            continue
        picked = _normalize_option_text(best.get("answer_label") or best.get("answer_value"))
        if picked and picked == _normalize_option_text(worst.get("answer_label") or worst.get("answer_value")):
            out.append((str(questions[roles["best"]].get("id", "")), str(questions[roles["worst"]].get("id", ""))))
    return out


def _flag_best_worst_conflicts(
    answers: list[dict[str, Any]], questions: list[dict[str, Any]], pairs: list[tuple[str, str]]
) -> list[dict[str, Any]]:
    """Both answers of a pair that names the same item twice: not data. Recorded as
    skipped (kept as given in answer_raw), never corrected by guessing."""
    flagged = {qid for pair in pairs for qid in pair}
    output = []
    for question, answer in zip(questions, answers):
        if str(question.get("id", "")) in flagged:
            answer = {
                **answer,
                "answer_value": "",
                "answer_label": "",
                "answer_raw": answer.get("answer_raw") or answer.get("answer_label") or "",
                "answer_status": ANSWER_STATUS_SKIPPED,
            }
        output.append(answer)
    return output


def _normalize_rank_order_groups(
    answers: list[dict[str, Any]],
    questions: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    output = [dict(answer) for answer in answers]
    groups: dict[str, list[int]] = {}

    for idx, question in enumerate(questions):
        if idx >= len(output):
            continue
        parent_id = str(question.get("parent_question_id", "") or "").strip()
        item_id = str(question.get("rank_order_item_id", "") or "").strip()
        if parent_id and item_id:
            groups.setdefault(parent_id, []).append(idx)

    for indices in groups.values():
        if len(indices) < 2:
            continue
        # Only items the twin actually ranked (value > 0) participate. A
        # default_rank the twin never touched and a display-order fill both
        # fabricate a complete ranking out of a skip — removed.
        ranked: list[tuple[float, int, int]] = []
        skipped: list[int] = []
        for sequence_idx, idx in enumerate(indices):
            value = _parse_numeric_answer_value(output[idx])
            if value > 0:
                ranked.append((value, sequence_idx, idx))
            else:
                skipped.append(idx)

        if not ranked:
            # Full skip: leave every item blank and flagged; no ranking exists.
            for idx in skipped:
                output[idx]["answer_value"] = ""
                output[idx]["answer_label"] = ""
                output[idx]["answer_raw"] = ""
                output[idx]["answer_status"] = ANSWER_STATUS_SKIPPED
            continue

        # Partial: dense-rank the genuinely ranked items (real intent honored);
        # unranked items stay blank + flagged so the group is visibly incomplete.
        ranked.sort(key=lambda item: (item[0], item[1]))
        for rank, (_value, _sequence_idx, idx) in enumerate(ranked, start=1):
            formatted = str(rank)
            output[idx]["answer_value"] = formatted
            output[idx]["answer_label"] = formatted
            output[idx]["answer_raw"] = formatted
        for idx in skipped:
            output[idx]["answer_value"] = ""
            output[idx]["answer_label"] = ""
            output[idx]["answer_raw"] = ""
            output[idx]["answer_status"] = ANSWER_STATUS_SKIPPED

    return output


def _scale_constant_sum_values(raw_values: list[float], total: float) -> list[float]:
    if not raw_values:
        return []

    raw_sum = sum(raw_values)
    if raw_sum <= 0:
        # No real allocation to scale. The caller records the group as a
        # flagged skip; inventing an even split here fabricated data.
        return list(raw_values)
    scaled = [value / raw_sum * total for value in raw_values]

    if float(total).is_integer():
        total_int = int(round(total))
        floors = [math.floor(value) for value in scaled]
        remainder = total_int - sum(floors)
        order = sorted(
            range(len(scaled)),
            key=lambda idx: (scaled[idx] - floors[idx], -idx),
            reverse=True,
        )
        for idx in order[: max(remainder, 0)]:
            floors[idx] += 1
        return [float(value) for value in floors]

    rounded = [round(value, 2) for value in scaled]
    correction = round(total - sum(rounded), 2)
    if rounded and correction:
        rounded[-1] = round(rounded[-1] + correction, 2)
    return rounded


def _normalize_constant_sum_groups(
    answers: list[dict[str, Any]],
    questions: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    output = [dict(answer) for answer in answers]
    groups: dict[str, list[int]] = {}

    for idx, question in enumerate(questions):
        if idx >= len(output):
            continue
        parent_id = str(question.get("parent_question_id", "") or "").strip()
        row_id = str(question.get("constant_sum_row_id", "") or "").strip()
        # Multi-column CS matrices total per column, not per question.
        group_id = str(question.get("constant_sum_group_id", "") or "").strip() or parent_id
        if group_id and row_id:
            groups.setdefault(group_id, []).append(idx)

    for indices in groups.values():
        if len(indices) < 2:
            continue
        total = CONSTANT_SUM_DEFAULT_TOTAL
        for idx in indices:
            total_raw = str(questions[idx].get("constant_sum_total", "") or "").strip()
            if not total_raw:
                continue
            try:
                total = float(total_raw)
                break
            except ValueError:
                continue
        if total <= 0:
            continue

        raw_values = [_parse_numeric_answer_value(output[idx]) for idx in indices]
        if sum(raw_values) <= 0:
            # No positive allocation anywhere. Fabricating an even split here
            # turned a skipped group into a perfectly balanced "answer" —
            # removed. Blank rows stay blank; explicit zeros (a real but
            # target-violating answer) keep their value; both are flagged.
            for idx in indices:
                has_text = str(
                    output[idx].get("answer_raw")
                    or output[idx].get("answer_label")
                    or output[idx].get("answer_value")
                    or ""
                ).strip()
                if not has_text:
                    output[idx]["answer_value"] = ""
                    output[idx]["answer_label"] = ""
                    output[idx]["answer_raw"] = ""
                output[idx]["answer_status"] = ANSWER_STATUS_SKIPPED
            continue

        # Real (possibly partial) allocation: proportional rescale to the target
        # honors the twin's expressed intent; untouched rows scale to 0, which
        # matches how Qualtrics records unfilled boxes in a passing submission.
        # Every row in a successfully normalized group is now a VALID value, so
        # any stale skip flag (from the pre-rescale fallback) is cleared — a
        # leftover flag would be a false skip for the report and rerun.
        scaled_values = _scale_constant_sum_values(raw_values, total)
        for idx, scaled_value in zip(indices, scaled_values):
            formatted = _format_constant_sum_value(scaled_value)
            output[idx]["answer_value"] = formatted
            output[idx]["answer_label"] = formatted
            output[idx]["answer_raw"] = formatted
            output[idx].pop("answer_status", None)

    return output


async def simulate_persona(
    *,
    caller: Any,
    persona_text: str,
    persona_id: str,
    questions: list[dict[str, Any]],
    survey_sequence_text: str | None = None,
    survey_sequence_media: list[dict[str, Any]] | None = None,
    precomputed_answer_text: str | None = None,
    blocked_image_urls: list[str] | None = None,
    notes: list[str] | None = None,
) -> list[dict[str, Any]]:
    return await _llm_answer_questions(
        caller=caller,
        persona_text=persona_text,
        persona_id=persona_id,
        questions=questions,
        survey_sequence_text=survey_sequence_text,
        survey_sequence_media=survey_sequence_media,
        precomputed_answer_text=precomputed_answer_text,
        blocked_image_urls=blocked_image_urls,
        notes=notes,
    )
