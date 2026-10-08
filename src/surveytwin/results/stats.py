"""Per-question summary statistics, ported from the website's ``services/v2/analytics.py``.

Only ``compute_summary_stats`` is kept; the website's chart and insight helpers
call a model and are not part of the library.
"""

from __future__ import annotations

import math
import re
import statistics
from collections import Counter, defaultdict
from typing import Any

from ..answers.executor import (
    ANSWER_STATUS_CASCADE_HIDDEN,
    ANSWER_STATUS_OUT_OF_RANGE,
    FLAGGED_ANSWER_STATUSES,
    SKIP_ANSWER_STATUSES,
)

_LM_PLACEHOLDER_RE = re.compile(r"\$\{lm://[^}]+\}")
_ED_PLACEHOLDER_RE = re.compile(r"\$\{e://[^}]+\}")

_NOISE_LABELS = frozenset({"(blank)", "n/a", "na", "", "none", "null", "blank"})


def _with_repair(row: dict[str, Any]) -> dict[str, Any]:
    """A repaired answer counts as its repair, as in the labels file and the
    validation report: answered, with the repaired label."""
    repaired = str(row.get("answer_repaired") or "").strip()
    if not repaired:
        return row
    return {**row, "answer_status": "", "answer_label": repaired, "answer_value": repaired}


def compute_summary_stats(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[row["question_id"]].append(_with_repair(row))

    metrics: list[dict[str, Any]] = []
    for question_id, qrows in grouped.items():
        question_text = qrows[0].get("question_text", "")
        question_type = qrows[0].get("question_type", "open_text")

        # Clean unresolved Qualtrics placeholders from display text
        question_text = _LM_PLACEHOLDER_RE.sub("", question_text)
        question_text = _ED_PLACEHOLDER_RE.sub("", question_text)
        question_text = re.sub(r"\s{2,}", " ", question_text).strip()

        # Valid-base counting, keyed on the skip FLAG — never on blankness:
        # a flagged skip (answer_status='needs_validation') is a twin who was
        # asked and gave no usable answer; an UNFLAGGED blank is structural
        # (multi-select not selected, question not shown, optional
        # skip-by-choice) and is a real negative/absent state, not noise.
        # Mirrors the deterministic report's col_stats so the dashboard and the
        # validation report show the same numbers.
        def _st(r):
            return str(r.get("answer_status") or "").strip()
        skipped_rows = [r for r in qrows if _st(r) in SKIP_ANSWER_STATUSES]
        oor_rows = [r for r in qrows if _st(r) == ANSWER_STATUS_OUT_OF_RANGE]
        cascade_rows = [r for r in qrows if _st(r) == ANSWER_STATUS_CASCADE_HIDDEN]
        # unflagged = everything else (real answers + true structural blanks);
        # cascade_hidden is carved out so it isn't miscounted as structural.
        _flagged_or_cascade = FLAGGED_ANSWER_STATUSES | {ANSWER_STATUS_CASCADE_HIDDEN}
        unflagged = [r for r in qrows if _st(r) not in _flagged_or_cascade]
        n_skipped = len(skipped_rows)
        n_out_of_range = len(oor_rows)
        n_cascade = len(cascade_rows)

        values = [str(r.get("answer_value", "")).strip() for r in unflagged]
        labels = [str(r.get("answer_label", "")).strip() for r in unflagged]
        valid_pairs = [(v, l) for v, l in zip(values, labels) if v or l]
        n_structural = len(unflagged) - len(valid_pairs)
        valid_values = [v for v, _ in valid_pairs]
        valid_labels = [l for _, l in valid_pairs]

        numeric_values = []
        for value in valid_values:
            try:
                numeric_values.append(float(value))
            except Exception:
                continue

        # numeric-kind gate decided over VALID answers only — a heavily-skipped
        # numeric question must not flip to a categorical "(blank)" chart
        if len(numeric_values) >= max(3, len(valid_values) // 2) and numeric_values:
            metric = {
                "question_id": question_id,
                "question_text": question_text,
                "question_type": question_type,
                "kind": "numeric",
                "n": len(numeric_values),
                "n_skipped": n_skipped,
                "n_out_of_range": n_out_of_range,
                "n_cascade": n_cascade,
                "n_structural": n_structural,
                # real answers that didn't parse as numbers ("twenty") — kept
                # visible so n + n_other + n_skipped + n_structural = fielded rows
                "n_other": len(valid_values) - len(numeric_values),
                "mean": statistics.fmean(numeric_values),
                "median": statistics.median(numeric_values),
                "min": min(numeric_values),
                "max": max(numeric_values),
                "std": statistics.pstdev(numeric_values) if len(numeric_values) > 1 else 0.0,
            }
        else:
            source = valid_labels if any(valid_labels) else valid_values
            counts = Counter([item for item in source if item])
            total = sum(counts.values())
            distribution = [
                {
                    "label": label,
                    "count": count,
                    # percentage of REAL answers — skips/structural blanks are
                    # reported separately, never folded into a "(blank)" bucket
                    "pct": (count / total) if total else 0.0,
                }
                for label, count in counts.most_common(8)
            ]
            metric = {
                "question_id": question_id,
                "question_text": question_text,
                "question_type": question_type,
                "kind": "categorical",
                "n": total,
                "n_skipped": n_skipped,
                "n_out_of_range": n_out_of_range,
                "n_cascade": n_cascade,
                "n_structural": n_structural,
                "n_other": max(0, len(valid_pairs) - total),
                "distribution": distribution,
            }

            # Quality gate: literal noise answers ("N/A" etc.) still count
            noise_count = sum(
                entry["count"]
                for entry in distribution
                if str(entry.get("label", "")).strip().lower() in _NOISE_LABELS
            )
            if total > 0 and (noise_count / total) > 0.5:
                metric["low_quality"] = True

        # Low-quality = dominated by flagged SKIPS (needs_validation), the only
        # abnormal blank. STRUCTURAL blanks are normal survey/export behavior —
        # a multi-select option most twins don't select, or a branch arm shown
        # to only some twins — and must NEVER suppress a valid variable, so they
        # are excluded from both sides of the ratio. Base = fielded responses
        # (skips + real answers); a fully-structural column (base 0) is never
        # flagged.
        flagged = n_skipped + n_out_of_range
        fielded = flagged + int(metric.get("n") or 0)
        if fielded > 0 and flagged / fielded > 0.5:
            metric["low_quality"] = True

        metrics.append({"metric_key": question_id, "metric": metric})

    metrics.sort(key=lambda item: item["metric_key"])
    return metrics
