"""Conjoint and MaxDiff blocks: checking, generating and presenting their tasks.

``check_experiment_blocks`` checks the blocks of a survey in the standard format
(run by ``spec.validate.check_survey``); ``tasks.compile_block`` and
``tasks.expand_block`` put them in the template and draw each twin's tasks;
``design`` holds the generators; ``exposure`` turns what was shown into rows.
"""

from __future__ import annotations

from collections import Counter
from typing import Any

from .design import ConjointRules, conjoint_feasibility, maxdiff_feasibility
from .tasks import question_id


def _dups(values: list[str]) -> list[str]:
    return [v for v, n in Counter(values).items() if n > 1]


def check_experiment_blocks(survey: dict[str, Any]) -> list[Any]:
    """Problems in the survey's conjoint and MaxDiff blocks, with paths."""
    from ..spec.validate import Finding

    findings: list[Finding] = []
    question_ids = {str(q.get("id")) for q in survey.get("questions") or [] if isinstance(q, dict)}
    shown: Counter[str] = Counter()

    def walk(nodes: list[Any]) -> None:
        for node in nodes or []:
            if isinstance(node, dict):
                if node.get("type") == "show_block":
                    shown[str(node.get("block"))] += 1
                for key in ("then", "else", "children"):
                    walk(node.get(key) or [])

    walk(survey.get("flow") or [])
    for index, block in enumerate(survey.get("blocks") or []):
        if not isinstance(block, dict) or block.get("kind") not in {"conjoint", "maxdiff"}:
            continue
        path = f"$.blocks[{index}]"
        problems = _check_conjoint(block, path) if block["kind"] == "conjoint" else _check_maxdiff(block, path)
        if shown[str(block.get("id"))] > 1:
            problems.append((path, f"block {block.get('id')!r} is shown {shown[str(block.get('id'))]} times; a {block['kind']} block is shown once", "experiment.shown_twice"))
        for qid in _generated_ids(block):
            if qid in question_ids:
                problems.append((path, f"question {qid!r} is a task of this block; another question has that ID", "id.duplicate"))
        findings += [Finding(p, message, code) for p, message, code in problems]
    return findings


def _generated_ids(block: dict[str, Any]) -> list[str]:
    from .tasks import _task_ids

    ids = []
    for task in _task_ids(block):
        if block.get("kind") == "conjoint":
            ids.append(question_id(str(block["id"]), task))
        else:
            ids += [question_id(str(block["id"]), task)] + [question_id(str(block["id"]), task, r) for r in ("best", "worst", "anchor")]
    return ids


def _label(value: Any) -> str:
    from ..spec.text import join_text

    return join_text(value).strip().casefold()


def _check_order(setting: Any, ids: list[str], path: str, what: str) -> list[tuple[str, str, str]]:
    problems = []
    fixed = (setting or {}).get("fixed_positions") or {}
    for item, position in fixed.items():
        if str(item) not in ids:
            problems.append((f"{path}.fixed_positions", f"no {what} has the ID {item!r}", "reference.experiment"))
        elif not 1 <= int(position) <= len(ids):
            problems.append((f"{path}.fixed_positions", f"{item!r}: position {position} is outside 1 to {len(ids)}", "experiment.order"))
    if _dups([str(p) for p in fixed.values()]):
        problems.append((f"{path}.fixed_positions", "two entries take the same position", "experiment.order"))
    return problems


def _check_conjoint(block: dict[str, Any], path: str) -> list[tuple[str, str, str]]:
    problems: list[tuple[str, str, str]] = []
    attributes = block.get("attributes") or []
    attribute_ids = [str(a.get("id")) for a in attributes]
    for dup in _dups(attribute_ids):
        problems.append((f"{path}.attributes", f"two attributes have the ID {dup!r}", "id.duplicate"))
    levels = {str(a.get("id")): [str(lv.get("id")) for lv in a.get("levels") or []] for a in attributes}
    for a_index, attribute in enumerate(attributes):
        for dup in _dups(levels[str(attribute.get("id"))]):
            problems.append((f"{path}.attributes[{a_index}].levels", f"two levels have the ID {dup!r}", "id.duplicate"))
        if not any(float(lv.get("weight", 1)) > 0 for lv in attribute.get("levels") or []):
            problems.append((f"{path}.attributes[{a_index}].levels", "every level has weight 0, so none can be drawn", "experiment.weights"))
    alternatives = block.get("alternatives") or {}
    response = block.get("response") or {}
    outside = response.get("outside_option")
    if alternatives.get("mode") == "fixed":
        items = alternatives.get("items") or []
        if alternatives.get("count") is not None:
            problems.append((f"{path}.alternatives.count", "'count' is for generic alternatives; fixed ones are listed in 'items'", "experiment.alternatives"))
        if not items:
            problems.append((f"{path}.alternatives.items", "fixed alternatives are listed in 'items'", "experiment.alternatives"))
        ids = [str(i.get("id")) for i in items]
        labels = [_label(i.get("label")) for i in items]
        for dup in _dups(ids):
            problems.append((f"{path}.alternatives.items", f"two alternatives have the ID {dup!r}", "id.duplicate"))
        for dup in _dups(labels):
            problems.append((f"{path}.alternatives.items", f"two alternatives read {dup!r}; the model answers with the label", "label.duplicate"))
        for i_index, item in enumerate(items):
            for attribute_id, level_id in (item.get("fixed_levels") or {}).items():
                if str(attribute_id) not in levels:
                    problems.append((f"{path}.alternatives.items[{i_index}].fixed_levels", f"no attribute has the ID {attribute_id!r}", "reference.experiment"))
                elif str(level_id) not in levels[str(attribute_id)]:
                    problems.append((f"{path}.alternatives.items[{i_index}].fixed_levels", f"attribute {attribute_id!r} has no level {level_id!r}", "reference.experiment"))
        option_ids, option_labels = ids, labels
    else:
        if alternatives.get("items"):
            problems.append((f"{path}.alternatives.items", "'items' is for fixed alternatives; generic ones take 'count'", "experiment.alternatives"))
        count = int(alternatives.get("count") or 2)
        template = str(alternatives.get("label") or "Option {letter}")
        if count > 1 and "{letter}" not in template and "{number}" not in template:
            problems.append((f"{path}.alternatives.label", "the label needs {letter} or {number}, so the alternatives read differently", "label.duplicate"))
        try:
            option_labels = [template.format(letter="A", number=1).casefold()]
        except (KeyError, IndexError, ValueError):
            problems.append((f"{path}.alternatives.label", "the label may use only {letter} and {number}", "experiment.alternatives"))
            option_labels = []
        option_ids = [str(n) for n in range(1, count + 1)]
    if outside:
        if str(outside.get("id")) in option_ids:
            problems.append((f"{path}.response.outside_option.id", f"an alternative already has the ID {outside.get('id')!r}", "id.duplicate"))
        if _label(outside.get("label")) in option_labels:
            problems.append((f"{path}.response.outside_option.label", "an alternative reads the same", "label.duplicate"))
    if len(option_ids) + (1 if outside else 0) < 2:
        problems.append((f"{path}.alternatives", "a task needs at least two options to choose from", "experiment.alternatives"))
    constraints = block.get("constraints") or {}
    for m_index, matrix in enumerate(constraints.get("applicability") or []):
        here = f"{path}.constraints.applicability[{m_index}]"
        row, column = str(matrix.get("row_attribute")), str(matrix.get("column_attribute"))
        if row not in levels or column not in levels:
            problems.append((here, "both attributes must be attributes of the block", "reference.experiment"))
            continue
        if row == column:
            problems.append((here, "a matrix relates two different attributes", "experiment.constraints"))
        for name, attribute, listed in (("row_levels", row, matrix.get("row_levels") or []), ("column_levels", column, matrix.get("column_levels") or [])):
            if sorted(map(str, listed)) != sorted(levels[attribute]):
                problems.append((f"{here}.{name}", f"must list every level of {attribute!r} once: {', '.join(levels[attribute])}", "experiment.constraints"))
        allowed = matrix.get("allowed") or []
        if len(allowed) != len(matrix.get("row_levels") or []) or any(len(r) != len(matrix.get("column_levels") or []) for r in allowed):
            problems.append((f"{here}.allowed", "needs one row per row level and one value per column level", "experiment.constraints"))
    for f_index, combo in enumerate(constraints.get("forbidden_profiles") or []):
        for attribute_id, level_id in combo.items():
            if str(attribute_id) not in levels or str(level_id) not in levels[str(attribute_id)]:
                problems.append((f"{path}.constraints.forbidden_profiles[{f_index}]", f"{attribute_id}={level_id} is not a level of the block", "reference.experiment"))
    presentation = block.get("presentation") or {}
    problems += _check_order(presentation.get("attribute_order"), attribute_ids, f"{path}.presentation.attribute_order", "attribute")
    if alternatives.get("mode") == "fixed":
        problems += _check_order(presentation.get("alternative_order"), option_ids, f"{path}.presentation.alternative_order", "alternative")
    design = block.get("design") or {}
    if design.get("mode") == "supplied":
        problems += _check_supplied_conjoint(block, path, option_ids, levels)
    else:
        if design.get("versions"):
            problems.append((f"{path}.design.versions", "versions are for a supplied design", "experiment.design"))
        if not design.get("tasks_per_participant"):
            problems.append((f"{path}.design.tasks_per_participant", "a generated design says how many tasks each twin gets", "experiment.design"))
    if not problems:
        problems += [(f"{path}.design", message, "experiment.infeasible") for message in conjoint_feasibility(block)]
    return problems


def _check_supplied_conjoint(block: dict[str, Any], path: str, option_ids: list[str], levels: dict[str, list[str]]) -> list[tuple[str, str, str]]:
    problems = []
    design = block.get("design") or {}
    versions = design.get("versions") or []
    if not versions:
        problems.append((f"{path}.design.versions", "a supplied design lists its versions", "experiment.design"))
    rules = ConjointRules(block)
    fixed = (block.get("alternatives") or {}).get("mode") == "fixed"
    fixed_levels = {a["id"]: a["fixed"] for a in rules.alternatives}
    for v_index, version in enumerate(versions):
        for t_index, task in enumerate(version.get("tasks") or []):
            here = f"{path}.design.versions[{v_index}].tasks[{t_index}]"
            ids = [str(a.get("id")) for a in task.get("alternatives") or []]
            if fixed and sorted(ids) != sorted(option_ids):
                problems.append((here, f"lists alternatives {', '.join(ids)}; the block's are {', '.join(option_ids)}", "experiment.design"))
            elif not fixed and len(ids) != len(option_ids):
                problems.append((here, f"has {len(ids)} alternatives; the block shows {len(option_ids)}", "experiment.design"))
            for a_index, alternative in enumerate(task.get("alternatives") or []):
                profile = {str(k): str(v) for k, v in (alternative.get("levels") or {}).items()}
                if sorted(profile) != sorted(levels):
                    problems.append((f"{here}.alternatives[{a_index}].levels", "gives a level for every attribute, and only those", "experiment.design"))
                    continue
                bad = [f"{k}={v}" for k, v in profile.items() if v not in levels[k]]
                if bad:
                    problems.append((f"{here}.alternatives[{a_index}].levels", f"not levels of the block: {', '.join(bad)}", "reference.experiment"))
                    continue
                if fixed and any(profile.get(k) != v for k, v in fixed_levels.get(str(alternative.get("id")), {}).items()):
                    problems.append((f"{here}.alternatives[{a_index}].levels", "differs from the alternative's fixed levels", "experiment.design"))
                if not rules.valid(profile):
                    problems.append((f"{here}.alternatives[{a_index}].levels", "breaks the block's constraints", "experiment.constraints"))
    return problems


def _check_maxdiff(block: dict[str, Any], path: str) -> list[tuple[str, str, str]]:
    problems: list[tuple[str, str, str]] = []
    items = block.get("items") or []
    ids = [str(i.get("id")) for i in items]
    for dup in _dups(ids):
        problems.append((f"{path}.items", f"two items have the ID {dup!r}", "id.duplicate"))
    for dup in _dups([_label(i.get("label")) for i in items]):
        problems.append((f"{path}.items", f"two items read {dup!r}; the model answers with the label", "label.duplicate"))
    response = block.get("response") or {}
    if _label(response.get("best_label")) == _label(response.get("worst_label")):
        problems.append((f"{path}.response", "best_label and worst_label must differ", "experiment.response"))
    design = block.get("design") or {}
    if design.get("mode") == "supplied":
        versions = design.get("versions") or []
        if not versions:
            problems.append((f"{path}.design.versions", "a supplied design lists its versions", "experiment.design"))
        for v_index, version in enumerate(versions):
            for t_index, task in enumerate(version.get("tasks") or []):
                here = f"{path}.design.versions[{v_index}].tasks[{t_index}]"
                unknown = [str(i) for i in task if str(i) not in ids]
                if unknown:
                    problems.append((here, f"not items of the block: {', '.join(unknown)}", "reference.experiment"))
                if _dups([str(i) for i in task]):
                    problems.append((here, "lists an item twice", "experiment.design"))
    else:
        if design.get("versions"):
            problems.append((f"{path}.design.versions", "versions are for a supplied design", "experiment.design"))
        if not design.get("items_per_task") or not design.get("tasks_per_participant"):
            problems.append((f"{path}.design", "a generated design says how many tasks each twin gets and how many items each shows", "experiment.design"))
    presentation = block.get("presentation") or {}
    problems += _check_order(presentation.get("item_order"), ids, f"{path}.presentation.item_order", "item")
    anchor = block.get("anchor")
    if anchor:
        options = (anchor.get("response") or {}).get("options") or []
        for dup in _dups([str(o.get("id")) for o in options]):
            problems.append((f"{path}.anchor.response.options", f"two options have the ID {dup!r}", "id.duplicate"))
        for dup in _dups([_label(o.get("label")) for o in options]):
            problems.append((f"{path}.anchor.response.options", f"two options read {dup!r}", "label.duplicate"))
        if (anchor.get("response") or {}).get("mode") != "single":
            problems.append((f"{path}.anchor.response.mode", "the follow-up is a single choice", "experiment.response"))
    if not problems:
        problems += [(f"{path}.design", message, "experiment.infeasible") for message in maxdiff_feasibility(block)]
    return problems
