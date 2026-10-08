"""A survey run stage by stage, for content that depends on earlier answers.

``StagedRuntime`` walks the definition's flow once per stage for one twin, with
the answers given so far: variables are set, branches and display conditions
decided, piped answers filled in, and the questions not answered yet that can be
asked now form the stage. After the model answers them (``apply_answers``), the
next walk goes further. A condition or a piped value that reads an answer not
given yet stops the walk there (the stage ends); a question the flow did not show
at that point counts as never shown, with no answer, so nothing waits for an
answer that cannot come.

The rules, the order of every draw and the audit record are those of the
template runtime this replaces, read off the standard form: each decision is
made the way Qualtrics logic was decided, and each twin gets the same stages.
"""

from __future__ import annotations

import copy
import hashlib
import random
import re
from dataclasses import dataclass, field
from typing import Any

from ..runtime.choice_identity import (
    _question_choices,
    answer_selects_qsf_choice,
    choice_for_qsf_key,
    choice_label_for_qsf_key,
    selected_choice_label,
)
from ..runtime.logic_compare import compare_logic_value, embedded_field_value
from ..prompts.picture_markers import PICTURE_MARKER_RE, picture_marker
from ..runtime.random_pipes import resolve_random_pipes_at
from ..spec.text import join_text
from . import choices, render
from .analysis import CHOICE_GROUP_PIPE_FORMS, ENTRY_PIPE_KINDS
from .conditions import OPERATOR_NAMES, Decider, decide, only_ignored, right_value
from .definition import Definition, assignment_value, flow_id, is_assignment_arms
from .experiment import experiment_questions
from .realize import map_condition_text, map_text, shown_question
from .static import _ends, branch_parts
from .text import item_labels

_ED_FIELD_RE = re.compile(r"\$\{e://Field/([^}]+)\}")
_LM_FIELD_RE = re.compile(r"\$\{lm://([^}]+)\}")
_Q_PIPE_RE = re.compile(r"\$\{q://([^}]+)\}")
_Q_LOCATOR_RE = re.compile(r"q://([^/]+)/([^/]+)(?:/([^/]+))?")
_ORDER_OPS = frozenset({"gt", "gte", "lt", "lte"})
_ORDER_OPERATORS = frozenset(
    {"GreaterThan", "GreaterThanOrEqual", "GreaterThanOrEqualTo", "LessThan", "LessThanOrEqual", "LessThanOrEqualTo"}
)
# An answer recorded with one of these statuses holds no answer in the data (its
# columns are blank): piped text and choices read from it are empty.
_NO_ANSWER_STATUSES = frozenset({"skipped", "out_of_range", "needs_validation"})
# The answer field a comparison reads, as the Qualtrics locator it was read from.
_FIELD_LOCATORS = {"text_entry": "ChoiceTextEntryValue", "number_entry": "ChoiceNumericEntryValue", "selected_count": "SelectedChoicesCount"}
_LOOP_FUNCTIONS = {"all": "evaluateAllInArrayFunction", "any": "evaluateAnyInArrayFunction"}


# ---------------------------------------------------------------- answers


def _clean(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _usable(answer: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(answer, dict):
        return None
    if str(answer.get("answer_status") or "").strip() in _NO_ANSWER_STATUSES:
        return None
    return answer


def _answer_tokens(answer: dict[str, Any] | None) -> set[str]:
    if not answer:
        return set()
    tokens: set[str] = set()
    for key in ("answer_value", "answer_label", "answer_raw"):
        value = _clean(answer.get(key))
        if not value:
            continue
        tokens.add(value.lower())
        for part in re.split(r"[,;|]", value):
            part = _clean(part).lower()
            if part:
                tokens.add(part)
    return tokens


def _is_split_yes(answer: dict[str, Any] | None) -> bool:
    tokens = _answer_tokens(answer)
    if not tokens:
        return False
    if tokens & {"yes", "true", "1", "selected", "select", "checked"}:
        return True
    if tokens & {"no", "false", "0", "not selected", "unselected", "unchecked"}:
        return False
    return bool(tokens)


def _numeric(answer: dict[str, Any] | None) -> float | None:
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


def _locator_parts(locator: Any) -> tuple[str, str, str]:
    raw = str(locator or "").strip()
    match = _Q_LOCATOR_RE.match(raw)
    if not match:
        return raw, "", ""
    return match.group(1).strip(), match.group(2).strip(), (match.group(3) or "").strip()


def _strings(value: Any) -> list[str]:
    found: list[str] = []
    if isinstance(value, str):
        found.append(value)
    elif isinstance(value, list):
        for item in value:
            found += _strings(item)
    elif isinstance(value, dict):
        for item in value.values():
            found += _strings(item)
    return found


def _has_text(value: Any) -> bool:
    return any(text.strip() for text in _strings(value))


# ---------------------------------------------------------------- what a stage holds


@dataclass
class Stage:
    questions: list[dict[str, Any]]
    blocks: list[dict[str, Any]]
    embedded_data: dict[str, Any]
    survey_sequence_text: str
    survey_sequence_media: list[dict[str, Any]]
    audit_event: dict[str, Any]


@dataclass
class State:
    persona_id: str
    persona_index: int
    answers_by_id: dict[str, dict[str, Any]] = field(default_factory=dict)
    questions_by_id: dict[str, dict[str, Any]] = field(default_factory=dict)
    displayed_qids: set[str] = field(default_factory=set)
    rendered_questions: list[dict[str, Any]] = field(default_factory=list)
    rendered_answers: list[dict[str, Any]] = field(default_factory=list)
    embedded_data: dict[str, Any] = field(default_factory=dict)
    randomizer_choices: dict[str, list[int]] = field(default_factory=dict)
    audit_events: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    # random values drawn so far, by place: a later pass draws the same
    random_draws: dict[str, str] = field(default_factory=dict)
    done: bool = False
    # variables given from outside the survey (a panel, the link), set before the flow
    outside_embedded_data: dict[str, Any] = field(default_factory=dict)
    stopped_at_limit: bool = False
    # the questions shown so far on the current pass through the flow: logic and
    # piped text reading any other question see it as never shown, with no answer
    seen_in_walk: set[str] = field(default_factory=set)
    # every variable value given on the latest pass, in order
    assignments: list[dict[str, str]] = field(default_factory=list)
    # each conjoint or MaxDiff block's tasks, drawn when the flow first reaches it
    experiments: dict[str, dict[str, Any]] = field(default_factory=dict)
    # text only, as a stage prompt first showed it, by question ID
    shown_text: dict[str, dict[str, Any]] = field(default_factory=dict)
    # the text the latest stage showed after its last question
    carried_text: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class _Result:
    blocked: bool = False
    done: bool = False
    paused: bool = False


def prior_answer_context(state: State) -> str:
    """A twin's earlier answers, as the next stage's prompt opens with them."""
    rows: list[str] = []
    for index, (question, answer) in enumerate(zip(state.rendered_questions, state.rendered_answers, strict=False), start=1):
        if not isinstance(question, dict) or not isinstance(answer, dict):
            continue
        question_text = _clean(question.get("text")) or _clean(question.get("id")) or f"Question {index}"
        answer_text = _clean(answer.get("answer_label") or answer.get("answer_value") or answer.get("answer_raw")) or "(blank)"
        rows.append(f"{index}. {question_text}\n   Previous answer: {answer_text}")
    if not rows:
        return ""
    return (
        "Earlier answers in this same survey:\n"
        "Treat these as this same respondent's own prior answers. "
        "Stay consistent with them unless the current question explicitly asks for a change.\n" + "\n".join(rows)
    )


def sequence_with_prior_context(*, prior_answer_context: str, survey_sequence_text: str) -> str:
    prior = str(prior_answer_context or "").strip()
    current = str(survey_sequence_text or "").strip()
    if prior and current:
        return f"{prior}\n\nCurrent stage survey text:\n{current}"
    return prior or current


def _sent_to_model(question: dict[str, Any]) -> bool:
    """Whether a stage question goes to the model (a prefilled box and a participant
    ID box are filled without a call)."""
    if question.get("prefill_only"):
        return False
    text = re.sub(r"\s+", " ", str(question.get("text", "") or "")).strip().lower()
    return not (str(question.get("type", "open_text")).lower() == "open_text" and "prolific" in text and "id" in text)


def _is_instruction(shown: dict[str, Any]) -> bool:
    return render.Shown(shown).kind == "instruction"


def _prompt_questions(blocks: list[dict[str, Any]]) -> list[tuple[dict[str, Any], bool]]:
    """The questions a stage prompt shows, in order, each with whether it is asked."""
    found = []
    for block in blocks:
        for shown in block["questions"]:
            kind = render.Shown(shown).kind
            if kind is None:
                continue
            found.append((shown, kind != "instruction"))
    return found


def _earlier_question_media(answered: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The pictures of questions answered at an earlier stage of the block, labelled
    as earlier (they take no Q<n> label, which names only what is asked now)."""
    media = render.sequence_media(answered) if answered else []
    for item in media:
        old = str(item.get("prompt_label", "") or "")
        item["prompt_label"] = "an earlier question"
        for entry in item.get("media") or []:
            label = str(entry.get("label", "") or "")
            if old and label.startswith(old):
                entry["label"] = "an earlier question" + label[len(old):]
            entry["prompt_label"] = "an earlier question"
        text = str(item.get("question_text_with_pictures", "") or "")
        if old and text:
            # the labels at the pictures' places name the question the same way
            named = re.compile(rf"\b(attached to|in) {re.escape(old)}\b")
            item["question_text_with_pictures"] = PICTURE_MARKER_RE.sub(
                lambda match: picture_marker(match.group(1), named.sub(r"\1 an earlier question", match.group(2))),
                text,
            )
    return media


# ---------------------------------------------------------------- facts about questions


def _choice_facts(shown: dict[str, Any]) -> dict[str, Any]:
    """What logic reads of a question's options: labels, IDs and recodes (in the
    order written), whether it is a grid, and its text."""
    question = shown["question"]
    view = render.Shown(shown)
    items: list[dict[str, Any]] = []
    if view.kind == "choice":
        items = (question.get("response") or {}).get("options") or []
    elif view.kind == "ranking":
        items = (question.get("response") or {}).get("items") or []
    elif view.kind in {"grid", "grid_text", "grid_sum"}:
        items = [c.raw for c in view.columns]
    elif view.kind == "group":
        items = _group_options(question)
    labels = [str(item.get("label", "")) for item in items]
    return {
        "options": labels,
        "option_ids": [str(item["id"]) for item in items],
        "recode_values": {str(item["id"]): item["code"] for item in items if "code" in item},
        "matrix": view.kind in {"grid", "grid_text", "grid_sum"},
        "text": view.text,
        "never_asked": view.kind in {None, "instruction"},
    }


def _group_options(question: dict[str, Any]) -> list[dict[str, Any]]:
    """A side-by-side question's own options: as the source listed them, else the
    first column's (with options)."""
    source = question.get("source") if isinstance(question.get("source"), dict) else {}
    sbs = source.get("sbs") if isinstance(source.get("sbs"), dict) else None
    if sbs is not None and isinstance(sbs.get("Options"), list):
        ids = sbs.get("OptionsID") if isinstance(sbs.get("OptionsID"), list) else [str(i + 1) for i in range(len(sbs["Options"]))]
        return [{"id": str(i), "label": str(label)} for i, label in zip(ids, sbs["Options"])]
    for field_ in (question.get("response") or {}).get("fields") or []:
        row_response = (field_.get("response") or {}).get("row_response") or {}
        if row_response.get("type") != "text" and row_response.get("options"):
            options = row_response["options"]
            return [{"id": str(o["id"]), "label": label} for o, label in zip(options, item_labels(options, "Image option"))]
    return []


# ---------------------------------------------------------------- loop rows


def _is_own(qid: str, own: set[str]) -> bool:
    return qid in own or qid.split("#", 1)[0] in own


def _row_id(value: str, row: str, own: set[str]) -> str:
    return f"{row}_{value}" if _is_own(str(value), own) else value


def _row_pipes(text: str, row: str, own: set[str]) -> str:
    def sub(match: re.Match[str]) -> str:
        parts = match.group(1).split("/")
        if _is_own(parts[0].strip(), own):
            return "${q://" + f"{row}_{parts[0].strip()}" + ("/" + "/".join(parts[1:]) if len(parts) > 1 else "") + "}"
        return match.group(0)

    return _Q_PIPE_RE.sub(sub, text)


def _scope_condition(condition: Any, row: str, own: set[str]) -> Any:
    """A condition read inside a repeat row: one that reads the block's own question
    (for the current row) reads this row's copy of it."""
    if isinstance(condition, list):
        return [_scope_condition(item, row, own) for item in condition]
    if not isinstance(condition, dict):
        return condition
    op = condition.get("op")
    if op == "source":
        return {**condition, "qualtrics": _scope_qualtrics(condition.get("qualtrics"), row, own)}
    out = {key: _scope_condition(value, row, own) if key in {"args", "arg"} else value for key, value in condition.items()}
    if isinstance(out.get("right"), str):
        out["right"] = _row_pipes(out["right"], row, own)
    if op in {"selected", "not_selected", "displayed", "not_displayed"} and not out.get("loop"):
        out["question"] = _row_id(str(out.get("question", "")), row, own)
    left = out.get("left")
    if isinstance(left, dict) and left.get("scope") == "answer":
        out["left"] = {**left, "question": _row_id(str(left.get("question", "")), row, own)}
    return out


_LOCATOR_QID_RE = re.compile(r"^(q://)([^/]+)(.*)$", re.DOTALL)
_LOCATOR_KEYS = ("LeftOperand", "ChoiceLocator", "Locator", "QuestionIDFromLocator")


def _row_reference(value: Any, row: str, own: set[str]) -> Any:
    if not isinstance(value, str):
        return value
    text = value.strip()
    match = _LOCATOR_QID_RE.match(text)
    if match:
        return f"{match.group(1)}{row}_{match.group(2)}{match.group(3)}" if _is_own(match.group(2), own) else value
    return f"{row}_{text}" if _is_own(text, own) else value


def _loop_spec(condition: dict[str, Any]) -> tuple[str, str]:
    spec = str(condition.get("LoopAndMergeLoops", "") or "").strip()
    if "," not in spec:
        return "", "current"
    block_id, _, which = spec.partition(",")
    return block_id.strip(), which.strip() or "current"


def _row_condition(condition: dict[str, Any], row: str, own: set[str]) -> dict[str, Any]:
    scoped = {key: value for key, value in condition.items() if key not in {"LoopAndMergeLoops", "LoopTarget", "FunctionName"}}
    for key in _LOCATOR_KEYS + ("QuestionID", "SkipToDestination"):
        if key in scoped:
            scoped[key] = _row_reference(scoped[key], row, own)
    return scoped


def _scope_qualtrics(value: Any, row: str, own: set[str]) -> Any:
    """Qualtrics logic kept as written, read inside a repeat row (as the template
    runtime scoped it)."""
    if isinstance(value, str):
        return _row_pipes(value, row, own)
    if isinstance(value, list):
        return [_scope_qualtrics(item, row, own) for item in value]
    if isinstance(value, dict):
        scoped = {key: _scope_qualtrics(child, row, own) for key, child in value.items()}
        if any(key in scoped for key in _LOCATOR_KEYS[:3]) and _loop_spec(scoped)[1] == "current":
            scoped = _row_condition(scoped, row, own)
        return scoped
    return value


def _fill_loop_value(text: str, row: dict[str, Any]) -> str:
    """Every ${lm://...} filled from the row (blank when the row has no such value)."""

    def sub(match: re.Match[str]) -> str:
        token = str(match.group(1) or "").strip()
        if token == "CurrentLoopNumber":
            return str(row.get("current_loop_number", ""))
        if token == "TotalLoops":
            return str(row.get("total_loops", ""))
        if token.startswith("Field/"):
            return str((row.get("fields") or {}).get(token.removeprefix("Field/").strip(), ""))
        return ""

    return _LM_FIELD_RE.sub(sub, text)


def _map_all_strings(value: Any, fn: Any) -> Any:
    if isinstance(value, str):
        return fn(value)
    if isinstance(value, list):
        return [_map_all_strings(item, fn) for item in value]
    if isinstance(value, dict):
        return {key: _map_all_strings(item, fn) for key, item in value.items()}
    return value


# ---------------------------------------------------------------- the runtime


class StagedRuntime:
    def __init__(self, definition: Definition, *, seed: int = 42, max_stages: int = 25, persona_count: int | None = None):
        self.definition = definition
        self.seed = int(seed)
        self.max_stages = max(1, int(max_stages))
        self.persona_count = max(0, int(persona_count or 0))
        self._even_cache: dict[tuple[str, int, int, int], list[list[int]]] = {}
        # facts about every question as written, by ID (logic and pipes read them)
        self.facts: dict[str, dict[str, Any]] = {
            qid: _choice_facts(shown_question(question, native=definition.native)) for qid, question in definition.questions.items()
        }
        self.block_of: dict[str, str] = {}
        for block in definition.blocks.values():
            for qid in block.get("questions") or []:
                self.block_of.setdefault(str(qid), str(block["id"]))
        # the block being rendered: its questions' IDs, and the questions as written
        # (a repeat row's copy, with the row's values)
        self._block_ids: set[str] = set()
        self._block_written: dict[str, dict[str, Any]] = {}

    def new_state(self, *, persona_id: str, persona_index: int) -> State:
        return State(persona_id=str(persona_id), persona_index=int(persona_index))

    # ---- a stage

    def render_next_stage(self, state: State) -> Stage:
        if state.done:
            return Stage([], [], dict(state.embedded_data), "", [], {"stage_index": len(state.audit_events) + 1, "events": []})
        stage_index = len(state.audit_events) + 1
        variables: dict[str, Any] = dict(state.outside_embedded_data)
        state.seen_in_walk = set()
        state.assignments = [{"source": "outside", "flow_id": "", "field": str(k), "value": str(v)} for k, v in state.outside_embedded_data.items()]
        blocks: list[dict[str, Any]] = []
        events: list[dict[str, Any]] = []
        result = self._walk(self.definition.flow, state, variables, blocks, events)
        state.embedded_data = dict(variables)

        questions = [
            dict(q)
            for q in render.prompt_questions([s for b in blocks for s in b["questions"]])
            if str(q.get("id", "") or "").strip() and str(q.get("id", "") or "").strip() not in state.answers_by_id
        ]
        if questions and stage_index > self.max_stages:
            state.done = True
            state.stopped_at_limit = True
            warning = f"Stopped QSF staged runtime after {self.max_stages} stages."
            state.warnings.append(warning)
            audit_event = {"stage_index": stage_index, "events": [{"type": "warning", "message": warning}]}
            state.audit_events.append(audit_event)
            return Stage([], [], dict(state.embedded_data), "", [], audit_event)
        if not questions:
            if result.blocked:
                warning = "QSF staged runtime stalled waiting for a question dependency that was never answered."
                state.warnings.append(warning)
                events.append({"type": "warning", "message": warning})
            state.done = True
        audit_event = {
            "stage_index": stage_index,
            "question_ids": [str(q.get("id", "")) for q in questions],
            "embedded_data": dict(variables),
            "blocked": result.blocked and not result.paused,
            **({"task_boundary": True} if result.paused else {}),
            "done": state.done or result.done,
            "events": events,
        }
        state.audit_events.append(audit_event)
        prompt_blocks, answered = self._prompt_blocks(blocks, questions, state)
        prompt_blocks = self._with_carried_text(prompt_blocks, state, audit_event, questions)
        shown = [s for b in prompt_blocks for s in b["questions"]]
        return Stage(
            questions=questions,
            blocks=blocks,
            embedded_data=dict(variables),
            survey_sequence_text=render.sequence_text(shown),
            survey_sequence_media=_earlier_question_media(answered) + render.sequence_media(shown),
            audit_event=audit_event,
        )

    def _prompt_blocks(self, blocks: list[dict[str, Any]], questions: list[dict[str, Any]], state: State) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """What a stage's prompt shows: the questions it asks, and the text only among
        them as it was first shown. A question answered at an earlier stage goes with
        the earlier answers instead (its pictures still go with the stage)."""
        asked = {str(q.get("id", "") or "") for q in questions}
        out = []
        answered: list[dict[str, Any]] = []
        for block in copy.deepcopy(blocks):
            kept = []
            for shown in block["questions"]:
                qid = str(shown["question"].get("id", "") or "")
                if _is_instruction(shown):
                    kept.append(copy.deepcopy(state.shown_text.setdefault(qid, copy.deepcopy(shown))))
                    continue
                units = render.prompt_questions([shown])
                if any(str(u.get("id", "") or "") in asked for u in units):
                    state.shown_text.setdefault(qid, copy.deepcopy(shown))
                    kept.append(shown)
                elif any(str(u.get("id", "") or "") in state.answers_by_id for u in units):
                    answered.append(copy.deepcopy(state.shown_text.get(qid, shown)))
            out.append({**block, "questions": kept})
        return out, answered

    @staticmethod
    def _with_carried_text(blocks: list[dict[str, Any]], state: State, audit_event: dict[str, Any], questions: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """The stage's prompt, opening with the text only the previous stage showed
        after its last question (unless shown again here); and what this stage
        shows after its own last question, kept for the next."""
        shown = _prompt_questions(blocks)
        if not any(asked for _q, asked in shown):
            return blocks
        if not any(_sent_to_model(q) for q in questions):
            kept = {str(q["question"].get("id", "") or "") for q in state.carried_text}
            state.carried_text = state.carried_text + [
                copy.deepcopy(q) for q, asked in shown if not asked and str(q["question"].get("id", "") or "") not in kept
            ]
            return blocks
        shown_now = {str(q["question"].get("id", "") or "") for q, _asked in shown}
        carried = [copy.deepcopy(q) for q in state.carried_text if str(q["question"].get("id", "") or "") not in shown_now]
        if carried:
            blocks = [{"block": "", "title": "", "row": None, "questions": carried}] + blocks
            audit_event["carried_text_ids"] = [str(q["question"].get("id", "") or "") for q in carried]
        trailing: list[dict[str, Any]] = []
        for q, asked in _prompt_questions(blocks):
            trailing = [] if asked else trailing + [copy.deepcopy(q)]
        state.carried_text = trailing
        return blocks

    def apply_answers(self, state: State, *, questions: list[dict[str, Any]], answers: list[dict[str, Any]]) -> None:
        by_qid = {
            str(a.get("question_id", "") or "").strip(): a for a in answers if isinstance(a, dict) and str(a.get("question_id", "") or "").strip()
        }
        for question in questions:
            qid = str(question.get("id", "") or "").strip()
            if not qid:
                continue
            state.questions_by_id[qid] = dict(question)
            answer = dict(by_qid.get(qid, {})) or {"question_id": qid, "answer_value": "", "answer_label": "", "answer_raw": ""}
            answer["question_id"] = qid
            state.answers_by_id[qid] = answer
            state.rendered_questions.append(dict(question))
            state.rendered_answers.append(dict(answer))

    # ---- the flow

    def _walk(self, nodes: list[dict[str, Any]], state: State, variables: dict[str, Any], blocks: list[dict[str, Any]], events: list[dict[str, Any]]) -> _Result:
        for node in nodes:
            if not isinstance(node, dict):
                continue
            if state.done:
                return _Result(done=True)
            kind = node.get("type")
            if kind == "set_variables":
                values = [assignment_value(a) for a in node.get("assignments") or [] if not a.get("from_outside")]
                if self._waits_for_pipe(values, state):
                    events.append({"type": "blocked", "reason": "embedded_data_q_piping"})
                    return _Result(blocked=True)
                self._assign(node, state, variables, events)
                continue
            if kind == "branch":
                for index, (condition, steps) in enumerate(branch_parts(node)):
                    flow = flow_id(node) + ("_else" if index else "")
                    if self._waits_for_answer(condition, state):
                        events.append({"type": "blocked", "reason": "branch_question_dependency", "flow_id": flow})
                        return _Result(blocked=True)
                    if (_is_screenout(steps) and _undecidable(condition)) or (only_ignored(condition) and _ends(steps)):
                        events.append({"type": "branch", "flow_id": flow, "taken": False, "reason": "undecidable_screenout_source"})
                        continue
                    taken = self._holds(condition, state, variables)
                    events.append({"type": "branch", "flow_id": flow, "taken": bool(taken)})
                    if taken:
                        result = self._walk(steps, state, variables, blocks, events)
                        if result.blocked or result.done:
                            return result
                continue
            if kind == "randomize":
                children = node.get("children") or []
                selected = self._randomizer_indexes(node, state, len(children))
                events.append({"type": "randomizer", "flow_id": flow_id(node), "even_presentation": node.get("evenly") is True, "selected_indexes": selected})
                for index in selected:
                    if 0 <= index < len(children):
                        result = self._walk([children[index]], state, variables, blocks, events)
                        if result.blocked or result.done:
                            return result
                continue
            if kind == "group":
                result = self._walk(node.get("children") or [], state, variables, blocks, events)
                if result.blocked or result.done:
                    return result
                continue
            if kind == "show_block":
                result = self._show_block(str(node.get("block")), state, variables, blocks, events)
                if result is not None:
                    return result
                continue
            if kind == "end":
                state.done = True
                events.append({"type": "end_survey", "flow_id": flow_id(node)})
                return _Result(done=True)
        return _Result()

    def _assign(self, node: dict[str, Any], state: State, variables: dict[str, Any], events: list[dict[str, Any]]) -> None:
        fields: dict[str, Any] = {}
        flow = flow_id(node)
        times: dict[str, int] = {}
        for assignment in node.get("assignments") or []:
            if assignment.get("from_outside"):
                continue
            name = str(assignment.get("variable", "") or "").strip()
            if not name:
                continue
            value = assignment_value(assignment)
            if value in (None, ""):
                continue  # no value: an earlier or outside value stays
            times[name] = times.get(name, 0) + 1
            again = f"#{times[name]}" if times[name] > 1 else ""
            rendered = self._render_text(str(value), state, variables, f"ed/{flow}/{name}{again}")
            variables[name] = rendered
            state.assignments.append({"source": "flow", "flow_id": flow, "field": name, "value": str(rendered)})
            fields[name] = rendered
        if fields:
            events.append({"type": "embedded_data", "flow_id": flow, "fields": fields})

    # ---- randomizers

    def _randomizer_indexes(self, node: dict[str, Any], state: State, child_count: int) -> list[int]:
        if child_count <= 0:
            return []
        flow = flow_id(node) or str(node.get("id") or "")
        if flow in state.randomizer_choices:
            return list(state.randomizer_choices[flow])
        count = max(1, min(int(node.get("take") or 1), child_count))
        children = node.get("children") or []
        if count == child_count and is_assignment_arms(children):
            if node.get("evenly") is True:
                assigned = self._even_indexes(flow=flow, child_count=child_count, count=1, persona_index=state.persona_index)[0]
            else:
                assigned = self._independent_indexes(flow=flow, persona_id=state.persona_id, child_count=child_count, count=1)[0]
            ordered = self._independent_indexes(flow=f"{flow}:presentation-order", persona_id=state.persona_id, child_count=child_count, count=child_count)
            indexes = [index for index in ordered if index != assigned] + [assigned]
        elif node.get("evenly") is True:
            indexes = self._even_indexes(flow=flow, child_count=child_count, count=count, persona_index=state.persona_index)
        else:
            indexes = self._independent_indexes(flow=flow, persona_id=state.persona_id, child_count=child_count, count=count)
        state.randomizer_choices[flow] = indexes
        return indexes

    def _independent_indexes(self, *, flow: str, persona_id: str, child_count: int, count: int) -> list[int]:
        ranked = sorted(
            (hashlib.sha256(f"{self.seed}:randomizer:{flow}:persona:{persona_id}:child:{index}".encode("utf-8")).hexdigest(), index)
            for index in range(child_count)
        )
        return [index for _digest, index in ranked[:count]]

    def _even_indexes(self, *, flow: str, child_count: int, count: int, persona_index: int) -> list[int]:
        if self.persona_count <= 0:
            return self._independent_indexes(flow=flow, persona_id=str(persona_index), child_count=child_count, count=count)
        key = (flow, child_count, count, self.persona_count)
        schedule = self._even_cache.get(key)
        if schedule is None:
            schedule = self._even_schedule(flow=flow, child_count=child_count, count=count)
            self._even_cache[key] = schedule
        if not schedule:
            return []
        return list(schedule[persona_index % len(schedule)])

    def _even_schedule(self, *, flow: str, child_count: int, count: int) -> list[list[int]]:
        total = max(0, self.persona_count * count)
        if total <= 0:
            return []
        bag = [index % child_count for index in range(total)]
        digest = hashlib.sha256(f"{self.seed}:even-presentation:{flow}:{child_count}:{count}:{self.persona_count}".encode("utf-8")).hexdigest()
        random.Random(int(digest[:16], 16)).shuffle(bag)
        schedule: list[list[int]] = []
        cursor = 0
        for _persona in range(self.persona_count):
            picks: list[int] = []
            for _slot in range(count):
                swap_at = cursor
                for candidate in range(cursor, len(bag)):
                    if bag[candidate] not in picks:
                        swap_at = candidate
                        break
                bag[cursor], bag[swap_at] = bag[swap_at], bag[cursor]
                picks.append(bag[cursor])
                cursor += 1
            schedule.append(picks)
        return schedule

    def _rng(self, *parts: str) -> random.Random:
        digest = hashlib.sha256(":".join([str(self.seed), *parts]).encode("utf-8")).hexdigest()
        return random.Random(int(digest[:16], 16))

    # ---- blocks

    def _rows(self, block: dict[str, Any], state: State | None) -> list[dict[str, Any]] | None:
        """The rows a repeated block shows this twin, in order (None when it does not
        repeat). Without a state, the rows as written."""
        repeat = block.get("repeat")
        if not repeat or not repeat.get("rows"):
            return None
        rows = list(repeat["rows"])
        if state is None:
            return rows
        if repeat.get("order") == "shuffle":
            self._rng("loop-and-merge", str(block["id"]), state.persona_id).shuffle(rows)
        if repeat.get("take"):
            rows = rows[: int(repeat["take"])]
        return rows

    def _expand(self, block: dict[str, Any], state: State) -> list[dict[str, Any]]:
        """A block as this twin sees it: a conjoint or MaxDiff block as its tasks
        (drawn once, then kept), a repeated block once per row."""
        block_id = str(block["id"])
        native = self.definition.native
        if block.get("kind") in {"conjoint", "maxdiff"}:
            if block_id not in state.experiments:
                tasks, records = experiment_questions(block, seed=self.seed, twin=state.persona_id, twin_index=state.persona_index)
                title = str(block.get("title") or block_id)
                if str((block.get("administration") or {}).get("unit") or "block") == "block":
                    expanded = [{"block": block_id, "title": title, "row": None, "questions": [q for task in tasks for q in task]}]
                else:
                    expanded = [
                        {"block": f"{block_id}_task{n}", "title": title, "row": None, "questions": questions, "stage_break": True}
                        for n, questions in enumerate(tasks, start=1)
                    ]
                state.experiments[block_id] = {"blocks": expanded, "records": records}
            return copy.deepcopy(state.experiments[block_id]["blocks"])
        questions = [
            shown_question(self.definition.questions[str(qid)], native=native, brand=self.definition.brand)
            for qid in block.get("questions") or []
            if str(qid) in self.definition.questions
        ]
        title = str(block.get("title") or "")
        rows = self._rows(block, state)
        if rows is None:
            return [{"block": block_id, "title": title, "row": None, "questions": questions}]
        own = {str(s["question"]["id"]) for s in questions}
        out = []
        for number, row in enumerate(rows, start=1):
            context = {
                "row_id": str(row["id"]),
                "fields": {str(k): join_text(v) for k, v in (row.get("values") or {}).items()},
                "current_loop_number": number,
                "total_loops": len(rows),
            }
            copies = []
            for shown in questions:
                shown = copy.deepcopy(shown)
                question = _map_all_strings(shown["question"], lambda text: _fill_loop_value(text, context))
                question = self._scope(question, str(row["id"]), own)
                shown["question"] = question
                shown["row"] = context
                copies.append(shown)
            out.append({"block": f"{block_id}_{row['id']}", "title": f"{title} (Loop {number}/{len(rows)})" if title else title, "row": context, "questions": copies})
        return out

    def _scope(self, question: dict[str, Any], row: str, own: set[str]) -> dict[str, Any]:
        """A question of a repeat row: its ID, its logic and its piped answers name this
        row's copies of the block's own questions."""
        question = dict(question)
        question["id"] = f"{row}_{question['id']}"
        response = question.get("response")
        if isinstance(response, dict) and response.get("type") == "group":
            for field_ in response.get("fields") or []:
                source = field_.get("source") if isinstance(field_.get("source"), dict) else {}
                if str(source.get("QuestionID", "") or "").strip():
                    source["QuestionID"] = f"{row}_{str(source['QuestionID']).strip()}"
        map_text(question, lambda _place, text: _row_pipes(text, row, own))
        for key in ("show_if",):
            if key in question:
                question[key] = _scope_condition(question[key], row, own)
        if "skip" in question:
            rules = []
            for rule in question["skip"]:
                rule = dict(rule)
                rule["when"] = _scope_condition(rule.get("when"), row, own)
                to = str(rule.get("to", ""))
                if to not in {"end_of_block", "end_of_survey"}:
                    rule["to"] = _row_id(to, row, own)
                rules.append(rule)
            question["skip"] = rules
        for item in _items_with_conditions(question):
            item["show_if"] = _scope_condition(item["show_if"], row, own)
        return question

    def _show_block(self, block_id: str, state: State, variables: dict[str, Any], blocks: list[dict[str, Any]], events: list[dict[str, Any]]) -> _Result | None:
        block = self.definition.blocks.get(block_id)
        if block is None:
            return None
        for shown_block in self._expand(block, state):
            if shown_block.get("stage_break") and self._stage_asks(blocks, state):
                events.append({"type": "task_boundary", "block_id": shown_block["block"]})
                return _Result(blocked=True, paused=True)
            own = {str(s["question"]["id"]) for s in shown_block["questions"]}
            if self._waits_for_pipe([s["question"] for s in shown_block["questions"]], state, besides=own):
                events.append({"type": "blocked", "reason": "block_q_piping", "block_id": shown_block["block"]})
                return _Result(blocked=True)
            displayed_before = set(state.displayed_qids)
            rendered, blocked, done = self._render_block(shown_block, block, state, variables, events)
            units = render.prompt_questions(rendered["questions"])
            new_units = [u for u in units if str(u.get("id", "") or "").strip() and str(u.get("id", "") or "").strip() not in state.answers_by_id]
            new_text = [
                s for s in rendered["questions"]
                if _is_instruction(s) and str(s["question"].get("id", "") or "").strip() and str(s["question"].get("id", "") or "").strip() not in displayed_before
            ]
            if new_units or new_text:
                blocks.append(rendered)
                events.append(
                    {
                        "type": "block",
                        "block_id": rendered["block"],
                        "block_name": rendered["title"],
                        "question_ids": [str(u.get("id", "")) for u in new_units],
                        **({"descriptive_ids": [str(s["question"].get("id", "")) for s in new_text]} if new_text else {}),
                    }
                )
            if done:
                state.done = True
                return _Result(done=True)
            if blocked:
                return _Result(blocked=True)
        return None

    def _stage_asks(self, blocks: list[dict[str, Any]], state: State) -> bool:
        return any(
            str(u.get("id", "") or "").strip() not in state.answers_by_id
            for u in render.prompt_questions([s for b in blocks for s in b["questions"]])
            if str(u.get("id", "") or "").strip()
        )

    def _render_block(self, shown_block: dict[str, Any], block: dict[str, Any], state: State, variables: dict[str, Any], events: list[dict[str, Any]]) -> tuple[dict[str, Any], bool, bool]:
        written = {str(s["question"].get("id", "") or "").strip(): s for s in shown_block["questions"]}
        self._block_ids = set(written)
        self._block_written = {qid: s["question"] for qid, s in written.items()}
        try:
            rendered = copy.deepcopy(shown_block)
            for shown in rendered["questions"]:
                self._render_question(shown, state, variables)
        finally:
            self._block_ids = set()
            self._block_written = {}
        # a preset answer that pipes a variable with no value here keeps its token: a
        # box filled from a panel or the link is taken as filled from outside, not asked
        for shown in rendered["questions"]:
            before = written.get(str(shown["question"].get("id", "") or "").strip())
            if before is not None:
                _keep_unfilled_defaults(before["question"], shown["question"])
        self._order_questions(rendered, block, state, events)
        self._order_options(rendered, state, events)

        visible: list[dict[str, Any]] = []
        skip_to = ""
        end_block = end_survey = False
        order = [str(s["question"].get("id", "") or "").strip() for s in rendered["questions"]]
        for position, shown in enumerate(rendered["questions"]):
            question = shown["question"]
            qid = str(question.get("id", "") or "").strip()
            if skip_to:
                if qid != skip_to:
                    continue
                skip_to = ""
            condition = question.get("show_if")
            if condition:
                if self._waits_for_answer((written.get(qid) or shown)["question"].get("show_if") or condition, state):
                    rendered["questions"] = visible
                    events.append({"type": "blocked", "reason": "question_display_logic_dependency", "question_id": qid})
                    return rendered, True, False
                if not self._holds(condition, state, variables):
                    continue
            item_conditions = choices.item_conditions((written.get(qid) or shown)["question"])
            if item_conditions and choices.has_item_conditions(question):
                # options shown on an earlier answer wait for it
                if any(self._waits_for_answer(c, state) for c in item_conditions):
                    rendered["questions"] = visible
                    events.append({"type": "blocked", "reason": "choice_display_logic_dependency", "question_id": qid})
                    return rendered, True, False
                hidden = choices.hidden_items(question, lambda c: self._holds(c, state, variables))
                choices.drop_item_conditions(question)
                if hidden:
                    if not choices.remove_items(question, hidden):
                        # no option or statement left: Qualtrics does not show the question
                        events.append({"type": "choices_all_hidden", "question_id": qid})
                        continue
                    shown["hidden"] = [item for ids in hidden.values() for item in ids]
            earlier = set(order[:position])
            if earlier and self._waits_for_pipe([(written.get(qid) or shown)["question"]], state, among=earlier):
                # pipes an answer given earlier in this block: what comes before it is
                # asked now, this question once that answer exists
                rendered["questions"] = visible
                events.append({"type": "blocked", "reason": "question_q_piping", "question_id": qid})
                return rendered, True, False
            visible.append(shown)
            if qid:
                state.displayed_qids.add(qid)
                state.seen_in_walk.add(qid)
            rules = question.get("skip") or []
            if not rules:
                continue
            if qid and not self._has_answer(qid, state):
                rendered["questions"] = visible
                events.append({"type": "blocked", "reason": "skip_logic_question_dependency", "question_id": qid})
                return rendered, True, False
            destination = self._skip_destination(rules, state, variables)
            if not destination:
                continue
            events.append({"type": "skip_logic", "question_id": qid, "destination": destination})
            if destination.upper() == "ENDOFSURVEY":
                end_survey = True
                break
            if destination.upper() == "ENDOFBLOCK":
                end_block = True
                break
            skip_to = destination
        rendered["questions"] = visible
        if end_block:
            return rendered, False, False
        return rendered, False, end_survey

    def _render_question(self, shown: dict[str, Any], state: State, variables: dict[str, Any]) -> None:
        question = shown["question"]
        qid = str(question.get("id", "") or "")
        map_text(question, lambda place, text: self._render_text(text, state, variables, f"q/{qid}/{place}"))
        fill = lambda text: self._render_condition_text(text, state, variables)  # noqa: E731
        for key in ("show_if",):
            if key in question:
                question[key] = map_condition_text(question[key], fill)
        if "skip" in question:
            question["skip"] = [{**rule, "when": map_condition_text(rule.get("when"), fill)} for rule in question["skip"]]
        for item in _items_with_conditions(question):
            item["show_if"] = map_condition_text(item["show_if"], fill)

    def _order_questions(self, rendered: dict[str, Any], block: dict[str, Any], state: State, events: list[dict[str, Any]]) -> None:
        randomization = block.get("randomization") if block.get("kind") == "survey" else None
        questions = rendered["questions"]
        if not randomization or len(questions) <= 1:
            return
        if randomization.get("order") != "shuffle":
            message = "Advanced block question randomization was preserved but not executed."
            rendered["warning"] = message
            events.append({"type": "question_randomization_warning", "block_id": rendered["block"], "message": message})
            return
        self._rng("question-randomization", rendered["block"], state.persona_id).shuffle(questions)
        ids = [str(s["question"].get("id", "") or "").strip() for s in questions]
        rendered["question_order"] = ids
        events.append({"type": "question_randomization", "block_id": rendered["block"], "question_ids": ids})

    def _order_options(self, rendered: dict[str, Any], state: State, events: list[dict[str, Any]]) -> None:
        for shown in rendered["questions"]:
            question = shown["question"]
            if not (question.get("response") or {}).get("randomization"):
                continue
            qid = str(question.get("id", "") or "").strip()
            applied = choices.order_items(question, self._rng("choice-randomization", qid, state.persona_id), state.persona_index)
            if not applied:
                continue
            shown["order"] = applied
            if qid and qid in state.answers_by_id:
                continue
            if applied.get("warning"):
                if applied["warning"] not in state.warnings:
                    state.warnings.append(applied["warning"])
                    events.append({"type": "choice_randomization_warning", "question_id": qid, "message": applied["warning"]})
                continue
            if applied.get("list") == "group":
                subs = []
                for done in applied["fields"]:
                    field_ = next(f for f in question["response"]["fields"] if str(f.get("id")) == done["field"])
                    source = field_.get("source") if isinstance(field_.get("source"), dict) else {}
                    subs.append({"QuestionID": source.get("QuestionID") or f"{qid}#{done['field']}", "PresentedOptionIDs": done["ids"]})
                record = {"Type": applied["type"], "SubQuestions": subs}
            else:
                record = {"Type": applied["type"], "PresentedOptionIDs": applied["ids"]}
            events.append({"type": "choice_randomization", "question_id": qid, "applied": record})

    def _skip_destination(self, rules: list[dict[str, Any]], state: State, variables: dict[str, Any]) -> str:
        for rule in rules:
            when = rule.get("when") or {}
            op = when.get("op")
            if op in {"selected", "not_selected"}:
                selected = self._is_selected(str(when.get("question", "")), str(when.get("option", "")), state)
                matches = selected if op == "selected" else not selected
            elif op == "source":
                matches = self._source_skip_matches(when.get("qualtrics") or {}, state)
            else:
                matches = False
            if matches:
                to = str(rule.get("to", "") or "").strip()
                return {"end_of_block": "ENDOFBLOCK", "end_of_survey": "ENDOFSURVEY"}.get(to, to)
        return ""

    def _source_skip_matches(self, rule: dict[str, Any], state: State) -> bool:
        qid, kind, value = _locator_parts(rule.get("ChoiceLocator") or rule.get("Locator") or rule.get("LeftOperand"))
        condition = str(rule.get("Condition", "") or rule.get("Operator", "") or "").strip()
        if kind == "SelectableChoice":
            selected = self._is_selected(qid, value, state)
            if condition == "Selected":
                return selected
            if condition == "NotSelected":
                return not selected
        return False

    # ---- text

    def _render_text(self, text: str, state: State, variables: dict[str, Any], place: str = "") -> str:
        """Piped text filled: random values (drawn once per twin and place), variables
        (blank when not set), earlier answers."""
        output = resolve_random_pipes_at(str(text), seed=self.seed, respondent=state.persona_id, path=place, drawn=state.random_draws)
        output = _ED_FIELD_RE.sub(lambda match: str(embedded_field_value(match.group(1), variables) or ""), output)
        replaced = 0

        def replace(match: re.Match[str]) -> str:
            nonlocal replaced
            replaced += 1
            return self._resolve_pipe(match.group(1), state)

        output = _Q_PIPE_RE.sub(replace, output)
        if replaced and output.endswith("Custom Value"):
            output = output[: -len("Custom Value")].rstrip()
        return output

    def _render_condition_text(self, text: str, state: State, variables: dict[str, Any]) -> str:
        """A value a condition compares with, as shown here (a random value is kept as written)."""
        output = _ED_FIELD_RE.sub(lambda match: str(embedded_field_value(match.group(1), variables) or ""), str(text))
        replaced = 0

        def replace(match: re.Match[str]) -> str:
            nonlocal replaced
            replaced += 1
            return self._resolve_pipe(match.group(1), state)

        output = _Q_PIPE_RE.sub(replace, output)
        if replaced and output.endswith("Custom Value"):
            output = output[: -len("Custom Value")].rstrip()
        return output

    def _fill_compared_value(self, text: str, state: State, variables: dict[str, Any]) -> str:
        """A value a condition compares with, filled when it is decided: variables
        (blank when not set) and earlier answers; a random value as written."""
        filled = _ED_FIELD_RE.sub(lambda match: str(embedded_field_value(match.group(1), variables) or ""), str(text))
        return _Q_PIPE_RE.sub(lambda match: self._resolve_pipe(match.group(1), state), filled)

    def _resolve_pipe(self, expression: str, state: State) -> str:
        parts = [part.strip() for part in str(expression or "").split("/") if part.strip()]
        if not parts:
            return ""
        qid = parts[0]
        kind = parts[1] if len(parts) > 1 else ""
        tail = parts[2:]
        in_block = qid in self._block_ids or qid.split("#", 1)[0] in self._block_ids
        if kind not in {"QuestionText", "ChoiceDescription"} and not in_block and self._never_shown(qid, state):
            return ""
        if kind == "QuestionText":
            entry = state.questions_by_id.get(qid)
            if entry:
                return _clean(entry.get("text") or "")
            written = self._block_written.get(qid)
            if written is not None:
                return _clean(render.Shown({"question": written}).text)
            facts = self.facts.get(qid) or self.facts.get(re.sub(r"^\d+_", "", qid))
            return _clean((facts or {}).get("text") or "")
        if kind == "ChoiceDescription" and tail:
            return _clean(self._choice_label(qid, tail[0], state))
        if kind == "ChoiceGroup" and tail and tail[0] in CHOICE_GROUP_PIPE_FORMS:
            return self._choice_group(qid, tail[0], tail[1:], state)
        if kind in {"SelectedChoices", "SelectedChoice"}:
            return ", ".join(self._selected_labels(qid, state))
        if kind == "SelectedChoicesCount":
            return str(len(self._selected_keys(qid, state)))
        if kind in ENTRY_PIPE_KINDS:
            answer = None
            if tail and kind == "ChoiceTextEntryValue":
                answer = state.answers_by_id.get(f"{qid}_{tail[0]}_TEXT")
            answer = answer or (state.answers_by_id.get(f"{qid}_{tail[0]}") if tail else None)
            answer = answer or state.answers_by_id.get(qid)
            if answer is None:
                answer = next((stored for answer_id, stored in state.answers_by_id.items() if answer_id.startswith(f"{qid}_")), None)
            answer = _usable(answer)
            return _clean((answer or {}).get("answer_value") or (answer or {}).get("answer_label") or (answer or {}).get("answer_raw"))
        answer = _usable(state.answers_by_id.get(qid))
        return _clean((answer or {}).get("answer_label") or (answer or {}).get("answer_value") or (answer or {}).get("answer_raw"))

    # ---- what logic reads of options

    def _written_facts(self, qid: str) -> dict[str, Any] | None:
        written = self._block_written.get(qid)
        return _choice_facts({"question": written}) if written is not None else None

    def _source(self, qid: str, state: State) -> dict[str, Any] | None:
        found = self.facts.get(qid) or self._written_facts(qid)
        if found:
            return found
        written = self.facts.get(re.sub(r"^\d+_", "", qid))
        entry = state.questions_by_id.get(qid)
        if written is not None and isinstance(entry, dict) and isinstance(entry.get("options"), list):
            # a repeat row read from another block: its options as the twin saw them
            # (row values filled in), with the question's own recodes
            return {"options": entry["options"], "option_ids": entry.get("option_ids"), "recode_values": written.get("recode_values")}
        return written or entry

    def _selected_keys(self, qid: str, state: State) -> list[str]:
        source = self._source(qid, state)
        base = _usable(state.answers_by_id.get(qid))
        if base is not None and _answer_tokens(base):
            return [c.qsf_key for c in _question_choices(source) if answer_selects_qsf_choice(source, c.qsf_key, base)]
        prefix = f"{qid}_"
        selected = {
            answer_id[len(prefix):]
            for answer_id, answer in state.answers_by_id.items()
            if answer_id.startswith(prefix) and not answer_id.endswith("_TEXT") and _is_split_yes(_usable(answer))
        }
        order = [c.qsf_key for c in _question_choices(source)]
        return [key for key in order if key in selected] + sorted(selected - set(order))

    def _displayed(self, qid: str, state: State) -> list[tuple[str, str]]:
        entry = state.questions_by_id.get(qid)
        if isinstance(entry, dict) and isinstance(entry.get("options"), list):
            labels = [_clean(label) for label in entry["options"]]
            keys = entry.get("option_ids")
            if not isinstance(keys, list) or len(keys) != len(labels):
                keys = [str(index + 1) for index in range(len(labels))]
            return [(str(key), label) for key, label in zip(keys, labels)]
        prefix = f"{qid}_"
        return [
            (str(unit.get("multi_answer_option_id")), _clean(unit.get("multi_answer_option_label")))
            for answer_id, unit in state.questions_by_id.items()
            if answer_id.startswith(prefix) and unit.get("multi_answer_option_id") not in (None, "")
        ]

    def _choice_group(self, qid: str, form: str, rest: list[str], state: State) -> str:
        if form == "SelectedChoices":
            return ", ".join(self._selected_labels(qid, state))
        source = self._source(qid, state)
        options = _question_choices(source)
        label_of = {c.qsf_key: c.display_label for c in options}
        displayed = self._displayed(qid, state)
        label_of.update({key: label for key, label in displayed if label})
        selected = self._selected_keys(qid, state)
        if form == "SelectedChoicesRecode":
            recode_of = {c.qsf_key: c.recode_value for c in options}
            return ", ".join(recode_of.get(key, key) for key in selected)
        if form == "SelectedChoicesTextEntry":
            texts = [_clean((_usable(state.answers_by_id.get(f"{qid}_{key}_TEXT")) or {}).get("answer_raw")) for key in selected]
            return ", ".join(text for text in texts if text)
        if form == "AllChoices":
            return ", ".join(label_of.get(c.qsf_key, c.display_label) for c in options)
        if form == "DisplayedChoices":
            return ", ".join(label for _key, label in displayed)
        if form == "NotDisplayedChoices":
            shown_keys = {key for key, _label in displayed}
            return ", ".join(label_of.get(c.qsf_key, c.display_label) for c in options if c.qsf_key not in shown_keys)
        if form == "UnselectedChoices":
            return ", ".join(label for key, label in displayed if key not in set(selected))
        if form == "SelectedAnswers":
            prefix = f"{qid}_"
            rows = [rest[0]] if rest else list(dict.fromkeys(answer_id[len(prefix):].split("_", 1)[0] for answer_id in state.answers_by_id if answer_id.startswith(prefix)))
            labels: list[str] = []
            for row in rows:
                single = _usable(state.answers_by_id.get(f"{prefix}{row}"))
                if single is not None:
                    labels.append(_clean(single.get("answer_label")))
                    continue
                cells = f"{prefix}{row}_"
                for answer_id, answer in state.answers_by_id.items():
                    if answer_id.startswith(cells) and not answer_id.endswith("_TEXT") and _is_split_yes(_usable(answer)):
                        unit = state.questions_by_id.get(answer_id) or {}
                        labels.append(_clean(unit.get("multi_answer_option_label") or unit.get("matrix_column_label") or answer_id[len(cells):]))
            return ", ".join(label for label in labels if label)
        return ""

    def _selected_labels(self, qid: str, state: State) -> list[str]:
        labels: list[str] = []
        source = self.facts.get(qid) or state.questions_by_id.get(qid)
        base = _usable(state.answers_by_id.get(qid))
        if base and _answer_tokens(base):
            label = selected_choice_label(source, base)
            if label:
                return [label]
        prefix = f"{qid}_"
        is_matrix = bool((source or {}).get("matrix"))
        for answer_id, answer in sorted(state.answers_by_id.items()):
            if not answer_id.startswith(prefix) or answer_id.endswith("_TEXT"):
                continue
            if is_matrix:
                label = selected_choice_label(source, _usable(answer))
                if label:
                    labels.append(label)
                continue
            if not _is_split_yes(_usable(answer)):
                continue
            choice_id = answer_id[len(prefix):]
            labels.append(self._choice_label(qid, choice_id, state) or choice_id)
        return labels

    def _choice_label(self, qid: str, choice_id: str, state: State) -> str:
        split = state.questions_by_id.get(f"{qid}_{choice_id}")
        if split:
            label = _clean(split.get("multi_answer_option_label"))
            if label:
                return label
        written = self.facts.get(qid) or self._written_facts(qid) or self.facts.get(re.sub(r"^\d+_", "", qid))
        if written:
            label = choice_label_for_qsf_key(written, choice_id)
            if label:
                return label
        return choice_label_for_qsf_key(state.questions_by_id.get(qid), choice_id) or ""

    def _is_selected(self, qid: str, choice_id: str, state: State) -> bool:
        if self._never_shown(qid, state):
            return False
        source = self.facts.get(qid) or state.questions_by_id.get(qid) or self.facts.get(re.sub(r"^\d+_", "", str(qid or "")))
        if choice_for_qsf_key(source, choice_id) is None:
            warning = f"Could not resolve Qualtrics choice {choice_id!r} for {qid!r}; treated it as not selected."
            if warning not in state.warnings:
                state.warnings.append(warning)
            return False
        if (source or {}).get("matrix"):
            prefix = f"{qid}_"
            return any(
                answer_selects_qsf_choice(source, choice_id, _usable(answer)) for answer_id, answer in state.answers_by_id.items() if answer_id.startswith(prefix)
            )
        split = state.answers_by_id.get(f"{qid}_{choice_id}")
        if split is not None:
            return _is_split_yes(_usable(split))
        return answer_selects_qsf_choice(source, choice_id, _usable(state.answers_by_id.get(qid)))

    # ---- what has been shown and answered

    def _exists(self, qid: str) -> bool:
        text = str(qid or "").strip()
        if not text:
            return True
        base = re.sub(r"^\d+_", "", text).split("#", 1)[0]
        return text in self.facts or base in self.facts

    def _never_answered(self, qid: str) -> bool:
        base = re.sub(r"^\d+_", "", str(qid or "").strip()).split("#", 1)[0]
        facts = self.facts.get(base)
        return bool(facts and facts.get("never_asked"))

    def _never_shown(self, qid: str, state: State) -> bool:
        if not self._exists(qid):
            return True
        text = str(qid or "").strip()
        return text not in state.seen_in_walk and text.split("#", 1)[0] not in state.seen_in_walk

    @staticmethod
    def _has_answer(qid: str, state: State) -> bool:
        if qid in state.answers_by_id:
            return True
        prefix = f"{qid}_"
        return any(answer_id.startswith(prefix) for answer_id in state.answers_by_id)

    def _pipe_waits(self, expression: str, state: State) -> bool:
        parts = [part.strip() for part in str(expression or "").split("/") if part.strip()]
        if not parts:
            return False
        qid = parts[0]
        if self._never_shown(qid, state) or self._never_answered(qid):
            return False
        kind = parts[1] if len(parts) > 1 else ""
        if kind in {"QuestionText", "ChoiceDescription"}:
            return False
        if kind in ENTRY_PIPE_KINDS and len(parts) > 2 and f"{qid}_{parts[2]}" in state.answers_by_id:
            return False
        return not self._has_answer(qid, state)

    def _waits_for_pipe(self, value: Any, state: State, *, among: set[str] | None = None, besides: set[str] | frozenset[str] = frozenset()) -> bool:
        """Whether piped text in ``value`` reads a question with no answer yet: any
        question, or only those in ``among``; never those in ``besides``."""
        for text in _strings(value):
            for match in _Q_PIPE_RE.finditer(text):
                source = match.group(1).split("/", 1)[0].strip()
                base = source.split("#", 1)[0]
                if source in besides or (among is not None and source not in among and base not in among):
                    continue
                if self._pipe_waits(match.group(1), state):
                    return True
        return False

    def _waits_for_answer(self, condition: Any, state: State) -> bool:
        """Whether a condition reads an answer that can still come (the question was
        shown on this pass and is not answered yet)."""
        if not isinstance(condition, dict):
            return False
        op = condition.get("op")
        if op in {"and", "or"}:
            return any(self._waits_for_answer(arg, state) for arg in condition.get("args") or [])
        if op == "not":
            return self._waits_for_answer(condition.get("arg"), state)
        if op == "source":
            return self._source_waits(condition.get("qualtrics"), state)
        if isinstance(condition.get("right"), str) and self._waits_for_pipe(condition["right"], state):
            return True
        if op in {"displayed", "not_displayed"}:
            return False
        if op in {"selected", "not_selected"}:
            qids = self._loop_questions(condition, state) or [str(condition.get("question", ""))]
            return any(not (self._never_shown(q, state) or self._never_answered(q)) and not self._has_answer(q, state) for q in qids)
        left = condition.get("left") if isinstance(condition.get("left"), dict) else {}
        if op in OPERATOR_NAMES and left.get("scope") == "answer":
            qid = str(left.get("question", ""))
            if self._never_shown(qid, state) or self._never_answered(qid):
                return False
            return not self._has_answer(qid, state)
        return False

    def _source_waits(self, logic: Any, state: State) -> bool:
        for item in _dicts(logic):
            if isinstance(item.get("RightOperand"), str) and self._waits_for_pipe(item["RightOperand"], state):
                return True
            if str(item.get("LogicType", "") or "").strip() != "Question" and not str(item.get("LeftOperand", "") or "").strip().startswith("q://"):
                continue
            for condition in self._source_loop_conditions(item, state) or [item]:
                qid, kind, _value = _locator_parts(condition.get("LeftOperand"))
                if qid and (self._never_shown(qid, state) or self._never_answered(qid)):
                    continue
                if kind in {"QuestionText", "QuestionDisplayed", "ChoiceDisplayed"}:
                    continue
                if not self._has_answer(qid, state):
                    return True
        return False

    # ---- deciding a condition

    def _holds(self, condition: Any, state: State, variables: dict[str, Any]) -> bool:
        result = decide(condition, _AnswersDecider(self, state, variables))
        return True if result is None else result

    def _loop_questions(self, condition: dict[str, Any], state: State) -> list[str] | None:
        """For a condition on a repeated block's question read "in all loops" or "in any
        loop", the question's copy in each row; None for any other condition."""
        if not condition.get("loop"):
            return None
        qid = str(condition.get("question", ""))
        block_id = self.block_of.get(qid)
        block = self.definition.blocks.get(block_id) if block_id else None
        if block is None or not block.get("repeat"):
            return None
        return [f"{row['id']}_{qid}" for row in self._rows(block, state) or []]

    def _source_loop_conditions(self, condition: dict[str, Any], state: State) -> list[dict[str, Any]] | None:
        block_id, which = _loop_spec(condition)
        block = self.definition.blocks.get(block_id) if block_id else None
        if block is None or which == "current" or not block.get("repeat"):
            return None
        own = {str(q) for q in block.get("questions") or []}
        rows = [str(row["id"]) for row in self._rows(block, state) or []] if which == "all" else [which]
        return [_row_condition(condition, row, own) for row in rows] or None

    def _answer_value(self, qid: str, kind: str, value: str, state: State) -> Any:
        """What a condition on an answer reads (as the template runtime read the
        Qualtrics locator ``q://qid/kind/value``)."""
        if kind in {"QuestionDisplayed", "ChoiceDisplayed"}:
            return not self._never_shown(qid, state)
        if kind != "QuestionText" and self._never_shown(qid, state):
            if kind == "SelectableChoice":
                return False
            if kind in {"ChoiceNumericEntryValue", "NumericEntryValue"}:
                return None
            if kind == "SelectedChoicesCount":
                return 0
            return ""
        if kind == "SelectableChoice":
            return self._is_selected(qid, value, state)
        if kind in {"ChoiceNumericEntryValue", "NumericEntryValue"}:
            answer = state.answers_by_id.get(f"{qid}_{value}") if value else None
            return _numeric(_usable(answer or state.answers_by_id.get(qid)))
        if kind == "SelectedChoicesCount":
            return len(self._selected_labels(qid, state))
        if kind == "QuestionText":
            entry = state.questions_by_id.get(qid)
            facts = self.facts.get(qid)
            return (entry or {}).get("text") or (facts or {}).get("text") or ""
        if kind in {"ChoiceTextEntryValue", "TextEntryValue", "SelectedValue"}:
            answer = state.answers_by_id.get(f"{qid}_{value}") if value else None
            answer = _usable(answer or state.answers_by_id.get(qid))
            return _clean((answer or {}).get("answer_value") or (answer or {}).get("answer_label") or (answer or {}).get("answer_raw"))
        answer = _usable(state.answers_by_id.get(qid))
        tokens = _answer_tokens(answer)
        if not tokens:
            return ", ".join(self._selected_labels(qid, state))
        return next(iter(tokens))


def _dicts(value: Any) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    if isinstance(value, dict):
        found.append(value)
        for child in value.values():
            found += _dicts(child)
    elif isinstance(value, list):
        for child in value:
            found += _dicts(child)
    return found


class _AnswersDecider(Decider):
    """A condition decided with the answers given so far, the variables set so far,
    and what the flow has shown on this pass."""

    def __init__(self, runtime: StagedRuntime, state: State, variables: dict[str, Any]):
        self.runtime = runtime
        self.state = state
        self.variables = variables

    def atom(self, condition: dict[str, Any]) -> bool:
        op = condition.get("op")
        rt, state = self.runtime, self.state
        if op in {"selected", "not_selected"}:
            rows = rt._loop_questions(condition, state)
            operator = "Selected" if op == "selected" else "NotSelected"
            if rows is not None:
                # "in all loops": every row must hold (a row not shown has no answer)
                results = [compare_logic_value(rt._answer_value(q, "SelectableChoice", str(condition.get("option", "")), state), None, operator) for q in rows]
                return any(results) if condition.get("loop") == "any" else all(results)
            actual = rt._answer_value(str(condition.get("question", "")), "SelectableChoice", str(condition.get("option", "")), state)
            return compare_logic_value(actual, None, operator)
        if op in {"displayed", "not_displayed"}:
            actual = rt._answer_value(str(condition.get("question", "")), "QuestionDisplayed", "", state)
            return compare_logic_value(actual, None, "Displayed" if op == "displayed" else "NotDisplayed")
        if op in OPERATOR_NAMES:
            left = condition.get("left") or {}
            operator = OPERATOR_NAMES[op]
            if left.get("scope") == "variable":
                actual = self.variables.get(str(left.get("variable", "") or "").strip())
                reads_answer = False
            elif left.get("scope") == "answer":
                kind = _FIELD_LOCATORS.get(str(left.get("field")), "")
                actual = rt._answer_value(str(left.get("question", "")), kind, str(left.get("item") or ""), state)
                reads_answer = True
            else:
                return True
            if op in _ORDER_OPS and reads_answer and (actual is None or (isinstance(actual, str) and not actual.strip())):
                return False  # no answer: no number to compare
            expected = right_value(condition)
            if isinstance(expected, str) and "${" in expected:
                expected = rt._fill_compared_value(expected, state, self.variables)
            return compare_logic_value(actual, expected, operator)
        return True

    def source(self, condition: dict[str, Any]) -> bool:
        """A condition kept as the source platform wrote it, decided as before."""
        rt, state = self.runtime, self.state
        rows = rt._source_loop_conditions(condition, state)
        if rows is not None:
            function = str(condition.get("LoopTarget") or condition.get("FunctionName") or "")
            results = [self.source(row) for row in rows]
            return any(results) if "any" in function.lower() else all(results)
        logic_type = str(condition.get("LogicType", "") or "").strip()
        left = condition.get("LeftOperand")
        if logic_type == "EmbeddedField":
            actual = self.variables.get(str(left or "").strip())
        elif logic_type != "Question" and not str(left or "").strip().startswith("q://"):
            actual = True
        else:
            qid, kind, value = _locator_parts(left)
            actual = rt._answer_value(qid, kind, value, state)
        operator = str(condition.get("Operator", ""))
        reads_question = logic_type == "Question" or str(left or "").strip().startswith("q://")
        if operator.strip() in _ORDER_OPERATORS and reads_question and (actual is None or (isinstance(actual, str) and not actual.strip())):
            return False
        expected = condition.get("RightOperand")
        if isinstance(expected, str) and "${" in expected:
            expected = rt._fill_compared_value(expected, state, self.variables)
        return compare_logic_value(actual, expected, operator)


# ---------------------------------------------------------------- helpers


def _items_with_conditions(question: dict[str, Any]) -> list[dict[str, Any]]:
    response = question.get("response") if isinstance(question.get("response"), dict) else {}
    found = []
    for key in ("options", "items", "fields", "rows", "columns"):
        found += [item for item in response.get(key) or [] if isinstance(item, dict) and item.get("show_if")]
    row_response = response.get("row_response") if isinstance(response.get("row_response"), dict) else {}
    found += [item for item in row_response.get("options") or [] if isinstance(item, dict) and item.get("show_if")]
    return found


def _keep_unfilled_defaults(written: dict[str, Any], rendered: dict[str, Any]) -> None:
    """Preset answers that pipe a variable keep their tokens when they filled to
    nothing (each kind of preset as a whole, as before)."""
    before: dict[str, str] = {}
    after: dict[str, str] = {}
    map_text(written, lambda place, text: before.__setitem__(place, text) or text)
    map_text(rendered, lambda place, text: after.__setitem__(place, text) or text)
    restore = set()
    for group in ("DefaultChoices", "DefaultEntries"):
        places = [place for place in before if place.split("/", 1)[0] == group]
        if any("${e://" in before[place] for place in places) and not any(after.get(place, "").strip() for place in places):
            restore.update(places)
    if restore:
        map_text(rendered, lambda place, text: before[place] if place in restore else text)


def _is_screenout(steps: list[dict[str, Any]]) -> bool:
    """Steps that only end the survey (a screen-out branch's body)."""
    return bool(steps) and all(isinstance(step, dict) and step.get("type") == "end" for step in steps)


def _undecidable(condition: Any) -> bool:
    """A condition with a part no twin can meet (a device, location or quota test)
    and none on an answer or a variable: a screen-out on it never fires."""
    from .conditions import _atoms, _source_conditions

    sources: set[str] = set()
    for atom in _atoms(condition):
        op = atom.get("op")
        if op in {"true", "false"}:
            sources.add("BooleanValue")
        elif op == "source":
            for found in _source_conditions(atom):
                sources.add(str(found.get("LogicType", "") or "").strip())
        elif op in {"selected", "not_selected", "displayed", "not_displayed"}:
            sources.add("Question")
        elif op in OPERATOR_NAMES:
            sources.add("EmbeddedField" if (atom.get("left") or {}).get("scope") == "variable" else "Question")
    return bool(sources - {"BooleanValue"}) and not (sources & {"Question", "EmbeddedField"})
