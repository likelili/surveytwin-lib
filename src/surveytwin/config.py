"""Run settings: what the website's configuration step collects, minus the AI report."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ._display import table
from .errors import ConfigError
from .personas.bank import DEFAULT_BANK, EMPTY_REPRESENTATION, Bank, open_bank
from .providers.models import Model, model_from_config

DEFAULT_SEED = 42
DEFAULT_MAX_STAGES = 25


@dataclass
class RunConfig:
    bank: Bank
    representation: str
    filters: dict[str, Any]
    sample_size: int
    seed: int
    model: Model
    provider: str
    batch: bool
    system_instruction: str | None
    embedded_data: dict[str, str] = field(default_factory=dict)
    carry_stage_images: bool = False
    max_stages: int = DEFAULT_MAX_STAGES
    accept_missing_images: bool = False

    def __str__(self) -> str:
        def describe(clause: Any) -> str:
            if isinstance(clause, dict):
                if clause.get("missing"):
                    return "missing"
                low, high = clause.get("min"), clause.get("max")
                return f"{low if low is not None else ''}..{high if high is not None else ''}"
            return ", ".join(map(str, clause)) if isinstance(clause, (list, tuple)) else str(clause)

        filters = "; ".join(f"{k} = {describe(v)}" for k, v in self.filters.items()) or "none"
        instruction = f"custom ({len(self.system_instruction):,} characters)" if self.system_instruction else "default"
        embedded = ", ".join(f"{k}={v}" for k, v in self.embedded_data.items()) or "none"
        rows = [
            ["bank", f"{self.bank.name} ({self.bank.id} v{self.bank.version})"],
            ["representation", self.bank.representation_label(self.representation)],
            ["filters", filters],
            ["twins", f"{self.sample_size:,}"],
            ["seed", str(self.seed)],
            ["model", f"{self.model.name} ({self.model.kind}{', ' + self.provider if self.model.kind == 'preset' else ''})"],
            ["batch", "yes" if self.batch else "no"],
            ["system instruction", instruction],
            ["embedded data", embedded],
            ["max stages", str(self.max_stages)],
            ["carry stage images", "yes" if self.carry_stage_images else "no"],
            ["missing images", "skip their questions" if self.accept_missing_images else "stop the run"],
        ]
        return "\n".join(["Run settings:"] + table(rows))

    def to_record(self) -> dict[str, Any]:
        """The settings as recorded with results (no keys, no persona text)."""
        return {
            "bank": {
                "bank_id": self.bank.id,
                "name": self.bank.name,
                "version": self.bank.version,
                "format": self.bank.format,
                "fingerprint": self.bank.fingerprint,
            },
            "representation": self.representation,
            "representation_label": self.bank.representation_label(self.representation),
            "filters": self.filters,
            "sample_size": self.sample_size,
            "seed": self.seed,
            "model": self.model.settings,
            "batch": self.batch,
            "system_instruction": self.system_instruction,
            "system_instruction_sha256": hashlib.sha256(self.system_instruction.encode("utf-8")).hexdigest()
            if self.system_instruction
            else None,
            "embedded_data": dict(self.embedded_data),
            "carry_stage_images": self.carry_stage_images,
            "max_stages": self.max_stages,
            "accept_missing_images": self.accept_missing_images,
        }


def configure(
    *,
    bank: str | Path | Bank = DEFAULT_BANK,
    representation: str | None = None,
    filters: dict[str, Any] | None = None,
    sample_size: int,
    seed: int = DEFAULT_SEED,
    model: str | Model,
    provider: str = "openai",
    batch: bool = False,
    system_instruction: str | None = None,
    embedded_data: dict[str, Any] | None = None,
    carry_stage_images: bool = False,
    max_stages: int = DEFAULT_MAX_STAGES,
    accept_missing_images: bool = False,
) -> RunConfig:
    """Validate every setting now, so a mistake fails before any model call.

    ``representation=None`` takes the bank's default (``summary`` for the
    Twin-2K-500 package); ``"empty"`` runs twins with no persona, a baseline.
    ``embedded_data`` gives values for fields the real study filled from outside
    the QSF (URL parameters, contact lists). ``accept_missing_images=True``
    allows a run whose images cannot be sent (unreachable, or a model without
    image input); those images are left out, a question that shows one is not
    asked and is recorded as skipped, and the results say so.
    """
    the_bank = bank if isinstance(bank, Bank) else open_bank(bank)
    view = the_bank.resolve_representation(representation)
    normalized_filters = dict(filters or {})
    the_bank.normalize_filters(normalized_filters)  # raises FilterError for unknown names
    try:
        size = int(sample_size)
    except (TypeError, ValueError) as exc:
        raise ConfigError("sample_size must be a whole number") from exc
    if size < 1:
        raise ConfigError("sample_size must be at least 1")
    try:
        seed_value = int(seed)
    except (TypeError, ValueError) as exc:
        raise ConfigError("seed must be a whole number") from exc
    if seed_value < 0:
        raise ConfigError("seed must be 0 or more")
    the_model = model_from_config(model, provider)
    stages = int(max_stages)
    if stages < 1:
        raise ConfigError("max_stages must be at least 1")
    instruction = str(system_instruction).strip() if system_instruction is not None else None
    if instruction == "":
        instruction = None
    if batch and not (the_model.kind == "preset" and the_model.provider == "openai"):
        raise ConfigError("batch=True needs an OpenAI preset model; the Batch API is OpenAI-only")
    outside = {str(k): "" if v is None else str(v) for k, v in (embedded_data or {}).items() if str(k).strip()}
    return RunConfig(
        bank=the_bank,
        representation=view,
        filters=normalized_filters,
        sample_size=size,
        seed=seed_value,
        model=the_model,
        provider=the_model.provider if the_model.kind == "preset" else the_model.kind,
        batch=bool(batch),
        system_instruction=instruction,
        embedded_data=outside,
        carry_stage_images=bool(carry_stage_images),
        max_stages=stages,
        accept_missing_images=bool(accept_missing_images),
    )


__all__ = ["RunConfig", "configure", "DEFAULT_SEED", "DEFAULT_MAX_STAGES", "EMPTY_REPRESENTATION"]
