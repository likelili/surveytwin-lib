"""Token usage for each model call, as the provider reports it.

Usage normalization is ported from the website's ``services/v3/billing.py``
(``normalize_usage``). The library reports token counts to the caller; it has
no prices and computes no cost (decided 2026-09-26).
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


def _int(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def normalize_usage(endpoint: str, usage: dict[str, Any] | None) -> dict[str, int]:
    """Token counts from an OpenAI-style ``usage`` block (Responses or Chat)."""
    usage = usage if isinstance(usage, dict) else {}
    if endpoint == "responses":
        input_tokens = _int(usage.get("input_tokens"))
        output_tokens = _int(usage.get("output_tokens"))
        input_details = usage.get("input_tokens_details")
        output_details = usage.get("output_tokens_details")
        cached = _int(input_details.get("cached_tokens")) if isinstance(input_details, dict) else 0
        reasoning = _int(output_details.get("reasoning_tokens")) if isinstance(output_details, dict) else 0
        return {
            "input_tokens": input_tokens,
            "cached_input_tokens": cached,
            "uncached_input_tokens": max(input_tokens - cached, 0),
            "output_tokens": output_tokens,
            "reasoning_tokens": reasoning,
            "total_tokens": _int(usage.get("total_tokens")) or input_tokens + output_tokens,
        }
    prompt_tokens = _int(usage.get("prompt_tokens") or usage.get("input_tokens"))
    completion_tokens = _int(usage.get("completion_tokens") or usage.get("output_tokens"))
    prompt_details = usage.get("prompt_tokens_details") or usage.get("input_tokens_details")
    completion_details = usage.get("completion_tokens_details") or usage.get("output_tokens_details")
    cached = _int(prompt_details.get("cached_tokens")) if isinstance(prompt_details, dict) else 0
    reasoning = _int(completion_details.get("reasoning_tokens")) if isinstance(completion_details, dict) else 0
    return {
        "input_tokens": prompt_tokens,
        "cached_input_tokens": cached,
        "uncached_input_tokens": max(prompt_tokens - cached, 0),
        "output_tokens": completion_tokens,
        "reasoning_tokens": reasoning,
        "total_tokens": _int(usage.get("total_tokens")) or prompt_tokens + completion_tokens,
    }


@dataclass
class UsageRecord:
    """One provider call, as reported to ``on_usage`` and kept in ``Results.usage``."""

    provider: str
    endpoint: str
    model: str
    resolved_model: str
    status: str
    input_tokens: int = 0
    cached_input_tokens: int = 0
    output_tokens: int = 0
    reasoning_tokens: int = 0
    total_tokens: int = 0
    latency_ms: int = 0
    request_id: str = ""
    twin_id: str = ""
    stage: int | None = None
    purpose: str = "answer"
    batch: bool = False
    error: str = ""
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
