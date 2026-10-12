"""Where a twin's prompt puts each question's answer details: the ID it is
answered under, its options and its rules.

- ``inline`` (the default): each question's details right under it in the
  survey ("Answer as:"), then the list of IDs to answer.
- ``json`` (the layout before prompt version 5): the survey, then every
  question again in a "Questions JSON" list, its text included.
- ``after``: the survey, then the same list without repeating the question text
  (it is in the survey); each entry names its question ("ref") and keeps its
  rules as words ("rules").

In a test on 19 study surveys (80 twins each) the three were equally accurate,
but ``inline`` left almost no answer blank, while with ``after`` twins sometimes
answered one question under a neighbour's ID.

For ``after`` and ``inline`` the survey text carries a marker after each
question, ``[[answers <QuestionID>|<label>]]`` (see ``answer_marker``), which
the prompt builder replaces or removes; with ``json`` no marker is written. A
survey text without markers (one saved by an older version) gets the ``json``
list.
"""

from __future__ import annotations

import json
import re
from collections import defaultdict
from typing import Any

# The layout in use.
LAYOUT = "inline"
LAYOUTS = ("json", "after", "inline")

ANSWER_MARKER_RE = re.compile(r"\n?\[\[answers ([^|\]]+)\|([^\]]*)\]\]")
_IDS_MARK = "IDs to answer, in order: "
_UNPLACED_MARK = "Questions JSON (questions with no place in the survey above):\n"

# What names a question's row, item, option or field, in the order looked for.
_SUB_LABEL_KEYS = (
    "matrix_row_label",
    "multi_answer_option_label",
    "constant_sum_row_label",
    "slider_statement_label",
    "rank_order_item_label",
    "form_field_label",
    "choice_text_entry_option_label",
    "sbs_row_label",
    "row_label",
    "sub_question_label",
)


def asked_questions(text: str) -> list[dict[str, Any]]:
    """The questions a prompt asks, each with its answer details (its ``id``,
    ``type``, ``options`` and the other fields), in the order asked: read from
    the user text of a request, whatever its layout. For a Python function that
    stands in for a real model."""
    if _IDS_MARK not in text:
        headers = list(re.finditer(r"Questions JSON[^\n]*:\n", text))
        return json.loads(text[headers[-1].end() :].split("\n", 1)[0]) if headers else []
    ids = json.loads(text[text.rindex(_IDS_MARK) + len(_IDS_MARK) :].split("\n", 1)[0])
    found: dict[str, dict[str, Any]] = {}
    under = False
    for line in text.splitlines():
        if line == "Answer as:":
            under = True
        elif under and line.startswith('{"id": '):
            entry = json.loads(line)
            found.setdefault(entry["id"], entry)
        else:
            under = False
    if _UNPLACED_MARK in text:
        start = text.rindex(_UNPLACED_MARK) + len(_UNPLACED_MARK)
        for entry in json.loads(text[start:].split("\n", 1)[0]):
            found.setdefault(entry["id"], entry)
    return [found[i] for i in ids if i in found]


def markers_wanted() -> bool:
    return LAYOUT in ("after", "inline")


def answer_marker(question_id: str, label: str) -> str:
    """The marker written after a question in the survey text."""
    return f"[[answers {question_id}|{label}]]"


def strip_answer_markers(text: str) -> str:
    return ANSWER_MARKER_RE.sub("", text or "")


def _sub_label(source: dict[str, Any]) -> str:
    parts = []
    sbs = str(source.get("sbs_sub_question_label") or "").strip()
    if sbs:
        parts.append(sbs)
    for key in _SUB_LABEL_KEYS:
        value = str(source.get(key) or "").strip()
        if value:
            parts.append(value)
            break
    column = str(source.get("matrix_column_label") or "").strip()
    if column:
        parts.append(column)
    return ", ".join(parts)


def _owner(source: dict[str, Any], labels: dict[str, str]) -> str | None:
    """The marked question an answer entry belongs to."""
    for key in ("parent_question_id", "id"):
        value = str(source.get(key) or "").strip()
        if value in labels:
            return value
    entry_id = str(source.get("id") or "")
    return next((qid for qid in labels if entry_id.startswith(f"{qid}_") or entry_id.startswith(f"{qid}#")), None)


def arrange(sequence_text: str, entries: list[dict[str, Any]], compact: list[dict[str, Any]], sources: list[dict[str, Any]]) -> tuple[str, str]:
    """(the survey text as the twin reads it, the part that follows it) for the
    layout in use. ``entries`` are the Questions JSON entries; ``compact`` the
    same entries without the question text (rules kept as words); ``sources``
    the questions they come from, in the same order."""
    found = [(m.group(1), m.group(2)) for m in ANSWER_MARKER_RE.finditer(sequence_text or "")]
    labels = {qid: label for qid, label in found}
    if LAYOUT not in ("after", "inline") or not labels:
        return strip_answer_markers(sequence_text), f"Questions JSON:\n{json.dumps(entries, ensure_ascii=False)}"
    placed: dict[str, list[int]] = defaultdict(list)
    unplaced: list[int] = []
    for index, source in enumerate(sources):
        owner = _owner(source, labels)
        (placed[owner].append(index) if owner else unplaced.append(index))
    if LAYOUT == "after":
        listed = []
        for index, entry in enumerate(compact):
            owner = _owner(sources[index], labels)
            sub = _sub_label(sources[index])
            ref = labels[owner] + (f", {sub}" if sub else "") if owner else ""
            listed.append({"id": entry["id"], **({"ref": ref} if ref else {"text": entries[index].get("text", "")}), **{k: v for k, v in entry.items() if k != "id"}})
        return strip_answer_markers(sequence_text), (
            "Questions JSON (each question's text is in the survey above; \"ref\" names the question and, for a row or item, which one):\n"
            f"{json.dumps(listed, ensure_ascii=False)}"
        )

    def details(match: re.Match[str]) -> str:
        indexes = placed.get(match.group(1)) or []
        if not indexes:
            return ""
        lines = []
        for index in indexes:
            sub = _sub_label(sources[index])
            entry = {"id": compact[index]["id"], **({"row": sub} if sub else {}), **{k: v for k, v in compact[index].items() if k != "id"}}
            lines.append(json.dumps(entry, ensure_ascii=False))
        return "\nAnswer as:\n" + "\n".join(lines)

    inline_text = ANSWER_MARKER_RE.sub(details, sequence_text)
    block = f"Answer each question under its \"Answer as\" line. {_IDS_MARK}{json.dumps([e['id'] for e in entries], ensure_ascii=False)}"
    if unplaced:
        block = f"{_UNPLACED_MARK}{json.dumps([entries[i] for i in unplaced], ensure_ascii=False)}\n\n{block}"
    return inline_text, block
