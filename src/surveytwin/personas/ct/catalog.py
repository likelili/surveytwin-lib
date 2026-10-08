"""Durable catalog for Croissant Twins persona banks.

Tables live in the same DB as the V3 repository (SQLite locally, Supabase
Postgres in production) and use ``?`` placeholders via the repository's
``_CompatConnection``. JSON columns are stored as ``TEXT`` so both backends
return strings consistently.

The catalog is written by the offline ingester and read by the live server.
Filtering uses an EAV index and counting/sampling are done in SQL so the
148k / 1M-persona banks never load into the 512 MB web instance.
"""

from __future__ import annotations

import json
import random
import threading
import uuid
from typing import Any, Iterable, Iterator

from ._db import active_db_override, db_connection, db_path_override, utcnow_iso

STATUS_PENDING = "pending"
STATUS_APPROVED = "approved"
STATUS_REJECTED = "rejected"
STATUS_WITHDRAWN = "withdrawn"

_SCHEMA_READY = False
_SCHEMA_LOCK = threading.Lock()

_SCHEMA_STATEMENTS = [
    """
    CREATE TABLE IF NOT EXISTS ct_banks (
        id TEXT PRIMARY KEY,
        name TEXT NOT NULL DEFAULT '',
        publisher TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL DEFAULT ''
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS ct_bank_versions (
        id TEXT PRIMARY KEY,
        bank_id TEXT NOT NULL,
        version TEXT NOT NULL DEFAULT '',
        status TEXT NOT NULL DEFAULT 'pending',
        manifest_json TEXT NOT NULL DEFAULT '{}',
        manifest_sha256 TEXT NOT NULL DEFAULT '',
        card_md TEXT NOT NULL DEFAULT '',
        summary_json TEXT NOT NULL DEFAULT '{}',
        persona_count INTEGER NOT NULL DEFAULT 0,
        representation_count INTEGER NOT NULL DEFAULT 0,
        persona_basis TEXT NOT NULL DEFAULT '',
        default_kind TEXT NOT NULL DEFAULT '',
        conditions_of_access TEXT NOT NULL DEFAULT '',
        license TEXT NOT NULL DEFAULT '',
        source_path TEXT NOT NULL DEFAULT '',
        reviewer TEXT NOT NULL DEFAULT '',
        review_notes TEXT NOT NULL DEFAULT '',
        accepted_warnings_json TEXT NOT NULL DEFAULT '[]',
        reviewed_at TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL DEFAULT '',
        updated_at TEXT NOT NULL DEFAULT '',
        payload_backend TEXT NOT NULL DEFAULT 'db',
        derivable INTEGER NOT NULL DEFAULT 0,
        UNIQUE (bank_id, version)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS ct_bank_views (
        id TEXT PRIMARY KEY,
        version_id TEXT NOT NULL,
        view_key TEXT NOT NULL,
        record_set_id TEXT NOT NULL DEFAULT '',
        kind TEXT NOT NULL DEFAULT '',
        kind_label TEXT NOT NULL DEFAULT '',
        selection_dims_json TEXT NOT NULL DEFAULT '{}',
        label TEXT NOT NULL DEFAULT '',
        description TEXT NOT NULL DEFAULT '',
        is_default INTEGER NOT NULL DEFAULT 0,
        ordinal INTEGER NOT NULL DEFAULT 0,
        coverage_count INTEGER NOT NULL DEFAULT 0,
        media_type TEXT NOT NULL DEFAULT '',
        payload_mode TEXT NOT NULL DEFAULT '',
        max_char INTEGER NOT NULL DEFAULT 0,
        UNIQUE (version_id, view_key)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS ct_bank_filters (
        id TEXT PRIMARY KEY,
        version_id TEXT NOT NULL,
        field_name TEXT NOT NULL,
        label TEXT NOT NULL DEFAULT '',
        semantic_iri TEXT NOT NULL DEFAULT '',
        data_type TEXT NOT NULL DEFAULT '',
        is_numeric INTEGER NOT NULL DEFAULT 0,
        codebook_json TEXT NOT NULL DEFAULT '[]',
        sensitive INTEGER NOT NULL DEFAULT 0,
        ordinal INTEGER NOT NULL DEFAULT 0,
        UNIQUE (version_id, field_name)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS ct_bank_personas (
        version_id TEXT NOT NULL,
        persona_id TEXT NOT NULL,
        attrs_json TEXT NOT NULL DEFAULT '{}',
        PRIMARY KEY (version_id, persona_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS ct_bank_persona_reps (
        version_id TEXT NOT NULL,
        persona_id TEXT NOT NULL,
        reps_json TEXT NOT NULL DEFAULT '{}',
        PRIMARY KEY (version_id, persona_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS ct_bank_persona_values (
        version_id TEXT NOT NULL,
        persona_id TEXT NOT NULL,
        field TEXT NOT NULL,
        value_text TEXT,
        value_num REAL,
        PRIMARY KEY (version_id, persona_id, field)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS ct_bank_representations (
        version_id TEXT NOT NULL,
        representation_id TEXT NOT NULL,
        persona_id TEXT NOT NULL,
        view_key TEXT NOT NULL DEFAULT '',
        kind TEXT NOT NULL DEFAULT '',
        is_default INTEGER NOT NULL DEFAULT 0,
        media_type TEXT NOT NULL DEFAULT '',
        payload_mode TEXT NOT NULL DEFAULT '',
        payload TEXT,
        payload_uri TEXT NOT NULL DEFAULT '',
        payload_offset INTEGER NOT NULL DEFAULT 0,
        payload_len INTEGER NOT NULL DEFAULT 0,
        char_count INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY (version_id, representation_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS ct_installations (
        user_id TEXT NOT NULL,
        bank_id TEXT NOT NULL,
        version_id TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL DEFAULT '',
        PRIMARY KEY (user_id, bank_id)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_ct_versions_bank ON ct_bank_versions(bank_id)",
    "CREATE INDEX IF NOT EXISTS idx_ct_versions_status ON ct_bank_versions(status)",
    "CREATE INDEX IF NOT EXISTS idx_ct_values_field_text ON ct_bank_persona_values(version_id, field, value_text)",
    # Note: no value_num index. Numeric range filtering has no UI yet and the old
    # full float index was 975 MB of never-scanned Postgres (float4 B-trees can't
    # deduplicate). Artifact-backed banks are small enough to scan for ranges.
    "CREATE INDEX IF NOT EXISTS idx_ct_reps_view ON ct_bank_representations(version_id, view_key)",
    "CREATE INDEX IF NOT EXISTS idx_ct_reps_persona ON ct_bank_representations(version_id, persona_id, view_key)",
    "CREATE INDEX IF NOT EXISTS idx_ct_views_version ON ct_bank_views(version_id)",
    "CREATE INDEX IF NOT EXISTS idx_ct_filters_version ON ct_bank_filters(version_id)",
    "CREATE INDEX IF NOT EXISTS idx_ct_install_user ON ct_installations(user_id)",
]


# Columns added after the initial schema; each runs in its own connection so a
# duplicate-column error (already applied) cannot poison the others.
_SCHEMA_MIGRATIONS = [
    "ALTER TABLE ct_bank_views ADD COLUMN max_char INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE ct_bank_versions ADD COLUMN payload_backend TEXT NOT NULL DEFAULT 'db'",
    "ALTER TABLE ct_bank_representations ADD COLUMN payload_uri TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE ct_bank_representations ADD COLUMN payload_offset INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE ct_bank_representations ADD COLUMN payload_len INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE ct_bank_versions ADD COLUMN derivable INTEGER NOT NULL DEFAULT 0",
    # Tier 3: per-bank data lives in an artifact SQLite file in the blob store.
    "ALTER TABLE ct_bank_versions ADD COLUMN artifact_uri TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE ct_bank_versions ADD COLUMN artifact_sha256 TEXT NOT NULL DEFAULT ''",
    # BIGINT: giant-bank artifacts exceed 2^31 bytes (France is ~3.1e9). SQLite
    # ints are 64-bit regardless; the TYPE widen below is Postgres-only and
    # no-ops (fails safe) on SQLite.
    "ALTER TABLE ct_bank_versions ADD COLUMN artifact_bytes BIGINT NOT NULL DEFAULT 0",
    "ALTER TABLE ct_bank_versions ADD COLUMN artifact_chunks INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE ct_bank_versions ALTER COLUMN artifact_bytes TYPE BIGINT",
    # Never-scanned 975 MB float index (see _SCHEMA_STATEMENTS note). One-time drop.
    "DROP INDEX IF EXISTS idx_ct_values_field_num",
]

# Predicate: a representation has a usable payload either inline (db backend) or
# in object storage (object backend, payload_len > 0).
_HAS_PAYLOAD = "((payload IS NOT NULL AND payload <> '') OR payload_len > 0)"


def ensure_schema(force: bool = False) -> None:
    # Artifact DBs (db_path_override active) get their schema explicitly at build
    # time via force=True; serve-time reads on a finished (often read-only)
    # artifact must never attempt DDL, so implicit calls are a no-op there.
    if active_db_override() is not None and not force:
        return
    global _SCHEMA_READY
    if _SCHEMA_READY and not force:
        return
    with _SCHEMA_LOCK:
        if _SCHEMA_READY and not force:
            return
        with db_connection() as conn:
            for stmt in _SCHEMA_STATEMENTS:
                conn.execute(stmt)
        for alter in _SCHEMA_MIGRATIONS:
            try:
                with db_connection() as conn:
                    conn.execute(alter)
            except Exception:
                pass  # already applied
        if active_db_override() is None:
            _SCHEMA_READY = True


def _new_id() -> str:
    return uuid.uuid4().hex


def _to_dict(row: Any) -> dict[str, Any]:
    return dict(row) if row is not None else {}


def _loads(value: Any, default: Any) -> Any:
    if value is None or value == "":
        return default
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value)
    except (json.JSONDecodeError, ValueError, TypeError):
        return default


# ---------------------------------------------------------------------------
# ingest writes
# ---------------------------------------------------------------------------


def upsert_bank(bank_id: str, name: str, publisher: str) -> None:
    ensure_schema()
    now = utcnow_iso()
    with db_connection() as conn:
        conn.execute(
            """
            INSERT INTO ct_banks (id, name, publisher, created_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET name = excluded.name, publisher = excluded.publisher
            """,
            (bank_id, name, publisher, now),
        )


def find_version(bank_id: str, version: str) -> dict[str, Any] | None:
    ensure_schema()
    with db_connection() as conn:
        row = conn.execute(
            "SELECT * FROM ct_bank_versions WHERE bank_id = ? AND version = ?",
            (bank_id, version),
        ).fetchone()
    return _to_dict(row) if row else None


def delete_version_data(version_id: str) -> None:
    """Remove all child rows for a version (used for --force re-ingest)."""
    ensure_schema()
    with db_connection() as conn:
        for table in (
            "ct_bank_views",
            "ct_bank_filters",
            "ct_bank_personas",
            "ct_bank_persona_reps",
            "ct_bank_persona_values",
            "ct_bank_representations",
        ):
            conn.execute(f"DELETE FROM {table} WHERE version_id = ?", (version_id,))


def purge_legacy_data(version_id: str) -> dict[str, int]:
    """Drop a version's *data* rows only (Tier 3 migration cleanup).

    Views/filters/version metadata stay — the storefront needs them; the data
    now lives in the artifact. Only call on versions with ``artifact_uri`` set.
    """
    ensure_schema()
    deleted: dict[str, int] = {}
    with db_connection() as conn:
        for table in (
            "ct_bank_personas",
            "ct_bank_persona_reps",
            "ct_bank_persona_values",
            "ct_bank_representations",
        ):
            cur = conn.execute(f"DELETE FROM {table} WHERE version_id = ?", (version_id,))
            deleted[table] = getattr(cur, "rowcount", -1)
    return deleted


def create_version(
    *,
    bank_id: str,
    version: str,
    manifest_json: str,
    manifest_sha256: str,
    card_md: str,
    summary: dict,
    persona_basis: str,
    default_kind: str,
    conditions_of_access: str,
    license: str,
    source_path: str,
    payload_backend: str = "db",
    derivable: bool = False,
    reuse_version_id: str | None = None,
) -> str:
    ensure_schema()
    now = utcnow_iso()
    version_id = reuse_version_id or _new_id()
    with db_connection() as conn:
        if reuse_version_id:
            conn.execute("DELETE FROM ct_bank_versions WHERE id = ?", (reuse_version_id,))
        conn.execute(
            """
            INSERT INTO ct_bank_versions (
                id, bank_id, version, status, manifest_json, manifest_sha256, card_md,
                summary_json, persona_count, representation_count, persona_basis, default_kind,
                conditions_of_access, license, source_path, created_at, updated_at, payload_backend, derivable
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, 0, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                version_id, bank_id, version, STATUS_PENDING, manifest_json, manifest_sha256, card_md,
                json.dumps(summary, ensure_ascii=False), persona_basis, default_kind,
                conditions_of_access, license, source_path, now, now, payload_backend, 1 if derivable else 0,
            ),
        )
    return version_id


def insert_filters(version_id: str, filters: list[dict]) -> None:
    rows = [
        (
            _new_id(), version_id, f["field_name"], f.get("label", ""), f.get("semantic_iri", "") or "",
            f.get("data_type", "") or "", 1 if f.get("is_numeric") else 0,
            json.dumps(f.get("codebook", []), ensure_ascii=False), 1 if f.get("sensitive") else 0, idx,
        )
        for idx, f in enumerate(filters)
    ]
    if not rows:
        return
    with db_connection() as conn:
        conn.executemany(
            """
            INSERT INTO ct_bank_filters
              (id, version_id, field_name, label, semantic_iri, data_type, is_numeric, codebook_json, sensitive, ordinal)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            rows,
        )


def upsert_views(version_id: str, views: list[dict]) -> None:
    rows = [
        (
            _new_id(), version_id, v["view_key"], v.get("record_set_id", ""), v.get("kind", ""),
            v.get("kind_label", ""), json.dumps(v.get("selection_dims", {}), ensure_ascii=False),
            v.get("label", ""), v.get("description", ""), 1 if v.get("is_default") else 0,
            int(v.get("ordinal", 0)), int(v.get("coverage_count", 0)), v.get("media_type", ""),
            v.get("payload_mode", ""), int(v.get("max_char", 0)),
        )
        for v in views
    ]
    if not rows:
        return
    with db_connection() as conn:
        conn.executemany(
            """
            INSERT INTO ct_bank_views
              (id, version_id, view_key, record_set_id, kind, kind_label, selection_dims_json,
               label, description, is_default, ordinal, coverage_count, media_type, payload_mode, max_char)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(version_id, view_key) DO UPDATE SET
               coverage_count = excluded.coverage_count, is_default = excluded.is_default,
               label = excluded.label, description = excluded.description, ordinal = excluded.ordinal,
               kind_label = excluded.kind_label, media_type = excluded.media_type, max_char = excluded.max_char
            """,
            rows,
        )


def insert_personas_batch(version_id: str, batch: list[tuple[str, str]]) -> None:
    """batch of (persona_id, attrs_json)."""
    if not batch:
        return
    rows = [(version_id, pid, attrs) for pid, attrs in batch]
    with db_connection() as conn:
        conn.executemany(
            "INSERT INTO ct_bank_personas (version_id, persona_id, attrs_json) VALUES (?, ?, ?) "
            "ON CONFLICT(version_id, persona_id) DO UPDATE SET attrs_json = excluded.attrs_json",
            rows,
        )


def insert_persona_values_batch(version_id: str, batch: list[tuple[str, str, str | None, float | None]]) -> None:
    """batch of (persona_id, field, value_text, value_num)."""
    if not batch:
        return
    rows = [(version_id, pid, field, vt, vn) for pid, field, vt, vn in batch]
    with db_connection() as conn:
        conn.executemany(
            "INSERT INTO ct_bank_persona_values (version_id, persona_id, field, value_text, value_num) "
            "VALUES (?, ?, ?, ?, ?) ON CONFLICT(version_id, persona_id, field) DO UPDATE SET "
            "value_text = excluded.value_text, value_num = excluded.value_num",
            rows,
        )


def insert_representations_batch(version_id: str, batch: list[dict]) -> None:
    if not batch:
        return
    rows = [
        (
            version_id, r["representation_id"], r["persona_id"], r.get("view_key", ""), r.get("kind", ""),
            1 if r.get("is_default") else 0, r.get("media_type", "") or "", r.get("payload_mode", "") or "",
            r.get("payload"), r.get("payload_uri", "") or "", int(r.get("payload_offset", 0) or 0),
            int(r.get("payload_len", 0) or 0), int(r.get("char_count", 0) or 0),
        )
        for r in batch
    ]
    with db_connection() as conn:
        conn.executemany(
            """
            INSERT INTO ct_bank_representations
              (version_id, representation_id, persona_id, view_key, kind, is_default, media_type, payload_mode,
               payload, payload_uri, payload_offset, payload_len, char_count)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(version_id, representation_id) DO UPDATE SET
              persona_id = excluded.persona_id, view_key = excluded.view_key, kind = excluded.kind,
              is_default = excluded.is_default, media_type = excluded.media_type,
              payload_mode = excluded.payload_mode, payload = excluded.payload,
              payload_uri = excluded.payload_uri, payload_offset = excluded.payload_offset,
              payload_len = excluded.payload_len, char_count = excluded.char_count
            """,
            rows,
        )


def finalize_counts(version_id: str, persona_count: int, representation_count: int) -> None:
    with db_connection() as conn:
        conn.execute(
            "UPDATE ct_bank_versions SET persona_count = ?, representation_count = ?, updated_at = ? WHERE id = ?",
            (persona_count, representation_count, utcnow_iso(), version_id),
        )


def set_version_status(
    version_id: str,
    status: str,
    *,
    reviewer: str = "",
    review_notes: str = "",
    accepted_warnings: list | None = None,
) -> None:
    with db_connection() as conn:
        conn.execute(
            """
            UPDATE ct_bank_versions SET status = ?, reviewer = ?, review_notes = ?,
              accepted_warnings_json = ?, reviewed_at = ?, updated_at = ? WHERE id = ?
            """,
            (
                status, reviewer, review_notes, json.dumps(accepted_warnings or [], ensure_ascii=False),
                utcnow_iso(), utcnow_iso(), version_id,
            ),
        )


def update_version_summary(version_id: str, patch: dict[str, Any]) -> dict[str, Any]:
    """Merge keys into a version's summary_json (ingest enrichment + display backfills)."""
    ensure_schema()
    with db_connection() as conn:
        row = conn.execute(
            "SELECT summary_json FROM ct_bank_versions WHERE id = ?", (version_id,)
        ).fetchone()
        if not row:
            raise KeyError(f"version {version_id} not found")
        summary = _loads(_to_dict(row).get("summary_json"), {}) or {}
        summary.update(patch)
        conn.execute(
            "UPDATE ct_bank_versions SET summary_json = ?, updated_at = ? WHERE id = ?",
            (json.dumps(summary, ensure_ascii=False), utcnow_iso(), version_id),
        )
    return summary


# ---------------------------------------------------------------------------
# serve-time reads
# ---------------------------------------------------------------------------


def get_version(version_id: str) -> dict[str, Any] | None:
    ensure_schema()
    with db_connection() as conn:
        row = conn.execute("SELECT * FROM ct_bank_versions WHERE id = ?", (version_id,)).fetchone()
    return _to_dict(row) if row else None


def latest_approved_version(bank_id: str) -> dict[str, Any] | None:
    ensure_schema()
    with db_connection() as conn:
        row = conn.execute(
            "SELECT * FROM ct_bank_versions WHERE bank_id = ? AND status = ? ORDER BY created_at DESC",
            (bank_id, STATUS_APPROVED),
        ).fetchone()
    return _to_dict(row) if row else None


def list_approved_banks() -> list[dict[str, Any]]:
    """One row per bank: its latest approved version."""
    ensure_schema()
    with db_connection() as conn:
        rows = conn.execute(
            """
            SELECT v.*, b.name AS bank_name, b.publisher AS bank_publisher
            FROM ct_bank_versions v JOIN ct_banks b ON b.id = v.bank_id
            WHERE v.status = ? ORDER BY v.created_at DESC
            """,
            (STATUS_APPROVED,),
        ).fetchall()
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for row in rows:
        d = _to_dict(row)
        if d["bank_id"] in seen:
            continue
        seen.add(d["bank_id"])
        out.append(d)
    return out


def list_all_versions() -> list[dict[str, Any]]:
    ensure_schema()
    with db_connection() as conn:
        rows = conn.execute(
            """
            SELECT v.*, b.name AS bank_name, b.publisher AS bank_publisher
            FROM ct_bank_versions v JOIN ct_banks b ON b.id = v.bank_id
            ORDER BY v.created_at DESC
            """
        ).fetchall()
    return [_to_dict(r) for r in rows]


def get_views(version_id: str) -> list[dict[str, Any]]:
    ensure_schema()
    with db_connection() as conn:
        rows = conn.execute(
            "SELECT * FROM ct_bank_views WHERE version_id = ? ORDER BY ordinal, label",
            (version_id,),
        ).fetchall()
    return [_to_dict(r) for r in rows]


def get_view(version_id: str, view_key: str) -> dict[str, Any] | None:
    ensure_schema()
    with db_connection() as conn:
        row = conn.execute(
            "SELECT * FROM ct_bank_views WHERE version_id = ? AND view_key = ?",
            (version_id, view_key),
        ).fetchone()
    return _to_dict(row) if row else None


def get_filters(version_id: str) -> list[dict[str, Any]]:
    ensure_schema()
    with db_connection() as conn:
        rows = conn.execute(
            "SELECT * FROM ct_bank_filters WHERE version_id = ? ORDER BY ordinal, field_name",
            (version_id,),
        ).fetchall()
    out = []
    for r in rows:
        d = _to_dict(r)
        d["codebook"] = _loads(d.pop("codebook_json", "[]"), [])
        out.append(d)
    return out


def distinct_filter_values(version_id: str, field: str, limit: int = 100) -> list[tuple[str, int]]:
    """Distinct values (with counts) loaded from an authoritative Persona filter field.

    This is CT 12.1 within-bank filtering: values come from the loaded field data, not
    from a codebook (codebooks are for ambiguous codes / cross-bank labels). Ordered by
    frequency and capped so a high-cardinality field can't flood the filter UI.
    """
    ensure_schema()
    with db_connection() as conn:
        rows = conn.execute(
            "SELECT value_text, COUNT(*) AS c FROM ct_bank_persona_values "
            "WHERE version_id = ? AND field = ? AND value_text IS NOT NULL AND value_text <> '' "
            "GROUP BY value_text ORDER BY c DESC, value_text LIMIT ?",
            (version_id, field, limit),
        ).fetchall()
    return [(_to_dict(r)["value_text"], int(_to_dict(r).get("c", 0))) for r in rows]


def default_view(version_id: str) -> dict[str, Any] | None:
    views = get_views(version_id)
    if not views:
        return None
    for v in views:
        if v.get("is_default"):
            return v
    return views[0]


# ---- filtering / counting / sampling ----


def _normalize_clause(clause: Any) -> dict[str, Any]:
    if isinstance(clause, dict) and "op" in clause:
        op = str(clause["op"]).lower()
        if op == "in":
            return {"op": "in", "values": [str(v) for v in (clause.get("values") or [])]}
        if op == "range":
            return {"op": "range", "gte": clause.get("gte"), "lte": clause.get("lte")}
        if op == "is_missing":
            return {"op": "is_missing", "value": bool(clause.get("value", True))}
    if isinstance(clause, list):
        return {"op": "in", "values": [str(v) for v in clause if str(v).strip()]}
    if isinstance(clause, str) and clause.strip():
        return {"op": "in", "values": [clause.strip()]}
    return {"op": "noop"}


def _in_placeholders(n: int) -> str:
    return "(" + ",".join(["?"] * n) + ")"


def _matching_persona_sql(version_id: str, filters: dict[str, Any]) -> tuple[str, list[Any]]:
    positive: list[tuple[str, dict]] = []
    missing_fields: list[str] = []
    for field, clause in (filters or {}).items():
        norm = _normalize_clause(clause)
        if norm["op"] == "in" and norm["values"]:
            positive.append((field, norm))
        elif norm["op"] == "range" and (norm["gte"] is not None or norm["lte"] is not None):
            positive.append((field, norm))
        elif norm["op"] == "is_missing" and norm["value"]:
            missing_fields.append(field)

    if not positive:
        sql = "SELECT persona_id FROM ct_bank_personas WHERE version_id = ?"
        params: list[Any] = [version_id]
    else:
        ors: list[str] = []
        params = [version_id]
        for field, norm in positive:
            if norm["op"] == "in":
                vals = norm["values"]
                ors.append(f"(field = ? AND value_text IN {_in_placeholders(len(vals))})")
                params.append(field)
                params.extend(vals)
            else:  # range
                sub = ["field = ?"]
                params.append(field)
                if norm["gte"] is not None:
                    sub.append("value_num >= ?")
                    params.append(float(norm["gte"]))
                if norm["lte"] is not None:
                    sub.append("value_num <= ?")
                    params.append(float(norm["lte"]))
                ors.append("(" + " AND ".join(sub) + ")")
        sql = (
            "SELECT persona_id FROM ct_bank_persona_values WHERE version_id = ? AND ("
            + " OR ".join(ors)
            + ") GROUP BY persona_id HAVING COUNT(DISTINCT field) = ?"
        )
        params.append(len(positive))

    for field in missing_fields:
        sql = (
            f"SELECT persona_id FROM ({sql}) AS m WHERE persona_id NOT IN "
            "(SELECT persona_id FROM ct_bank_persona_values WHERE version_id = ? AND field = ?)"
        )
        params.extend([version_id, field])
    return sql, params


def _eligible_sql(
    version_id: str, view_key: str, filters: dict[str, Any], *, derivable: bool = False
) -> tuple[str, list[Any]]:
    match_sql, match_params = _matching_persona_sql(version_id, filters)
    if derivable:
        # Dense bank: every matching persona has every view (coverage asserted at ingest),
        # so eligibility is just the filter match — no per-representation join needed.
        return match_sql, list(match_params)
    sql = (
        f"SELECT persona_id FROM ({match_sql}) AS mm "
        "INTERSECT SELECT persona_id FROM ct_bank_representations "
        f"WHERE version_id = ? AND view_key = ? AND {_HAS_PAYLOAD}"
    )
    params = list(match_params) + [version_id, view_key]
    return sql, params


def count_total_for_view(version_id: str, view_key: str, *, derivable: bool = False) -> int:
    ensure_schema()
    with db_connection() as conn:
        if derivable:
            row = conn.execute(
                "SELECT COUNT(*) AS c FROM ct_bank_personas WHERE version_id = ?", (version_id,)
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT COUNT(DISTINCT persona_id) AS c FROM ct_bank_representations "
                f"WHERE version_id = ? AND view_key = ? AND {_HAS_PAYLOAD}",
                (version_id, view_key),
            ).fetchone()
    return int(_to_dict(row).get("c", 0))


def count_matching(version_id: str, view_key: str, filters: dict[str, Any], *, derivable: bool = False) -> int:
    ensure_schema()
    sql, params = _eligible_sql(version_id, view_key, filters, derivable=derivable)
    with db_connection() as conn:
        row = conn.execute(f"SELECT COUNT(*) AS c FROM ({sql}) AS e", params).fetchone()
    return int(_to_dict(row).get("c", 0))


def list_matching_persona_ids(
    version_id: str, view_key: str, filters: dict[str, Any], *,
    offset: int = 0, limit: int = 10, derivable: bool = False
) -> list[str]:
    """Stable page of eligible persona ids (ordered by persona_id) for browsing UIs.

    LIMIT/OFFSET over the indexed eligible-set subquery keeps memory bounded on
    giant banks; the deterministic ordering makes pages reproducible.
    """
    ensure_schema()
    if limit <= 0:
        return []
    sql, params = _eligible_sql(version_id, view_key, filters, derivable=derivable)
    sql = f"SELECT persona_id FROM ({sql}) AS e ORDER BY persona_id LIMIT ? OFFSET ?"
    with db_connection() as conn:
        rows = conn.execute(sql, [*params, int(limit), max(0, int(offset))]).fetchall()
    return [str(_to_dict(row)["persona_id"]) for row in rows]


def sample_persona_ids(
    version_id: str, view_key: str, filters: dict[str, Any], sample_size: int, *,
    seed: int = 42, derivable: bool = False
) -> list[str]:
    """Reservoir-sample eligible persona ids in a single streamed pass (bounded memory)."""
    ensure_schema()
    if sample_size <= 0:
        return []
    sql, params = _eligible_sql(version_id, view_key, filters, derivable=derivable)
    sql = f"SELECT persona_id FROM ({sql}) AS e ORDER BY persona_id"
    rng = random.Random(seed)
    reservoir: list[str] = []
    with db_connection() as conn:
        cursor = conn.execute(sql, params)
        i = 0
        while True:
            chunk = cursor.fetchmany(5000)
            if not chunk:
                break
            for row in chunk:
                pid = _to_dict(row)["persona_id"]
                if i < sample_size:
                    reservoir.append(pid)
                else:
                    j = rng.randint(0, i)
                    if j < sample_size:
                        reservoir[j] = pid
                i += 1
    return reservoir


def insert_persona_reps_batch(version_id: str, batch: list[tuple[str, str]]) -> None:
    """Derivable banks store one compact row per persona: (persona_id, reps_json).

    reps_json maps view_key -> {u: payload_uri, o: offset, l: len, c: char_count, m: media_type, k: kind}.
    This replaces the per-representation rows, cutting N-reps-per-persona to a single row.
    """
    if not batch:
        return
    rows = [(version_id, pid, reps_json) for pid, reps_json in batch]
    with db_connection() as conn:
        conn.executemany(
            "INSERT INTO ct_bank_persona_reps (version_id, persona_id, reps_json) VALUES (?, ?, ?) "
            "ON CONFLICT(version_id, persona_id) DO UPDATE SET reps_json = excluded.reps_json",
            rows,
        )


def get_representation(
    version_id: str, persona_id: str, view_key: str, *, derivable: bool = False
) -> dict[str, Any] | None:
    ensure_schema()
    if derivable:
        with db_connection() as conn:
            row = conn.execute(
                "SELECT reps_json FROM ct_bank_persona_reps WHERE version_id = ? AND persona_id = ?",
                (version_id, persona_id),
            ).fetchone()
        if not row:
            return None
        entry = _loads(_to_dict(row).get("reps_json", "{}"), {}).get(view_key)
        if not entry:
            return None
        shard, off, ln, char = (list(entry) + [0, 0, 0, 0])[:4]
        from . import payload_store as _ps

        view = get_view(version_id, view_key) or {}
        return {
            "representation_id": f"{persona_id}:{view_key}",
            "persona_id": persona_id,
            "kind": view.get("kind") or "",
            "media_type": view.get("media_type") or "",
            "payload_mode": "text",
            "payload": None,
            "payload_uri": _ps.shard_key(version_id, int(shard)),
            "payload_offset": int(off),
            "payload_len": int(ln),
            "char_count": int(char),
        }
    with db_connection() as conn:
        row = conn.execute(
            "SELECT representation_id, persona_id, kind, media_type, payload_mode, payload, "
            "payload_uri, payload_offset, payload_len, char_count "
            "FROM ct_bank_representations WHERE version_id = ? AND persona_id = ? AND view_key = ?",
            (version_id, persona_id, view_key),
        ).fetchone()
    return _to_dict(row) if row else None


def max_char_count(version_id: str, view_key: str) -> int:
    ensure_schema()
    with db_connection() as conn:
        row = conn.execute(
            "SELECT MAX(char_count) AS m FROM ct_bank_representations WHERE version_id = ? AND view_key = ?",
            (version_id, view_key),
        ).fetchone()
    return int(_to_dict(row).get("m") or 0)


def get_persona_attrs(version_id: str, persona_id: str) -> dict[str, Any]:
    ensure_schema()
    with db_connection() as conn:
        row = conn.execute(
            "SELECT attrs_json FROM ct_bank_personas WHERE version_id = ? AND persona_id = ?",
            (version_id, persona_id),
        ).fetchone()
    if not row:
        return {}
    return _loads(_to_dict(row).get("attrs_json", "{}"), {})


def persona_ids_present(version_id: str, persona_ids: list[str]) -> set[str]:
    """Subset of ``persona_ids`` that exist in this bank version.

    Distinguishes "not part of this bank" from "exists but has empty attrs", which
    ``get_persona_attrs`` alone cannot (it returns {} for both).
    """
    ensure_schema()
    ids = [str(p).strip() for p in persona_ids if str(p).strip()]
    if not ids:
        return set()
    placeholders = ",".join("?" for _ in ids)
    with db_connection() as conn:
        rows = conn.execute(
            f"SELECT persona_id FROM ct_bank_personas WHERE version_id = ? AND persona_id IN ({placeholders})",
            (version_id, *ids),
        ).fetchall()
    return {str(_to_dict(r)["persona_id"]) for r in rows}


# ---- installations (favourites) ----


def install_bank(user_id: str, bank_id: str, version_id: str) -> None:
    ensure_schema()
    with db_connection() as conn:
        conn.execute(
            """
            INSERT INTO ct_installations (user_id, bank_id, version_id, created_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(user_id, bank_id) DO UPDATE SET version_id = excluded.version_id
            """,
            (user_id, bank_id, version_id, utcnow_iso()),
        )


def uninstall_bank(user_id: str, bank_id: str) -> None:
    ensure_schema()
    with db_connection() as conn:
        conn.execute("DELETE FROM ct_installations WHERE user_id = ? AND bank_id = ?", (user_id, bank_id))


def list_installed_bank_ids(user_id: str) -> set[str]:
    ensure_schema()
    with db_connection() as conn:
        rows = conn.execute(
            "SELECT bank_id FROM ct_installations WHERE user_id = ?", (user_id,)
        ).fetchall()
    return {_to_dict(r)["bank_id"] for r in rows}


def is_installed(user_id: str, bank_id: str) -> bool:
    return bank_id in list_installed_bank_ids(user_id)


# ---------------------------------------------------------------------------
# artifact support (Tier 3)
#
# A bank "artifact" is a standalone SQLite file with this exact schema holding
# one bank version's rows. It is built at ingest, stored in the blob store, and
# opened locally at serve time via ``bank_db``. The durable catalog keeps only
# the small metadata tables (versions/views/filters) plus artifact pointers.
# ---------------------------------------------------------------------------


def bank_db(path: str | None, *, readonly: bool = True):
    """Context manager routing catalog reads/writes to a bank artifact file."""
    return db_path_override(path, readonly=readonly)


_VERSION_COLS = (
    "id, bank_id, version, status, manifest_json, manifest_sha256, card_md, summary_json, "
    "persona_count, representation_count, persona_basis, default_kind, conditions_of_access, "
    "license, source_path, reviewer, review_notes, accepted_warnings_json, reviewed_at, "
    "created_at, updated_at, payload_backend, derivable, artifact_uri, artifact_sha256, "
    "artifact_bytes, artifact_chunks"
)
_VIEW_COLS = (
    "id, version_id, view_key, record_set_id, kind, kind_label, selection_dims_json, label, "
    "description, is_default, ordinal, coverage_count, media_type, payload_mode, max_char"
)
_FILTER_COLS = (
    "id, version_id, field_name, label, semantic_iri, data_type, is_numeric, codebook_json, "
    "sensitive, ordinal"
)


def set_artifact_info(version_id: str, *, uri: str, sha256: str, size_bytes: int, chunks: int) -> None:
    with db_connection() as conn:
        conn.execute(
            "UPDATE ct_bank_versions SET artifact_uri = ?, artifact_sha256 = ?, "
            "artifact_bytes = ?, artifact_chunks = ?, updated_at = ? WHERE id = ?",
            (uri, sha256, int(size_bytes), int(chunks), utcnow_iso(), version_id),
        )


def read_artifact_bundle() -> dict[str, Any]:
    """Read the (single) version + views + filters + bank rows from the active DB.

    Call inside ``bank_db(path)``. Raises if the artifact holds != 1 version.
    """
    with db_connection() as conn:
        versions = [dict(r) for r in conn.execute(f"SELECT {_VERSION_COLS} FROM ct_bank_versions").fetchall()]
        if len(versions) != 1:
            raise ValueError(f"artifact must hold exactly one bank version, found {len(versions)}")
        version = versions[0]
        views = [dict(r) for r in conn.execute(
            f"SELECT {_VIEW_COLS} FROM ct_bank_views WHERE version_id = ?", (version["id"],)
        ).fetchall()]
        filters = [dict(r) for r in conn.execute(
            f"SELECT {_FILTER_COLS} FROM ct_bank_filters WHERE version_id = ?", (version["id"],)
        ).fetchall()]
        bank = conn.execute(
            "SELECT id, name, publisher, created_at FROM ct_banks WHERE id = ?", (version["bank_id"],)
        ).fetchone()
    return {"version": version, "views": views, "filters": filters, "bank": _to_dict(bank)}


def publish_bundle_to_durable(bundle: dict[str, Any]) -> None:
    """Write a bank's metadata bundle into the durable catalog (idempotent).

    Replaces any existing rows for the same version id. Bank data rows
    (personas/values/reps) intentionally stay out of the durable DB — they live
    in the artifact.
    """
    ensure_schema()
    version = bundle["version"]
    bank = bundle.get("bank") or {}
    upsert_bank(version["bank_id"], bank.get("name") or "", bank.get("publisher") or "")
    placeholders = lambda cols: ",".join("?" for _ in cols.split(","))  # noqa: E731
    with db_connection() as conn:
        conn.execute("DELETE FROM ct_bank_versions WHERE id = ?", (version["id"],))
        conn.execute("DELETE FROM ct_bank_views WHERE version_id = ?", (version["id"],))
        conn.execute("DELETE FROM ct_bank_filters WHERE version_id = ?", (version["id"],))
        conn.execute(
            f"INSERT INTO ct_bank_versions ({_VERSION_COLS}) VALUES ({placeholders(_VERSION_COLS)})",
            [version.get(c.strip()) for c in _VERSION_COLS.split(",")],
        )
        for row in bundle.get("views") or []:
            conn.execute(
                f"INSERT INTO ct_bank_views ({_VIEW_COLS}) VALUES ({placeholders(_VIEW_COLS)})",
                [row.get(c.strip()) for c in _VIEW_COLS.split(",")],
            )
        for row in bundle.get("filters") or []:
            conn.execute(
                f"INSERT INTO ct_bank_filters ({_FILTER_COLS}) VALUES ({placeholders(_FILTER_COLS)})",
                [row.get(c.strip()) for c in _FILTER_COLS.split(",")],
            )


def finalize_artifact_file(path: str) -> None:
    """Compact a freshly built artifact so it ships as one clean file."""
    import sqlite3 as _sq

    conn = _sq.connect(path)
    try:
        conn.execute("PRAGMA journal_mode=DELETE")
        conn.execute("ANALYZE")
        conn.execute("VACUUM")
        conn.commit()
    finally:
        conn.close()
