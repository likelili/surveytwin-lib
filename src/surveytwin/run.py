"""Running a plan: every twin answers its survey, then the answers become ``Results``."""

from __future__ import annotations

import asyncio
import os
import re
import time
import uuid
from typing import Any, Callable

from . import __version__
from .answers.simulate import simulate_staged, simulate_static
from .check.qsf_checks import BLOCKING_IMAGE_STATUSES, image_gate_findings
from .errors import BatchNotAvailable, ConfigError, ImagesRefused
from .plan import Plan, system_instruction
from .providers.caller import PresetCaller, make_caller
from .providers.context import current_call_context
from .providers.usage import UsageRecord
from .results.results import Results, now_iso


class _RecordingCaller:
    """Keeps the exact request of every call when ``keep_prompts=True``."""

    def __init__(self, inner: Any, sink: list[dict[str, Any]]) -> None:
        self.inner = inner
        self.sink = sink

    @property
    def enabled(self) -> bool:
        return self.inner.enabled

    def _note(self, kind: str, payload: dict[str, Any]) -> None:
        ctx = current_call_context()
        self.sink.append({"twin_id": ctx.get("twin_id", ""), "stage": ctx.get("stage"), "kind": kind, **payload})

    async def chat_json(self, **kwargs: Any) -> dict[str, Any]:
        self._note("chat", {"system": kwargs.get("system_prompt"), "user": kwargs.get("user_prompt"), "max_tokens": kwargs.get("max_tokens")})
        return await self.inner.chat_json(**kwargs)

    async def responses_input_text(self, **kwargs: Any) -> str:
        self._note("multimodal", {"input_items": kwargs.get("input_items"), "max_tokens": kwargs.get("max_tokens")})
        return await self.inner.responses_input_text(**kwargs)

    async def close(self) -> None:
        await self.inner.close()


def _require_key(plan: Plan, api_key: str | None) -> None:
    model = plan.config.model
    if model.kind != "preset":
        require_model_env(model)
        return
    if model.provider == "openrouter":
        if not (api_key or os.environ.get("OPENROUTER_API_KEY", "").strip()):
            raise ConfigError("No OpenRouter key: pass api_key= or set OPENROUTER_API_KEY")
        return
    if not (api_key or os.environ.get("OPENAI_API_KEY", "").strip()):
        raise ConfigError("No OpenAI key: pass api_key= or set OPENAI_API_KEY")


def require_model_env(model: Any) -> None:
    """Stop before any call when a defined model's ``env(...)`` variables are not
    set: otherwise every call would fail, be tried again, and every answer come
    back skipped."""
    missing = model.missing_env() if hasattr(model, "missing_env") else []
    if missing:
        raise ConfigError(
            f"Model {model.name!r} reads {', '.join(missing)} from the environment, which "
            f"{'is' if len(missing) == 1 else 'are'} not set. Set {'it' if len(missing) == 1 else 'them'} before running."
        )


def _image_gate(plan: Plan, url_checker: Callable[[str], dict[str, str]] | None) -> tuple[list[str], list[str]]:
    """Images the run cannot send: refused unless the caller accepted leaving them out."""
    survey = plan.survey
    missing = survey.unresolved_media
    missing_refs: list[str] = []
    missing_notes: list[str] = []
    if missing:
        if not plan.config.accept_missing_images:
            listed = "\n".join(f"  {m.get('question')}: {m.get('reference')!r}" for m in missing[:10])
            raise ImagesRefused(
                f"{len(missing)} survey image(s) were not found (the image resolver returned nothing):\n{listed}\n"
                "Resolve them, remove them, or configure(accept_missing_images=True) to run without them "
                "(a question that shows one is then skipped)."
            )
        missing_refs = sorted({str(m.get("reference", "")) for m in missing})
        missing_notes.append(
            f"{len(missing)} survey image(s) were not found and were left out. A question that shows one of "
            "them was not asked and is recorded as skipped; an image in descriptive text was left out, and "
            "the questions after it were asked without it."
        )
    blocked, notes = _image_gate_urls(plan, url_checker)
    return blocked + missing_refs, missing_notes + notes


def _image_gate_urls(plan: Plan, url_checker: Callable[[str], dict[str, str]] | None) -> tuple[list[str], list[str]]:
    survey = plan.survey
    findings = image_gate_findings(
        definition=plan.instrument.get("definition") or survey.standard,
        content=survey.raw_bytes,
        url_checker=url_checker,
    )
    urls = sorted({str(f.get("url", "")) for f in findings if str(f.get("url", "")).strip()})
    notes: list[str] = []
    blocked: list[str] = []
    if urls and not plan.config.model.images:
        if not plan.config.accept_missing_images:
            raise ImagesRefused(
                f"This survey shows {len(urls)} image(s), but model {plan.config.model.name!r} takes no image input. "
                "Use a model that takes images, or configure(accept_missing_images=True) to run without them "
                "(a question that shows one is then skipped)."
            )
        blocked = urls
        notes.append(
            f"{len(urls)} survey image(s) were left out because the model takes no image input. A question "
            "that shows one of them was not asked and is recorded as skipped; an image in descriptive text "
            "was left out, and the questions after it were asked without it."
        )
        return blocked, notes
    bad = [f for f in findings if str(f.get("status", "")) in BLOCKING_IMAGE_STATUSES]
    if bad:
        bad_urls = sorted({str(f.get("url", "")) for f in bad if f.get("url")})
        if not plan.config.accept_missing_images:
            listed = "\n".join(f"  {f.get('status')}: {f.get('url')}" for f in bad[:10])
            raise ImagesRefused(
                f"{len(bad_urls)} survey image(s) cannot be loaded (unreachable or behind a Qualtrics login):\n{listed}\n"
                "Fix the links, drop those blocks, or configure(accept_missing_images=True) to run without them "
                "(a question that shows one is then skipped)."
            )
        blocked = bad_urls
        labels = list(dict.fromkeys(str(f.get("label") or "").strip() for f in bad if str(f.get("label") or "").strip()))
        named = f": {', '.join(labels)}" if labels else ""
        notes.append(
            f"{len(bad_urls)} survey image(s) could not be loaded (unreachable link or Qualtrics login "
            f"required){named}. A question that shows one of them was not asked and is recorded as "
            "skipped; an image in descriptive text was left out, and the questions after it were "
            "asked without it."
        )
    return blocked, notes


def batch_prompt_records(plan: Plan, items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """``keep_prompts`` for batch lines: each line's request, under its twin."""
    records: list[dict[str, Any]] = []
    for item in items:
        idx = int(item["custom_id"]) if str(item.get("custom_id", "")).isdigit() else -1
        body = item.get("body") or {}
        records.append({
            "twin_id": plan.twins[idx].id if 0 <= idx < len(plan.twins) else "",
            "stage": None,
            "kind": "batch",
            "messages": body.get("messages"),
            "max_tokens": body.get("max_completion_tokens") or body.get("max_tokens"),
        })
    return records


async def _batch_precompute(
    plan: Plan,
    caller: PresetCaller,
    blocked: list[str],
    warnings: list[str],
    batch_state: dict[str, Any],
    prompts: list[dict[str, Any]] | None = None,
) -> dict[int, str]:
    items = plan.batch_items(caller.client, blocked)
    if not items:
        return {}
    if prompts is not None:
        prompts.extend(batch_prompt_records(plan, items))
    try:
        submitted = await caller.client.submit_chat_batch(items, metadata={"library": "surveytwin"})
        batch_state.update(submitted)
        batch = await caller.client.wait_for_batch(submitted["batch_id"])
        batch_state["status"] = batch.get("status")
        if batch.get("status") != "completed":
            warnings.append(f"Batch ended with status {batch.get('status')}; every twin was asked in real time instead.")
            return {}
        results = await caller.client.collect_chat_batch_results(
            submitted["batch_id"], model=plan.config.model.name, item_count=len(items), twin_ids=[t.id for t in plan.twins]
        )
    except Exception as exc:  # noqa: BLE001 - the website falls back to real time, too
        warnings.append(f"Batch API submission failed; ran in real time instead ({exc}).")
        return {}
    missing = len(items) - len(results)
    if missing:
        warnings.append(f"{missing} batch answer(s) were unusable; those twins were asked again in real time.")
    return {int(k): v for k, v in results.items() if str(k).isdigit()}


async def arun(
    plan: Plan,
    *,
    api_key: str | None = None,
    max_concurrency: int = 8,
    keep_prompts: bool = False,
    on_usage: Callable[[UsageRecord], None] | None = None,
    on_progress: Callable[[int, int], None] | None = None,
    image_checker: Callable[[str], dict[str, str]] | None = None,
) -> Results:
    """``run`` for async code."""
    _require_key(plan, api_key)
    started_at = now_iso()
    started = time.monotonic()
    usage: list[dict[str, Any]] = []

    def record(item: UsageRecord) -> None:
        usage.append(item.to_dict())
        if on_usage is not None:
            on_usage(item)

    blocked, notes = _image_gate(plan, image_checker)
    warnings: list[str] = list(notes) + list(plan.warnings)
    caller: Any = make_caller(plan.config.model, api_key=api_key, on_usage=record)
    prompts: list[dict[str, Any]] = []
    if keep_prompts:
        caller = _RecordingCaller(caller, prompts)
    twins_input = [{"persona_id": t.id, "persona_text": t.text} for t in plan.twins]
    batch_state: dict[str, Any] = {}
    try:
        with system_instruction(plan.config.system_instruction):
            if plan.mode == "static":
                precomputed: dict[int, str] = {}
                if plan.config.batch:
                    inner = caller.inner if isinstance(caller, _RecordingCaller) else caller
                    if not isinstance(inner, PresetCaller):
                        raise BatchNotAvailable("The Batch API needs an OpenAI preset model")
                    precomputed = await _batch_precompute(plan, inner, blocked, warnings, batch_state, prompts if keep_prompts else None)
                rows, run_warnings, records = await simulate_static(
                    caller=caller,
                    twins=twins_input,
                    instances=plan.instrument["instances"],
                    concurrency=max_concurrency,
                    precomputed=precomputed,
                    blocked_image_urls=blocked,
                    on_progress=on_progress,
                )
                audits = _describe_static_twins(plan, records)
            else:
                rows, run_warnings, audits = await simulate_staged(
                    caller=caller,
                    twins=twins_input,
                    definition=plan.definition,
                    seed=plan.config.seed,
                    max_stages=plan.config.max_stages,
                    initial_embedded_data=plan.config.embedded_data or None,
                    carry_stage_images=plan.config.carry_stage_images,
                    concurrency=max_concurrency,
                    blocked_image_urls=blocked,
                    on_progress=on_progress,
                    persona_count=plan.population,
                )
    finally:
        await caller.close()
    warnings.extend(run_warnings)
    _keep_shown_for_reruns(audits, rows, plan)
    return _build_results(plan, rows, audits, usage, warnings, prompts, started_at, time.monotonic() - started, batch_state, images_left_out=blocked)


def _describe_static_twins(plan: Plan, records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Each static twin's audit record with the values its survey copy was given,
    in order, and what the copy showed (real-time and batch runs alike)."""
    from .answers.rerun import shown_questions

    instances = plan.instrument["instances"]
    for record in records:
        instance = instances[int(record.get("persona_index", 0)) % len(instances)]
        record["assignments"] = [dict(a) for a in instance.get("assignments") or []]
        record["shown"] = shown_questions(instance.get("blocks"))
        if instance.get("experiments"):
            record["experiments"] = [dict(e) for e in instance["experiments"]]
    return records


# The question an answer belongs to: a loop row (1_QID5), a matrix or
# multi-select row (QID3_1), a side-by-side cell (QID4#1_2) or a write-in (QID6_TEXT).
_ANSWER_QUESTION = re.compile(r"^(?:\d+_)?(QID\d+)")


def _keep_shown_for_reruns(audits: list[dict[str, Any]], rows: list[dict[str, Any]], plan: Plan) -> None:
    """Keep in each twin's audit what a rerun of it needs to show the twin's own
    survey: the options and descriptive text as shown, for its flagged answers
    and for answers to questions whose options or text pipe or draw a value (the
    rest the template gives as they were)."""
    from .answers.rerun import _is_flagged, piped_question_ids

    piped = piped_question_ids(plan.instrument.get("definition") or plan.survey.standard)
    flagged: dict[str, set[str]] = {}
    for row in rows:
        if _is_flagged(row.get("answer_status", "")):
            flagged.setdefault(str(row.get("persona_id", "")), set()).add(str(row.get("question_id", "")))

    def piped_answer(answer_id: str) -> bool:
        match = _ANSWER_QUESTION.match(answer_id)
        return answer_id in piped or bool(match and match.group(1) in piped)

    for record in audits:
        if isinstance(record, dict) and isinstance(record.get("shown"), dict):
            mine = flagged.get(str(record.get("persona_id", "")), set())
            record["shown"] = {qid: value for qid, value in record["shown"].items() if qid in mine or piped_answer(qid)}


def run(plan: Plan, **kwargs: Any) -> Results:
    """Ask every twin its survey and return ``Results``. Needs the model's key."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(arun(plan, **kwargs))
    raise RuntimeError("run() was called inside a running event loop; use 'await surveytwin.arun(plan)' instead")


def _twin_records(plan: Plan, rows: list[dict[str, Any]], audits: list[dict[str, Any]], usage: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_twin_usage: dict[str, dict[str, Any]] = {}
    for u in usage:
        entry = by_twin_usage.setdefault(str(u.get("twin_id", "")), {"calls": 0, "input_tokens": 0, "output_tokens": 0})
        entry["calls"] += 1
        entry["input_tokens"] += int(u.get("input_tokens") or 0)
        entry["output_tokens"] += int(u.get("output_tokens") or 0)
    audit_by_twin = {str(a.get("persona_id", "")): a for a in audits if isinstance(a, dict)}
    records: list[dict[str, Any]] = []
    for idx, twin in enumerate(plan.twins):
        audit = audit_by_twin.get(twin.id, {})
        twin_rows = [r for r in rows if str(r.get("persona_id", "")) == twin.id]
        if plan.mode == "static":
            status = "failed" if audit.get("status") == "failed" else "complete"
            reason = audit.get("error", "")
            if status == "complete" and audit.get("no_usable_answers"):
                status, reason = "failed", "the model gave no usable answers; every question recorded as skipped"
        else:
            asked = int(audit.get("model_stages") or 0)
            if audit.get("stopped_at_stage_limit"):
                status, reason = "incomplete", f"stopped at the stage limit ({plan.config.max_stages})"
            elif not twin_rows:
                status, reason = "failed", "no answers"
            elif asked and int(audit.get("no_usable_answer_stages") or 0) == asked:
                status, reason = "failed", "the model gave no usable answers at any stage; every question recorded as skipped"
            else:
                status, reason = "complete", ""
        # (blank multi-select options and prefilled boxes are not answers from the
        # model: the call-level check above catches a twin that has only those)
        if twin_rows and all(str(r.get("answer_status") or "") in {"skipped", "needs_validation"} for r in twin_rows):
            status, reason = "failed", "no usable answers; every question recorded as skipped"
        records.append(
            {
                "id": twin.id,
                "index": idx,
                "attributes": twin.attributes,
                "representation": twin.representation,
                "source": twin.source,
                "seed": plan.config.seed,
                "status": status,
                "reason": reason,
                "usage": by_twin_usage.get(twin.id, {"calls": 0, "input_tokens": 0, "output_tokens": 0}),
            }
        )
    return records


def _build_results(
    plan: Plan,
    rows: list[dict[str, Any]],
    audits: list[dict[str, Any]],
    usage: list[dict[str, Any]],
    warnings: list[str],
    prompts: list[dict[str, Any]],
    started_at: str,
    elapsed_s: float,
    batch_state: dict[str, Any],
    *,
    images_left_out: list[str] | None = None,
) -> Results:
    from .answers import executor as ex
    from .personas.wrapping import CT_SYSTEM_INSTRUCTION
    from .prompts import PROMPT_VERSION
    from .survey import support_table

    survey = plan.survey
    config_record = plan.config.to_record()
    instruction = plan.config.system_instruction or ex.DEFAULT_SYSTEM_INSTRUCTION
    system_prompt = f"{instruction} {ex.SIMULATION_FORMAT_CONTRACT}\n{CT_SYSTEM_INSTRUCTION}"
    names = {b["block_id"]: b["name"] for b in _all_blocks(survey)}
    provenance = {
        "library_version": __version__,
        "prompt_version": PROMPT_VERSION,
        "support_table_version": support_table().get("version"),
        "run_seed": plan.config.seed,
        "execution_mode": plan.mode,
        "survey_name": survey.name,
        "survey_checksum_sha256": survey.checksum,
        "survey_source": survey.source_name,
        "survey_kind": survey.kind,
        "survey_edits": [dict(e) for e in survey.edits],
        "excluded_blocks": list(survey.exclude_blocks),
        "bank": config_record["bank"],
        "representation": config_record["representation"],
        "model": config_record["model"],
        "custom_system_instruction": plan.config.system_instruction is not None,
        "started_at": started_at,
        "finished_at": now_iso(),
        "elapsed_s": round(elapsed_s, 3),
        # Survey images that were not sent (accept_missing_images=True).
        "images_left_out": sorted(images_left_out or []),
    }
    if batch_state:
        provenance["batch"] = {k: v for k, v in batch_state.items() if k in {"batch_id", "input_file_id", "item_count", "status"}}
    # Embedded images (data URIs) are stored once, in run["media"], and referred to
    # by digest elsewhere: a static run would otherwise keep one copy per twin.
    media: dict[str, str] = {}
    # the survey as asked (without the dropped blocks' questions: a dropped question
    # no longer exists, so no file lists it)
    definition = _compact_media(plan.instrument.get("definition") or plan.survey.standard, media)
    # each static twin's copy: its blocks of shown questions (a staged twin's are in its audit)
    copies = _compact_media([instance.get("blocks") or [] for instance in plan.instrument.get("instances") or []], media)
    provenance["images_left_out"] = [_media_key(u, media) if str(u).startswith("data:image/") else u for u in provenance["images_left_out"]]
    run_info = {
        "run_id": f"run-{uuid.uuid4().hex[:12]}",
        "mode": plan.mode,
        "config": config_record,
        "system_prompt": system_prompt,
        "excluded_blocks": {bid: names.get(bid, bid) for bid in survey.exclude_blocks},
        "analysis": plan.instrument.get("analysis", {}),
        "definition": definition,
        "copies": copies,
        "media": media,
        "batch_phase": "collected" if batch_state.get("batch_id") else "",
        "twin_ids": [t.id for t in plan.twins],
        # The plan's full size: arms, orders and draws were dealt over this many
        # twins; a plan.head(n) run asked the first n of them.
        "population": plan.population,
        # Persona text is kept only for twins a rerun may re-ask.
        "rerun_personas": {t.id: t.text for t in plan.twins if t.id in _flagged_ids(rows)},
    }
    result = Results(
        rows=rows,
        twins=_twin_records(plan, rows, audits, usage),
        audit=audits,
        usage=usage,
        warnings=warnings,
        provenance=provenance,
        run=run_info,
        prompts=prompts,
    )
    result._model = plan.config.model
    result._survey = plan.survey
    return result


def _media_key(uri: str, table: dict[str, str]) -> str:
    import hashlib

    key = "media:sha256:" + hashlib.sha256(str(uri).encode("utf-8")).hexdigest()[:16]
    table[key] = str(uri)
    return key


def _compact_media(value: Any, table: dict[str, str]) -> Any:
    """A copy with every long data URI replaced by a key into ``table``."""
    if isinstance(value, str):
        is_image = value.startswith("data:image/") and ";base64," in value[:100]
        return _media_key(value, table) if is_image and len(value) > 200 else value
    if isinstance(value, list):
        return [_compact_media(v, table) for v in value]
    if isinstance(value, dict):
        return {k: _compact_media(v, table) for k, v in value.items()}
    return value


def _flagged_ids(rows: list[dict[str, Any]]) -> set[str]:
    from .answers.rerun import flagged_twins

    return set(flagged_twins(rows))


def _all_blocks(survey: Any) -> list[dict[str, str]]:
    from .survey import _blocks

    return [
        {"block_id": str(b.get("BlockID", "")), "name": str(b.get("BlockName") or b.get("Description") or "")}
        for b, _ in _blocks(survey._full_template)
    ]
