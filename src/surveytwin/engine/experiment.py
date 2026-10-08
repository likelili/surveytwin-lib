"""Conjoint and MaxDiff tasks as shown questions.

The tasks are drawn by ``experiments.tasks`` (``conjoint_tasks``,
``maxdiff_tasks``): from the run's seed, the twin and the block only, never from
the survey's other draws. Here each task becomes the questions the twin is
shown: a conjoint task one choice of the alternatives (and the outside option);
a MaxDiff task its items once, as text, then the best and the worst pick (and
the anchor question, when the block has one).
"""

from __future__ import annotations

from typing import Any

from ..experiments.tasks import _task_ids, conjoint_tasks, maxdiff_tasks, question_id
from ..spec.text import join_text


def _required(value: Any) -> bool | str:
    return value if value in (True, "request") else False


def _image(media: dict[str, str]) -> dict[str, str]:
    out = {"type": "image", "source": media["src"]}
    for key in ("alt", "title"):
        if media.get(key):
            out[key] = media[key]
    return out


def _item_media(item: dict[str, Any]) -> list[dict[str, str]]:
    """An item's pictures, titled by the item when they have no title of their own."""
    label = join_text(item["label"])
    out = []
    for media in item.get("media") or []:
        entry = {"source": str(media["source"])}
        if media.get("alt"):
            entry["alt"] = str(media["alt"])
        if media.get("title") or label:
            entry["title"] = str(media.get("title") or label)
        out.append(entry)
    return out


def _choice(qid: str, text: str, options: list[dict[str, Any]], required: Any, *, images: list[dict[str, str]] | None = None, with_pictures: str = "") -> dict[str, Any]:
    """A single choice; ``with_pictures``: its text with "[[picture N]]" where its Nth
    picture (of ``images``) sits."""
    return {
        "question": {
            "id": qid,
            "export_name": qid,
            "content": [{"type": "text", "text": text, **({"with_pictures": with_pictures} if with_pictures else {})}] + [_image(m) for m in images or []],
            "response": {"type": "choice", "mode": "single", "options": options, "required": _required(required)},
            "presentation": {"control": "list"},
        },
        "lines": True,
        "native": True,
    }


def conjoint_shown(block: dict[str, Any], *, seed: Any, twin: str, twin_index: int) -> tuple[list[list[dict[str, Any]]], list[dict[str, Any]]]:
    tasks, _design = conjoint_tasks(block, seed=seed, twin=twin, twin_index=twin_index)
    response = block.get("response") or {}
    outside = response.get("outside_option") or None
    out = []
    for task in tasks:
        options = [{"id": option_id, "label": label} for option_id, label in task["options"]]
        if outside is not None and outside.get("code") is not None:
            for option in options:
                if option["id"] == str(outside["id"]):
                    option["code"] = outside["code"]
        out.append([_choice(question_id(str(block["id"]), task["task"]), task["text"], options, response.get("required", False), images=task["media"], with_pictures=task["with_pictures"])])
    return out, [task["record"] for task in tasks]


def maxdiff_shown(block: dict[str, Any], *, seed: Any, twin: str, twin_index: int) -> tuple[list[list[dict[str, Any]]], list[dict[str, Any]]]:
    tasks, _design = maxdiff_tasks(block, seed=seed, twin=twin, twin_index=twin_index)
    return [maxdiff_task_questions(block, task["task"], task["items"]) for task in tasks], [task["record"] for task in tasks]


def maxdiff_task_questions(block: dict[str, Any], task: int, items: list[dict[str, Any]], *, skeleton: bool = False) -> list[dict[str, Any]]:
    """A MaxDiff task's questions: the items as text, the best and worst picks, the anchor."""
    block_id = str(block["id"])
    presentation = block.get("presentation") or {}
    response = block.get("response") or {}
    best, worst = join_text(response.get("best_label", "Best")), join_text(response.get("worst_label", "Worst"))
    instruction = join_text(presentation.get("instruction", "")) or f"Which of these is {best.lower()}, and which is {worst.lower()}?"
    labels = [join_text(i["label"]) for i in items]
    listing = "(The items of each task are drawn for each twin.)" if skeleton else "\n".join(f"- {label}" for label in labels)
    group = question_id(block_id, task)
    item_media = [_item_media(i) for i in items]
    intro = {
        "question": {
            "id": group,
            "export_name": group,
            "content": [{"type": "text", "text": f"{instruction}\n\n{listing}"}]
            + [{"type": "image", **m} for media in item_media for m in media],
        },
        "lines": True,
        "native": False,
    }
    out = [intro]
    for role, label, note in (
        ("best", best, f'one of the items above; not the same item as "{worst}"'),
        ("worst", worst, f'one of the items above; not the same item as "{best}"'),
    ):
        options = []
        for item, media in zip(items, item_media):
            option: dict[str, Any] = {"id": str(item["id"]), "label": join_text(item["label"])}
            if item.get("code") is not None:
                option["code"] = item["code"]
            if media:
                option["media"] = media
            options.append(option)
        shown = _choice(question_id(block_id, task, role), f"{label} ({note})", options, response.get("required", False))
        shown["best_worst"] = {"group": group, "role": role}
        out.append(shown)
    anchor = block.get("anchor")
    if anchor:
        anchor_response = anchor.get("response") or {}
        options = []
        for option in anchor_response.get("options") or []:
            entry: dict[str, Any] = {"id": str(option["id"]), "label": join_text(option["label"])}
            if option.get("code") is not None:
                entry["code"] = option["code"]
            options.append(entry)
        text = "\n\n".join(join_text(c.get("text", "")) for c in anchor.get("content") or [] if c.get("type") == "text")
        out.append(_choice(question_id(block_id, task, "anchor"), text, options, anchor_response.get("required", False)))
    return out


def experiment_questions(block: dict[str, Any], *, seed: Any, twin: str, twin_index: int) -> tuple[list[list[dict[str, Any]]], list[dict[str, Any]]]:
    """One twin's tasks of a conjoint or MaxDiff block: the shown questions of each
    task, and a record of each task as shown."""
    if block.get("kind") == "conjoint":
        return conjoint_shown(block, seed=seed, twin=twin, twin_index=twin_index)
    return maxdiff_shown(block, seed=seed, twin=twin, twin_index=twin_index)


def task_count(block: dict[str, Any]) -> int:
    return len(_task_ids(block))
