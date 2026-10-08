"""Named model presets, copied from the website's ``llm_client.MODEL_SPECS``.

A preset name such as ``gpt-5.6-luna-medium`` pins the API model and its default
reasoning effort, so results stay comparable with runs on the website.
"""

from __future__ import annotations

# UI-facing aliases can pin both the underlying API model and the default reasoning tier.
MODEL_SPECS: dict[str, dict[str, str | None]] = {
    "gpt-5.6-luna-low": {
        "model_name": "gpt-5.6-luna",
        "openrouter_model_name": "openai/gpt-5.6-luna",
        "default_reasoning_effort": "low",
    },
    "gpt-5.6-luna-medium": {
        "model_name": "gpt-5.6-luna",
        "openrouter_model_name": "openai/gpt-5.6-luna",
        "default_reasoning_effort": "medium",
    },
    "gpt-5.6-luna": {
        "model_name": "gpt-5.6-luna",
        "openrouter_model_name": "openai/gpt-5.6-luna",
        "default_reasoning_effort": None,
    },
    "gpt-5.4-low": {
        "model_name": "gpt-5.4",
        "openrouter_model_name": "openai/gpt-5.4",
        "default_reasoning_effort": "low",
    },
    "gpt-5.4-medium": {
        "model_name": "gpt-5.4",
        "openrouter_model_name": "openai/gpt-5.4",
        "default_reasoning_effort": "medium",
    },
    "gpt-5.4-high": {
        "model_name": "gpt-5.4",
        "openrouter_model_name": "openai/gpt-5.4",
        "default_reasoning_effort": "high",
    },
    "gpt-5.4": {
        "model_name": "gpt-5.4",
        "openrouter_model_name": "openai/gpt-5.4",
        "default_reasoning_effort": None,
    },
    "gpt-5.3": {
        "model_name": "gpt-5.3-chat-latest",
        "openrouter_model_name": "openai/gpt-5.3-chat-latest",
        "default_reasoning_effort": None,
        "max_verbosity": "medium",
    },
    "gpt-5.3-chat-latest": {
        "model_name": "gpt-5.3-chat-latest",
        "openrouter_model_name": "openai/gpt-5.3-chat-latest",
        "default_reasoning_effort": None,
        "max_verbosity": "medium",
    },
    "gpt-5.3-instant": {
        "model_name": "gpt-5.3-chat-latest",
        "openrouter_model_name": "openai/gpt-5.3-chat-latest",
        "default_reasoning_effort": None,
        "max_verbosity": "medium",
    },
    "gpt-5.2-low": {
        "model_name": "gpt-5.2",
        "openrouter_model_name": "openai/gpt-5.2",
        "default_reasoning_effort": "low",
    },
    "gpt-5.2-medium": {
        "model_name": "gpt-5.2",
        "openrouter_model_name": "openai/gpt-5.2",
        "default_reasoning_effort": "medium",
    },
    "gpt-5.2-high": {
        "model_name": "gpt-5.2",
        "openrouter_model_name": "openai/gpt-5.2",
        "default_reasoning_effort": "high",
    },
    "gpt-5.2": {
        "model_name": "gpt-5.2",
        "openrouter_model_name": "openai/gpt-5.2",
        "default_reasoning_effort": None,
    },
    "gpt-5.1-low": {
        "model_name": "gpt-5.1",
        "openrouter_model_name": "openai/gpt-5.1",
        "default_reasoning_effort": "low",
    },
    "gpt-5.1-medium": {
        "model_name": "gpt-5.1",
        "openrouter_model_name": "openai/gpt-5.1",
        "default_reasoning_effort": "medium",
    },
    "gpt-5.1-high": {
        "model_name": "gpt-5.1",
        "openrouter_model_name": "openai/gpt-5.1",
        "default_reasoning_effort": "high",
    },
    "gpt-5.1": {
        "model_name": "gpt-5.1",
        "openrouter_model_name": "openai/gpt-5.1",
        "default_reasoning_effort": None,
    },
    "gpt-5-mini-low": {
        "model_name": "gpt-5-mini",
        "openrouter_model_name": "openai/gpt-5-mini",
        "default_reasoning_effort": "low",
    },
    "gpt-5-mini-medium": {
        "model_name": "gpt-5-mini",
        "openrouter_model_name": "openai/gpt-5-mini",
        "default_reasoning_effort": "medium",
    },
    "gpt-5-mini-high": {
        "model_name": "gpt-5-mini",
        "openrouter_model_name": "openai/gpt-5-mini",
        "default_reasoning_effort": "high",
    },
    "gpt-5-mini": {
        "model_name": "gpt-5-mini",
        "openrouter_model_name": "openai/gpt-5-mini",
        "default_reasoning_effort": None,
    },
    "gpt-4o-mini": {
        "model_name": "gpt-4o-mini",
        "openrouter_model_name": "openai/gpt-4o-mini",
        "default_reasoning_effort": None,
        "max_verbosity": "medium",
        "min_verbosity": "medium",
    },
}

# Reasoning models spend hidden reasoning tokens from the same max_output_tokens
# budget as the visible answer. Without headroom, high effort can consume the
# entire budget and return an empty or truncated completion (observed in prod:
# gpt-5-mini-high averaged ~3.2k reasoning tokens against a 3.5k cap).
_REASONING_TOKEN_ALLOWANCES: dict[str, int] = {
    "none": 0,
    "minimal": 0,
    "low": 2000,
    "medium": 8000,
    "high": 16000,
    "xhigh": 24000,
}
