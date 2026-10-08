"""Each twin's conjoint profiles and MaxDiff subsets, drawn from the block's rules.

Every draw comes from a random generator seeded by the run's seed, the twin and
the block (``design_rng``), so the same twin gets the same tasks whenever its
survey is prepared again: in a static copy, at every stage of a staged run, in a
preview or a rerun. Nothing is redrawn.

Generators (the version is recorded with each design):

- conjoint ``random_valid_profiles`` (version 1): for each alternative, every
  attribute not fixed by the alternative gets a level drawn by its weights
  (weight 0 never draws); a profile that breaks an applicability matrix or a
  forbidden combination is drawn again in full; a task with two identical
  profiles (``duplicates: within_task``), or a profile the twin already saw
  (``within_participant``), is drawn again. Rejection changes how often levels
  appear among the profiles kept: the weights are the draw, not the outcome.
  A design is feasible when every alternative has a valid profile (checked in
  full when there are at most 200,000 combinations); a draw that fails
  ``max_attempts`` times stops with ``DesignError`` instead of weakening a rule.
- MaxDiff ``uniform_subsets`` (version 1): each task draws ``items_per_task``
  different items, every subset equally likely; with ``duplicate_tasks:
  forbid``, a subset the twin already saw (in any order) is drawn again. No
  balance is promised.
- MaxDiff ``balanced_subsets`` (version 1): each task takes the items the twin
  has seen least (ties broken at random, preferring items it has seen least
  together with those already taken). Within a twin, how often items appear
  differs by at most one; pair balance is the best this greedy rule finds, not a
  proven optimum; whether every item is linked to every other through shared
  tasks is reported, not forced.
- ``supplied`` designs: each twin gets one of the block's versions, at random or
  by its position in the sample (``assignment: by_index``).
"""

from __future__ import annotations

import hashlib
import itertools
import random
from typing import Any

from ..spec.text import join_text

CONJOINT_GENERATOR_VERSION = "random_valid_profiles/1"
MAXDIFF_GENERATOR_VERSIONS = {"uniform_subsets": "uniform_subsets/1", "balanced_subsets": "balanced_subsets/1"}
ENUMERATION_LIMIT = 200_000
DEFAULT_MAX_ATTEMPTS = 1000


class DesignError(ValueError):
    """A design cannot be drawn: its rules allow no valid task, or too few."""


def design_rng(seed: Any, twin: str, block_id: str, purpose: str = "design") -> random.Random:
    """The random generator of one twin's design for one block."""
    digest = hashlib.sha256(f"{seed}:{twin}:{block_id}:{purpose}".encode("utf-8")).hexdigest()
    return random.Random(int(digest[:16], 16))


def letter(position: int) -> str:
    """1 -> A, 26 -> Z, 27 -> AA."""
    out = ""
    while position > 0:
        position, rest = divmod(position - 1, 26)
        out = chr(65 + rest) + out
    return out


# ---------------------------------------------------------------- conjoint rules


class ConjointRules:
    """A conjoint block's attributes, alternatives and constraints, ready to test profiles."""

    def __init__(self, block: dict[str, Any]):
        self.block = block
        self.attributes = [a for a in block.get("attributes") or []]
        self.attribute_ids = [str(a["id"]) for a in self.attributes]
        self.levels = {str(a["id"]): [lv for lv in a.get("levels") or []] for a in self.attributes}
        self.level_index = {
            (str(a["id"]), str(lv["id"])): lv for a in self.attributes for lv in a.get("levels") or []
        }
        constraints = block.get("constraints") or {}
        self.matrices = []
        for matrix in constraints.get("applicability") or []:
            allowed = {
                (str(r), str(c))
                for i, r in enumerate(matrix.get("row_levels") or [])
                for j, c in enumerate(matrix.get("column_levels") or [])
                if (matrix.get("allowed") or [])[i][j]
            }
            self.matrices.append((str(matrix["row_attribute"]), str(matrix["column_attribute"]), allowed))
        self.forbidden = [{str(k): str(v) for k, v in combo.items()} for combo in constraints.get("forbidden_profiles") or []]
        alternatives = block.get("alternatives") or {}
        if alternatives.get("mode") == "fixed":
            self.alternatives = [
                {"id": str(item["id"]), "label": item["label"], "fixed": {str(k): str(v) for k, v in (item.get("fixed_levels") or {}).items()}}
                for item in alternatives.get("items") or []
            ]
            self.generic = False
        else:
            count = int(alternatives.get("count") or 2)
            self.alternatives = [{"id": str(n), "label": None, "fixed": {}} for n in range(1, count + 1)]
            self.generic = True
            self.label_template = str(alternatives.get("label") or "Option {letter}")

    def valid(self, profile: dict[str, str]) -> bool:
        for row_attribute, column_attribute, allowed in self.matrices:
            if row_attribute in profile and column_attribute in profile and (profile[row_attribute], profile[column_attribute]) not in allowed:
                return False
        for combo in self.forbidden:
            if all(profile.get(k) == v for k, v in combo.items()):
                return False
        return True

    def drawable(self, attribute_id: str, fixed: dict[str, str]) -> list[dict[str, Any]]:
        if attribute_id in fixed:
            return [lv for lv in self.levels[attribute_id] if str(lv["id"]) == fixed[attribute_id]]
        return [lv for lv in self.levels[attribute_id] if float(lv.get("weight", 1)) > 0]

    def combinations(self, fixed: dict[str, str]) -> int:
        total = 1
        for attribute_id in self.attribute_ids:
            total *= max(len(self.drawable(attribute_id, fixed)), 0)
        return total

    def valid_profiles(self, fixed: dict[str, str]) -> list[dict[str, str]] | None:
        """Every valid profile of an alternative, or None when there are too many to list."""
        if self.combinations(fixed) > ENUMERATION_LIMIT:
            return None
        pools = [[str(lv["id"]) for lv in self.drawable(a, fixed)] for a in self.attribute_ids]
        out = []
        for combo in itertools.product(*pools):
            profile = dict(zip(self.attribute_ids, combo))
            if self.valid(profile):
                out.append(profile)
        return out

    def draw(self, rng: random.Random, fixed: dict[str, str]) -> dict[str, str]:
        profile = {}
        for attribute_id in self.attribute_ids:
            pool = self.drawable(attribute_id, fixed)
            weights = [float(lv.get("weight", 1)) for lv in pool]
            profile[attribute_id] = str(rng.choices(pool, weights=weights, k=1)[0]["id"]) if attribute_id not in fixed else fixed[attribute_id]
        return profile


def conjoint_feasibility(block: dict[str, Any]) -> list[str]:
    """Why a conjoint block cannot be drawn (empty when it can, as far as can be checked)."""
    rules = ConjointRules(block)
    design = block.get("design") or {}
    if design.get("mode") == "supplied":
        return []
    problems: list[str] = []
    tasks = int(design.get("tasks_per_participant") or 1)
    duplicates = design.get("duplicates", "within_task")
    for alternative in rules.alternatives:
        for attribute_id in rules.attribute_ids:
            if not rules.drawable(attribute_id, alternative["fixed"]):
                problems.append(f"alternative {alternative['id']!r}: attribute {attribute_id!r} has no level that can be drawn")
        profiles = rules.valid_profiles(alternative["fixed"])
        if profiles is None:
            continue
        if not profiles:
            problems.append(f"alternative {alternative['id']!r}: no combination of levels passes the constraints")
        elif duplicates == "within_participant" and len(profiles) < tasks:
            problems.append(
                f"alternative {alternative['id']!r}: {len(profiles)} valid profiles, fewer than the {tasks} tasks that may not repeat one"
            )
    if rules.generic and duplicates in {"within_task", "within_participant"}:
        profiles = rules.valid_profiles({})
        if profiles is not None:
            needed = len(rules.alternatives) * (tasks if duplicates == "within_participant" else 1)
            if len(profiles) < needed:
                problems.append(f"{len(profiles)} valid profiles, fewer than the {needed} different ones the design needs")
    return problems


def draw_conjoint(block: dict[str, Any], rng: random.Random, *, twin_index: int = 0) -> dict[str, Any]:
    """One twin's tasks: for each task, its alternatives (in the order generated) with their levels."""
    rules = ConjointRules(block)
    design = block.get("design") or {}
    if design.get("mode") == "supplied":
        return _supplied(block, rng, twin_index)
    tasks_wanted = int(design.get("tasks_per_participant") or 1)
    duplicates = design.get("duplicates", "within_task")
    max_attempts = int(design.get("max_attempts") or DEFAULT_MAX_ATTEMPTS)
    seen: set[tuple[tuple[str, str], ...]] = set()
    tasks = []
    for task_number in range(1, tasks_wanted + 1):
        for _attempt in range(max_attempts):
            alternatives = []
            for alternative in rules.alternatives:
                for _profile_attempt in range(max_attempts):
                    profile = rules.draw(rng, alternative["fixed"])
                    if rules.valid(profile):
                        break
                else:
                    raise DesignError(
                        f"conjoint block {block.get('id')!r}: no valid profile for alternative {alternative['id']!r} "
                        f"in {max_attempts} draws; check its constraints"
                    )
                alternatives.append({"id": alternative["id"], "levels": profile})
            keys = [tuple(sorted(a["levels"].items())) for a in alternatives]
            if duplicates in {"within_task", "within_participant"} and len(set(keys)) < len(keys):
                continue
            if duplicates == "within_participant" and any(key in seen for key in keys):
                continue
            seen.update(keys)
            tasks.append({"task": task_number, "alternatives": alternatives})
            break
        else:
            raise DesignError(
                f"conjoint block {block.get('id')!r}: task {task_number} could not be drawn without a duplicate profile "
                f"in {max_attempts} tries; allow duplicates or add levels"
            )
    return {"generator": CONJOINT_GENERATOR_VERSION, "tasks": tasks}


def _supplied(block: dict[str, Any], rng: random.Random, twin_index: int) -> dict[str, Any]:
    design = block.get("design") or {}
    versions = design.get("versions") or []
    if design.get("assignment") == "by_index":
        version = versions[twin_index % len(versions)]
    else:
        version = rng.choice(versions)
    if block.get("kind") == "maxdiff":
        tasks = [{"task": n, "items": [str(i) for i in task]} for n, task in enumerate(version.get("tasks") or [], start=1)]
    else:
        tasks = [
            {"task": n, "source_id": str(task.get("id")), "alternatives": [{"id": str(a["id"]), "levels": {str(k): str(v) for k, v in a["levels"].items()}} for a in task.get("alternatives") or []]}
            for n, task in enumerate(version.get("tasks") or [], start=1)
        ]
    return {"generator": "supplied", "version": str(version.get("id")), "tasks": tasks}


def order(ids: list[str], setting: dict[str, Any] | None, rng_participant: random.Random, rng_task: random.Random) -> list[str]:
    """``ids`` in the order a presentation setting gives: as authored, or shuffled
    once per twin (``scope: participant``) or per task, keeping fixed positions."""
    setting = setting or {}
    if setting.get("mode") != "shuffle":
        return list(ids)
    rng = rng_task if setting.get("scope") == "task" else rng_participant
    fixed = {str(k): int(v) for k, v in (setting.get("fixed_positions") or {}).items() if str(k) in ids}
    free = [i for i in ids if i not in fixed]
    rng.shuffle(free)
    out: list[str | None] = [None] * len(ids)
    for item, position in fixed.items():
        if 1 <= position <= len(ids) and out[position - 1] is None:
            out[position - 1] = item
        else:
            free.insert(0, item)
    rest = iter(free)
    return [slot if slot is not None else next(rest) for slot in out]


# ---------------------------------------------------------------- MaxDiff


def maxdiff_feasibility(block: dict[str, Any]) -> list[str]:
    design = block.get("design") or {}
    items = [str(i["id"]) for i in block.get("items") or []]
    problems = []
    if design.get("mode") == "supplied":
        return problems
    k = int(design.get("items_per_task") or 0)
    tasks = int(design.get("tasks_per_participant") or 1)
    if k > len(items):
        problems.append(f"{k} items per task, but the pool has {len(items)}")
    elif design.get("duplicate_tasks", "forbid") == "forbid":
        from math import comb

        if comb(len(items), k) < tasks:
            problems.append(f"only {comb(len(items), k)} different subsets of {k} items, fewer than the {tasks} tasks that may not repeat one")
    return problems


def draw_maxdiff(block: dict[str, Any], rng: random.Random, *, twin_index: int = 0) -> dict[str, Any]:
    design = block.get("design") or {}
    if design.get("mode") == "supplied":
        return _supplied(block, rng, twin_index)
    items = [str(i["id"]) for i in block.get("items") or []]
    k = int(design.get("items_per_task") or 0)
    tasks_wanted = int(design.get("tasks_per_participant") or 1)
    forbid = design.get("duplicate_tasks", "forbid") == "forbid"
    max_attempts = int(design.get("max_attempts") or DEFAULT_MAX_ATTEMPTS)
    generator = design.get("generator") or "uniform_subsets"
    counts = {item: 0 for item in items}
    pairs: dict[frozenset[str], int] = {}
    seen: set[frozenset[str]] = set()
    tasks = []
    for task_number in range(1, tasks_wanted + 1):
        for _attempt in range(max_attempts):
            if generator == "balanced_subsets":
                subset = _balanced_subset(items, k, counts, pairs, rng)
            else:
                subset = rng.sample(items, k)
            if forbid and frozenset(subset) in seen:
                continue
            break
        else:
            raise DesignError(
                f"MaxDiff block {block.get('id')!r}: task {task_number} repeats an earlier subset in every one of "
                f"{max_attempts} draws; allow repeated tasks or add items"
            )
        seen.add(frozenset(subset))
        for item in subset:
            counts[item] += 1
        for a, b in itertools.combinations(subset, 2):
            pairs[frozenset((a, b))] = pairs.get(frozenset((a, b)), 0) + 1
        tasks.append({"task": task_number, "items": list(subset)})
    return {"generator": MAXDIFF_GENERATOR_VERSIONS.get(generator, generator), "tasks": tasks}


def _balanced_subset(items: list[str], k: int, counts: dict[str, int], pairs: dict[frozenset[str], int], rng: random.Random) -> list[str]:
    """The k items seen least; among items seen equally often, those seen least
    with the items already taken (ties at random)."""
    chosen: list[str] = []
    candidates = list(items)
    rng.shuffle(candidates)
    while len(chosen) < k:
        lowest = min(counts[i] for i in candidates if i not in chosen)
        tied = [i for i in candidates if i not in chosen and counts[i] == lowest]
        best = min(tied, key=lambda i: sum(pairs.get(frozenset((i, c)), 0) for c in chosen))
        chosen.append(best)
    return chosen


def maxdiff_diagnostics(block: dict[str, Any], designs: list[dict[str, Any]]) -> dict[str, Any]:
    """How often each item and pair appeared across twins' designs, items never shown,
    and whether every item is linked to every other through shared tasks."""
    items = [str(i["id"]) for i in block.get("items") or []]
    counts = {i: 0 for i in items}
    pairs: dict[str, int] = {}
    links = {i: set() for i in items}
    for design in designs:
        for task in design.get("tasks") or []:
            subset = [str(i) for i in task.get("items") or []]
            for i in subset:
                counts[i] = counts.get(i, 0) + 1
            for a, b in itertools.combinations(sorted(subset), 2):
                pairs[f"{a}|{b}"] = pairs.get(f"{a}|{b}", 0) + 1
                links.setdefault(a, set()).add(b)
                links.setdefault(b, set()).add(a)
    reached: set[str] = set()
    stack = [items[0]] if items else []
    while stack:
        node = stack.pop()
        if node in reached:
            continue
        reached.add(node)
        stack.extend(links.get(node, set()) - reached)
    return {
        "item_counts": counts,
        "items_never_shown": [i for i, c in counts.items() if c == 0],
        "pair_counts": pairs,
        "connected": len(reached) == len(items),
    }


def conjoint_diagnostics(block: dict[str, Any], designs: list[dict[str, Any]]) -> dict[str, Any]:
    """How often each level appeared across twins' designs (all alternatives)."""
    counts: dict[str, dict[str, int]] = {str(a["id"]): {str(lv["id"]): 0 for lv in a.get("levels") or []} for a in block.get("attributes") or []}
    profiles = 0
    for design in designs:
        for task in design.get("tasks") or []:
            for alternative in task.get("alternatives") or []:
                profiles += 1
                for attribute_id, level_id in (alternative.get("levels") or {}).items():
                    counts.setdefault(attribute_id, {}).setdefault(level_id, 0)
                    counts[attribute_id][level_id] += 1
    return {"profiles": profiles, "level_counts": counts}


def label_text(value: Any) -> str:
    return join_text(value).strip()
