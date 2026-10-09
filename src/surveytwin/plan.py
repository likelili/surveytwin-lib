"""Planning a run: sample the twins, build each twin's survey, preview prompts.

Nothing here calls a model or needs a key.
"""

from __future__ import annotations

import json
from contextlib import contextmanager
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Iterator

from ._display import plural, short_list
from .answers import executor as ex
from .config import RunConfig
from .errors import BatchNotAvailable, ContextBudgetError, PersonaError, StaticModeRefused, SurveyError
from .personas.bank import EMPTY_REPRESENTATION, Twin
from .personas.wrapping import PERSONA_PAYLOAD_TOKEN_BUDGET, approx_tokens
from .prompts import PROMPT_VERSION
from .prompts.picture_markers import picture_markers_as_text
from .runtime.instrument_plan import build_survey_instrument
from .engine.definition import Definition
from .engine.staged import StagedRuntime, sequence_with_prior_context as staged_survey_sequence_with_prior_context
from .survey import Survey

_BLANK_PERSONA_MARKERS = {"", "[Persona representation unavailable for this view]"}


@contextmanager
def system_instruction(value: str | None) -> Iterator[None]:
    token = ex.set_system_instruction_override(value)
    try:
        yield
    finally:
        ex._SYSTEM_INSTRUCTION_OVERRIDE.reset(token)


@dataclass
class PromptPreview:
    twin_id: str
    stage: int
    text: str
    messages: list[dict[str, Any]]
    images: list[str]
    input_tokens: int
    max_output_tokens: int
    kind: str

    def __str__(self) -> str:
        return self.text


def build_call(
    persona_text: str,
    questions: list[dict[str, Any]],
    *,
    survey_sequence_text: str | None,
    survey_sequence_media: list[dict[str, Any]] | None,
    blocked_image_urls: list[str] | None = None,
) -> dict[str, Any] | None:
    """The exact first request ``_llm_answer_questions`` sends for one twin. None
    when nothing is asked, which includes every question showing a picture the
    model cannot load."""
    answerable = [q for q in questions if not bool(q.get("prefill_only")) and not ex._is_participant_id_question(q)]
    media = ex._normalize_sequence_media(survey_sequence_media)
    if not media:
        media = ex._fallback_sequence_media_from_questions(questions)
    media, picture_skipped, _ = ex._leave_out_unavailable_pictures(
        media, answerable, {str(url).strip() for url in (blocked_image_urls or []) if str(url).strip()}
    )
    if len(answerable) <= len(picture_skipped):
        return None
    max_tokens = ex._answer_generation_max_tokens(len(answerable) - len(picture_skipped))
    questions = [q for q in questions if str(q.get("id", "")).strip() not in picture_skipped]
    if ex._has_sequence_media(media):
        items = ex._build_multimodal_input_items(
            persona_text=persona_text,
            questions=questions,
            survey_sequence_text=survey_sequence_text,
            survey_sequence_media=media,
        )
        images = [
            str(part.get("image_url"))
            for item in items
            for part in (item.get("content") if isinstance(item.get("content"), list) else [])
            if isinstance(part, dict) and part.get("type") == "input_image"
        ]
        return {"kind": "multimodal", "input_items": items, "max_tokens": max_tokens, "images": images}
    system_prompt, user_prompt = ex.build_simulation_prompt_messages(
        persona_text, questions, survey_sequence_text=picture_markers_as_text(survey_sequence_text or "", shown=False)
    )
    return {
        "kind": "chat",
        "messages": [{"role": "system", "content": system_prompt}, {"role": "user", "content": user_prompt}],
        "max_tokens": max_tokens,
        "images": [],
    }


def call_text(call: dict[str, Any]) -> str:
    """A readable rendering of a request: SYSTEM and USER parts, images as markers."""
    if call["kind"] == "chat":
        return "\n\n".join(f"=== {m['role'].upper()} ===\n{m['content']}" for m in call["messages"])
    parts: list[str] = []
    for item in call["input_items"]:
        role = str(item.get("role", "user")).upper()
        texts: list[str] = []
        for part in item.get("content") or []:
            if part.get("type") == "input_text":
                texts.append(str(part.get("text", "")))
            elif part.get("type") == "input_image":
                texts.append(f"[image: {part.get('image_url')}]")
        parts.append(f"=== {role} ===\n" + "\n".join(texts))
    return "\n\n".join(parts)


class Plan:
    """What a run will do. Build one with ``st.plan(survey, config)``."""

    def __init__(
        self,
        *,
        survey: Survey,
        config: RunConfig,
        mode: str,
        instrument: dict[str, Any],
        twins: list[Twin],
        warnings: list[str],
        population: int | None = None,
    ) -> None:
        self.survey = survey
        self.config = config
        self.mode = mode
        self.instrument = instrument
        self.twins = twins
        self.warnings = warnings
        self.prompt_version = PROMPT_VERSION
        self._twin_surveys: list[dict[str, Any]] | None = None
        # How many twins the survey's randomization is laid out for. ``head(n)``
        # keeps the full plan's number, so its twins get the same branches and
        # orders (evenly presented randomizers balance over this many twins).
        self.population = int(population or len(twins))

    def __repr__(self) -> str:
        return f"Plan({self.mode}, {len(self.twins)} twins, model={self.config.model.name!r}, seed={self.config.seed})"

    def __str__(self) -> str:
        how = (
            "one model call per twin" if self.mode == "static"
            else "stage by stage: each stage's questions depend on the twin's earlier answers"
        )
        ids = short_list([t.id for t in self.twins], 10)
        lines = [
            f"Plan: {self.mode} survey, {plural(len(self.twins), 'twin')}, seed {self.config.seed}, model {self.config.model.name}",
            f"  asked {how}",
            f"  twins: {ids}",
        ]
        if self.population != len(self.twins):
            lines.append(f"  the first {len(self.twins)} of a {self.population}-twin plan (plan.head)")
        for loop in self.loops:
            # every row is asked, as in Qualtrics: the cost of a long loop shows here
            lines.append(
                f"  loop: {loop['name']} ({loop['block_id']}) asks {plural(loop['rows'], 'row')} of "
                f"{plural(loop['questions'], 'question')} ({plural(loop['answers_per_row'], 'answer')} a row), "
                f"up to {loop['rows'] * loop['answers_per_row']} answers per twin"
            )
        lines += [f"  warning: {w}" for w in self.warnings]
        return "\n".join(lines)

    @property
    def loops(self) -> list[dict[str, Any]]:
        """Each Loop & Merge block a twin can be shown: its rows (every row is
        asked, or a random subset's size) and its questions per row."""
        from .engine.analysis import loop_blocks

        return loop_blocks(self._definition())

    def _copies(self) -> list[dict[str, Any]]:
        """Each static twin's copy as the run reads it, in the order of ``twins``."""
        instances = self.instrument.get("instances") or []
        return [instances[i % len(instances)] for i in range(len(self.twins))] if instances else []

    @property
    def twin_surveys(self) -> list[dict[str, Any]]:
        """Each static twin's survey copy (questions, survey text, images, embedded
        data, the blocks shown), with ``elements``: the same copy in the
        Qualtrics-like form earlier versions gave."""
        if self._twin_surveys is None:
            from .engine.definition import Definition
            from .engine.legacy import template_elements

            native = Definition(self.instrument["definition"]).native if self.instrument.get("definition") else False
            written: dict[int, dict[str, Any]] = {}
            for instance in self._copies():
                if id(instance) not in written:
                    written[id(instance)] = {**instance, "elements": template_elements(instance.get("blocks"), native=native)}
            self._twin_surveys = [written[id(instance)] for instance in self._copies()]
        return self._twin_surveys

    @property
    def definition(self) -> dict[str, Any]:
        """The survey this plan runs, in the standard format (without dropped blocks)."""
        return self.instrument["definition"]

    @property
    def template(self) -> dict[str, Any] | None:
        """For a staged plan, the same survey in the template form earlier versions
        ran, for code written for them (the run itself reads ``definition``). None
        for a static plan."""
        if self.mode != "staged":
            return None
        from .spec.compile import compile_survey

        return compile_survey(self.instrument["definition"])

    def _twin_index(self, twin_id: str | None) -> int:
        if twin_id is None:
            return 0
        for idx, twin in enumerate(self.twins):
            if twin.id == str(twin_id):
                return idx
        raise SurveyError(f"Twin {twin_id!r} is not in this plan")

    def _call_for(self, idx: int, stage: int = 0) -> dict[str, Any] | None:
        twin = self.twins[idx]
        with system_instruction(self.config.system_instruction):
            if self.mode == "static":
                instance = self._copies()[idx]
                return build_call(
                    twin.text,
                    instance.get("questions", []),
                    survey_sequence_text=str(instance.get("prompt_sequence_text", "")) or None,
                    survey_sequence_media=instance.get("prompt_sequence_media") or None,
                )
            if stage != 0:
                raise SurveyError("Only stage 0 of a staged survey is known before the twin answers")
            runtime = StagedRuntime(
                self._definition(),
                seed=self.config.seed,
                persona_count=self.population,
                max_stages=self.config.max_stages,
            )
            state = runtime.new_state(persona_id=twin.id, persona_index=idx)
            if self.config.embedded_data:
                state.outside_embedded_data = dict(self.config.embedded_data)
            first = runtime.render_next_stage(state)
            if not first.questions:
                return None
            text = staged_survey_sequence_with_prior_context(
                prior_answer_context="", survey_sequence_text=first.survey_sequence_text
            )
            return build_call(
                twin.text,
                first.questions,
                survey_sequence_text=text or None,
                survey_sequence_media=first.survey_sequence_media or None,
            )

    def _definition(self) -> Definition:
        """The survey definition the plan runs."""
        return Definition(self.instrument["definition"])

    def prompts(self, twin_id: str | None = None, *, stage: int = 0) -> list[dict[str, Any]]:
        """The exact messages (or Responses-API input items, with images) for one twin."""
        call = self._call_for(self._twin_index(twin_id), stage)
        if call is None:
            return []
        return call["messages"] if call["kind"] == "chat" else call["input_items"]

    def preview_prompt(self, twin_id: str | None = None, *, stage: int = 0) -> PromptPreview:
        """The complete prompt for a twin (the first sampled by default), as readable text."""
        idx = self._twin_index(twin_id)
        call = self._call_for(idx, stage)
        if call is None:
            return PromptPreview(self.twins[idx].id, stage, "(no answerable questions; no model call)", [], [], 0, 0, "none")
        text = call_text(call)
        return PromptPreview(
            twin_id=self.twins[idx].id,
            stage=stage,
            text=text,
            messages=call["messages"] if call["kind"] == "chat" else call["input_items"],
            images=call["images"],
            input_tokens=approx_tokens(len(text)),
            max_output_tokens=int(call["max_tokens"]),
            kind=call["kind"],
        )

    def head(self, n: int) -> "Plan":
        """The same plan for its first ``n`` twins, to try a prompt or a model cheaply.

        Each of these twins gets exactly the survey (branches, orders, draws) it
        gets in the full plan.
        """
        n = max(1, int(n))
        clone = Plan(
            survey=self.survey,
            config=replace(self.config, sample_size=min(n, len(self.twins))),
            mode=self.mode,
            instrument=self.instrument,
            twins=self.twins[:n],
            warnings=list(self.warnings),
            population=self.population,
        )
        return clone

    def batch_items(self, client: Any, blocked_image_urls: list[str] | None = None) -> list[dict[str, Any]]:
        """One OpenAI Batch line per twin that needs a model call (custom id = the twin's position)."""
        items: list[dict[str, Any]] = []
        copies = self._copies()
        with system_instruction(self.config.system_instruction):
            for idx, twin in enumerate(self.twins):
                instance = copies[idx]
                item = ex.build_persona_batch_item(
                    custom_id=str(idx),
                    model=self.config.model.name,
                    client=client,
                    persona_text=twin.text,
                    questions=instance.get("questions", []),
                    survey_sequence_text=str(instance.get("prompt_sequence_text", "")) or None,
                    survey_sequence_media=instance.get("prompt_sequence_media") or None,
                    blocked_image_urls=blocked_image_urls or None,
                )
                if item is not None:
                    items.append(item)
        return items

    def to_batch_file(self, path: str | Path, *, image_checker: Any = None) -> Path:
        """The OpenAI Batch JSONL for a static plan (one line per twin).

        Images are checked first, as for a run: an image that cannot be sent
        stops here unless ``accept_missing_images=True``, and is then left out,
        with the question that shows it.
        """
        if self.mode != "static":
            raise BatchNotAvailable("Staged surveys cannot be batched: each stage needs the previous answers")
        if not (self.config.model.kind == "preset" and self.config.model.provider == "openai"):
            raise BatchNotAvailable("The Batch API is OpenAI-only; use an OpenAI preset model")
        from .providers.openai_client import OpenAIClient
        from .run import _image_gate

        blocked, _notes = _image_gate(self, image_checker)
        out = Path(path)
        out.parent.mkdir(parents=True, exist_ok=True)
        lines = [
            json.dumps({"custom_id": item["custom_id"], "method": "POST", "url": "/v1/chat/completions", "body": item["body"]})
            for item in self.batch_items(OpenAIClient(), blocked)
        ]
        out.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
        return out


def plan(survey: Survey, config: RunConfig, *, mode: str = "auto") -> Plan:
    """Sample the twins and build each twin's survey. No model call, no key."""
    if mode not in {"auto", "static", "staged"}:
        raise SurveyError("mode must be 'auto', 'static' or 'staged'")
    warnings: list[str] = []
    # The twins first: static survey copies are built for exactly these twins,
    # in order, so each twin's random draws are keyed by the twin as in a
    # staged run (a twin draws the same values in either mode).
    bank = config.bank
    ids = bank.sample(representation=config.representation, filters=config.filters, n=config.sample_size, seed=config.seed)
    if not ids:
        raise PersonaError("No personas matched the selected filters and representation.")
    if len(ids) < config.sample_size:
        warnings.append(f"Only {len(ids)} twins match the filters and representation; asked for {config.sample_size}.")
    try:
        instrument = build_survey_instrument(
            survey._runnable_standard(),
            len(ids),
            seed=config.seed,
            mode=mode,
            borrow_unset_embedded_data=False,
            initial_embedded_data=config.embedded_data or None,
            source={"builder": "survey_builder", "standard": "standard"}.get(survey.kind, "qsf"),
            respondent_ids=[str(persona_id) for persona_id in ids],
        )
    except (ValueError, RuntimeError) as exc:
        # The website's messages: no questions left after dropping blocks, or a
        # survey with nothing to answer (only descriptive text).
        raise SurveyError(f"Nothing for a twin to answer: {exc}") from exc
    if instrument.get("execution_mode") == "refused":
        raise StaticModeRefused(
            "This survey needs staged execution (its later content depends on answers); "
            "static mode would be wrong. Use mode='auto' or 'staged'."
        )
    run_mode = str(instrument.get("execution_mode"))
    # One note per run (the runtime no longer warns once per twin): what logic
    # reads questions no twin will see.
    if survey.drop_report.references:
        warnings.append(
            f"Logic in {plural(len(survey.drop_report.references), 'place')} reads a dropped question; "
            "each counts as never shown, with no answer (see survey.drop_report)."
        )
    if survey.drop_report.choice_logic:
        warnings.append(
            f"Display logic on {plural(len(survey.drop_report.choice_logic), 'choice')} reads a dropped question; "
            "each counts as never shown, with no answer (see survey.drop_report)."
        )
    if survey._reads_missing:
        warnings.append(
            f"Logic in {plural(len(survey._reads_missing), 'place')} reads a question this survey does not have; "
            "each counts as never shown, with no answer (see survey.check())."
        )
    unused = sorted(set(str(k) for k in (config.embedded_data or {})) - set(survey._fields))
    if unused:
        warnings.append(f"embedded_data supplies {short_list(unused, 6)}, which the survey never reads or sets; check the spelling.")
    from .engine.analysis import undrawable_random_tokens

    undrawable = undrawable_random_tokens(instrument["definition"])
    if undrawable:
        warnings.append(
            f"{plural(len(undrawable), 'random-value token')} cannot be drawn and reach the model as written "
            f"(for example {undrawable[0][1]} at {undrawable[0][0]}; see survey.check())."
        )
    left = [p for p in (instrument.get("generation") or {}).get("unresolved_placeholders") or []]
    if left:
        warnings.append(f"Piped text left unfilled in the static survey copies: {short_list(sorted(left), 6)}.")
    if config.batch and run_mode == "staged":
        raise BatchNotAvailable("Staged surveys cannot be batched: each stage needs the previous answers")

    if config.representation != EMPTY_REPRESENTATION:
        budget_chars = bank.max_payload_chars(config.representation)
        if approx_tokens(budget_chars) > PERSONA_PAYLOAD_TOKEN_BUDGET:
            others = [r.label for r in bank.representations() if r.id != config.representation][:4]
            raise ContextBudgetError(
                f"The {bank.representation_label(config.representation)} representation is too large for one prompt "
                f"(~{approx_tokens(budget_chars):,} tokens > {PERSONA_PAYLOAD_TOKEN_BUDGET:,}). Nothing is cut to fit."
                + (f" Choose a smaller one: {', '.join(others)}." if others else "")
            )
    twins: list[Twin] = []
    blank: list[str] = []
    for persona_id in ids:
        twin = bank.twin(persona_id, config.representation)
        if config.representation != EMPTY_REPRESENTATION and _is_blank(twin.text):
            blank.append(persona_id)
        twins.append(twin)
    if blank:
        raise PersonaError(
            f"{len(blank)} twin(s) have a blank persona for this representation and would run with no persona: "
            + ", ".join(blank[:10])
        )
    if run_mode == "static" and len(instrument.get("instances") or []) < len(twins):
        # The website generates one survey copy per requested twin; with fewer
        # matching twins the extra copies are simply unused.
        pass
    if run_mode == "staged" and twins:
        ceiling = 500
        needed = _stages_with_placeholder_answers(Definition(instrument["definition"]), config, twins[0].id, len(ids), ceiling=ceiling)
        if needed > config.max_stages:
            advice = (
                f"Pass configure(max_stages={needed + 5}) or more."
                if needed < ceiling
                else f"The count stopped at {ceiling}; the survey needs at least that many."
            )
            warnings.append(
                f"The first twin needs {'about' if needed < ceiling else 'at least'} {needed} stages with placeholder answers, "
                f"more than max_stages ({config.max_stages}): a twin cut off at the limit is marked incomplete and the rest of "
                f"its survey is not asked. {advice}"
            )
    return Plan(survey=survey, config=config, mode=run_mode, instrument=instrument, twins=twins, warnings=warnings)


def _stages_with_placeholder_answers(definition: Definition, config: RunConfig, twin_id: str, population: int, *, ceiling: int = 500) -> int:
    """How many stages a staged run asks one twin when every answer is a placeholder
    (the first option, the lowest number, a short text); no model call. Paths that
    depend on real answers can need more or fewer."""
    runtime = StagedRuntime(definition, seed=config.seed, persona_count=population, max_stages=ceiling)
    state = runtime.new_state(persona_id=str(twin_id), persona_index=0)
    if config.embedded_data:
        state.outside_embedded_data = dict(config.embedded_data)
    stages = 0
    while stages < ceiling:
        stage = runtime.render_next_stage(state)
        if not stage.questions:
            break
        stages += 1
        answers = []
        for question in stage.questions:
            options = question.get("options") or []
            value = str(options[0]) if options else str(question.get("numeric_min") or ("1" if question.get("type") == "numeric" else "x"))
            answers.append({"question_id": question.get("id"), "answer_value": value, "answer_label": value, "answer_raw": value})
        runtime.apply_answers(state, questions=stage.questions, answers=answers)
        if state.done:
            break
    return stages


def _is_blank(text: str) -> bool:
    marker = "----- BEGIN PERSONA REPRESENTATION (untrusted data) -----"
    if marker in text:
        body = text.split(marker, 1)[1].split("----- END PERSONA REPRESENTATION -----", 1)[0]
        return body.strip() in _BLANK_PERSONA_MARKERS
    return text.strip() in _BLANK_PERSONA_MARKERS
