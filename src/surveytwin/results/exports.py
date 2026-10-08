"""Export file builders, ported from the website's ``services/v3/tools/export_tool.py``.

The CSV builders are the website's. Where each answer goes (columns, their
descriptions and ImportIds, option values) comes from the survey definition
(``results.layout``), where the website read its survey template; the files are
the same.

Original notes follow.

Export bundle tool for V3 orchestration.

Produces a single responses.csv matching the surveytwin format:
  Row 0: Column descriptions (question text for question cols, empty for others)
  Row 1: ImportId JSON metadata
  Row 2+: Data rows, one per persona
  Columns: TWIN_ID, demo_*, embedded-data columns, question columns
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Callable

from datetime import datetime, timezone

logger = logging.getLogger(__name__)


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()

_QID_SUFFIX_RE = re.compile(r"(QID\d+(?:#\d+)?(?:_\d+)*)$")
_EMBEDDED_FIELD_RE = re.compile(r"\$\{e://Field/([^}]+)\}")


def _natural_question_sort_key(question_id: str) -> tuple[int, str]:
    match = re.search(r"(\d+)", question_id)
    if not match:
        return (10**9, question_id)
    return (int(match.group(1)), question_id)


def _question_lookup_keys(question_id: str) -> list[str]:
    cleaned = str(question_id).strip()
    if not cleaned:
        return []
    keys = [cleaned]
    match = _QID_SUFFIX_RE.search(cleaned)
    if match:
        base = match.group(1)
        if base not in keys:
            keys.append(base)
    return keys


def _normalize_public_text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


# Flagged statuses whose CODED answer is deliberately blank UNLESS a repair was
# accepted. answer_raw may hold the verbatim (kept for out_of_range), but it must
# NOT leak into the values/labels CSVs. Per PR-G item 3, the export is a snapshot
# of the CURRENT data: a cell with an accepted answer_repaired shows the repaired
# value (and responses_status.csv marks it 'repaired'); an unrepaired flagged cell
# stays blank. The verbatim + original flag remain in long.csv / the DB for audit.
_FLAGGED_EXPORT_STATUSES = frozenset({"out_of_range", "skipped", "needs_validation"})


def _is_flagged_answer(row: dict[str, Any]) -> bool:
    return str(row.get("answer_status") or "").strip() in _FLAGGED_EXPORT_STATUSES


def _resolve_label_answer(row: dict[str, Any], *, use_repaired: bool = True) -> str:
    # use_repaired=True  -> current/fixed snapshot: an accepted Layer-2 repair/rerun
    #   is coalesced over the original answer.
    # use_repaired=False -> the ORIGINAL first-simulation answer: the Layer-1
    #   deterministic-match result (no fuzzy), ignoring any later repair/rerun; a
    #   flagged cell stays blank exactly as it did at simulation time. Backs *_raw.csv.
    if use_repaired:
        repaired = str(row.get("answer_repaired") or "").strip()
        if repaired:
            return repaired
    if _is_flagged_answer(row):
        return ""
    return str(row.get("answer_label") or row.get("answer_raw") or row.get("answer_value") or "")


# Alias kept for the (now-identical) responses_repaired.csv column.
_resolve_repaired_answer = _resolve_label_answer


def _embedded_data_for_row(row: dict[str, Any]) -> dict[str, Any]:
    embedded = _parse_json_blob(row.get("embedded_data_json") or row.get("embedded_data"), {})
    return embedded if isinstance(embedded, dict) else {}


def _render_embedded_field_tokens(value: str, embedded_data: dict[str, Any]) -> str:
    def replace(match: re.Match[str]) -> str:
        field_name = match.group(1).strip()
        if field_name in embedded_data:
            return str(embedded_data[field_name])
        return match.group(0)

    return _EMBEDDED_FIELD_RE.sub(replace, value)


def _resolve_value_answer(
    row: dict[str, Any],
    *,
    question_value_maps: dict[str, dict[str, str]],
    use_repaired: bool = True,
) -> str:
    # Current-snapshot value: an accepted repair maps like any label; an
    # unrepaired flagged cell stays blank (never leak the verbatim answer_raw).
    # use_repaired=False gives the ORIGINAL first-simulation value (ignores any
    # repair/rerun) — the Layer-1 match result, flagged cells blank. Backs *_raw.csv.
    repaired = str(row.get("answer_repaired") or "").strip() if use_repaired else ""
    if not repaired and _is_flagged_answer(row):
        return ""
    code = str(row.get("answer_code") or "").strip()
    if code and not repaired:
        # the code recorded from the options the twin was shown
        return code
    question_id = str(row.get("question_id", "")).strip()
    label = repaired or str(row.get("answer_label") or row.get("answer_raw") or "").strip()
    embedded_data = _embedded_data_for_row(row)
    for key in _question_lookup_keys(question_id):
        mapping = question_value_maps.get(key)
        if not mapping or not label:
            continue
        mapped = mapping.get(label)
        if mapped is not None and str(mapped).strip():
            return str(mapped)
        normalized_label = _normalize_public_text(label)
        for option_label, option_value in mapping.items():
            rendered_label = _normalize_public_text(
                _render_embedded_field_tokens(option_label, embedded_data)
            )
            if rendered_label and rendered_label == normalized_label and str(option_value).strip():
                return str(option_value)
    answer_value = str(row.get("answer_value") or "").strip()
    if answer_value:
        return answer_value
    # No value map resolved: fall back to the best label candidate (which already
    # coalesces an accepted repair), not the verbatim answer_raw.
    return label or str(row.get("answer_label") or "")


def _parse_json_blob(value: Any, fallback: Any) -> Any:
    if isinstance(value, (dict, list)):
        return value
    if not value:
        return fallback
    try:
        return json.loads(str(value))
    except Exception:
        return fallback


def _stringify_export_value(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (dict, list)):
        try:
            return json.dumps(value, ensure_ascii=False)
        except Exception:
            return str(value)
    return str(value)


def _collect_embedded_data_by_persona(
    long_rows: list[dict[str, Any]],
) -> tuple[dict[str, dict[str, str]], list[str]]:
    by_persona: dict[str, dict[str, str]] = {}
    embedded_columns: list[str] = []
    seen_columns: set[str] = set()

    for row in long_rows:
        persona_id = str(row.get("persona_id", "")).strip()
        if not persona_id:
            continue
        embedded_payload = _parse_json_blob(row.get("embedded_data_json"), {})
        if not isinstance(embedded_payload, dict):
            continue
        target = by_persona.setdefault(persona_id, {})
        for raw_key, raw_value in embedded_payload.items():
            key = str(raw_key).strip()
            if not key:
                continue
            if key not in seen_columns:
                seen_columns.add(key)
                embedded_columns.append(key)
            target[key] = _stringify_export_value(raw_value)

    return by_persona, embedded_columns


def _group_rows_by_persona(long_rows: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in long_rows:
        persona_id = str(row.get("persona_id", "")).strip()
        if not persona_id:
            continue
        grouped.setdefault(persona_id, []).append(row)
    for persona_id, rows in grouped.items():
        grouped[persona_id] = sorted(
            rows,
            key=lambda item: _natural_question_sort_key(str(item.get("question_id", ""))),
        )
    return grouped


def _append_multiline_field(lines: list[str], label: str, value: str) -> None:
    cleaned = str(value or "").strip()
    if not cleaned:
        return
    if "\n" not in cleaned:
        lines.append(f"{label}: {cleaned}")
        return
    lines.append(f"{label}:")
    lines.extend(cleaned.splitlines())


def _build_questions_text(persona_id: str, rows: list[dict[str, Any]]) -> str:
    lines = [
        f"Participant ID: {persona_id}",
        f"Question Count: {len(rows)}",
        "",
    ]
    for idx, row in enumerate(rows, start=1):
        question_id = str(row.get("question_id", "")).strip() or f"Q{idx}"
        question_text = str(row.get("question_text", "")).strip() or question_id
        question_type = str(row.get("question_type", "")).strip()
        lines.append(f"{idx}. [{question_id}] {question_text}")
        if question_type:
            lines.append(f"Type: {question_type}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _build_answers_text(persona_id: str, rows: list[dict[str, Any]]) -> str:
    lines = [
        f"Participant ID: {persona_id}",
        f"Answer Count: {len(rows)}",
        "",
    ]
    for idx, row in enumerate(rows, start=1):
        question_id = str(row.get("question_id", "")).strip() or f"Q{idx}"
        question_text = str(row.get("question_text", "")).strip() or question_id
        question_type = str(row.get("question_type", "")).strip()
        answer_raw = str(row.get("answer_raw", "")).strip()
        answer_label = str(row.get("answer_label", "")).strip()
        answer_value = str(row.get("answer_value", "")).strip()
        lines.append(f"{idx}. [{question_id}] {question_text}")
        if question_type:
            lines.append(f"Type: {question_type}")
        primary_answer = answer_raw or answer_label or answer_value
        _append_multiline_field(lines, "Answer", primary_answer)
        if answer_label and answer_label != primary_answer:
            _append_multiline_field(lines, "Answer Label", answer_label)
        if answer_value and answer_value not in {primary_answer, answer_label}:
            _append_multiline_field(lines, "Answer Value", answer_value)
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _build_surveytwin_csv(
    long_rows: list[dict[str, Any]],
    persona_demographics: dict[str, dict[str, Any]],
    embedded_data_by_persona: dict[str, dict[str, str]],
    embedded_data_cols: list[str],
    *,
    answer_resolver: Callable[[dict[str, Any]], str],
) -> tuple[
    list[str],
    list[dict[str, str]],
    list[dict[str, str]],
    list[dict[str, str]],
    list[str],
    list[str],
    list[str],
]:
    """Build surveytwin-compatible CSV data with 2 metadata header rows + data rows.

    Returns (
        header,
        description_row,
        import_id_row,
        data_rows,
        persona_attr_cols,
        embedded_data_cols,
        question_cols,
    ).
    """
    question_ids: list[str] = []
    question_id_set: set[str] = set()
    question_text_by_id: dict[str, str] = {}
    persona_rows: dict[str, dict[str, str]] = {}
    persona_attr_keys: set[str] = set()

    for demographics in persona_demographics.values():
        for key in demographics.keys():
            normalized = f"demo_{key}"
            persona_attr_keys.add(normalized)

    for row in long_rows:
        persona_id = str(row.get("persona_id", ""))
        qid = str(row.get("question_id", ""))
        if not persona_id or not qid:
            continue
        if qid not in question_id_set:
            question_id_set.add(qid)
            question_ids.append(qid)
        if qid not in question_text_by_id:
            question_text_by_id[qid] = str(row.get("question_text", "")).strip()
        persona_rows.setdefault(persona_id, {})
        value = str(answer_resolver(row))
        persona_rows[persona_id][qid] = value

    question_ids = sorted(question_ids, key=_natural_question_sort_key)
    persona_attr_cols = sorted(persona_attr_keys)
    header = ["TWIN_ID", *persona_attr_cols, *embedded_data_cols, *question_ids]

    # Row 0: descriptions (question text for question cols, empty for others)
    description_row: dict[str, str] = {}
    for col in header:
        if col in question_text_by_id:
            description_row[col] = question_text_by_id[col]
        else:
            description_row[col] = ""

    # Row 1: ImportId metadata JSON
    import_id_row: dict[str, str] = {}
    for col in header:
        if col == "TWIN_ID":
            import_id_row[col] = json.dumps({"ImportId": "TWIN_ID"})
        elif col.startswith("demo_"):
            import_id_row[col] = json.dumps({"ImportId": col})
        else:
            import_id_row[col] = json.dumps({"ImportId": col})

    # Data rows
    data_rows: list[dict[str, str]] = []
    for persona_id in sorted(persona_rows.keys()):
        row: dict[str, str] = {"TWIN_ID": persona_id}
        demographics = persona_demographics.get(persona_id, {})
        embedded_data = embedded_data_by_persona.get(persona_id, {})
        for attr in persona_attr_cols:
            source_key = attr.replace("demo_", "", 1)
            row[attr] = str(demographics.get(source_key, ""))
        for field in embedded_data_cols:
            row[field] = str(embedded_data.get(field, ""))
        for qid in question_ids:
            row[qid] = persona_rows[persona_id].get(qid, "")
        data_rows.append(row)

    return (
        header,
        description_row,
        import_id_row,
        data_rows,
        persona_attr_cols,
        embedded_data_cols,
        question_ids,
    )


def _build_public_surveytwin_csv(
    long_rows: list[dict[str, Any]],
    persona_demographics: dict[str, dict[str, Any]],
    embedded_data_by_persona: dict[str, dict[str, str]],
    embedded_data_cols: list[str],
    *,
    answer_resolver: Callable[[dict[str, Any]], str],
    layout: Any,
) -> tuple[list[str], list[str], list[str], list[list[str]]]:
    """Build public CSV rows using Qualtrics-style column labels where available
    (``layout``: ``results.layout.Layout`` of the survey)."""
    question_id_set: set[str] = set()
    question_text_by_id: dict[str, str] = layout.text_by_id()
    # A twin's row holds the text as that twin was shown it (its answers and
    # values piped in); a column header is the question as written, the same for
    # every twin. Row text names only a column the survey itself cannot describe.
    row_text_by_id: dict[str, str] = {}
    persona_rows: dict[str, dict[str, str]] = {}
    persona_attr_keys: set[str] = set()

    for demographics in persona_demographics.values():
        for key in demographics.keys():
            persona_attr_keys.add(f"demo_{key}")

    for row in long_rows:
        persona_id = str(row.get("persona_id", "")).strip()
        question_id = str(row.get("question_id", "")).strip()
        if not persona_id or not question_id:
            continue
        question_id_set.add(question_id)
        row_text_by_id.setdefault(
            question_id, str(row.get("question_text", "")).strip() or question_id
        )
        persona_rows.setdefault(persona_id, {})
        persona_rows[persona_id][question_id] = str(answer_resolver(row))

    persona_attr_cols = sorted(persona_attr_keys)
    expected, repeated = layout.expected_ids()
    question_ids = (question_id_set | expected) - repeated
    public_question_columns = layout.columns(
        question_ids=question_ids,
        text_by_id=question_text_by_id,
        fallback_text_by_id=row_text_by_id,
    )

    # Public exports intentionally follow the Qualtrics three-row convention:
    # headers, question descriptions, then ImportId metadata before data rows.
    header = [
        "TWIN_ID",
        *persona_attr_cols,
        *embedded_data_cols,
        *[item["header"] for item in public_question_columns],
    ]
    description_row = [
        "",
        *([""] * len(persona_attr_cols)),
        *([""] * len(embedded_data_cols)),
        *[item["description"] for item in public_question_columns],
    ]
    import_id_row = [
        json.dumps({"ImportId": "TWIN_ID"}, separators=(",", ":")),
        *[json.dumps({"ImportId": col}, separators=(",", ":")) for col in persona_attr_cols],
        *[json.dumps({"ImportId": col}, separators=(",", ":")) for col in embedded_data_cols],
        *[
            json.dumps({"ImportId": item["import_id"]}, separators=(",", ":"))
            for item in public_question_columns
        ],
    ]

    data_rows: list[list[str]] = []
    for persona_id in sorted(persona_rows.keys()):
        demographics = persona_demographics.get(persona_id, {})
        embedded_data = embedded_data_by_persona.get(persona_id, {})
        row = [
            persona_id,
            *[str(demographics.get(col.replace("demo_", "", 1), "")) for col in persona_attr_cols],
            *[str(embedded_data.get(col, "")) for col in embedded_data_cols],
            *[
                persona_rows[persona_id].get(item["question_id"], "")
                for item in public_question_columns
            ],
        ]
        data_rows.append(row)

    return header, description_row, import_id_row, data_rows


def _build_legacy_wide_rows(
    *,
    data_rows: list[dict[str, str]],
    persona_attr_cols: list[str],
    embedded_data_cols: list[str],
    question_cols: list[str],
) -> tuple[list[str], list[dict[str, str]]]:
    header = ["persona_id", *persona_attr_cols, *embedded_data_cols, *question_cols]
    output: list[dict[str, str]] = []
    for row in data_rows:
        serialized = {"persona_id": row.get("TWIN_ID", "")}
        for column in persona_attr_cols:
            serialized[column] = row.get(column, "")
        for column in embedded_data_cols:
            serialized[column] = row.get(column, "")
        for column in question_cols:
            serialized[column] = row.get(column, "")
        output.append(serialized)
    return header, output


def _build_legacy_long_rows(
    *,
    long_rows: list[dict[str, Any]],
    persona_demographics: dict[str, dict[str, Any]],
    embedded_data_by_persona: dict[str, dict[str, str]],
    persona_attr_cols: list[str],
    embedded_data_cols: list[str],
) -> tuple[list[str], list[dict[str, str]]]:
    header = [
        "persona_id",
        "question_id",
        "question_text",
        "question_type",
        "answer_raw",
        "answer_value",
        "answer_label",
        "answer_status",
        "answer_repaired",
        "shaped_by_repaired",
        *persona_attr_cols,
        *embedded_data_cols,
    ]
    output: list[dict[str, str]] = []
    for row in long_rows:
        persona_id = str(row.get("persona_id", ""))
        demo = persona_demographics.get(persona_id, {})
        embedded_data = embedded_data_by_persona.get(persona_id, {})
        serialized = {
            "persona_id": persona_id,
            "question_id": str(row.get("question_id", "")),
            "question_text": str(row.get("question_text", "")),
            "question_type": str(row.get("question_type", "")),
            "answer_raw": str(row.get("answer_raw", "")),
            "answer_value": str(row.get("answer_value", "")),
            "answer_label": str(row.get("answer_label", "")),
            # 'needs_validation' = flagged skip; '' = real answer or structural blank
            "answer_status": str(row.get("answer_status") or ""),
            # accepted Layer-2 repair (if any); lets the report reconstruct per-cell
            # status directly from long.csv (no separate responses_status.csv).
            "answer_repaired": str(row.get("answer_repaired") or ""),
            # answers repaired since that decided this question's text, options or
            # visibility (worked out from the repairs stored)
            "shaped_by_repaired": _shaped_by_repaired(row),
        }
        for column in persona_attr_cols:
            serialized[column] = str(demo.get(column.replace("demo_", "", 1), ""))
        for column in embedded_data_cols:
            serialized[column] = str(embedded_data.get(column, ""))
        output.append(serialized)
    return header, output




def _shaped_by_repaired(row: dict[str, Any]) -> str:
    value = row.get("shaped_by_repaired")
    if isinstance(value, list):
        return ",".join(str(item) for item in value)
    return str(value or "")


EXPORT_MANIFEST_VERSION = "1.1"  # the website's report_schema.EXPORT_MANIFEST_VERSION
