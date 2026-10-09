"""The OpenAI Batch path without waiting: submit a static plan, check it, collect it.

    batch_id = st.batch.submit(plan)
    st.batch.status(batch_id)          # "validating", "in_progress", "completed", ...
    results = st.batch.collect(batch_id, plan)

Twins whose batch line is missing or unusable are asked again in real time when
collecting, as on the website.
"""

from __future__ import annotations

import asyncio
import os
from typing import Any

from .errors import BatchNotAvailable, ConfigError, ImagesRefused


def _client(api_key: str | None):
    from .providers.openai_client import OpenAIClient

    key = api_key or os.environ.get("OPENAI_API_KEY", "")
    if not key:
        raise ConfigError("The Batch API needs an OpenAI key: pass api_key= or set OPENAI_API_KEY")
    return OpenAIClient(openai_api_key=key)


def _key(api_key: str | None, model: Any) -> str | None:
    """The key a batch uses: the one given, else the model's own key variable."""
    name = getattr(model, "api_key_env", None)
    return api_key or (os.environ.get(str(name), "").strip() if name else "") or None


def _check(plan: Any) -> None:
    if plan.mode != "static":
        raise BatchNotAvailable("Staged surveys cannot be batched: each stage needs the previous answers")
    if not (plan.config.model.kind == "preset" and plan.config.model.provider == "openai"):
        raise BatchNotAvailable("The Batch API is OpenAI-only; use an OpenAI preset model")


def submit(plan: Any, *, api_key: str | None = None, image_checker: Any = None) -> str:
    """Upload the plan's batch file and create the batch; returns the batch id.

    Images are checked before anything is sent, as for a run: an image that
    cannot be sent stops here unless ``accept_missing_images=True``.
    """
    _check(plan)
    from .run import _image_gate

    blocked, _notes = _image_gate(plan, image_checker)

    async def go() -> str:
        client = _client(_key(api_key, plan.config.model))
        try:
            items = plan.batch_items(client, blocked)
            if not items:
                raise BatchNotAvailable("No twin in this plan needs a model call; there is nothing to submit")
            submitted = await client.submit_chat_batch(items, metadata={"library": "surveytwin"})
            return str(submitted["batch_id"])
        finally:
            await client.close()

    return asyncio.run(go())


def status(batch_id: str, *, api_key: str | None = None) -> str:
    async def go() -> str:
        client = _client(api_key)
        try:
            return str((await client.poll_chat_batch_once(batch_id)).get("status"))
        finally:
            await client.close()

    return asyncio.run(go())


def _collect_image_gate(plan: Any, image_checker: Any) -> tuple[list[str], list[str]]:
    """The images left out when collecting. The batch was submitted after its own
    image check, so an image link that broke since does not stop the collection."""
    from .run import _image_gate

    try:
        return _image_gate(plan, image_checker)
    except ImagesRefused:
        # Without accept_missing_images the batch was submitted with every
        # image (its own check would have stopped it otherwise), so its answers
        # were given with them and are kept: nothing is left out. A twin asked
        # again in real time sends the image, and when the provider names it as
        # one it cannot load, the question that shows it is skipped.
        return [], [
            "Some survey images could no longer be sent when the batch was collected. The batch "
            "answers were given with them; a twin asked again in real time did not answer a question "
            "that shows one the model can no longer load (recorded as skipped)."
        ]


def collect(
    batch_id: str,
    plan: Any,
    *,
    api_key: str | None = None,
    max_concurrency: int = 8,
    keep_prompts: bool = False,
    image_checker: Any = None,
):
    """Results from a finished batch; unusable lines are re-asked in real time.

    ``image_checker`` checks the image links as in ``submit`` (without it they
    are tested over the network). ``keep_prompts`` keeps each batch line's
    request and each request made again in real time."""
    _check(plan)
    from .answers.simulate import simulate_static
    from .plan import system_instruction
    from .providers.caller import PresetCaller
    from .providers.usage import UsageRecord
    from .results.results import now_iso
    from .run import _RecordingCaller, _build_results, _describe_static_twins, _keep_shown_for_reruns, batch_prompt_records
    import time

    async def go():
        usage: list[dict[str, Any]] = []

        def record(item: UsageRecord) -> None:
            usage.append(item.to_dict())

        client = _client(_key(api_key, plan.config.model))
        client.on_usage = record
        caller: Any = PresetCaller(plan.config.model, client)
        prompts: list[dict[str, Any]] = []
        if keep_prompts:
            caller = _RecordingCaller(caller, prompts)
        started = time.monotonic()
        started_at = now_iso()
        try:
            batch = await client.poll_chat_batch_once(batch_id)
            if batch.get("status") != "completed":
                raise BatchNotAvailable(f"Batch {batch_id} is {batch.get('status')}, not completed")
            precomputed = await client.collect_chat_batch_results(
                batch_id, model=plan.config.model.name, item_count=len(plan.twins), twin_ids=[t.id for t in plan.twins]
            )
            blocked, notes = _collect_image_gate(plan, image_checker)
            notes = notes + list(plan.warnings)
            from .providers.openai_client import OpenAIClient

            with system_instruction(plan.config.system_instruction):
                # the lines as submitted (building them needs no key and no network)
                items = plan.batch_items(OpenAIClient(), blocked)
            usable = sum(1 for k in precomputed if str(k).isdigit())
            if len(items) > usable:
                notes.append(f"{len(items) - usable} batch answer(s) were unusable; those twins were asked again in real time.")
            if keep_prompts:
                prompts.extend(batch_prompt_records(plan, items))
            with system_instruction(plan.config.system_instruction):
                rows, warnings, records = await simulate_static(
                    caller=caller,
                    twins=[{"persona_id": t.id, "persona_text": t.text} for t in plan.twins],
                    instances=plan.instrument["instances"],
                    concurrency=max_concurrency,
                    precomputed={int(k): v for k, v in precomputed.items() if str(k).isdigit()},
                    blocked_image_urls=blocked,
                )
            records = _describe_static_twins(plan, records)
            _keep_shown_for_reruns(records, rows, plan)
        finally:
            await client.close()
        return _build_results(
            plan, rows, records, usage, notes + warnings, prompts, started_at, time.monotonic() - started,
            {"batch_id": batch_id, "status": "completed"}, images_left_out=blocked,
        )

    return asyncio.run(go())
