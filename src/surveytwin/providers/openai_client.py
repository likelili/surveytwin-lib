"""OpenAI and OpenRouter calls for the named presets.

Ported from the website's ``services/v2/llm_client.py``. Request bodies, the
reasoning-token headroom, the truncation retry and the OpenRouter fallback are
unchanged, so a preset sends exactly what the website sends. The differences:
keys are passed in (never read from a ``.env`` file), and usage goes to a
callback instead of the website's billing ledger.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from typing import Any, Callable

import httpx

from .context import current_call_context
from .presets import MODEL_SPECS, _REASONING_TOKEN_ALLOWANCES
from .usage import UsageRecord, normalize_usage

_logger = logging.getLogger(__name__)

OPENAI_BASE_URL = "https://api.openai.com/v1"
OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"


def extract_json_string(raw_text: str) -> str:
    """The JSON object inside a reply, tolerating code fences and prose around it."""
    text = raw_text.strip()
    fenced = re.match(r"^```(?:json)?\s*(.*?)\s*```$", text, flags=re.DOTALL | re.IGNORECASE)
    if fenced:
        text = fenced.group(1).strip()
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end >= start:
        return text[start : end + 1]
    return text


def extract_responses_text(payload: dict[str, Any]) -> str:
    """Assistant text from a Responses API payload (citation markers removed)."""
    text_chunks: list[str] = []
    output_items = payload.get("output", [])
    if isinstance(output_items, list):
        for item in output_items:
            if not isinstance(item, dict) or item.get("type") != "message":
                continue
            content_items = item.get("content", [])
            if not isinstance(content_items, list):
                continue
            for content in content_items:
                if not isinstance(content, dict):
                    continue
                if str(content.get("type", "")).strip() not in ("text", "output_text"):
                    continue
                raw = content.get("text")
                if isinstance(raw, str) and raw.strip():
                    text_chunks.append(raw.strip())
    raw_text = "\n".join(text_chunks).strip()
    if not raw_text:
        fallback = payload.get("output_text")
        if isinstance(fallback, str) and fallback.strip():
            raw_text = fallback.strip()
    clean_text = re.sub(r"【\d+†[^】]*】", "", raw_text)
    clean_text = re.sub(r"\s{2,}", " ", clean_text).strip()
    return clean_text


def _empty_response_detail(payload: dict[str, Any]) -> str:
    parts: list[str] = []
    status = str(payload.get("status", "")).strip()
    if status:
        parts.append(f"status={status}")
    incomplete = payload.get("incomplete_details")
    if isinstance(incomplete, dict):
        reason = str(incomplete.get("reason", "")).strip()
        if reason:
            parts.append(f"incomplete_reason={reason}")
    usage = payload.get("usage")
    if isinstance(usage, dict):
        if usage.get("total_tokens") is not None:
            parts.append(f"total_tokens={usage.get('total_tokens')}")
        if usage.get("output_tokens") is not None:
            parts.append(f"output_tokens={usage.get('output_tokens')}")
        details = usage.get("output_tokens_details")
        if isinstance(details, dict) and details.get("reasoning_tokens") is not None:
            parts.append(f"reasoning_tokens={details.get('reasoning_tokens')}")
    return " ".join(parts) or "no details"


def _twin_for(custom_id: str, twin_ids: list[str] | None) -> str:
    """A batch line's custom id is the twin's position in the plan; map it back."""
    if twin_ids and str(custom_id).isdigit() and int(custom_id) < len(twin_ids):
        return str(twin_ids[int(custom_id)])
    return str(custom_id)


class OpenAIClient:
    """The website's ``LLMClient`` for survey calls, with keys passed in."""

    _VERBOSITY_RANK = {"low": 0, "medium": 1, "high": 2}
    _BATCH_TERMINAL_STATUSES = {"completed", "failed", "expired", "cancelled", "canceled"}

    def __init__(
        self,
        *,
        openai_api_key: str = "",
        openrouter_api_key: str = "",
        on_usage: Callable[[UsageRecord], None] | None = None,
        openai_base_url: str = OPENAI_BASE_URL,
        openrouter_base_url: str = OPENROUTER_BASE_URL,
    ) -> None:
        self.openai_api_key = str(openai_api_key or "").strip()
        self.openrouter_api_key = str(openrouter_api_key or "").strip()
        self.on_usage = on_usage
        self.openai_base_url = openai_base_url.rstrip("/")
        self.openrouter_base_url = openrouter_base_url.rstrip("/")
        self._client: httpx.AsyncClient | None = None

    # ---- presets -----------------------------------------------------------

    @property
    def enabled(self) -> bool:
        return bool(self.openai_api_key or self.openrouter_api_key)

    @staticmethod
    def _model_spec(model: str) -> dict[str, str | None]:
        spec = MODEL_SPECS.get(model)
        return spec if isinstance(spec, dict) else {}

    @classmethod
    def _openrouter_model_name(cls, model: str, model_name: str) -> str:
        explicit_name = cls._model_spec(model).get("openrouter_model_name")
        if explicit_name:
            return str(explicit_name)
        if model_name.startswith("gpt-"):
            return f"openai/{model_name}"
        return model_name

    # OpenAI's reasoning models: GPT-5 and every later GPT, and the o-series. They
    # are called through the Responses API and take a reasoning effort instead of a
    # temperature (OpenAI's reasoning guide).
    _REASONING_MODEL = re.compile(r"^(gpt-([5-9]|\d{2,})|o[1-9])(\b|[.-])")

    @classmethod
    def _is_gpt5_model(cls, model_name: str) -> bool:
        """A reasoning model (GPT-5 and later, or o-series); OpenRouter names the
        same models "openai/gpt-5...". Fine-tuned names ("ft:gpt-5...") count too."""
        name = model_name.rsplit("/", 1)[-1]
        name = name[3:] if name.startswith("ft:") else name
        return bool(cls._REASONING_MODEL.match(name))

    @staticmethod
    def _normalize_reasoning_effort(model_name: str, reasoning_effort: str | None) -> str | None:
        if not reasoning_effort:
            return None
        effort = reasoning_effort.strip().lower()
        if not effort:
            return None
        if model_name.rsplit("/", 1)[-1] == "gpt-5-mini" and effort == "none":
            return "minimal"
        return effort

    @classmethod
    def _default_reasoning_effort_for_model(cls, model: str) -> str | None:
        spec = cls._model_spec(model)
        model_name = str(spec.get("model_name", model))
        default_reasoning_effort = spec.get("default_reasoning_effort")
        if not default_reasoning_effort:
            return None
        return cls._normalize_reasoning_effort(model_name, default_reasoning_effort)

    @classmethod
    def clamp_verbosity(cls, model: str, verbosity: str) -> str:
        spec = cls._model_spec(model)
        rank = cls._VERBOSITY_RANK.get(verbosity, 1)
        min_v = spec.get("min_verbosity")
        if min_v and min_v in cls._VERBOSITY_RANK and rank < cls._VERBOSITY_RANK[min_v]:
            return str(min_v)
        max_v = spec.get("max_verbosity")
        if max_v and max_v in cls._VERBOSITY_RANK and rank > cls._VERBOSITY_RANK[max_v]:
            return str(max_v)
        return verbosity

    @classmethod
    def _reasoning_token_allowance(cls, model_name: str, effort: str | None) -> int:
        if not cls._is_gpt5_model(model_name) or "chat" in model_name:
            return 0
        key = (effort or "medium").strip().lower()
        return _REASONING_TOKEN_ALLOWANCES.get(key, _REASONING_TOKEN_ALLOWANCES["medium"])

    @staticmethod
    def _retry_reasoning_effort(effort: str | None) -> str | None:
        if effort in (None, "none", "minimal", "low"):
            return effort
        return "low"

    @staticmethod
    def _is_truncated_response(payload: dict[str, Any]) -> bool:
        if str(payload.get("status", "")).strip() != "incomplete":
            return False
        details = payload.get("incomplete_details")
        reason = str(details.get("reason", "")).strip() if isinstance(details, dict) else ""
        return reason == "max_output_tokens"

    def _resolve_provider_and_model(self, model: str) -> tuple[str, str, str | None]:
        spec = self._model_spec(model)
        model_name = str(spec.get("model_name", model))
        default_reasoning_effort = self._default_reasoning_effort_for_model(model)
        if self.openai_api_key:
            return ("openai", model_name, default_reasoning_effort)
        if self.openrouter_api_key:
            return ("openrouter", self._openrouter_model_name(model, model_name), default_reasoning_effort)
        raise RuntimeError("No LLM provider configured")

    def resolved_model_name(self, model: str) -> str:
        return str(self._model_spec(model).get("model_name", model))

    # ---- transport ------------------------------------------------------------

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient()
        return self._client

    async def close(self) -> None:
        if self._client is not None and not self._client.is_closed:
            await self._client.aclose()
        self._client = None

    def _record_usage(
        self,
        *,
        provider: str,
        endpoint: str,
        model_alias: str,
        resolved_model: str,
        response: httpx.Response | None,
        payload: dict[str, Any],
        status: str,
        latency_ms: int,
        error_text: str = "",
        batch: bool = False,
        twin_id: str | None = None,
    ) -> None:
        if self.on_usage is None:
            return
        tokens = normalize_usage(endpoint, payload.get("usage") if isinstance(payload, dict) else {})
        request_id = ""
        if response is not None:
            request_id = response.headers.get("x-request-id") or response.headers.get("openai-request-id") or ""
        if not request_id and isinstance(payload, dict):
            request_id = str(payload.get("id", "") or "")
        ctx = current_call_context()
        record = UsageRecord(
            provider=provider,
            endpoint=endpoint,
            model=model_alias,
            resolved_model=resolved_model,
            status=status,
            input_tokens=tokens["input_tokens"],
            cached_input_tokens=tokens["cached_input_tokens"],
            output_tokens=tokens["output_tokens"],
            reasoning_tokens=tokens["reasoning_tokens"],
            total_tokens=tokens["total_tokens"],
            latency_ms=latency_ms,
            request_id=request_id,
            twin_id=twin_id if twin_id is not None else str(ctx.get("twin_id", "")),
            stage=ctx.get("stage"),
            purpose=str(ctx.get("purpose", "answer")),
            batch=batch,
            error=error_text[:500],
        )
        try:
            self.on_usage(record)
        except Exception:  # a caller's callback must never break a run
            _logger.warning("on_usage callback failed", exc_info=True)

    async def _post(
        self,
        *,
        url: str,
        api_key: str,
        payload: dict[str, Any],
        timeout: float,
        provider: str,
        endpoint: str,
        model_alias: str,
        model_name: str,
        error_label: str,
    ) -> dict[str, Any]:
        client = self._get_client()
        started = time.monotonic()
        response = await client.post(
            url,
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json=payload,
            timeout=timeout,
        )
        latency_ms = int((time.monotonic() - started) * 1000)
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            detail = response.text[:1000]
            self._record_usage(
                provider=provider,
                endpoint=endpoint,
                model_alias=model_alias,
                resolved_model=model_name,
                response=response,
                payload={},
                status="failed",
                latency_ms=latency_ms,
                error_text=detail,
            )
            raise RuntimeError(f"{error_label} error {response.status_code}: {detail}") from exc
        data = response.json()
        self._record_usage(
            provider=provider,
            endpoint=endpoint,
            model_alias=model_alias,
            resolved_model=model_name,
            response=response,
            payload=data,
            status="completed",
            latency_ms=latency_ms,
        )
        return data

    async def _responses_text_from_input(
        self,
        *,
        api_key: str,
        model_name: str,
        model_alias: str,
        input_items: list[dict[str, Any]],
        max_tokens: int,
        reasoning_effort: str | None,
        verbosity: str,
        fail_on_truncation: bool = False,
    ) -> str:
        normalized_effort = self._normalize_reasoning_effort(model_name, reasoning_effort)
        base_budget = max_tokens + self._reasoning_token_allowance(model_name, normalized_effort)
        attempts: list[tuple[int, str | None]] = [
            (base_budget, normalized_effort),
            (base_budget * 2, self._retry_reasoning_effort(normalized_effort)),
        ]
        data: dict[str, Any] = {}
        text = ""
        for attempt_idx, (budget, effort) in enumerate(attempts):
            payload: dict[str, Any] = {
                "model": model_name,
                "input": input_items,
                "max_output_tokens": budget,
                "text": {"verbosity": verbosity},
            }
            if effort and self._is_gpt5_model(model_name):
                payload["reasoning"] = {"effort": effort}
            data = await self._post(
                url=f"{self.openai_base_url}/responses",
                api_key=api_key,
                payload=payload,
                timeout=240.0,
                provider="openai",
                endpoint="responses",
                model_alias=model_alias,
                model_name=model_name,
                error_label="Responses API",
            )
            text = extract_responses_text(data)
            truncated = self._is_truncated_response(data)
            if text and not truncated:
                return text
            if attempt_idx + 1 < len(attempts):
                next_budget, next_effort = attempts[attempt_idx + 1]
                _logger.warning(
                    "Responses API output %s (%s); retrying model=%s with max_output_tokens=%s effort=%s",
                    "truncated" if truncated else "empty",
                    _empty_response_detail(data),
                    model_name,
                    next_budget,
                    next_effort,
                )
        if not text:
            raise RuntimeError(
                f"Responses API returned no assistant text after retry ({_empty_response_detail(data)})"
            )
        if fail_on_truncation:
            raise RuntimeError(f"Responses API output truncated after retry ({_empty_response_detail(data)})")
        _logger.warning("Returning truncated Responses API output after retry model=%s", model_name)
        return text

    async def _chat_completions_text(
        self,
        *,
        provider: str,
        api_key: str,
        model_name: str,
        model_alias: str,
        system_prompt: str,
        user_prompt: str,
        temperature: float,
        max_tokens: int,
        response_format: dict[str, Any] | None,
        reasoning_effort: str | None,
        messages: list[dict[str, Any]] | None = None,
    ) -> str:
        # OpenAI's own endpoint names the limit max_completion_tokens (max_tokens is
        # deprecated there and refused by reasoning models); OpenRouter's, max_tokens.
        limit_key = "max_completion_tokens" if provider == "openai" else "max_tokens"
        payload: dict[str, Any] = {
            "model": model_name,
            "messages": messages
            or [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            limit_key: max_tokens,
        }
        # Reasoning models reject temperature/top_p unless effort is none.
        if not self._is_gpt5_model(model_name):
            payload["temperature"] = temperature
        else:
            normalized_effort = self._normalize_reasoning_effort(model_name, reasoning_effort)
            if normalized_effort:
                if provider == "openai":
                    payload["reasoning_effort"] = normalized_effort
                else:
                    payload["reasoning"] = {"effort": normalized_effort}  # OpenRouter's form
            # room for the reasoning too, as a call to OpenAI's Responses API has
            payload[limit_key] = max_tokens + self._reasoning_token_allowance(model_name, normalized_effort)
        if response_format is not None:
            payload["response_format"] = response_format
        base = self.openrouter_base_url if provider == "openrouter" else self.openai_base_url
        data = await self._post(
            url=f"{base}/chat/completions",
            api_key=api_key,
            payload=payload,
            timeout=180.0,
            provider=provider,
            endpoint="chat_completions",
            model_alias=model_alias,
            model_name=model_name,
            error_label="Chat completions API",
        )
        return data["choices"][0]["message"]["content"]

    # ---- the two calls the survey engine makes ----------------------------------

    async def chat_text(
        self,
        model: str,
        system_prompt: str,
        user_prompt: str,
        *,
        temperature: float = 0.3,
        max_tokens: int = 2500,
        response_format: dict[str, Any] | None = None,
        reasoning_effort: str | None = None,
        verbosity: str = "medium",
    ) -> str:
        if not self.enabled:
            raise RuntimeError("No LLM provider configured")
        provider, model_name, default_effort = self._resolve_provider_and_model(model)
        effort = reasoning_effort if reasoning_effort and reasoning_effort.strip() else default_effort
        use_responses_api = provider == "openai" and self._is_gpt5_model(model_name)
        try:
            if use_responses_api:
                return await self._responses_text_from_input(
                    api_key=self.openai_api_key,
                    model_name=model_name,
                    model_alias=model,
                    input_items=[
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": user_prompt},
                    ],
                    max_tokens=max_tokens,
                    reasoning_effort=effort,
                    verbosity=verbosity,
                    # JSON callers cannot use a truncated payload.
                    fail_on_truncation=response_format is not None,
                )
            api_key = self.openai_api_key if provider == "openai" else self.openrouter_api_key
            return await self._chat_completions_text(
                provider=provider,
                api_key=api_key,
                model_name=model_name,
                model_alias=model,
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                temperature=temperature,
                max_tokens=max_tokens,
                response_format=response_format,
                reasoning_effort=effort,
            )
        except Exception:
            if provider == "openai" and self.openrouter_api_key:
                fallback_model_name = self._openrouter_model_name(model, model_name)
                _logger.warning("Primary OpenAI request failed; retrying via OpenRouter model=%s", fallback_model_name)
                return await self._chat_completions_text(
                    provider="openrouter",
                    api_key=self.openrouter_api_key,
                    model_name=fallback_model_name,
                    model_alias=model,
                    system_prompt=system_prompt,
                    user_prompt=user_prompt,
                    temperature=temperature,
                    max_tokens=max_tokens,
                    response_format=response_format,
                    reasoning_effort=effort,
                )
            raise

    async def chat_json(
        self,
        model: str,
        system_prompt: str,
        user_prompt: str,
        *,
        temperature: float = 0.3,
        max_tokens: int = 2500,
    ) -> dict[str, Any]:
        raw_text = await self.chat_text(
            model=model,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            temperature=temperature,
            max_tokens=max_tokens,
            response_format={"type": "json_object"},
        )
        return json.loads(extract_json_string(raw_text))

    @staticmethod
    def chat_messages_from_input(input_items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Responses-API input items as Chat-Completions messages, images included
        (``input_image`` becomes an ``image_url`` part), for a call through
        OpenRouter. A file attachment, which chat messages cannot carry, becomes a
        line of text."""
        messages: list[dict[str, Any]] = []
        for item in input_items:
            if not isinstance(item, dict):
                continue
            role = str(item.get("role", "user")).strip().lower() or "user"
            content = item.get("content")
            parts: list[dict[str, Any]] = []
            if isinstance(content, str):
                if content.strip():
                    parts.append({"type": "text", "text": content.strip()})
            elif isinstance(content, list):
                for part in content:
                    if not isinstance(part, dict):
                        continue
                    part_type = str(part.get("type", "")).strip().lower()
                    if part_type in {"text", "input_text"}:
                        text = str(part.get("text", "")).strip()
                        if text:
                            parts.append({"type": "text", "text": text})
                    elif part_type in {"input_image", "image_url"}:
                        source = part.get("image_url")
                        url = str(source.get("url", "") if isinstance(source, dict) else source or "").strip()
                        if url:
                            parts.append({"type": "image_url", "image_url": {"url": url}})
                    elif part_type in {"input_file", "file_url"}:
                        parts.append({"type": "text", "text": "[PDF/file attachment provided by the user.]"})
            if not parts:
                continue
            if role == "system" or all(p["type"] == "text" for p in parts):
                messages.append({"role": role, "content": "\n".join(p["text"] for p in parts if p["type"] == "text")})
            else:
                messages.append({"role": role, "content": parts})
        return messages

    @staticmethod
    def flatten_input_items_for_text(input_items: list[dict[str, Any]]) -> tuple[str, str]:
        system_parts: list[str] = []
        user_parts: list[str] = []
        for item in input_items:
            if not isinstance(item, dict):
                continue
            role = str(item.get("role", "user")).strip().lower() or "user"
            content = item.get("content")
            lines: list[str] = []
            if isinstance(content, str):
                if content.strip():
                    lines.append(content.strip())
            elif isinstance(content, list):
                for part in content:
                    if not isinstance(part, dict):
                        continue
                    part_type = str(part.get("type", "")).strip().lower()
                    if part_type in {"text", "input_text"}:
                        text = str(part.get("text", "")).strip()
                        if text:
                            lines.append(text)
                    elif part_type in {"input_image", "image_url"}:
                        lines.append("[Image attachment provided by the user.]")
                    elif part_type in {"input_file", "file_url"}:
                        lines.append("[PDF/file attachment provided by the user.]")
            block = "\n".join(lines).strip()
            if not block:
                continue
            if role == "system":
                system_parts.append(block)
            else:
                user_parts.append(block)
        return ("\n\n".join(system_parts).strip(), "\n\n".join(user_parts).strip())

    async def responses_input_text(
        self,
        *,
        model: str,
        input_items: list[dict[str, Any]],
        max_tokens: int = 2500,
        reasoning_effort: str | None = None,
        verbosity: str = "medium",
        fail_on_truncation: bool = False,
    ) -> str:
        if not self.enabled:
            raise RuntimeError("No LLM provider configured")
        provider, model_name, default_effort = self._resolve_provider_and_model(model)
        effort = reasoning_effort if reasoning_effort and reasoning_effort.strip() else default_effort
        effective_verbosity = self.clamp_verbosity(model, verbosity)
        try:
            if provider == "openai":
                return await self._responses_text_from_input(
                    api_key=self.openai_api_key,
                    model_name=model_name,
                    model_alias=model,
                    input_items=input_items,
                    max_tokens=max_tokens,
                    reasoning_effort=effort,
                    verbosity=effective_verbosity,
                    fail_on_truncation=fail_on_truncation,
                )
            system_prompt, user_prompt = self.flatten_input_items_for_text(input_items)
            return await self._chat_completions_text(
                provider="openrouter",
                api_key=self.openrouter_api_key,
                model_name=model_name,
                model_alias=model,
                system_prompt=system_prompt or "You are a helpful assistant.",
                user_prompt=user_prompt or "Respond to the user's latest request.",
                temperature=0.3,
                max_tokens=max_tokens,
                response_format=None,
                reasoning_effort=effort,
                messages=self.chat_messages_from_input(input_items),  # with the images
            )
        except Exception:
            if provider == "openai" and self.openrouter_api_key:
                fallback_model_name = self._openrouter_model_name(model, model_name)
                _logger.warning("Primary OpenAI multimodal request failed; retrying via OpenRouter")
                system_prompt, user_prompt = self.flatten_input_items_for_text(input_items)
                return await self._chat_completions_text(
                    provider="openrouter",
                    api_key=self.openrouter_api_key,
                    model_name=fallback_model_name,
                    model_alias=model,
                    system_prompt=system_prompt or "You are a helpful assistant.",
                    user_prompt=user_prompt or "Respond to the user's latest request.",
                    temperature=0.3,
                    max_tokens=max_tokens,
                    response_format=None,
                    reasoning_effort=effort,
                    messages=self.chat_messages_from_input(input_items),  # with the images
                )
            raise

    # ---- OpenAI Batch API (static surveys only) ------------------------------------

    @property
    def batch_available(self) -> bool:
        return bool(self.openai_api_key)

    def build_chat_request_body(
        self,
        *,
        model: str,
        messages: list[dict[str, Any]],
        temperature: float = 0.3,
        max_tokens: int = 2500,
        response_format: dict[str, Any] | None = None,
        reasoning_effort: str | None = None,
    ) -> dict[str, Any]:
        """One /v1/chat/completions body for a batch line (the website's shape);
        ``reasoning_effort`` replaces the preset's own."""
        spec = self._model_spec(model)
        model_name = str(spec.get("model_name", model))
        default_effort = reasoning_effort or self._default_reasoning_effort_for_model(model)
        body: dict[str, Any] = {"model": model_name, "messages": messages}
        if self._is_gpt5_model(model_name):
            normalized_effort = self._normalize_reasoning_effort(model_name, default_effort)
            # room for the reasoning too, as a real-time call has
            body["max_completion_tokens"] = max_tokens + self._reasoning_token_allowance(model_name, normalized_effort)
            if normalized_effort:
                body["reasoning_effort"] = normalized_effort
        else:
            body["max_completion_tokens"] = max_tokens  # (max_tokens is deprecated on OpenAI's endpoint)
            body["temperature"] = temperature
        if response_format is not None:
            body["response_format"] = response_format
        return body

    async def submit_chat_batch(self, items: list[dict[str, Any]], *, metadata: dict[str, str] | None = None) -> dict[str, Any]:
        if not self.batch_available:
            raise RuntimeError("OpenAI Batch API requires an OpenAI API key")
        if not items:
            return {"batch_id": None, "input_file_id": None, "item_count": 0}
        auth = {"Authorization": f"Bearer {self.openai_api_key}"}
        client = self._get_client()
        jsonl = "\n".join(
            json.dumps({"custom_id": str(item["custom_id"]), "method": "POST", "url": "/v1/chat/completions", "body": item["body"]})
            for item in items
        ).encode("utf-8")
        upload = await client.post(
            f"{self.openai_base_url}/files",
            headers=auth,
            files={"file": ("batch_input.jsonl", jsonl, "application/jsonl")},
            data={"purpose": "batch"},
            timeout=180.0,
        )
        upload.raise_for_status()
        input_file_id = upload.json()["id"]
        create_body: dict[str, Any] = {
            "input_file_id": input_file_id,
            "endpoint": "/v1/chat/completions",
            "completion_window": "24h",
        }
        if metadata:
            create_body["metadata"] = {str(k): str(v) for k, v in metadata.items()}
        created = await client.post(
            f"{self.openai_base_url}/batches",
            headers={**auth, "Content-Type": "application/json"},
            json=create_body,
            timeout=60.0,
        )
        created.raise_for_status()
        batch = created.json()
        return {"batch_id": batch["id"], "input_file_id": input_file_id, "item_count": len(items)}

    async def poll_chat_batch_once(self, batch_id: str) -> dict[str, Any]:
        auth = {"Authorization": f"Bearer {self.openai_api_key}"}
        polled = await self._get_client().get(f"{self.openai_base_url}/batches/{batch_id}", headers=auth, timeout=60.0)
        polled.raise_for_status()
        return polled.json()

    async def cancel_chat_batch(self, batch_id: str) -> bool:
        if not batch_id:
            return False
        try:
            auth = {"Authorization": f"Bearer {self.openai_api_key}"}
            resp = await self._get_client().post(f"{self.openai_base_url}/batches/{batch_id}/cancel", headers=auth, timeout=60.0)
            return resp.status_code < 400
        except Exception:
            return False

    async def collect_chat_batch_results(self, batch_id: str, *, model: str, item_count: int, twin_ids: list[str] | None = None) -> dict[str, str]:
        """A finished batch's usable lines, ``{custom_id: text}``.

        A truncated, empty or unparseable line is left out, so that twin falls
        back to one real-time call, as on the website. Every line's token usage
        is reported, credited to ``twin_ids[custom_id]`` when ``twin_ids`` is given.
        """
        if not batch_id:
            return {}
        auth = {"Authorization": f"Bearer {self.openai_api_key}"}
        batch = await self.poll_chat_batch_once(batch_id)
        output_file_id = batch.get("output_file_id")
        if not output_file_id:
            return {}
        content = await self._get_client().get(f"{self.openai_base_url}/files/{output_file_id}/content", headers=auth, timeout=180.0)
        content.raise_for_status()
        resolved_model = self.resolved_model_name(model)
        results: dict[str, str] = {}
        for line in content.text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except Exception:
                continue
            custom_id = str(obj.get("custom_id", ""))
            body = (obj.get("response") or {}).get("body") or {}
            choices = body.get("choices") or []
            text = (choices[0].get("message", {}) or {}).get("content") if choices else None
            finish_reason = str((choices[0].get("finish_reason") if choices else "") or "").strip()
            usable = False
            if custom_id and text and finish_reason != "length":
                try:
                    json.loads(extract_json_string(text))
                    usable = True
                except Exception:
                    usable = False
            if usable:
                results[custom_id] = text
            if custom_id and isinstance(body, dict) and isinstance(body.get("usage"), dict):
                self._record_usage(
                    provider="openai",
                    endpoint="chat_completions",
                    model_alias=model,
                    resolved_model=resolved_model,
                    response=None,
                    payload=body,
                    status="completed",
                    latency_ms=0,
                    batch=True,
                    twin_id=_twin_for(custom_id, twin_ids),
                )
        return results

    async def wait_for_batch(self, batch_id: str, *, poll_interval_s: float = 15.0, max_wait_s: float = 86400.0) -> dict[str, Any]:
        started = time.monotonic()
        while True:
            batch = await self.poll_chat_batch_once(batch_id)
            status = batch.get("status")
            if status in self._BATCH_TERMINAL_STATUSES:
                return batch
            if time.monotonic() - started > max_wait_s:
                raise RuntimeError(f"OpenAI batch {batch_id} did not finish within {max_wait_s:.0f}s")
            await asyncio.sleep(poll_interval_s)
