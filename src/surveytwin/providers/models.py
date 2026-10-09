"""Model objects: every model a run can call, each with the code that calls it.

Built in (the request and reply formats of the main APIs are written here):

* ``Model.openai(...)`` and ``Model.openrouter(...)``: OpenAI models
  (``Model.preset(name)`` and ``model="gpt-5.4-low"`` are the same thing by name).
* ``Model.anthropic(...)``: Claude, through Anthropic's Messages API.
* ``Model.gemini(...)``: Gemini, through Google's generateContent API.
* ``Model.deepseek(...)``: DeepSeek, through its OpenAI-compatible API.

Defined by the caller:

* ``Model.openai_compatible(...)``: any endpoint that speaks OpenAI's chat format.
* ``Model.http(...)``: an address plus one of four request layouts
  (``openai-chat``, ``openai-responses``, ``anthropic``, ``gemini``). Each layout
  knows where its API puts the reply text, token usage and stop reason; key
  paths override them when a gateway reshapes the reply.
* ``Model.from_function(fn)``: a Python function that makes the call.

Every model takes a ``ModelRequest`` and gives back a ``Reply`` (``acall``,
``ask``); a key is read from the environment when a call is made, never stored.
"""

from __future__ import annotations

import asyncio
import base64
import copy
import dataclasses
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


# Body fields whose value is a secret by its name: never recorded, even when
# written as plain text (use env("NAME") instead). "token" names a secret only
# at the end of a name, in the singular (token, access_token, authToken): a
# field counting tokens (max_tokens, maxOutputTokens, budget_tokens) is not one.
_SECRET_NAME = re.compile(
    r"(api[_-]?key|apikey|secret|password|passwd|passphrase|credential|authorization|private[_-]?key|access[_-]?key|cookie)",
    re.IGNORECASE,
)
_SECRET_WORD = re.compile(r"(token|^auth)$", re.IGNORECASE)
_ENV_MARK = re.compile(r"^<env (?P<name>[^<>]+)>$")
HIDDEN = "<hidden>"


def _is_secret_name(key: str) -> bool:
    name = str(key or "").strip()
    return bool(name) and bool(_SECRET_NAME.search(name) or _SECRET_WORD.search(name))


def _redact(value: Any, key: str = "", *, secret: bool = False) -> Any:
    """``value`` as settings record it: an ``env(...)`` placeholder as its name,
    and every value inside a field named like a secret hidden, however deep
    (a nested object or list under such a name is hidden whole)."""
    if isinstance(value, env):
        return f"<env {value.name}>"
    secret = secret or _is_secret_name(key)
    if isinstance(value, dict):
        return {k: _redact(v, str(k), secret=secret) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_redact(v, key, secret=secret) for v in value]
    if secret and value is not None and not isinstance(value, bool):
        return HIDDEN
    return value


def _restore_env(value: Any) -> Any:
    """Recorded settings made usable again: ``"<env NAME>"`` becomes ``env("NAME")``.
    Raises ``ValueError`` at a hidden value, which only the model's definition has."""
    if isinstance(value, str):
        if value == HIDDEN:
            raise ValueError("a hidden value")
        match = _ENV_MARK.match(value)
        return env(match.group("name")) if match else value
    if isinstance(value, dict):
        return {k: _restore_env(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_restore_env(v) for v in value]
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
    # Of the input, the tokens read from the provider's cache; of the output, the
    # tokens spent thinking (both counted in the totals above; None when not known).
    cached_input_tokens: int | None = None
    reasoning_tokens: int | None = None


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
        # (worded as the providers word it, so a run leaves the picture out: see answers/executor.py)
        raise RuntimeError(f"failed to download image (HTTP {response.status_code}): {url}")
    mime = response.headers.get("content-type", "image/jpeg").split(";")[0].strip() or "image/jpeg"
    return mime, base64.b64encode(response.content).decode("ascii")


async def build_layout_body(layout: str, request: ModelRequest, client: httpx.AsyncClient) -> dict[str, Any]:
    """The request body a layout sends, before the caller's ``body`` fields. An
    empty system prompt is left out. The ``anthropic`` and ``gemini`` layouts send
    no temperature (current Claude models refuse one, and Gemini 3 models are
    meant to keep their default): a model's ``body`` adds it."""
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
        messages: list[dict[str, Any]] = [{"role": "system", "content": request.system}] if request.system else []
        messages.append({"role": "user", "content": user_content})
        body: dict[str, Any] = {
            "messages": messages,
            "max_tokens": request.max_output_tokens,
            "temperature": request.temperature,
        }
        if request.json_mode:
            body["response_format"] = {"type": "json_object"}
        return body
    if layout == "openai-responses":
        items: list[dict[str, Any]] = (
            [{"role": "system", "content": [{"type": "input_text", "text": request.system}]}] if request.system else []
        )
        items.append(
            {
                "role": "user",
                "content": [
                    {"type": "input_text", "text": part["text"]}
                    if part["type"] == "text"
                    else {"type": "input_image", "image_url": part["url"]}
                    for part in request.content
                ],
            }
        )
        body = {"input": items, "max_output_tokens": request.max_output_tokens}
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
        body = {"messages": [{"role": "user", "content": content}], "max_tokens": request.max_output_tokens}
        if request.system:
            body = {"system": request.system, **body}
        return body
    if layout == "gemini":
        parts: list[dict[str, Any]] = []
        for part in request.content:
            if part["type"] == "text":
                parts.append({"text": part["text"]})
            else:
                mime, data = await _image_bytes(part["url"], client)
                parts.append({"inlineData": {"mimeType": mime, "data": data}})
        generation: dict[str, Any] = {"maxOutputTokens": request.max_output_tokens}
        if request.json_mode:
            generation["responseMimeType"] = "application/json"
        body = {"contents": [{"role": "user", "parts": parts}], "generationConfig": generation}
        if request.system:
            body = {"systemInstruction": {"parts": [{"text": request.system}]}, **body}
        return body
    raise ConfigError(f"Unknown request layout {layout!r}; use one of {', '.join(LAYOUTS)}")


@dataclass
class _Parsed:
    """What a layout reads from a provider's reply."""

    text: str
    input_tokens: int | None = None
    output_tokens: int | None = None
    cached_input_tokens: int | None = None
    reasoning_tokens: int | None = None
    stop: str | None = None
    cut: bool = False
    # Why the provider gave no answer to use ("" when it gave one): a refusal, a
    # safety stop, a blocked prompt, no capacity left.
    stopped: str = ""


def _sum(*values: Any) -> int | None:
    numbers = [_to_int(v) for v in values]
    known = [n for n in numbers if n is not None]
    return sum(known) if known else None


# Stop reasons that mean the provider gave no answer to use, by layout.
_STOPPED_GEMINI = {
    "SAFETY", "RECITATION", "LANGUAGE", "BLOCKLIST", "PROHIBITED_CONTENT", "SPII", "IMAGE_SAFETY",
    "IMAGE_PROHIBITED_CONTENT", "IMAGE_RECITATION", "IMAGE_OTHER", "MALFORMED_RESPONSE",
}
_STOPPED_CHAT = {
    "content_filter": "the reply was filtered (content_filter)",
    "insufficient_system_resource": "the provider ran out of capacity (insufficient_system_resource)",
    "aborted": "the provider stopped the reply (aborted)",
}


def _layout_reply(layout: str, data: dict[str, Any]) -> _Parsed:
    """The reply text, token usage and stop reason, where each API puts them."""
    if layout == "openai-chat":
        choice = (data.get("choices") or [{}])[0] if isinstance(data.get("choices"), list) else {}
        message = choice.get("message") or {}
        raw = message.get("content")
        if isinstance(raw, list):
            text = "".join(str(p.get("text", "")) for p in raw if isinstance(p, dict))
        else:
            text = str(raw or "")
        usage = data.get("usage") or {}
        prompt = usage.get("prompt_tokens_details") if isinstance(usage.get("prompt_tokens_details"), dict) else {}
        completion = usage.get("completion_tokens_details") if isinstance(usage.get("completion_tokens_details"), dict) else {}
        stop = choice.get("finish_reason")
        stopped = _STOPPED_CHAT.get(str(stop), "")
        if message.get("refusal"):
            stopped = f"refusal: {str(message['refusal'])[:300]}"
        return _Parsed(
            text, _to_int(usage.get("prompt_tokens")), _to_int(usage.get("completion_tokens")),
            _to_int(prompt.get("cached_tokens", usage.get("prompt_cache_hit_tokens"))),  # (DeepSeek also names it prompt_cache_hit_tokens)
            _to_int(completion.get("reasoning_tokens")), stop, stop == "length", stopped,
        )
    if layout == "openai-responses":
        from .openai_client import extract_responses_text

        usage = data.get("usage") or {}
        details = data.get("incomplete_details") if isinstance(data.get("incomplete_details"), dict) else {}
        input_details = usage.get("input_tokens_details") if isinstance(usage.get("input_tokens_details"), dict) else {}
        output_details = usage.get("output_tokens_details") if isinstance(usage.get("output_tokens_details"), dict) else {}
        stop = data.get("status")
        stopped = ""
        if stop == "incomplete" and details.get("reason") == "content_filter":
            stopped = "the reply was filtered (content_filter)"
        refusals = [
            str(part.get("refusal", ""))
            for item in data.get("output") or [] if isinstance(item, dict)
            for part in item.get("content") or [] if isinstance(part, dict) and part.get("type") == "refusal"
        ]
        if refusals:
            stopped = f"refusal: {refusals[0][:300]}"
        return _Parsed(
            extract_responses_text(data), _to_int(usage.get("input_tokens")), _to_int(usage.get("output_tokens")),
            _to_int(input_details.get("cached_tokens")), _to_int(output_details.get("reasoning_tokens")),
            stop, stop == "incomplete" and details.get("reason") == "max_output_tokens", stopped,
        )
    if layout == "anthropic":
        blocks = data.get("content") if isinstance(data.get("content"), list) else []
        text = "".join(str(b.get("text", "")) for b in blocks if isinstance(b, dict) and b.get("type") == "text")
        usage = data.get("usage") or {}
        output_details = usage.get("output_tokens_details") if isinstance(usage.get("output_tokens_details"), dict) else {}
        stop = data.get("stop_reason")
        stopped = ""
        if stop == "refusal":
            details = data.get("stop_details") if isinstance(data.get("stop_details"), dict) else {}
            why = "; ".join(str(details[k]) for k in ("category", "explanation") if details.get(k))
            stopped = f"refusal ({why})" if why else "refusal"
        elif stop == "model_context_window_exceeded":
            stopped = "the request and its answer do not fit the model's context window (model_context_window_exceeded)"
        # The input is the uncached tokens plus those written to and read from the cache.
        return _Parsed(
            text, _sum(usage.get("input_tokens"), usage.get("cache_creation_input_tokens"), usage.get("cache_read_input_tokens")),
            _to_int(usage.get("output_tokens")), _to_int(usage.get("cache_read_input_tokens")),
            _to_int(output_details.get("thinking_tokens")), stop, stop == "max_tokens", stopped,
        )
    if layout == "gemini":
        candidates = data.get("candidates") if isinstance(data.get("candidates"), list) else []
        first = candidates[0] if candidates and isinstance(candidates[0], dict) else {}
        parts = ((first.get("content") or {}).get("parts")) or []
        text = "".join(str(p.get("text", "")) for p in parts if isinstance(p, dict) and not p.get("thought"))
        usage = data.get("usageMetadata") or {}
        thoughts = _to_int(usage.get("thoughtsTokenCount"))
        output = _sum(usage.get("candidatesTokenCount"), thoughts) if usage.get("candidatesTokenCount") is not None else None
        stop = first.get("finishReason")
        stopped = f"the reply was stopped ({stop})" if stop in _STOPPED_GEMINI else ""
        feedback = data.get("promptFeedback") if isinstance(data.get("promptFeedback"), dict) else {}
        if not candidates and feedback.get("blockReason"):
            stopped = f"the prompt was blocked ({feedback['blockReason']})"
        return _Parsed(
            text, _sum(usage.get("promptTokenCount"), usage.get("toolUsePromptTokenCount")), output,
            _to_int(usage.get("cachedContentTokenCount")), thoughts, stop, stop == "MAX_TOKENS", stopped,
        )
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
    # A built-in model ("openai", "openrouter", "anthropic", "gemini", "deepseek"):
    # its settings are enough to make it again (Model.from_settings).
    builtin: str = ""
    # The environment variable that holds the key, and for a model called over
    # HTTP the header a key given to run(api_key=...) goes in, with its prefix.
    api_key_env: str | None = None
    key_header: str | None = None
    key_prefix: str = ""
    # OpenAI and OpenRouter models: a reasoning effort other than the preset's own,
    # and whether a failed OpenAI call is tried again through OpenRouter.
    reasoning_effort: str | None = None
    fallback_to_openrouter: bool = False
    # Claude, Gemini and DeepSeek models: tokens added to every call's output limit
    # for the model's thinking, which it spends from the same limit as the answer.
    thinking_room: int = 0

    # ---- constructors ---------------------------------------------------------------

    @classmethod
    def preset(cls, name: str, *, provider: str = "openai") -> "Model":
        """A named preset, or any OpenAI model name: the same as
        ``Model.openai(name)`` (or ``Model.openrouter(name)``)."""
        provider = str(provider or "openai").strip().lower()
        if provider not in {"openai", "openrouter"}:
            raise ConfigError("provider must be 'openai' or 'openrouter' for a preset model")
        return cls.openrouter(name) if provider == "openrouter" else cls.openai(name)

    @classmethod
    def openai(
        cls,
        name: str,
        *,
        reasoning_effort: str | None = None,
        api_key_env: str = "OPENAI_API_KEY",
        fallback_to_openrouter: bool = False,
    ) -> "Model":
        """An OpenAI model: a preset name (``gpt-5.4-low`` pins the API model and
        its reasoning effort) or any OpenAI model name, fine-tuned ones included.
        Called through the Responses API for GPT-5 models and for any call with
        images, chat completions in JSON mode otherwise.
        ``reasoning_effort`` replaces the preset's own effort. A failed call is
        tried again through OpenRouter only with ``fallback_to_openrouter=True``
        (and ``OPENROUTER_API_KEY`` set); the run then warns how many calls went
        there."""
        return cls(
            name=str(name), kind="preset", layout=None, images=True, provider="openai", builtin="openai",
            api_key_env=str(api_key_env), reasoning_effort=_effort(reasoning_effort), fallback_to_openrouter=bool(fallback_to_openrouter),
        )

    @classmethod
    def openrouter(cls, name: str, *, reasoning_effort: str | None = None, api_key_env: str = "OPENROUTER_API_KEY") -> "Model":
        """An OpenAI model through OpenRouter (``openai/<model>``), by preset or
        model name, called as ``Model.openai`` would be; a GPT-5 model keeps its
        reasoning effort, and images go with the call."""
        return cls(
            name=str(name), kind="preset", layout=None, images=True, provider="openrouter", builtin="openrouter",
            api_key_env=str(api_key_env), reasoning_effort=_effort(reasoning_effort),
        )

    @classmethod
    def anthropic(
        cls,
        name: str,
        *,
        api_key_env: str = "ANTHROPIC_API_KEY",
        effort: str | None = None,
        temperature: float | None = None,
        body: dict[str, Any] | None = None,
        images: bool = True,
        url: str = "https://api.anthropic.com/v1/messages",
        timeout_s: float = 600.0,
        thinking_room: int | None = None,
    ) -> "Model":
        """A Claude model (such as ``claude-opus-5-5``) through Anthropic's Messages
        API. ``effort`` sets ``output_config.effort`` (low, medium, high, xhigh or
        max). Current Claude models think by default and refuse a ``temperature``
        other than 1.0, so it is sent only when given (for an older model).
        ``thinking_room`` tokens are added to every call's output limit for the
        thinking (by default from the effort: 16,000 at the models' usual
        ``high``). ``body`` adds request fields."""
        extra: dict[str, Any] = {"model": str(name)}
        if effort:
            extra["output_config"] = {"effort": str(effort)}
        if temperature is not None:
            extra["temperature"] = float(temperature)
        model = cls.http(
            name, url, layout="anthropic", images=images, timeout_s=timeout_s,
            headers={"x-api-key": env(api_key_env), "anthropic-version": "2023-06-01"},
            body=_merge_body(extra, body),
        )
        model.builtin, model.provider, model.api_key_env, model.key_header = "anthropic", "anthropic", str(api_key_env), "x-api-key"
        model.thinking_room = _room(thinking_room, effort or ((body or {}).get("output_config") or {}).get("effort"))
        return model

    @classmethod
    def gemini(
        cls,
        name: str,
        *,
        api_key_env: str = "GEMINI_API_KEY",
        temperature: float | None = None,
        thinking_level: str | None = None,
        body: dict[str, Any] | None = None,
        images: bool = True,
        timeout_s: float = 600.0,
        thinking_room: int | None = None,
    ) -> "Model":
        """A Gemini model (such as ``gemini-3.1-pro-preview``) through Google's
        generateContent API; pictures are sent inside the request. No temperature
        is sent unless given: Google asks that Gemini 3 models keep their default.
        ``thinking_level`` sets ``generationConfig.thinkingConfig.thinkingLevel``
        (minimal, low, medium or high); ``thinking_room`` tokens are added to every
        call's output limit for the thinking (by default from the level: 16,000 at
        the default ``high``)."""
        generation: dict[str, Any] = {}
        if temperature is not None:
            generation["temperature"] = float(temperature)
        if thinking_level:
            generation["thinkingConfig"] = {"thinkingLevel": str(thinking_level)}
        merged = _merge_body({"generationConfig": generation} if generation else {}, body)
        model = cls.http(
            name,
            f"https://generativelanguage.googleapis.com/v1beta/models/{name}:generateContent",
            layout="gemini", images=images, timeout_s=timeout_s,
            headers={"x-goog-api-key": env(api_key_env)},
            body=merged,
        )
        model.builtin, model.provider, model.api_key_env, model.key_header = "gemini", "gemini", str(api_key_env), "x-goog-api-key"
        thinking = (merged.get("generationConfig") or {}).get("thinkingConfig") or {}
        budget = _to_int(thinking.get("thinkingBudget"))
        if thinking_room is None and budget is not None and budget >= 0:
            thinking_room = budget  # a fixed thinking budget (Gemini 2.5): that much room, 0 for none
        model.thinking_room = _room(thinking_room, thinking.get("thinkingLevel"))
        return model

    @classmethod
    def deepseek(
        cls,
        name: str = "deepseek-flash",
        *,
        api_key_env: str = "DEEPSEEK_API_KEY",
        base_url: str = "https://api.deepseek.com",
        reasoning_effort: str | None = None,
        body: dict[str, Any] | None = None,
        images: bool | None = None,
        timeout_s: float = 600.0,
        thinking_room: int | None = None,
    ) -> "Model":
        """A DeepSeek model through its OpenAI-compatible API: ``deepseek-flash``
        (the default, which takes pictures) or ``deepseek-v4-pro`` (text only).
        Both think by default; ``reasoning_effort`` is none (no thinking), low,
        high or max, and ``thinking_room`` tokens are added to every call's output
        limit for the thinking (by default from the effort: 16,000 at the default
        ``high``). ``images`` defaults to whether the model is a Flash model."""
        extra: dict[str, Any] = {"reasoning_effort": str(reasoning_effort)} if reasoning_effort else {}
        merged = _merge_body(extra, body)
        if images is None:
            images = "flash" in str(name).lower()
        model = cls.openai_compatible(
            name, base_url, api_key_env=api_key_env, model=name, body=merged or None, images=images, timeout_s=timeout_s
        )
        model.builtin, model.provider = "deepseek", "deepseek"
        effort = merged.get("reasoning_effort")
        if thinking_room is None and ((merged.get("thinking") or {}).get("type") == "disabled" or effort == "none"):
            thinking_room = 0
        model.thinking_room = _room(thinking_room, effort)
        return model

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
        timeout_s: float = 180.0,
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
            timeout_s=timeout_s,
        )
        instance.kind = "openai-compatible"
        if api_key_env:
            instance.api_key_env, instance.key_header, instance.key_prefix = str(api_key_env), "Authorization", "Bearer "
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
                    "reasoning_effort": self.reasoning_effort or spec.get("default_reasoning_effort"),
                    "api_key_env": self.api_key_env,
                }
            )
            if self.reasoning_effort:
                out["reasoning_effort_set"] = True
            if self.fallback_to_openrouter:
                out["fallback_to_openrouter"] = True
            return out
        if self.builtin:
            out["builtin"] = self.builtin
            out["api_key_env"] = self.api_key_env
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
        if self.kind != "function":
            out["timeout_s"] = self.timeout_s
        if self.thinking_room:
            out["thinking_room"] = self.thinking_room
        return out

    # ---- calling a defined model -------------------------------------------------------

    def openai_client(self, *, api_key: str | None = None, on_usage: Callable[[UsageRecord], None] | None = None) -> Any:
        """The OpenAI client an OpenAI or OpenRouter model calls through, with its
        key (``api_key``, else the model's environment variable); OpenRouter's key
        is attached for a fallback only when the model asks for one."""
        from .openai_client import OpenAIClient

        if self.kind != "preset":
            raise ConfigError(f"Model {self.name!r} is not an OpenAI or OpenRouter model")
        key = api_key or os.environ.get(str(self.api_key_env or ""), "")
        if self.provider == "openrouter":
            return OpenAIClient(openrouter_api_key=key, on_usage=on_usage)
        fallback = os.environ.get("OPENROUTER_API_KEY", "") if self.fallback_to_openrouter else ""
        return OpenAIClient(openai_api_key=key, openrouter_api_key=fallback, on_usage=on_usage)

    async def _acall_openai(self, request: ModelRequest, *, api_key: str | None = None) -> tuple[Reply, dict[str, Any]]:
        """One call of an OpenAI or OpenRouter model, as a run makes it."""
        records: list[UsageRecord] = []
        client = self.openai_client(api_key=api_key, on_usage=records.append)
        started = time.monotonic()
        try:
            if request.images:
                parts = [
                    {"type": "input_text", "text": part["text"]} if part["type"] == "text" else {"type": "input_image", "image_url": part["url"]}
                    for part in request.content
                ]
                items = [{"role": "system", "content": [{"type": "input_text", "text": request.system}]}, {"role": "user", "content": parts}]
                text = await client.responses_input_text(
                    model=self.name, input_items=items, max_tokens=request.max_output_tokens, reasoning_effort=self.reasoning_effort
                )
            else:
                text = await client.chat_text(
                    self.name, request.system, request.user, temperature=request.temperature, max_tokens=request.max_output_tokens,
                    response_format={"type": "json_object"} if request.json_mode else None, reasoning_effort=self.reasoning_effort,
                )
        finally:
            await client.close()
        done = [r for r in records if r.status == "completed"]
        reply = Reply(
            text=str(text or ""),
            input_tokens=sum(r.input_tokens for r in done) if done else None,
            output_tokens=sum(r.output_tokens for r in done) if done else None,
            request_id=done[-1].request_id if done else "",
        )
        providers = sorted({r.provider for r in done})
        return reply, {"latency_ms": int((time.monotonic() - started) * 1000), "status": "completed", "providers": providers}

    async def acall(self, request: ModelRequest, *, client: httpx.AsyncClient, api_key: str | None = None) -> tuple[Reply, dict[str, Any]]:
        """Call the model once. Returns the reply and a usage summary. ``api_key``
        replaces the key the model reads from the environment (a built-in model's,
        or one made with ``openai_compatible(api_key_env=...)``)."""
        if self.kind == "preset":
            return await self._acall_openai(request, api_key=api_key)
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
        if self.thinking_room:
            request = dataclasses.replace(request, max_output_tokens=request.max_output_tokens + self.thinking_room)
        body = _merge_body(await build_layout_body(self.layout, request, client), _resolve_secrets(self.body))
        sent_headers = dict(self.headers)
        if api_key and self.key_header:
            sent_headers[self.key_header] = f"{self.key_prefix}{api_key}"
        headers = {"Content-Type": "application/json", **{k: str(v) for k, v in _resolve_secrets(sent_headers).items()}}
        started = time.monotonic()
        response = await client.post(self.url, headers=headers, json=body, timeout=self.timeout_s)
        latency_ms = int((time.monotonic() - started) * 1000)
        if response.status_code >= 400:
            raise RuntimeError(f"{self.name} API error {response.status_code}: {response.text[:1000]}")
        try:
            data = response.json()
        except ValueError as exc:
            raise RuntimeError(f"{self.name} API error 502: the reply was not JSON") from exc
        parsed = _layout_reply(self.layout, data)
        text, in_tokens, out_tokens, stop, cut = parsed.text, parsed.input_tokens, parsed.output_tokens, parsed.stop, parsed.cut
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
            cached_input_tokens=parsed.cached_input_tokens,
            reasoning_tokens=parsed.reasoning_tokens,
        )
        meta: dict[str, Any] = {"latency_ms": latency_ms, "status": "completed", "stop_reason": stop}
        if parsed.stopped and not self.stop_key:
            meta["stopped"] = parsed.stopped
        return reply, meta

    # ---- asking a model directly -------------------------------------------------

    async def aask(
        self,
        prompt: str,
        *,
        system: str = "",
        images: list[str] | tuple[str, ...] = (),
        json: bool = False,
        max_output_tokens: int = 2000,
        api_key: str | None = None,
    ) -> Reply:
        """Ask the model one thing, outside a run: ``prompt`` and optional
        ``images`` (web links, ``data:`` addresses, or picture files), in the same
        request shape a run sends. Returns the ``Reply``; raises ``ProviderError``
        when the provider refuses or stops without an answer."""
        from ..spec.pictures import picture_file_address

        pictures = [picture_file_address(str(image)) for image in images]
        request = ModelRequest(
            system=str(system),
            user=str(prompt),
            content=[{"type": "text", "text": str(prompt)}, *({"type": "image", "url": url} for url in pictures)],
            images=pictures,
            max_output_tokens=int(max_output_tokens),
            json_mode=bool(json),
        )
        if pictures and not self.images:
            raise ConfigError(f"Model {self.name!r} takes no image input")
        async with httpx.AsyncClient() as http:
            reply, meta = await self.acall(request, client=http, api_key=api_key)
        if meta.get("stopped"):
            raise ProviderError(f"{self.name} gave no answer: {meta['stopped']}")
        return reply

    def ask(
        self,
        prompt: str,
        *,
        system: str = "",
        images: list[str] | tuple[str, ...] = (),
        json: bool = False,
        max_output_tokens: int = 2000,
        api_key: str | None = None,
    ) -> Reply:
        """``aask`` for code that is not async (it runs its own event loop)."""
        return asyncio.run(
            self.aask(prompt, system=system, images=images, json=json, max_output_tokens=max_output_tokens, api_key=api_key)
        )

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
            records: list[UsageRecord] = []
            client = self.openai_client(api_key=api_key, on_usage=records.append)
            try:
                text = await client.chat_text(
                    self.name, request.system, request.user, max_tokens=512, response_format={"type": "json_object"},
                    reasoning_effort=self.reasoning_effort,
                )
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
                reply, meta = await self.acall(request, client=http, api_key=api_key)
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
        """The environment variables this model needs that are not set now: an
        OpenAI or OpenRouter model's key variable, and every ``env(...)``
        placeholder of a model called over HTTP. Empty for a function model."""
        missing = _unset_env(self.headers) + _unset_env(self.body)
        if self.kind == "preset" and self.api_key_env and not os.environ.get(self.api_key_env, "").strip():
            missing.insert(0, self.api_key_env)
        return list(dict.fromkeys(missing))

    @classmethod
    def from_settings(cls, settings: dict[str, Any]) -> "Model | None":
        """A built-in model made again from the settings a run recorded
        (``Model.settings``); None for a model only its definition can make (an
        HTTP model of your own, a function, or a built-in model whose body hides a
        field named like a secret)."""
        if not isinstance(settings, dict):
            return None
        kind, builtin, name = settings.get("kind"), settings.get("builtin"), str(settings.get("name") or "")
        if kind == "preset":
            effort = settings.get("reasoning_effort") if settings.get("reasoning_effort_set") else None
            if settings.get("provider") == "openrouter":
                return cls.openrouter(name, reasoning_effort=effort, api_key_env=str(settings.get("api_key_env") or "OPENROUTER_API_KEY"))
            return cls.openai(
                name, reasoning_effort=effort, api_key_env=str(settings.get("api_key_env") or "OPENAI_API_KEY"),
                fallback_to_openrouter=bool(settings.get("fallback_to_openrouter")),
            )
        if builtin not in {"anthropic", "gemini", "deepseek"}:
            return None
        try:
            body = _restore_env(copy.deepcopy(settings.get("body") or {}))
        except ValueError:
            return None  # a secret written into the body is not recorded
        common: dict[str, Any] = {"api_key_env": str(settings.get("api_key_env")), "images": settings.get("images")}
        if settings.get("timeout_s") is not None:
            common["timeout_s"] = float(settings["timeout_s"])
        common["thinking_room"] = int(settings.get("thinking_room") or 0)
        if builtin == "anthropic":
            body.pop("model", None)
            effort = (body.get("output_config") or {}).pop("effort", None) if isinstance(body.get("output_config"), dict) else None
            if body.get("output_config") == {}:
                body.pop("output_config")
            return cls.anthropic(
                name, effort=effort, temperature=body.pop("temperature", None),
                url=str(settings.get("url") or "https://api.anthropic.com/v1/messages"), body=body or None,
                **{**common, "images": bool(common["images"] if common["images"] is not None else True)},
            )
        if builtin == "gemini":
            generation = body.get("generationConfig") if isinstance(body.get("generationConfig"), dict) else {}
            temperature = generation.pop("temperature", None)
            thinking = generation.get("thinkingConfig") if isinstance(generation.get("thinkingConfig"), dict) else {}
            level = thinking.pop("thinkingLevel", None)
            if "thinkingConfig" in generation and not thinking:
                generation.pop("thinkingConfig")
            if "generationConfig" in body and not generation:
                body.pop("generationConfig")
            return cls.gemini(
                name, temperature=temperature, thinking_level=level, body=body or None,
                **{**common, "images": bool(common["images"] if common["images"] is not None else True)},
            )
        body.pop("model", None)
        effort = body.pop("reasoning_effort", None)
        base = str(settings.get("url") or "https://api.deepseek.com/chat/completions").rsplit("/chat/completions", 1)[0]
        return cls.deepseek(
            name, base_url=base, reasoning_effort=effort, body=body or None,
            **{**common, "images": bool(common["images"]) if common["images"] is not None else None},
        )

    def check(self, *, api_key: str | None = None) -> ModelCheck:
        """One tiny call: was the reply text found, and were usage and stop reason?"""
        return asyncio.run(self.acheck(api_key=api_key))


def _effort(value: Any) -> str | None:
    text = str(value or "").strip().lower()
    return text or None


# Output room for thinking, by effort or thinking level (the OpenAI presets use
# the same steps: providers/presets.py). An unknown or unset level gets the room
# of "high", the level most models think at by default.
THINKING_ROOM = {"none": 0, "minimal": 2000, "low": 2000, "medium": 8000, "high": 16000, "xhigh": 24000, "max": 32000}


def _room(given: int | None, level: Any) -> int:
    if given is not None:
        room = _to_int(given)
        if room is None or room < 0:
            raise ConfigError(f"thinking_room must be a whole number of tokens, 0 or more; got {given!r}")
        return room
    return THINKING_ROOM.get(str(level or "high").strip().lower(), THINKING_ROOM["high"])


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
