"""The two calls the survey engine makes, for a preset or a defined model.

The engine (ported from the website) asks for either ``chat_json`` (text-only
surveys) or ``responses_input_text`` (surveys with images). A preset sends both
through the website's OpenAI client unchanged. A defined model gets the same
content as a ``ModelRequest`` in its own layout.
"""

from __future__ import annotations

import json
from typing import Any, Callable

import httpx

from .context import current_call_context
from .models import Model, ModelRequest, Reply
from .openai_client import OpenAIClient, extract_json_string  # noqa: F401 - OpenAIClient for callers that import it here
from .usage import UsageRecord


class PresetCaller:
    def __init__(self, model: Model, client: OpenAIClient) -> None:
        self.model = model
        self.client = client

    @property
    def enabled(self) -> bool:
        return self.client.enabled

    async def chat_json(self, *, system_prompt: str, user_prompt: str, temperature: float, max_tokens: int) -> dict[str, Any]:
        if self.model.reasoning_effort:
            return json.loads(extract_json_string(await self.json_text(
                system_prompt=system_prompt, user_prompt=user_prompt, temperature=temperature, max_tokens=max_tokens
            )))
        return await self.client.chat_json(
            model=self.model.name,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            temperature=temperature,
            max_tokens=max_tokens,
        )

    async def responses_input_text(self, *, input_items: list[dict[str, Any]], max_tokens: int) -> str:
        return await self.client.responses_input_text(
            model=self.model.name, input_items=input_items, max_tokens=max_tokens, reasoning_effort=self.model.reasoning_effort
        )

    async def json_text(self, *, system_prompt: str, user_prompt: str, temperature: float, max_tokens: int) -> str:
        """The reply text of a JSON-mode call, unparsed (for callers that check its shape)."""
        return await self.client.chat_text(
            self.model.name, system_prompt, user_prompt, temperature=temperature, max_tokens=max_tokens,
            response_format={"type": "json_object"}, reasoning_effort=self.model.reasoning_effort,
        )

    async def close(self) -> None:
        await self.client.close()


def _request_from_items(input_items: list[dict[str, Any]], max_tokens: int, json_mode: bool) -> ModelRequest:
    system_parts: list[str] = []
    content: list[dict[str, str]] = []
    for item in input_items:
        role = str(item.get("role", "user"))
        parts = item.get("content")
        if isinstance(parts, str):
            parts = [{"type": "input_text", "text": parts}]
        for part in parts or []:
            if not isinstance(part, dict):
                continue
            if part.get("type") in {"input_text", "text"}:
                text = str(part.get("text", ""))
                if role == "system":
                    system_parts.append(text)
                else:
                    content.append({"type": "text", "text": text})
            elif part.get("type") in {"input_image", "image_url"} and role != "system":
                url = part.get("image_url")
                if isinstance(url, dict):
                    url = url.get("url")
                if url:
                    content.append({"type": "image", "url": str(url)})
    user_text = "\n\n".join(p["text"] for p in content if p["type"] == "text")
    return ModelRequest(
        system="\n".join(system_parts),
        user=user_text,
        content=content,
        images=[p["url"] for p in content if p["type"] == "image"],
        max_output_tokens=max_tokens,
        json_mode=json_mode,
    )


class DefinedModelCaller:
    """Calls a defined model; a cut-off reply is retried once with twice the room."""

    def __init__(self, model: Model, on_usage: Callable[[UsageRecord], None] | None, *, api_key: str | None = None) -> None:
        self.model = model
        self.on_usage = on_usage
        self.api_key = api_key
        self._http: httpx.AsyncClient | None = None

    @property
    def enabled(self) -> bool:
        return True

    def _client(self) -> httpx.AsyncClient:
        if self._http is None or self._http.is_closed:
            self._http = httpx.AsyncClient()
        return self._http

    def _record(self, reply: Reply | None, meta: dict[str, Any], status: str, error: str = "") -> None:
        if self.on_usage is None:
            return
        ctx = current_call_context()
        in_tokens = (reply.input_tokens if reply else None) or 0
        out_tokens = (reply.output_tokens if reply else None) or 0
        known = reply is not None and reply.input_tokens is not None and reply.output_tokens is not None
        record = UsageRecord(
            # the provider a built-in model calls ("anthropic", "gemini", "deepseek");
            # for a model of your own, how it is called ("http", "openai-compatible", "function")
            provider=self.model.builtin or self.model.kind,
            endpoint=self.model.layout or "function",
            model=self.model.name,
            resolved_model=self.model.name,
            status=status,
            input_tokens=in_tokens,
            cached_input_tokens=(reply.cached_input_tokens if reply else None) or 0,
            output_tokens=out_tokens,
            reasoning_tokens=(reply.reasoning_tokens if reply else None) or 0,
            total_tokens=in_tokens + out_tokens,
            latency_ms=int(meta.get("latency_ms", 0) or 0),
            request_id=reply.request_id if reply else "",
            twin_id=str(ctx.get("twin_id", "")),
            stage=ctx.get("stage"),
            purpose=str(ctx.get("purpose", "answer")),
            error=error[:500],
            extra={"usage_known": known, "stop_reason": meta.get("stop_reason"), "kind": self.model.kind},
        )
        try:
            self.on_usage(record)
        except Exception:
            pass

    async def _call(self, request: ModelRequest, *, fail_on_truncation: bool) -> str:
        budget = request.max_output_tokens
        reply: Reply | None = None
        for attempt in range(2):
            request.max_output_tokens = budget if attempt == 0 else budget * 2
            try:
                reply, meta = await self.model.acall(request, client=self._client(), api_key=self.api_key)
            except Exception as exc:
                self._record(None, {}, "failed", str(exc))
                raise
            if meta.get("stopped"):
                # a refusal, a safety stop, a blocked prompt: the call was made (and
                # its tokens are recorded) but gave no answer to use
                self._record(reply, meta, "stopped", str(meta["stopped"]))
                raise RuntimeError(f"{self.model.name} gave no answer: {meta['stopped']}")
            self._record(reply, meta, "completed")
            if reply.text.strip() and not reply.cut_off:
                return reply.text
        if reply is None or not reply.text.strip():
            raise RuntimeError(f"{self.model.name} returned no reply text after retry")
        if fail_on_truncation:
            raise RuntimeError(f"{self.model.name} reply was cut off by the output limit after retry")
        return reply.text

    async def chat_json(self, *, system_prompt: str, user_prompt: str, temperature: float, max_tokens: int) -> dict[str, Any]:
        request = ModelRequest(
            system=system_prompt,
            user=user_prompt,
            content=[{"type": "text", "text": user_prompt}],
            images=[],
            max_output_tokens=max_tokens,
            json_mode=True,
            temperature=temperature,
        )
        text = await self._call(request, fail_on_truncation=True)
        return json.loads(extract_json_string(text))

    async def json_text(self, *, system_prompt: str, user_prompt: str, temperature: float, max_tokens: int) -> str:
        """The reply text of a JSON-mode call, unparsed (for callers that check its shape)."""
        request = ModelRequest(
            system=system_prompt,
            user=user_prompt,
            content=[{"type": "text", "text": user_prompt}],
            images=[],
            max_output_tokens=max_tokens,
            json_mode=True,
            temperature=temperature,
        )
        return await self._call(request, fail_on_truncation=True)

    async def responses_input_text(self, *, input_items: list[dict[str, Any]], max_tokens: int) -> str:
        request = _request_from_items(input_items, max_tokens, json_mode=False)
        if request.images and not self.model.images:
            raise RuntimeError(f"{self.model.name} API error 400: this model does not take image input")
        return await self._call(request, fail_on_truncation=False)

    async def close(self) -> None:
        if self._http is not None and not self._http.is_closed:
            await self._http.aclose()
        self._http = None


def make_caller(model: Model, *, api_key: str | None, on_usage: Callable[[UsageRecord], None] | None):
    """The caller a run uses for ``model``: an OpenAI or OpenRouter model through
    its OpenAI client (the same requests as before; OpenRouter as a fallback only when
    the model asks for it), any other model through ``Model.acall``. ``api_key``
    replaces the key the model reads from the environment."""
    if model.kind == "preset":
        return PresetCaller(model, model.openai_client(api_key=api_key, on_usage=on_usage))
    return DefinedModelCaller(model, on_usage, api_key=api_key)
