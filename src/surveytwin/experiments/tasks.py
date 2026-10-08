"""Conjoint and MaxDiff blocks as questions: the block in the template, and each twin's tasks.

``compile_block`` turns a block of the standard format into a template block
that holds the block's configuration (``Experiment``) and one question per
task, with stable IDs and export names, so a survey lists, checks and exports
those questions before any twin is drawn. ``expand_block`` turns that block into
one twin's tasks: its draws (``design.py``), each task's text and options as
shown, and a record of what was shown (``ExperimentTask`` on each question).

The model answers through the usual contract: a conjoint task is a single
choice of the alternatives shown (plus any outside option), copied by label;
the engine records the option ID, which names the alternative, and the record
names its levels. A MaxDiff task is two linked single choices of the same shown
items, best and worst, which must differ (checked with the answers).

With ``administration.unit: block`` (the default) all tasks of the block are
asked together, in the same model call, each with its own answer: drawing the
tasks needs no earlier answer, so it never makes a survey staged. With
``"task"``, each task is its own block marked ``StageBreak``, and the engine asks
it in a model call of its own.
"""

from __future__ import annotations

import copy
from typing import Any

from ..spec.qualtrics import force_response_setting
from ..spec.text import join_text
from .design import DesignError, design_rng, draw_conjoint, draw_maxdiff, letter, order

FORMAT_VERSION = "0.1"


def _media(items: Any, title: str = "") -> list[dict[str, str]]:
    out = []
    for item in items or []:
        media = {"kind": "image", "src": str(item["source"])}
        if item.get("alt"):
            media["alt"] = str(item["alt"])
        if item.get("title") or title:
            media["title"] = str(item.get("title") or title)
        out.append(media)
    return out


def _settings(required: Any) -> dict[str, Any]:
    return {"Selector": "SAVR", "SubSelector": "TX", "ForceResponse": force_response_setting(required), "ValidationType": "None"}


def _task_ids(block: dict[str, Any]) -> list[int]:
    design = block.get("design") or {}
    if design.get("mode") == "supplied":
        counts = {len(v.get("tasks") or []) for v in design.get("versions") or []}
        return list(range(1, max(counts or {0}) + 1))
    return list(range(1, int(design.get("tasks_per_participant") or 1) + 1))


def question_id(block_id: str, task: int, role: str = "") -> str:
    return f"{block_id}_t{task}" + (f"_{role}" if role else "")


# ---------------------------------------------------------------- conjoint


def _conjoint_options(block: dict[str, Any]) -> list[tuple[str, str]]:
    """(option ID, label) of every alternative, as authored, then the outside option."""
    alternatives = block.get("alternatives") or {}
    if alternatives.get("mode") == "fixed":
        options = [(str(a["id"]), join_text(a["label"])) for a in alternatives.get("items") or []]
    else:
        template = str(alternatives.get("label") or "Option {letter}")
        options = [(str(n), template.format(letter=letter(n), number=n)) for n in range(1, int(alternatives.get("count") or 2) + 1)]
    outside = ((block.get("response") or {}).get("outside_option")) or None
    if outside:
        options.append((str(outside["id"]), join_text(outside["label"])))
    return options


def _conjoint_question(block: dict[str, Any], task: int, text: str, options: list[tuple[str, str]], media: list[dict[str, str]], record: dict[str, Any] | None) -> dict[str, Any]:
    response = block.get("response") or {}
    outside = response.get("outside_option") or None
    qid = question_id(str(block["id"]), task)
    question: dict[str, Any] = {
        "QuestionID": qid,
        "QuestionText": text,
        "QuestionType": "MC",
        "QuestionName": qid,
        "KeepLines": True,
        "ExactType": True,
        "Options": [label for _id, label in options],
        "OptionsID": [option_id for option_id, _label in options],
        "Settings": _settings(response.get("required", False)),
    }
    if outside is not None and outside.get("code") is not None:
        question["RecodeValues"] = {str(outside["id"]): outside["code"]}
    if media:
        question["QuestionMedia"] = media
    if record is not None:
        question["ExperimentTask"] = record
    return question


def _conjoint_skeleton(block: dict[str, Any]) -> list[dict[str, Any]]:
    instruction = join_text((block.get("presentation") or {}).get("instruction", "")) or "Which of these would you choose?"
    note = "(The alternatives of each task are drawn for each twin.)"
    return [_conjoint_question(block, t, f"{instruction}\n\n{note}", _conjoint_options(block), [], None) for t in _task_ids(block)]


def _layout(block: dict[str, Any]) -> str:
    """How a conjoint task shows its alternatives: ``table`` (the default) or ``list``."""
    return str((block.get("presentation") or {}).get("layout") or "table")


def _cell(text: str) -> str:
    """Text as one table cell: its lines kept apart with "; ", a "|" escaped."""
    lines = [line.strip() for line in str(text).split("\n")]
    return "; ".join(line for line in lines if line).replace("|", "\\|")


def _rows(kind: str, attribute_order: list[str], names: dict[str, str], shown: list[dict[str, Any]], places: dict[tuple[str, int], str]) -> list[tuple[str, list[str]]]:
    """Each attribute's name and its level in each alternative shown (a table cell in
    a table), with the markers of its pictures (``places``, by attribute and column)."""
    out = []
    for attribute_id in attribute_order:
        values = []
        for column, alternative in enumerate(shown):
            value = alternative["levels"][attribute_id]["label"]
            value = _cell(value) if kind == "table" else value
            if (attribute_id, column) in places:
                value = f"{value} {places[(attribute_id, column)]}"
            values.append(value)
        out.append((names[attribute_id], values))
    return out


def _task_text(instruction: str, kind: str, labels: list[str], rows: list[tuple[str, list[str]]]) -> str:
    """A task's text in its layout. ``rows``: each attribute's name and its level in
    each alternative (table cells already, in a table)."""
    if kind == "list":
        lines = [instruction, ""]
        for column, label in enumerate(labels):
            lines.append(label)
            lines += [f"- {name}: {values[column]}" for name, values in rows]
            lines.append("")
        return "\n".join(lines).strip()
    lines = ["| | " + " | ".join(_cell(label) for label in labels) + " |", "|" + "---|" * (len(labels) + 1)]
    lines += ["| " + " | ".join([_cell(name), *values]) + " |" for name, values in rows]
    return instruction + "\n\n" + "\n".join(lines)


def conjoint_tasks(block: dict[str, Any], *, seed: Any, twin: str, twin_index: int) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """One twin's conjoint tasks, as shown: for each task its number, text, options
    (ID, label), pictures and record; and the design drawn.

    The text is the instruction, then the alternatives in the block's layout: a
    table with a column per alternative and a row per attribute (the default),
    or each alternative's label with a line per attribute. A level's picture is
    sent in its cell (or after its line): ``with_pictures`` is the text with
    "[[picture N]]" there, N counting the task's pictures (``media``) in reading
    order. A picture shown in two cells is sent twice."""
    block_id = str(block["id"])
    rng = design_rng(seed, twin, block_id)
    design = draw_conjoint(block, rng, twin_index=twin_index)
    presentation = block.get("presentation") or {}
    participant_order = design_rng(seed, twin, block_id, "order")
    attributes = {str(a["id"]): a for a in block.get("attributes") or []}
    levels = {(str(a["id"]), str(lv["id"])): lv for a in block.get("attributes") or [] for lv in a.get("levels") or []}
    fixed = (block.get("alternatives") or {}).get("mode") == "fixed"
    alternative_labels = {option_id: label for option_id, label in _conjoint_options(block)}
    instruction = join_text(presentation.get("instruction", "")) or "Which of these would you choose?"
    outside = ((block.get("response") or {}).get("outside_option")) or None
    template = str((block.get("alternatives") or {}).get("label") or "Option {letter}")
    kind = _layout(block)
    tasks: list[dict[str, Any]] = []
    participant_attribute_order = order(list(attributes), presentation.get("attribute_order"), participant_order, participant_order)
    for task in design["tasks"]:
        task_rng = design_rng(seed, twin, block_id, f"order:{task['task']}")
        attribute_order = (
            order(list(attributes), presentation.get("attribute_order"), participant_order, task_rng)
            if (presentation.get("attribute_order") or {}).get("scope") == "task"
            else participant_attribute_order
        )
        generated = task["alternatives"]
        ids = [a["id"] for a in generated]
        # (a new generator from the same seed for each task: one order for the twin's every task)
        shown_ids = order(ids, presentation.get("alternative_order"), design_rng(seed, twin, block_id, "alternatives"), task_rng)
        by_id = {a["id"]: a for a in generated}
        options: list[tuple[str, str]] = []
        shown: list[dict[str, Any]] = []
        for position, alternative_id in enumerate(shown_ids, start=1):
            alternative = by_id[alternative_id]
            if fixed:
                option_id, label = alternative_id, alternative_labels.get(alternative_id, alternative_id)
            else:
                option_id, label = str(position), template.format(letter=letter(position), number=position)
            options.append((option_id, label))
            entry_levels = {}
            for attribute_id in attribute_order:
                level_id = alternative["levels"][attribute_id]
                level = levels[(attribute_id, level_id)]
                entry = {"level": level_id, "label": join_text(level["label"])}
                if "value" in level:
                    entry["value"] = level["value"]
                entry_levels[attribute_id] = entry
            shown.append(
                {
                    "position": position,
                    "option_id": option_id,
                    "label": label,
                    **({"alternative": alternative_id} if fixed else {}),
                    "levels": entry_levels,
                }
            )
        labels = [entry["label"] for entry in shown]
        names = {attribute_id: join_text(attributes[attribute_id]["name"]) for attribute_id in attribute_order}
        # the cells in reading order: row by row in a table, alternative by alternative in a list
        cells = (
            [(a, c) for a in attribute_order for c in range(len(shown))]
            if kind == "table"
            else [(a, c) for c in range(len(shown)) for a in attribute_order]
        )
        media: list[dict[str, str]] = []
        places: dict[tuple[str, int], str] = {}
        for attribute_id, column in cells:
            entry = shown[column]["levels"][attribute_id]
            pictures = _media(levels[(attribute_id, entry["level"])].get("media"), f"{labels[column]}, {names[attribute_id]}: {entry['label']}")
            if pictures:
                places[(attribute_id, column)] = " ".join(f"[[picture {len(media) + n}]]" for n in range(1, len(pictures) + 1))
                media += pictures
        if outside:
            options.append((str(outside["id"]), join_text(outside["label"])))
        record = {
            "block": block_id,
            "kind": "conjoint",
            "task": task["task"],
            "question_id": question_id(block_id, task["task"]),
            "generator": design["generator"],
            **({"version": design["version"]} if "version" in design else {}),
            **({"source_task": task["source_id"]} if "source_id" in task else {}),
            "layout": kind,
            "attribute_order": list(attribute_order),
            "alternatives": shown,
            **({"outside": {"option_id": str(outside["id"]), "label": join_text(outside["label"])}} if outside else {}),
        }
        tasks.append(
            {
                "task": task["task"],
                "text": _task_text(instruction, kind, labels, _rows(kind, attribute_order, names, shown, {})),
                "with_pictures": _task_text(instruction, kind, labels, _rows(kind, attribute_order, names, shown, places)) if media else "",
                "options": options,
                "media": media,
                "record": record,
            }
        )
    return tasks, design


def _expand_conjoint(block: dict[str, Any], *, seed: Any, twin: str, twin_index: int) -> tuple[list[list[dict[str, Any]]], list[dict[str, Any]], dict[str, Any]]:
    """(questions of each task, a record per task, the design drawn)."""
    tasks, design = conjoint_tasks(block, seed=seed, twin=twin, twin_index=twin_index)
    questions = [[_conjoint_question(block, t["task"], t["text"], t["options"], t["media"], t["record"])] for t in tasks]
    return questions, [t["record"] for t in tasks], design


# ---------------------------------------------------------------- MaxDiff


def _maxdiff_questions(block: dict[str, Any], task: int, items: list[dict[str, Any]], record: dict[str, Any] | None, *, skeleton: bool) -> list[dict[str, Any]]:
    block_id = str(block["id"])
    presentation = block.get("presentation") or {}
    response = block.get("response") or {}
    best, worst = join_text(response.get("best_label", "Best")), join_text(response.get("worst_label", "Worst"))
    instruction = join_text(presentation.get("instruction", "")) or f"Which of these is {best.lower()}, and which is {worst.lower()}?"
    labels = [join_text(i["label"]) for i in items]
    listing = "(The items of each task are drawn for each twin.)" if skeleton else "\n".join(f"- {label}" for label in labels)
    group = question_id(block_id, task)
    intro: dict[str, Any] = {
        "QuestionID": group,
        "QuestionText": f"{instruction}\n\n{listing}",
        "QuestionType": "DB",
        "QuestionName": group,
        "KeepLines": True,
        "is_descriptive": True,
        "Settings": {"Selector": "TB", "SubSelector": "", "ValidationType": "None"},
    }
    item_media = [_media(i.get("media"), join_text(i["label"])) for i in items]
    if any(item_media):
        intro["QuestionMedia"] = [m for media in item_media for m in media]
    codes = {str(i["id"]): i["code"] for i in items if i.get("code") is not None}
    out = [intro]
    for role, label, note in (
        ("best", best, f'one of the items above; not the same item as "{worst}"'),
        ("worst", worst, f'one of the items above; not the same item as "{best}"'),
    ):
        qid = question_id(block_id, task, role)
        question: dict[str, Any] = {
            "QuestionID": qid,
            "QuestionText": f"{label} ({note})",
            "QuestionType": "MC",
            "QuestionName": qid,
            "KeepLines": True,
            "ExactType": True,
            "Options": labels,
            "OptionsID": [str(i["id"]) for i in items],
            "Settings": _settings(response.get("required", False)),
            "BestWorst": {"group": group, "role": role},
        }
        if codes:
            question["RecodeValues"] = dict(codes)
        if any(item_media):
            question["OptionMedia"] = item_media
        if record is not None:
            question["ExperimentTask"] = record
        out.append(question)
    anchor = block.get("anchor")
    if anchor:
        qid = question_id(block_id, task, "anchor")
        anchor_response = anchor.get("response") or {}
        options = anchor_response.get("options") or []
        question = {
            "QuestionID": qid,
            "QuestionText": "\n\n".join(join_text(c.get("text", "")) for c in anchor.get("content") or [] if c.get("type") == "text"),
            "QuestionType": "MC",
            "QuestionName": qid,
            "KeepLines": True,
            "ExactType": True,
            "Options": [join_text(o["label"]) for o in options],
            "OptionsID": [str(o["id"]) for o in options],
            "Settings": _settings(anchor_response.get("required", False)),
        }
        anchor_codes = {str(o["id"]): o["code"] for o in options if o.get("code") is not None}
        if anchor_codes:
            question["RecodeValues"] = anchor_codes
        if record is not None:
            question["ExperimentTask"] = record
        out.append(question)
    return out


def maxdiff_tasks(block: dict[str, Any], *, seed: Any, twin: str, twin_index: int) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """One twin's MaxDiff tasks, as shown: for each task its number, items in the
    order shown, and record; and the design drawn."""
    block_id = str(block["id"])
    design = draw_maxdiff(block, design_rng(seed, twin, block_id), twin_index=twin_index)
    items = {str(i["id"]): i for i in block.get("items") or []}
    presentation = block.get("presentation") or {}
    setting = presentation.get("item_order") or {}
    # once per twin: one random ranking of the whole pool, each task's items in it
    ranking = {item: n for n, item in enumerate(order(list(items), {"mode": "shuffle"}, design_rng(seed, twin, block_id, "order"), design_rng(seed, twin, block_id, "order")))}
    tasks: list[dict[str, Any]] = []
    for task in design["tasks"]:
        task_rng = design_rng(seed, twin, block_id, f"order:{task['task']}")
        subset = [str(i) for i in task["items"]]
        if setting.get("mode") == "shuffle" and setting.get("scope") != "task":
            shown_ids = _pin(sorted(subset, key=lambda item: ranking[item]), setting.get("fixed_positions") or {})
        else:
            shown_ids = order(subset, setting, task_rng, task_rng)
        record = {
            "block": block_id,
            "kind": "maxdiff",
            "task": task["task"],
            "question_ids": {
                "best": question_id(block_id, task["task"], "best"),
                "worst": question_id(block_id, task["task"], "worst"),
                **({"anchor": question_id(block_id, task["task"], "anchor")} if block.get("anchor") else {}),
            },
            "generator": design["generator"],
            **({"version": design["version"]} if "version" in design else {}),
            "items": [{"position": n, "item": i, "label": join_text(items[i]["label"])} for n, i in enumerate(shown_ids, start=1)],
        }
        tasks.append({"task": task["task"], "items": [items[i] for i in shown_ids], "record": record})
    return tasks, design


def _expand_maxdiff(block: dict[str, Any], *, seed: Any, twin: str, twin_index: int) -> tuple[list[list[dict[str, Any]]], list[dict[str, Any]], dict[str, Any]]:
    tasks, design = maxdiff_tasks(block, seed=seed, twin=twin, twin_index=twin_index)
    questions = [_maxdiff_questions(block, t["task"], t["items"], t["record"], skeleton=False) for t in tasks]
    return questions, [t["record"] for t in tasks], design


def _pin(ids: list[str], fixed: dict[str, Any]) -> list[str]:
    """``ids`` in their order, with each pinned item moved to its position."""
    pinned = {str(k): int(v) for k, v in fixed.items() if str(k) in ids and 1 <= int(v) <= len(ids)}
    rest = [i for i in ids if i not in pinned]
    out: list[str | None] = [None] * len(ids)
    for item, position in sorted(pinned.items(), key=lambda kv: kv[1]):
        if out[position - 1] is None:
            out[position - 1] = item
        else:
            rest.insert(0, item)
    filler = iter(rest)
    return [slot if slot is not None else next(filler) for slot in out]


# ---------------------------------------------------------------- blocks


def _unit(block: dict[str, Any]) -> str:
    return str((block.get("administration") or {}).get("unit") or "block")


def compile_block(block: dict[str, Any]) -> dict[str, Any]:
    """The template block of a conjoint or MaxDiff block: its configuration and one
    question per task (before any twin is drawn)."""
    if block.get("kind") == "conjoint":
        questions = _conjoint_skeleton(block)
    else:
        questions = [
            question
            for task in _task_ids(block)
            for question in _maxdiff_questions(block, task, list(block.get("items") or []), None, skeleton=True)
        ]
    return {
        "ElementType": "Block",
        "BlockName": str(block.get("title") or block["id"]),
        "BlockType": "Standard",
        "BlockID": str(block["id"]),
        "Questions": questions,
        "Experiment": {"kind": block["kind"], "format_version": FORMAT_VERSION, "block": copy.deepcopy(block)},
    }


def expand_block(element: dict[str, Any], *, seed: Any, twin: str, twin_index: int = 0, design: dict[str, Any] | None = None) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """One twin's tasks of a template block made by ``compile_block``: the blocks to
    show (one per task when tasks are asked one at a time, marked ``StageBreak``)
    and a record of each task as shown. ``design`` is not used to draw: the draw
    depends only on the run's seed, the twin and the block."""
    config = (element.get("Experiment") or {}).get("block") or {}
    if config.get("kind") == "conjoint":
        tasks, records, _design = _expand_conjoint(config, seed=seed, twin=twin, twin_index=twin_index)
    elif config.get("kind") == "maxdiff":
        tasks, records, _design = _expand_maxdiff(config, seed=seed, twin=twin, twin_index=twin_index)
    else:
        return [element], []
    base = {k: v for k, v in element.items() if k not in {"Questions", "Experiment"}}
    if _unit(config) == "block":
        return [{**copy.deepcopy(base), "Questions": [q for task in tasks for q in task]}], records
    blocks = []
    for number, questions in enumerate(tasks, start=1):
        blocks.append({**copy.deepcopy(base), "BlockID": f"{base['BlockID']}_task{number}", "Questions": questions, "StageBreak": True})
    return blocks, records


def has_stage_breaks(elements: Any) -> bool:
    """Whether a template asks some tasks one at a time (a conjoint or MaxDiff block
    with ``administration.unit: task``)."""
    if isinstance(elements, list):
        return any(has_stage_breaks(e) for e in elements)
    if not isinstance(elements, dict):
        return False
    experiment = elements.get("Experiment")
    if isinstance(experiment, dict) and _unit(experiment.get("block") or {}) == "task":
        return True
    return has_stage_breaks(elements.get("Elements"))


__all__ = ["DesignError", "compile_block", "conjoint_tasks", "expand_block", "experiment_records", "has_stage_breaks", "maxdiff_tasks", "question_id"]


def experiment_records(elements: Any) -> list[dict[str, Any]]:
    """The record of every conjoint or MaxDiff task in prepared survey elements
    (``ExperimentTask`` on their questions), once each, in order."""
    out: list[dict[str, Any]] = []
    seen: set[tuple[str, int]] = set()

    def walk(nodes: Any) -> None:
        for node in nodes if isinstance(nodes, list) else []:
            if not isinstance(node, dict):
                continue
            for question in node.get("Questions") or []:
                record = question.get("ExperimentTask") if isinstance(question, dict) else None
                if isinstance(record, dict):
                    key = (str(record.get("block")), int(record.get("task") or 0))
                    if key not in seen:
                        seen.add(key)
                        out.append(copy.deepcopy(record))
            walk(node.get("Elements"))

    walk(elements)
    return out
