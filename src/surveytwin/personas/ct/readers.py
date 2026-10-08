"""Streaming readers + record normalization for CT bank packages.

These run at **ingest time** only (offline / admin), so heavy dependencies such
as ``pyarrow`` are imported lazily here and never required by the live server.
Everything streams in bounded batches so the 1M-persona / 6M-representation
fixtures never load fully into memory.
"""

from __future__ import annotations

import csv
import hashlib
import json
from pathlib import Path
from typing import Any, Iterator

from . import manifest as _m
from .iris import local_name
from .manifest import CTManifest, CTRepresentationSet, CTResource


class UnsupportedEncodingError(RuntimeError):
    """Raised for a physical encoding outside the CT Core Serialization Set."""


class ChecksumError(RuntimeError):
    pass


# ---------------------------------------------------------------------------
# file-level helpers
# ---------------------------------------------------------------------------


def sha256_file(path: Path, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while True:
            block = fh.read(chunk)
            if not block:
                break
            h.update(block)
    return h.hexdigest()


def verify_checksum(resource: CTResource, bank_dir: Path) -> None:
    if not resource.sha256 or not resource.content_url:
        return
    path = bank_dir / resource.content_url
    if not path.exists():
        raise ChecksumError(f"declared resource missing on disk: {resource.content_url}")
    actual = sha256_file(path)
    if actual.lower() != resource.sha256.lower():
        raise ChecksumError(
            f"sha256 mismatch for {resource.content_url}: manifest={resource.sha256} actual={actual}"
        )


def _resolve_local(bank_dir: Path, rel: str) -> Path:
    """Resolve a package-relative path, refusing traversal outside the bank dir."""
    base = bank_dir.resolve()
    target = (base / rel).resolve()
    if base != target and base not in target.parents:
        raise ValueError(f"path escapes bank directory: {rel}")
    return target


# ---------------------------------------------------------------------------
# record iteration
# ---------------------------------------------------------------------------


def iter_records(resource: CTResource, bank_dir: Path, batch_size: int = 2000) -> Iterator[dict]:
    """Yield records (dicts) from a distributed RecordSet source resource."""
    if not resource.content_url:
        raise UnsupportedEncodingError(f"resource {resource.id} has no contentUrl")
    if resource.content_url.startswith(("http://", "https://")):
        raise UnsupportedEncodingError(f"remote RecordSet source not supported at ingest: {resource.content_url}")
    path = _resolve_local(bank_dir, resource.content_url)
    enc = (resource.encoding_format or "").lower()
    suffix = path.suffix.lower()
    if "ndjson" in enc or "jsonl" in enc or suffix in (".jsonl", ".ndjson"):
        yield from _iter_jsonl(path)
    elif "parquet" in enc or suffix == ".parquet":
        yield from _iter_parquet(path, batch_size)
    elif "csv" in enc or suffix == ".csv":
        yield from _iter_csv(path)
    elif "json" in enc or suffix == ".json":
        yield from _iter_json_array(path)
    else:
        raise UnsupportedEncodingError(f"unsupported encoding for {resource.content_url}: {resource.encoding_format}")


def count_records(resource: CTResource, bank_dir: Path) -> int:
    path = _resolve_local(bank_dir, resource.content_url or "")
    suffix = path.suffix.lower()
    if suffix == ".parquet":
        import pyarrow.parquet as pq  # lazy

        return pq.ParquetFile(path).metadata.num_rows
    return sum(1 for _ in iter_records(resource, bank_dir))


def _iter_jsonl(path: Path) -> Iterator[dict]:
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            yield json.loads(line)


def _iter_json_array(path: Path) -> Iterator[dict]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(data, list):
        yield from (d for d in data if isinstance(d, dict))
    elif isinstance(data, dict):
        yield data


def _iter_csv(path: Path) -> Iterator[dict]:
    with open(path, encoding="utf-8", newline="") as fh:
        for row in csv.DictReader(fh):
            yield dict(row)


def _iter_parquet(path: Path, batch_size: int) -> Iterator[dict]:
    import pyarrow.parquet as pq  # lazy; ingest-time only

    pf = pq.ParquetFile(path)
    for batch in pf.iter_batches(batch_size=batch_size):
        cols = batch.schema.names
        columns = [batch.column(i).to_pylist() for i in range(len(cols))]
        for r in range(batch.num_rows):
            yield {cols[c]: columns[c][r] for c in range(len(cols))}


# ---------------------------------------------------------------------------
# normalization (resolve records into CT semantic rows)
# ---------------------------------------------------------------------------


def normalize_persona(record: dict, manifest: CTManifest) -> tuple[str, dict[str, Any]]:
    """Return ``(persona_id, {field_name: value})`` using the manifest's field sources.

    Only persona fields are read. ``persona_id`` is coerced to a non-empty string.
    """
    pid_field = manifest.persona_id_field
    persona_id = pid_field.source.read(record) if pid_field else None
    if persona_id is None or str(persona_id).strip() == "":
        raise ValueError("persona record missing a non-empty persona_id")
    attrs: dict[str, Any] = {}
    for f in manifest.persona_set.fields:
        if f is pid_field:
            continue
        attrs[f.name] = f.source.read(record)
    return str(persona_id), attrs


def selection_values(record: dict, rep_set: CTRepresentationSet) -> dict[str, str]:
    out: dict[str, str] = {}
    for f in rep_set.selection_dimensions:
        v = f.source.read(record)
        if v is not None:
            out[f.name] = str(v)
    return out


def normalize_representation(
    record: dict, rep_set: CTRepresentationSet, bank_dir: Path, *, read_external: bool = True
) -> dict[str, Any]:
    """Return a normalized representation row.

    Keys: ``representation_id, persona_id, kind, selection_dims, is_default,
    media_type, payload_mode, payload`` (payload is the prompt-ready text, or
    None when external content is not read).
    """
    rs = rep_set
    rid = rs.id_field.source.read(record) if rs.id_field else None
    persona_id = rs.persona_fk_field.source.read(record) if rs.persona_fk_field else None
    kind_raw = rs.kind_field.source.read(record) if rs.kind_field else None
    media = rs.media_type_field.source.read(record) if rs.media_type_field else None
    is_default = bool(rs.default_field.source.read(record)) if rs.default_field else None

    payload: str | None = None
    if rs.payload_mode == _m.PAYLOAD_TEXT and rs.payload_field:
        val = rs.payload_field.source.read(record)
        payload = None if val is None else str(val)
    elif rs.payload_mode == _m.PAYLOAD_STRUCTURED and rs.payload_field:
        val = rs.payload_field.source.read(record)
        payload = render_structured_payload(val)
        if not media:
            media = "application/json"
    elif rs.payload_mode == _m.PAYLOAD_EXTERNAL and rs.payload_field:
        rel = rs.payload_field.source.read(record)
        if read_external and rel:
            payload = _read_external_payload(str(rel), bank_dir)

    return {
        "representation_id": str(rid) if rid is not None else None,
        "persona_id": str(persona_id) if persona_id is not None else None,
        "kind": str(kind_raw) if kind_raw is not None else None,
        "kind_label": _m.humanize_label(str(kind_raw)) if kind_raw is not None else None,
        "selection_dims": selection_values(record, rs),
        "is_default": is_default,
        "media_type": str(media) if media is not None else None,
        "payload_mode": rs.payload_mode,
        "payload": payload,
    }


def render_structured_payload(value: Any) -> str:
    """Render a structured payload as canonical, stable JSON text."""
    if value is None:
        return ""
    if isinstance(value, str):
        # already serialized JSON text — re-canonicalize when parseable
        try:
            value = json.loads(value)
        except (json.JSONDecodeError, ValueError):
            return value
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ": "))


def _read_external_payload(rel: str, bank_dir: Path) -> str:
    if rel.startswith(("http://", "https://")):
        raise UnsupportedEncodingError(f"absolute external payload URLs not fetched at ingest: {rel}")
    path = _resolve_local(bank_dir, rel)
    if not path.exists():
        raise FileNotFoundError(f"external payload missing: {rel}")
    return path.read_text(encoding="utf-8")
