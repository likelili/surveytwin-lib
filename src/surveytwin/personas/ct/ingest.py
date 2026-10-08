"""Offline ingester: load a CT bank package into the durable catalog.

Run as a module (heavy deps such as pyarrow are only needed here):

    PYTHONPATH=web/backend python -m services.ct.ingest /path/to/bank --approve

It validates the manifest, verifies checksums, then streams personas and
representations into the catalog in bounded batches. Representation *views*
(kind x selection-dimension tuples) and their coverage are derived from the
data and stored, so the live server never rescans the package.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

from . import catalog, readers
from .iris import local_name
from .manifest import CTManifest, humanize_label
from .readers import sha256_file


def _slugify(value: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (value or "").lower()).strip("-")
    return s or "bank"


def _view_key(record_set_id: str, kind: str, selection_dims: dict[str, str]) -> str:
    base = f"{record_set_id}::{local_name(kind)}"
    for dim in sorted(selection_dims):
        base += f"::{dim}={selection_dims[dim]}"
    return base


def _coerce_num(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return None


FILTER_VALUE_CAP = 100


def _build_filter_defs(manifest: CTManifest, version_id: str) -> list[dict]:
    defs: list[dict] = []
    for f in manifest.filter_fields:
        cb = manifest.codebooks.get(f.codebook_id) if f.codebook_id else None
        values: list[dict] = []
        counts: dict[str, int] = {}
        if not f.is_numeric:
            counts = dict(catalog.distinct_filter_values(version_id, f.name, limit=FILTER_VALUE_CAP))
        if f.values:
            # Inline ct:values (spec rev 2026-07-09): array order = display order,
            # labels/descriptions from the entries, counts always computed from data.
            declared = set()
            for e in f.values:
                declared.add(e.code)
                values.append(
                    {"code": e.code, "label": e.label or humanize_label(e.code),
                     "description": e.description, "count": counts.get(e.code)}
                )
            for value, count in counts.items():
                if value not in declared:  # observed but undeclared: keep visible, after the declared order
                    values.append({"code": value, "label": value, "description": None, "count": count})
        elif cb:
            # Legacy declared codebook: authoritative value set + labels + ordering.
            for e in sorted(cb.entries, key=lambda e: (e.ordinal if e.ordinal is not None else 1_000_000, e.code)):
                values.append(
                    {"code": e.code, "label": e.label or humanize_label(e.code),
                     "description": e.description,
                     "count": e.count if e.count is not None else counts.get(e.code)}
                )
        elif not f.is_numeric:
            # No declaration: distinct values from the authoritative Persona field
            # (within-bank filtering fallback). Numeric fields get no chips (range UI is TBD).
            for value, count in counts.items():
                values.append({"code": value, "label": value, "description": None, "count": count})
        defs.append(
            {
                "field_name": f.name,
                "label": humanize_label(f.name),
                "semantic_iri": f.equivalent_property or "",
                "data_type": local_name(f.data_types[0]) if f.data_types else "Text",
                "is_numeric": f.is_numeric,
                "sensitive": f.sensitive,
                "codebook": values,
            }
        )
    return defs


def _build_summary(manifest: CTManifest) -> dict:
    return {
        "name": manifest.name,
        "description": manifest.description,
        "version": manifest.version,
        "license": manifest.license,
        "url": manifest.url,
        "same_as": manifest.same_as,
        "creators": manifest.creators,
        "publisher": manifest.publisher,
        "citation": manifest.citation,
        "date_published": manifest.date_published,
        "languages": manifest.languages,
        "persona_basis": local_name(manifest.persona_basis) if manifest.persona_basis else None,
        "default_representation_kind": local_name(manifest.default_representation_kind)
        if manifest.default_representation_kind else None,
        "conditions_of_access": manifest.conditions_of_access,
        "usage_info": manifest.usage_info,
        "human_data_statement": manifest.human_data_statement,
        "data_collection": manifest.data_collection,
        "target_population": manifest.target_population,
        "data_limitations": manifest.data_limitations,
        "data_biases": manifest.data_biases,
        "data_use_cases": manifest.data_use_cases,
        "capabilities": manifest.capabilities,
        "keywords": manifest.keywords,
        "domain": manifest.domain,
        "region": manifest.region,
    }


def ingest_bank(
    bank_dir: str | Path,
    *,
    bank_id: str | None = None,
    force: bool = False,
    approve: bool = False,
    reviewer: str = "",
    verify_checksums: bool = True,
    batch_size: int = 2000,
    limit: int | None = None,
    payload_store: str = "db",
    blob_store=None,
    force_version_id: str | None = None,
    upload_payloads: bool = True,
    log=print,
) -> dict[str, Any]:
    """Stream a CT bank package into the active catalog DB.

    ``blob_store`` overrides the payload pack destination (ephemeral banks pass a
    LocalDirBlobStore so nothing durable is written). ``force_version_id`` pins
    the created version id (artifact rebuilds keep ids stable so installs and
    run history stay valid). ``upload_payloads=False`` recomputes pack offsets
    without re-uploading shards (migration over identical sources).
    """
    bank_dir = Path(bank_dir)
    if bank_dir.name != "bank" and (bank_dir / "bank").is_dir():
        bank_dir = bank_dir / "bank"
    manifest = CTManifest.from_path(bank_dir)

    issues = manifest.validate()
    errors = [i for i in issues if i.severity == "error"]
    for i in issues:
        log(f"  [{i.severity.upper()} {i.code}] {i.message}{(' @' + i.pointer) if i.pointer else ''}")
    if errors:
        raise SystemExit(f"Manifest has {len(errors)} error(s); refusing to ingest. Fix the bank first.")

    package_dir = bank_dir.parent
    bank_id = bank_id or _slugify(package_dir.name if package_dir.name != bank_dir.name else manifest.name)
    version = manifest.version or "0.1.0"

    manifest_path = bank_dir / "croissant.json"
    manifest_sha = sha256_file(manifest_path)
    card_path = bank_dir / "README.md"
    card_md = card_path.read_text(encoding="utf-8") if card_path.exists() else ""

    # checksums on declared FileObjects
    if verify_checksums:
        for res in manifest.resources.values():
            if res.type == "FileObject" and res.sha256 and res.content_url and not res.content_url.startswith("http"):
                readers.verify_checksum(res, bank_dir)
                log(f"  checksum OK: {res.content_url}")

    log(f"Ingesting bank_id={bank_id} version={version} from {bank_dir}")
    catalog.ensure_schema()
    catalog.upsert_bank(bank_id, manifest.name, manifest.publisher or "")

    existing = catalog.find_version(bank_id, version)
    reuse_id = None
    if existing:
        if not force:
            raise SystemExit(
                f"Version already ingested (id={existing['id']}, status={existing['status']}). Use --force to re-ingest."
            )
        reuse_id = existing["id"]
        catalog.delete_version_data(reuse_id)

    # Derivable: one representation set, declared derivable + complete (dense) coverage.
    # We then store one compact row per persona instead of one row per representation.
    rep_sets = manifest.representation_sets
    derivable = (
        len(rep_sets) == 1
        and rep_sets[0].record_set.derivable
        and rep_sets[0].record_set.complete_coverage
    )
    if derivable and payload_store != "object":
        payload_store = "object"  # derivable keeps Postgres lean -> payloads must live in the blob store
        log("  (derivable bank -> using object payload backend)")

    version_id = catalog.create_version(
        bank_id=bank_id,
        version=version,
        manifest_json=manifest_path.read_text(encoding="utf-8"),
        manifest_sha256=manifest_sha,
        card_md=card_md,
        summary=_build_summary(manifest),
        persona_basis=local_name(manifest.persona_basis) if manifest.persona_basis else "",
        default_kind=local_name(manifest.default_representation_kind) if manifest.default_representation_kind else "",
        conditions_of_access=manifest.conditions_of_access or "",
        license=manifest.license or "",
        source_path=str(bank_dir),
        payload_backend=payload_store,
        derivable=derivable,
        reuse_version_id=reuse_id or force_version_id,
    )
    if derivable:
        log("  representations: derivable (one compact row per persona; dense coverage asserted)")

    pack = None
    if payload_store == "object":
        from . import payload_store as _ps

        store = blob_store if blob_store is not None else _ps.get_active_store()
        pack = _ps.PackBuilder(store if upload_payloads else None, version_id)
        if not upload_payloads:
            log("  payload packs: offsets recomputed, shard upload skipped (reuse existing)")
        else:
            log("  payload backend: object storage (payloads packed to blob store)")

    # ---- stream personas ----
    filter_fields = manifest.filter_fields
    filter_names = {f.name for f in filter_fields}
    numeric_names = {f.name for f in filter_fields if f.is_numeric}
    missing_by_field = {f.name: set(str(m) for m in f.missing_values) for f in filter_fields}

    persona_resource = manifest.resource(manifest.persona_id_field.source.file_id) if manifest.persona_id_field else None
    if persona_resource is None:
        raise SystemExit("Persona RecordSet source resource not found in distribution")

    persona_count = 0
    persona_batch: list[tuple[str, str]] = []
    value_batch: list[tuple[str, str, str | None, float | None]] = []
    for record in readers.iter_records(persona_resource, bank_dir, batch_size=batch_size):
        persona_id, attrs = readers.normalize_persona(record, manifest)
        display = {k: attrs.get(k) for k in filter_names}
        persona_batch.append((persona_id, json.dumps(display, ensure_ascii=False)))
        for name in filter_names:
            val = attrs.get(name)
            if val is None or (isinstance(val, str) and val.strip() == ""):
                continue
            sval = str(val)
            if sval in missing_by_field.get(name, set()):
                continue
            num = _coerce_num(val) if name in numeric_names else None
            value_batch.append((persona_id, name, sval, num))
        persona_count += 1
        if len(persona_batch) >= batch_size:
            catalog.insert_personas_batch(version_id, persona_batch)
            catalog.insert_persona_values_batch(version_id, value_batch)
            persona_batch, value_batch = [], []
            if persona_count % (batch_size * 25) == 0:
                log(f"    personas ingested: {persona_count:,}")
        if limit and persona_count >= limit:
            break
    catalog.insert_personas_batch(version_id, persona_batch)
    catalog.insert_persona_values_batch(version_id, value_batch)
    log(f"  personas ingested: {persona_count:,}")

    # Filter values: declared codebook when present (labels/order/cross-bank), else the
    # distinct values loaded from the authoritative Persona field (CT spec 12.1).
    catalog.insert_filters(version_id, _build_filter_defs(manifest, version_id))

    # ---- stream representations ----
    rep_count = 0
    view_stats: dict[str, dict[str, Any]] = {}
    rep_batch: list[dict] = []                       # enumerated: one row per representation
    persona_reps: dict[str, dict[str, list]] = {}    # derivable: persona_id -> {view_key: [shard, off, len, char]}

    for rep_set in manifest.representation_sets:
        rep_resource = manifest.resource(rep_set.payload_field.source.file_id) if rep_set.payload_field else None
        if rep_resource is None and rep_set.id_field:
            rep_resource = manifest.resource(rep_set.id_field.source.file_id)
        if rep_resource is None:
            log(f"  WARNING: representation set {rep_set.record_set.id} has no source resource; skipping")
            continue
        for record in readers.iter_records(rep_resource, bank_dir, batch_size=batch_size):
            row = readers.normalize_representation(record, rep_set, bank_dir)
            if not row["representation_id"] or not row["persona_id"]:
                continue
            vkey = _view_key(rep_set.record_set.id, row["kind"] or "", row["selection_dims"])
            payload = row.get("payload") or ""
            char = len(payload)
            st = view_stats.setdefault(
                vkey,
                {
                    "record_set_id": rep_set.record_set.id, "kind": row["kind"] or "",
                    "kind_label": row["kind_label"] or "", "selection_dims": row["selection_dims"],
                    "coverage_count": 0, "default_votes": 0, "max_char": 0,
                    "payload_mode": row["payload_mode"] or "", "media_type": row["media_type"] or "",
                },
            )
            if payload.strip():
                st["coverage_count"] += 1
            if char > st["max_char"]:
                st["max_char"] = char
            if row["is_default"]:
                st["default_votes"] += 1

            shard = off = ln = None
            if pack is not None and payload:
                shard, off, ln = pack.add(payload.encode("utf-8"))

            if derivable:
                # Accumulate by persona (the file may be grouped by scope, not persona).
                persona_reps.setdefault(row["persona_id"], {})[vkey] = [shard or 0, off or 0, ln or 0, char]
            else:
                row["view_key"] = vkey
                row["char_count"] = char
                if pack is not None:
                    if shard is not None:
                        row["payload_uri"] = pack.shard_key(shard)
                        row["payload_offset"], row["payload_len"] = off, ln
                    row["payload"] = None
                rep_batch.append(row)
                if len(rep_batch) >= batch_size:
                    catalog.insert_representations_batch(version_id, rep_batch)
                    rep_batch = []

            rep_count += 1
            if rep_count % (batch_size * 25) == 0:
                log(f"    representations ingested: {rep_count:,}")
            if limit and rep_count >= max(limit * 12, 100):
                break

    if derivable:
        reps_batch: list[tuple[str, str]] = []
        for pid, m in persona_reps.items():
            reps_batch.append((pid, json.dumps(m, ensure_ascii=False)))
            if len(reps_batch) >= batch_size:
                catalog.insert_persona_reps_batch(version_id, reps_batch)
                reps_batch = []
        if reps_batch:
            catalog.insert_persona_reps_batch(version_id, reps_batch)
        distinct_picks = sum(len(m) for m in persona_reps.values())
        if rep_count > distinct_picks:
            # CT015: a (persona, kind[, dims]) pick resolved to more than one representation.
            log(f"  [WARNING CT015] {rep_count - distinct_picks} duplicate (persona, kind) rows overwrote earlier ones")
        log(f"  derivable persona-rep rows: {len(persona_reps):,}")
    elif rep_batch:
        catalog.insert_representations_batch(version_id, rep_batch)
    if pack is not None:
        shard_count = pack.finalize()
        log(f"  payload pack shards written: {shard_count}")
    log(f"  representations ingested: {rep_count:,}")

    # ---- derive + store views ----
    default_kind_local = local_name(manifest.default_representation_kind) if manifest.default_representation_kind else ""
    views = _materialize_views(manifest, view_stats, default_kind_local)
    catalog.upsert_views(version_id, views)
    catalog.finalize_counts(version_id, persona_count, rep_count)

    log(f"  views: {[(v['label'], v['coverage_count'], 'default' if v['is_default'] else '') for v in views]}")

    if approve:
        catalog.set_version_status(version_id, catalog.STATUS_APPROVED, reviewer=reviewer or "cli", review_notes="approved via ct_ingest")
        log(f"  status: APPROVED (reviewer={reviewer or 'cli'})")
    else:
        log("  status: PENDING (run with --approve to publish to the storefront)")

    return {"bank_id": bank_id, "version_id": version_id, "version": version,
            "persona_count": persona_count, "representation_count": rep_count, "views": views}


def _materialize_views(manifest: CTManifest, view_stats: dict[str, dict], default_kind_local: str) -> list[dict]:
    # codebook for selection-dimension ordering/labels (legacy banks; first selection dim across rep sets)
    dim_codebooks = {}
    # inline ct:values on the kind field (spec rev 2026-07-09): label/description/order per kind
    kind_values: dict[str, dict[str, Any]] = {}
    rs_descriptions: dict[str, str] = {}
    for rep_set in manifest.representation_sets:
        rs_descriptions[rep_set.record_set.id] = rep_set.record_set.description or ""
        if rep_set.kind_field and rep_set.kind_field.values:
            kind_values[rep_set.record_set.id] = {e.code: e for e in rep_set.kind_field.values}
        for f in rep_set.selection_dimensions:
            cb = manifest.codebooks.get(f.codebook_id) if f.codebook_id else None
            if cb:
                dim_codebooks[f.name] = {e.code: e for e in cb.entries}
    # single-kind sets may carry their kind description on the RecordSet description
    from collections import Counter
    views_per_set = Counter(st["record_set_id"] for st in view_stats.values())

    views: list[dict] = []
    for vkey, st in view_stats.items():
        dims = st["selection_dims"]
        kv = kind_values.get(st["record_set_id"], {})
        kentry = kv.get(st["kind"]) or kv.get(local_name(st["kind"]))
        kind_label = (kentry.label if kentry and kentry.label else None) or st["kind_label"] or humanize_label(st["kind"])
        kind_desc = kentry.description if kentry and kentry.description else ""
        kind_ord = kentry.ordinal if kentry and kentry.ordinal is not None else None
        if dims:
            parts = []
            ordinal = 0
            for dname in sorted(dims):
                code = dims[dname]
                entry = dim_codebooks.get(dname, {}).get(code)
                parts.append(entry.label if (entry and entry.label) else humanize_label(code))
                if entry and entry.ordinal is not None:
                    ordinal = entry.ordinal
            # Drop dimension parts that merely restate the kind (some banks encode the
            # view as both the kind and a selection dimension), so we don't get "Narrative — Narrative".
            _norm = lambda s: "".join(str(s).lower().split())
            distinct = [p for p in parts if _norm(p) != _norm(kind_label)]
            label = (f"{kind_label} — " + ", ".join(distinct)) if distinct else kind_label
            description = kind_desc or next(
                (dim_codebooks[d][dims[d]].description for d in sorted(dims)
                 if dim_codebooks.get(d, {}).get(dims[d]) and dim_codebooks[d][dims[d]].description),
                "",
            )
            if kind_ord is not None:
                ordinal = kind_ord
        else:
            label = kind_label
            description = kind_desc
            ordinal = kind_ord if kind_ord is not None else 0
        if not description and views_per_set.get(st["record_set_id"]) == 1:
            # Single-kind RecordSet: the RecordSet description is the kind description.
            description = rs_descriptions.get(st["record_set_id"], "")
        views.append(
            {
                "view_key": vkey,
                "record_set_id": st["record_set_id"],
                "kind": st["kind"],
                "kind_label": kind_label,
                "selection_dims": dims,
                "label": label,
                "description": description or "",
                "ordinal": ordinal,
                "coverage_count": st["coverage_count"],
                "max_char": st.get("max_char", 0),
                "media_type": st["media_type"],
                "payload_mode": st["payload_mode"],
                "is_default": False,
                "_default_votes": st["default_votes"],
            }
        )

    # choose a single default view: most is_default reps, else kind match, else first
    if views:
        voted = [v for v in views if v["_default_votes"] > 0]
        if voted:
            default = max(voted, key=lambda v: v["_default_votes"])
        else:
            default = next((v for v in views if local_name(v["kind"]) == default_kind_local), views[0])
        default["is_default"] = True
    for v in views:
        v.pop("_default_votes", None)
    views.sort(key=lambda v: (0 if v["is_default"] else 1, v["ordinal"], v["label"]))
    return views


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Ingest a Croissant Twins bank into the catalog")
    parser.add_argument("bank_dir", nargs="?", help="Path to the bank package (dir containing croissant.json, or its parent)")
    parser.add_argument("--bank-id", default=None, help="Stable bank id (defaults to package dir name)")
    parser.add_argument("--force", action="store_true", help="Re-ingest, replacing an existing version")
    parser.add_argument("--approve", action="store_true", help="Mark the version approved (publish to storefront)")
    parser.add_argument("--reviewer", default="", help="Reviewer identity recorded on approval")
    parser.add_argument("--no-verify", action="store_true", help="Skip sha256 checksum verification")
    parser.add_argument("--limit", type=int, default=None, help="Ingest at most N personas (smoke testing)")
    parser.add_argument("--batch-size", type=int, default=2000)
    parser.add_argument(
        "--payload-store",
        choices=["db", "object"],
        default="db",
        help="Where representation payloads go: 'db' (inline in Postgres) or 'object' (blob store + offset index)",
    )
    parser.add_argument("--approve-version", default=None, help="Approve an already-ingested version by id and exit")
    args = parser.parse_args(argv)

    if args.approve_version:
        catalog.set_version_status(args.approve_version, catalog.STATUS_APPROVED, reviewer=args.reviewer or "cli")
        print(f"Approved version {args.approve_version}")
        return 0

    if not args.bank_dir:
        parser.error("bank_dir is required (unless --approve-version is given)")

    result = ingest_bank(
        args.bank_dir,
        bank_id=args.bank_id,
        force=args.force,
        approve=args.approve,
        reviewer=args.reviewer,
        verify_checksums=not args.no_verify,
        batch_size=args.batch_size,
        limit=args.limit,
        payload_store=args.payload_store,
    )
    print(json.dumps({k: v for k, v in result.items() if k != "views"}, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
