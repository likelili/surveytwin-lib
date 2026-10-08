"""Model objects: the named presets and models the caller defines.

A preset goes through the website's OpenAI/OpenRouter client unchanged. A
defined model is one of:

* ``Model.openai_compatible(...)``: any endpoint that speaks OpenAI's chat format.
* ``Model.http(...)``: an address plus one of four request layouts
  (``openai-chat``, ``openai-responses``, ``anthropic``, ``gemini``). Each layout
  knows where its API puts the reply text, token usage and stop reason; key
  paths override them when a gateway reshapes the reply.
* ``Model.from_function(fn)``: a Python function that makes the call.
"""

from __future__ import annotations

import asyncio
import base64
import copy
import inspect
import json
import os
import re
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

import httpx

from ..errors import ConfigError, ProviderError
from .presets import MODEL_SPECS
from .usage import UsageRecord

LAYOUTS = ("openai-chat", "openai-responses", "anthropic", "gemini")


class env:  # noqa: N801 - reads like a function at the call site: st.env("NAME")
    """A value read from an environment variable when a call is made.

    Keys given this way never appear in recorded settings, files or results.
    """

    def __init__(self, name: str) -> None:
        self.name = str(name)

    def resolve(self) -> str:
        value = os.environ.get(self.name, "").strip()
        if not value:
            raise ConfigError(f"Environment variable {self.name} is not set")
        return value

    def __repr__(self) -> str:
        return f"env({self.name!r})"


def _resolve_secrets(value: Any) -> Any:
    if isinstance(value, env):
        return value.resolve()
    if isinstance(value, dict):
        return {k: _resolve_secrets(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_resolve_secrets(v) for v in value]
    return value


def _unset_env(value: Any) -> list[str]:
    """The environment variables of the ``env(...)`` placeholders in ``value``
    that are not set."""
    if isinstance(value, env):
        return [] if os.environ.get(value.name, "").strip() else [value.name]
    if isinstance(value, dict):
        return [name for v in value.values() for name in _unset_env(v)]
    if isinstance(value, list):
        return [name for v in value for name in _unset_env(v)]
    return []


def _redact(value: Any) -> Any:
    if isinstance(value, env):
        return f"<env {value.name}>"
    if isinstance(value, dict):
        return {k: _redact(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_redact(v) for v in value]
    return value


@dataclass
class ModelRequest:
    """What a defined model is asked. ``content`` keeps text and images in order."""

    system: str
    user: str
    content: list[dict[str, str]]
    images: list[str]
    max_output_tokens: int
    json_mode: bool
    temperature: float = 0.3


@dataclass
class Reply:
    """What a function model returns (a plain string is accepted too)."""

    text: str
    input_tokens: int | None = None
    output_tokens: int | None = None
    cut_off: bool = False
    request_id: str = ""
    raw: Any = None


@dataclass
class ModelCheck:
    ok: bool
    text_found: bool
    usage_found: bool
    stop_reason_found: bool
    latency_s: float
    response: Any = None
    error: str = ""


def get_path(data: Any, path: str) -> Any:
    """Follow a dotted key path such as ``content.0.text``; None when missing."""
    current = data
    for part in [p for p in str(path).split(".") if p != ""]:
        if isinstance(current, dict):
            current = current.get(part)
        elif isinstance(current, list):
            try:
                current = current[int(part)]
            except (ValueError, IndexError):
                return None
        else:
            return None
        if current is None:
            return None
    return current


def _merge_body(base: dict[str, Any], extra: dict[str, Any] | None) -> dict[str, Any]:
    merged = copy.deepcopy(base)
    for key, value in (extra or {}).items():
        if value is None:
            merged.pop(key, None)
        elif isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _merge_body(merged[key], value)
        else:
            merged[key] = copy.deepcopy(value)
    return merged


_DATA_URI_RE = re.compile(r"^data:(?P<mime>[\w.+/-]+);base64,(?P<data>.+)$", re.DOTALL)


async def _image_bytes(url: str, client: httpx.AsyncClient) -> tuple[str, str]:
    """(mime type, base64 data) for an image URL or data URI."""
    match = _DATA_URI_RE.match(url)
    if match:
        return match.group("mime"), match.group("data")
    response = await client.get(url, timeout=60.0, follow_redirects=True)
    if response.status_code >= 400:
        raise RuntimeError(f"image download failed with HTTP {response.status_code}: {url}")
    mime = response.headers.get("content-type", "image/jpeg").split(";")[0].strip() or "image/jpeg"
    return mime, base64.b64encode(response.content).decode("ascii")


async def build_layout_body(layout: str, request: ModelRequest, client: httpx.AsyncClient) -> dict[str, Any]:
    """The request body a layout sends, before the caller's ``body`` fields."""
    if layout == "openai-chat":
        if request.images:
            user_content: Any = [
                {"type": "text", "text": part["text"]}
                if part["type"] == "text"
                else {"type": "image_url", "image_url": {"url": part["url"]}}
                for part in request.content
            ]
        else:
            user_content = request.user
        body: dict[str, Any] = {
            "messages": [
                {"role": "system", "content": request.system},
                {"role": "user", "content": user_content},
            ],
            "max_tokens": request.max_output_tokens,
            "temperature": request.temperature,
        }
        if request.json_mode:
            body["response_format"] = {"type": "json_object"}
        return body
    if layout == "openai-responses":
        body = {
            "input": [
                {"role": "system", "content": [{"type": "input_text", "text": request.system}]},
                {
                    "role": "user",
                    "content": [
                        {"type": "input_text", "text": part["text"]}
                        if part["type"] == "text"
                        else {"type": "input_image", "image_url": part["url"]}
                        for part in request.content
                    ],
                },
            ],
            "max_output_tokens": request.max_output_tokens,
        }
        if request.json_mode:
            body["text"] = {"format": {"type": "json_object"}}
        return body
    if layout == "anthropic":
        content: list[dict[str, Any]] = []
        for part in request.content:
            if part["type"] == "text":
                content.append({"type": "text", "text": part["text"]})
                continue
            match = _DATA_URI_RE.match(part["url"])
            if match:
                content.append(
                    {"type": "image", "source": {"type": "base64", "media_type": match.group("mime"), "data": match.group("data")}}
                )
            else:
                content.append({"type": "image", "source": {"type": "url", "url": part["url"]}})
        return {
            "system": request.system,
            "messages": [{"role": "user", "content": content}],
            "max_tokens": request.max_output_tokens,
            "temperature": request.temperature,
        }
    if layout == "gemini":
        parts: list[dict[str, Any]] = []
        for part in request.content:
            if part["type"] == "text":
                parts.append({"text": part["text"]})
            else:
                mime, data = await _image_bytes(part["url"], client)
                parts.append({"inlineData": {"mimeType": mime, "data": data}})
        generation: dict[str, Any] = {
            "maxOutputTokens": request.max_output_tokens,
            "temperature": request.temperature,
        }
        if request.json_mode:
            generation["responseMimeType"] = "application/json"
        return {
            "systemInstruction": {"parts": [{"text": request.system}]},
            "contents": [{"role": "user", "parts": parts}],
            "generationConfig": generation,
        }
    raise ConfigError(f"Unknown request layout {layout!r}; use one of {', '.join(LAYOUTS)}")


def _layout_reply(layout: str, data: dict[str, Any]) -> tuple[str, int | None, int | None, str | None, bool]:
    """(text, input tokens, output tokens, stop reason, cut off) for a layout's response."""
    if layout == "openai-chat":
        choice = (data.get("choices") or [{}])[0] if isinstance(data.get("choices"), list) else {}
        message = choice.get("message") or {}
        raw = message.get("content")
        if isinstance(raw, list):
            text = "".join(str(p.get("text", "")) for p in raw if isinstance(p, dict))
        else:
            text = str(raw or "")
        usage = data.get("usage") or {}
        stop = choice.get("finish_reason")
        return text, usage.get("prompt_tokens"), usage.get("completion_tokens"), stop, stop == "length"
    if layout == "openai-responses":
        from .openai_client import extract_responses_text

        usage = data.get("usage") or {}
        details = data.get("incomplete_details") if isinstance(data.get("incomplete_details"), dict) else {}
        stop = data.get("status")
        cut = stop == "incomplete" and details.get("reason") == "max_output_tokens"
        return extract_responses_text(data), usage.get("input_tokens"), usage.get("output_tokens"), stop, cut
    if layout == "anthropic":
        blocks = data.get("content") if isinstance(data.get("content"), list) else []
        text = "".join(str(b.get("text", "")) for b in blocks if isinstance(b, dict) and b.get("type") == "text")
        usage = data.get("usage") or {}
        stop = data.get("stop_reason")
        return text, usage.get("input_tokens"), usage.get("output_tokens"), stop, stop == "max_tokens"
    if layout == "gemini":
        candidates = data.get("candidates") if isinstance(data.get("candidates"), list) else []
        first = candidates[0] if candidates and isinstance(candidates[0], dict) else {}
        parts = ((first.get("content") or {}).get("parts")) or []
        text = "".join(str(p.get("text", "")) for p in parts if isinstance(p, dict) and not p.get("thought"))
        usage = data.get("usageMetadata") or {}
        output = usage.get("candidatesTokenCount")
        if output is not None and usage.get("thoughtsTokenCount"):
            output = int(output) + int(usage.get("thoughtsTokenCount") or 0)
        stop = first.get("finishReason")
        return text, usage.get("promptTokenCount"), output, stop, stop == "MAX_TOKENS"
    raise ConfigError(f"Unknown request layout {layout!r}")


@dataclass
class Model:
    """A model a run can call. Build one with the class methods, not directly."""

    name: str
    kind: str
    layout: str | None = None
    images: bool = True
    url: str | None = None
    headers: dict[str, Any] = field(default_factory=dict)
    body: dict[str, Any] = field(default_factory=dict)
    text_key: str | Callable[[Any], str] | None = None
    usage_keys: dict[str, str] | None = None
    stop_key: str | None = None
    cut_off_values: list[str] | None = None
    function: Callable[[ModelRequest], Any] | None = None
    provider: str = "openai"
    timeout_s: float = 180.0
    # Where the model was defined, when the command line loaded it from a file:
    # "FILE.py:NAME (sha256 <first 12>)". The file name only, never its folder.
    source: str = ""

    # ---- constructors ---------------------------------------------------------------

    @classmethod
    def preset(cls, name: str, *, provider: str = "openai") -> "Model":
        """A named preset from the website, or any OpenAI model name."""
        provider = str(provider or "openai").strip().lower()
        if provider not in {"openai", "openrouter"}:
            raise ConfigError("provider must be 'openai' or 'openrouter' for a preset model")
        return cls(name=str(name), kind="preset", layout=None, images=True, provider=provider)

    @classmethod
    def http(
        cls,
        name: str,
        url: str,
        *,
        headers: dict[str, Any] | None = None,
        layout: str,
        body: dict[str, Any] | None = None,
        text_key: str | Callable[[Any], str] | None = None,
        usage_keys: dict[str, str] | None = None,
        stop_key: str | None = None,
        cut_off_values: list[str] | None = None,
        images: bool = True,
        timeout_s: float = 180.0,
    ) -> "Model":
        if layout not in LAYOUTS:
            raise ConfigError(f"layout must be one of {', '.join(LAYOUTS)}; got {layout!r}")
        if not str(url or "").startswith(("http://", "https://")):
            raise ConfigError(f"url must be an http(s) address; got {url!r}")
        if stop_key and not cut_off_values:
            raise ConfigError("stop_key needs cut_off_values: the stop reasons that mean the reply was cut off")
        return cls(
            name=str(name),
            kind="http",
            layout=layout,
            images=bool(images),
            url=str(url),
            headers=dict(headers or {}),
            body=dict(body or {}),
            text_key=text_key,
            usage_keys=dict(usage_keys) if usage_keys else None,
            stop_key=stop_key,
            cut_off_values=[str(v) for v in (cut_off_values or [])] or None,
            timeout_s=float(timeout_s),
        )

    @classmethod
    def openai_compatible(
        cls,
        name: str,
        base_url: str,
        *,
        api_key_env: str | None = None,
        model: str | None = None,
        body: dict[str, Any] | None = None,
        images: bool = True,
    ) -> "Model":
        """An endpoint that accepts OpenAI's chat format (local servers, many hosts)."""
        headers: dict[str, Any] = {}
        if api_key_env:
            headers["Authorization"] = _BearerEnv(api_key_env)
        merged = {"model": model or name, **(body or {})}
        instance = cls.http(
            name,
            str(base_url).rstrip("/") + "/chat/completions",
            headers=headers,
            layout="openai-chat",
            body=merged,
            images=images,
        )
        instance.kind = "openai-compatible"
        return instance

    @classmethod
    def from_function(
        cls,
        fn: Callable[[ModelRequest], Any],
        *,
        name: str,
        images: bool = False,
    ) -> "Model":
        if not callable(fn):
            raise ConfigError("from_function needs a callable")
        return cls(name=str(name), kind="function", layout=None, images=bool(images), function=fn)

    # ---- description --------------------------------------------------------------

    @property
    def settings(self) -> dict[str, Any]:
        """What the run records about this model, without any key."""
        out: dict[str, Any] = {"name": self.name, "kind": self.kind}
        if self.source:
            out["defined_in"] = self.source
        if self.kind == "preset":
            spec = MODEL_SPECS.get(self.name, {})
            out.update(
                {
                    "provider": self.provider,
                    "api_model": spec.get("model_name", self.name),
                    "reasoning_effort": spec.get("default_reasoning_effort"),
                }
            )
            return out
        if self.layout:
            out["layout"] = self.layout
        if self.url:
            out["url"] = self.url.split("?", 1)[0]
        if self.headers:
            out["headers"] = {k: "<hidden>" for k in self.headers}
        if self.body:
            out["body"] = _redact(self.body)
        if self.text_key is not None:
            out["text_key"] = self.text_key if isinstance(self.text_key, str) else "<function>"
        if self.usage_keys:
            out["usage_keys"] = dict(self.usage_keys)
        if self.stop_key:
            out["stop_key"] = self.stop_key
            out["cut_off_values"] = list(self.cut_off_values or [])
        out["images"] = self.images
        return out

    # ---- calling a defined model -------------------------------------------------------

    async def acall(self, request: ModelRequest, *, client: httpx.AsyncClient) -> tuple[Reply, dict[str, Any]]:
        """Call a defined model once. Returns the reply and a usage summary."""
        if self.kind == "function":
            assert self.function is not None
            started = time.monotonic()
            result = self.function(request)
            if inspect.isawaitable(result):
                result = await result  # type: ignore[assignment]
            latency_ms = int((time.monotonic() - started) * 1000)
            reply = result if isinstance(result, Reply) else Reply(text=str(result or ""))
            return reply, {"latency_ms": latency_ms, "status": "completed"}
        if self.kind not in {"http", "openai-compatible"} or not self.layout or not self.url:
            raise ConfigError(f"Model {self.name!r} cannot be called directly")
        body = _merge_body(await build_layout_body(self.layout, request, client), _resolve_secrets(self.body))
        headers = {"Content-Type": "application/json", **{k: str(v) for k, v in _resolve_secrets(self.headers).items()}}
        started = time.monotonic()
        response = await client.post(self.url, headers=headers, json=body, timeout=self.timeout_s)
        latency_ms = int((time.monotonic() - started) * 1000)
        if response.status_code >= 400:
            raise RuntimeError(f"{self.name} API error {response.status_code}: {response.text[:1000]}")
        try:
            data = response.json()
        except ValueError as exc:
            raise RuntimeError(f"{self.name} API error 502: the reply was not JSON") from exc
        text, in_tokens, out_tokens, stop, cut = _layout_reply(self.layout, data)
        if self.text_key is not None:
            text = self.text_key(data) if callable(self.text_key) else get_path(data, self.text_key)
            text = "" if text is None else str(text)
        if self.usage_keys:
            in_tokens = get_path(data, self.usage_keys.get("input_tokens", "")) if self.usage_keys.get("input_tokens") else None
            out_tokens = get_path(data, self.usage_keys.get("output_tokens", "")) if self.usage_keys.get("output_tokens") else None
        if self.stop_key:
            stop = get_path(data, self.stop_key)
            cut = stop is not None and str(stop) in set(self.cut_off_values or [])
        request_id = response.headers.get("request-id") or response.headers.get("x-request-id") or str(data.get("id", "") or "")
        reply = Reply(
            text=text,
            input_tokens=_to_int(in_tokens),
            output_tokens=_to_int(out_tokens),
            cut_off=bool(cut),
            request_id=request_id,
            raw=data,
        )
        return reply, {"latency_ms": latency_ms, "status": "completed", "stop_reason": stop}

    # ---- checking a model ---------------------------------------------------------

    async def acheck(self, *, api_key: str | None = None) -> ModelCheck:
        request = ModelRequest(
            system="You are a connectivity check. Reply with JSON only.",
            user='Return exactly this JSON object: {"ok": true}',
            content=[{"type": "text", "text": 'Return exactly this JSON object: {"ok": true}'}],
            images=[],
            max_output_tokens=64,
            json_mode=True,
        )
        started = time.monotonic()
        if self.kind == "preset":
            from .openai_client import OpenAIClient

            records: list[UsageRecord] = []
            client = OpenAIClient(
                openai_api_key=(api_key or os.environ.get("OPENAI_API_KEY", "")) if self.provider == "openai" else "",
                openrouter_api_key=(api_key or os.environ.get("OPENROUTER_API_KEY", "")) if self.provider == "openrouter" else "",
                on_usage=records.append,
            )
            try:
                text = await client.chat_text(self.name, request.system, request.user, max_tokens=512, response_format={"type": "json_object"})
                return ModelCheck(
                    ok=bool(text.strip()),
                    text_found=bool(text.strip()),
                    usage_found=any(r.total_tokens for r in records),
                    stop_reason_found=True,
                    latency_s=round(time.monotonic() - started, 3),
                    response=text,
                )
            except Exception as exc:  # noqa: BLE001
                raise ProviderError(f"Model check failed for {self.name}: {exc}") from exc
            finally:
                await client.close()
        async with httpx.AsyncClient() as http:
            try:
                reply, meta = await self.acall(request, client=http)
            except Exception as exc:  # noqa: BLE001
                raise ProviderError(f"Model check failed for {self.name}: {exc}") from exc
        text_found = bool(reply.text.strip())
        return ModelCheck(
            ok=text_found,
            text_found=text_found,
            usage_found=reply.input_tokens is not None and reply.output_tokens is not None,
            stop_reason_found=self.kind == "function" or meta.get("stop_reason") is not None,
            latency_s=round(time.monotonic() - started, 3),
            response=reply.raw if reply.raw is not None else reply.text,
        )

    def missing_env(self) -> list[str]:
        """The environment variables this model's ``env(...)`` placeholders need
        that are not set now (a defined model's keys); empty when every one is
        set, and for presets and function models."""
        return list(dict.fromkeys(_unset_env(self.headers) + _unset_env(self.body)))

    def check(self, *, api_key: str | None = None) -> ModelCheck:
        """One tiny call: was the reply text found, and were usage and stop reason?"""
        return asyncio.run(self.acheck(api_key=api_key))


class _BearerEnv(env):
    """``Authorization: Bearer <value of the variable>``."""

    def resolve(self) -> str:
        return f"Bearer {super().resolve()}"


def _to_int(value: Any) -> int | None:
    try:
        return None if value is None else int(value)
    except (TypeError, ValueError):
        return None


def model_from_config(model: "Model | str", provider: str = "openai") -> Model:
    if isinstance(model, Model):
        return model
    if not isinstance(model, str) or not model.strip():
        raise ConfigError("model must be a preset name or a Model")
    return Model.preset(model.strip(), provider=provider)


FunctionModelCallable = Callable[[ModelRequest], "str | Reply | Awaitable[str | Reply]"]
