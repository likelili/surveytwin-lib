"""What each twin was shown in conjoint and MaxDiff tasks, as rows for analysis.

The run keeps, per twin, a record of every task it was asked (``experiments`` in
its audit record): the alternatives or items in the order shown, with their
levels. These functions join those records with the answers:

- conjoint: one row per twin, task and option shown (the outside option
  included), with each attribute's level and whether the twin chose it;
- MaxDiff: one row per twin, task and item shown, with whether it was picked as
  best and as worst (and the anchored follow-up's answer, when asked).

A choice is blank (not 0) when the task has no usable answer: skipped, or a
best/worst pair that named one item twice. A repaired answer counts as repaired.
"""

from __future__ import annotations

from typing import Any


def _answer_labels(rows: list[dict[str, Any]]) -> dict[tuple[str, str], str | None]:
    """(twin, question) -> the chosen label (repaired when repaired), or None when flagged."""
    out: dict[tuple[str, str], str | None] = {}
    for row in rows:
        key = (str(row.get("persona_id", "")), str(row.get("question_id", "")))
        repaired = str(row.get("answer_repaired") or "").strip()
        if repaired:
            out[key] = repaired
        elif row.get("answer_status"):
            out[key] = None
        else:
            out[key] = str(row.get("answer_label") or row.get("answer_value") or "").strip() or None
    return out


def _same(label: Any, chosen: str | None) -> str:
    if chosen is None:
        return ""
    return "1" if str(label).strip().casefold() == chosen.strip().casefold() else "0"


def exposure_tables(audit: list[dict[str, Any]], rows: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    """{"conjoint": [...], "maxdiff": [...]}: every option or item shown to every twin."""
    answers = _answer_labels(rows)
    conjoint: list[dict[str, Any]] = []
    maxdiff: list[dict[str, Any]] = []
    for record in audit:
        twin = str(record.get("persona_id", ""))
        for task in record.get("experiments") or []:
            base = {
                "twin_id": twin,
                "block": task.get("block", ""),
                "task": task.get("task", ""),
                "generator": task.get("generator", ""),
                "version": task.get("version", ""),
            }
            if task.get("kind") == "conjoint":
                question = str(task.get("question_id", ""))
                chosen = answers.get((twin, question))
                for alternative in task.get("alternatives") or []:
                    row = {
                        **base,
                        "question_id": question,
                        "position": alternative.get("position", ""),
                        "option_id": alternative.get("option_id", ""),
                        "label": alternative.get("label", ""),
                        "alternative": alternative.get("alternative", ""),
                        "outside": "0",
                        "chosen": _same(alternative.get("label", ""), chosen),
                    }
                    for attribute_id in task.get("attribute_order") or []:
                        level = (alternative.get("levels") or {}).get(attribute_id) or {}
                        row[attribute_id] = level.get("level", "")
                        row[f"{attribute_id}_label"] = level.get("label", "")
                        if "value" in level:
                            row[f"{attribute_id}_value"] = level["value"]
                    conjoint.append(row)
                outside = task.get("outside")
                if outside:
                    conjoint.append(
                        {
                            **base,
                            "question_id": question,
                            "position": len(task.get("alternatives") or []) + 1,
                            "option_id": outside.get("option_id", ""),
                            "label": outside.get("label", ""),
                            "alternative": "",
                            "outside": "1",
                            "chosen": _same(outside.get("label", ""), chosen),
                        }
                    )
            elif task.get("kind") == "maxdiff":
                ids = task.get("question_ids") or {}
                best = answers.get((twin, str(ids.get("best", ""))))
                worst = answers.get((twin, str(ids.get("worst", ""))))
                anchor = answers.get((twin, str(ids.get("anchor", "")))) if ids.get("anchor") else None
                for item in task.get("items") or []:
                    maxdiff.append(
                        {
                            **base,
                            "position": item.get("position", ""),
                            "item": item.get("item", ""),
                            "label": item.get("label", ""),
                            "best": _same(item.get("label", ""), best),
                            "worst": _same(item.get("label", ""), worst),
                            **({"anchor": anchor or ""} if ids.get("anchor") else {}),
                        }
                    )
    return {"conjoint": conjoint, "maxdiff": maxdiff}


def columns(rows: list[dict[str, Any]]) -> list[str]:
    """Every column of the rows, in first-seen order."""
    out: list[str] = []
    for row in rows:
        for key in row:
            if key not in out:
                out.append(key)
    return out
