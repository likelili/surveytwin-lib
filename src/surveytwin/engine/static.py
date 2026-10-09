"""Each twin's copy of a survey, made before any answer (a static run).

``survey_copies`` walks the definition's flow once per twin: variables are set
in order, a randomizer picks its arms, a branch runs when its condition holds
on the variables set so far, and each block shown adds its questions as the
twin sees them (``realize.shown_question``): a repeated block once per row,
questions shuffled when the block says so, a question or option left out when
its display condition does not hold. Then every text gets the twin's values:
variables, repeat rows, random values; and options their order.

The rules and every random draw are the ones the template planner used, in
the same order: one generator seeded by
the run's seed for orders (repeat rows, a block's questions, options), and
draws keyed by the seed, the twin and the place for randomizer arms and random
values. A survey whose later content depends on an answer runs staged
(``engine.staged``), never here.
"""

from __future__ import annotations

import copy
import hashlib
import json
import random
import re
from typing import Any

from ..runtime.logic_compare import embedded_field_value
from ..runtime.random_pipes import has_random_pipes, resolve_random_pipes_at
from . import choices
from .conditions import VariablesOnly, holds, negate, only_ignored, reads_answers
from .definition import Definition, assignment_value, flow_id, is_assignment_arms
from .experiment import experiment_questions
from .realize import every_text, map_condition_text, map_text, shown_question

EMBEDDED_FIELD = re.compile(r"\$\{e://Field/([^}]+)\}")
LOOP_TOKEN = re.compile(r"\$\{lm://([^}]+)\}")


# ---------------------------------------------------------------- randomizers


def _independent_indexes(*, flow: str, participant_index: int, child_count: int, count: int, seed: int) -> list[int]:
    ranked = sorted(
        (hashlib.sha256(f"{seed}:static-randomizer:{flow}:participant:{participant_index}:child:{index}".encode("utf-8")).hexdigest(), index)
        for index in range(child_count)
    )
    return [index for _digest, index in ranked[:count]]


def _even_schedule(*, flow: str, child_count: int, count: int, participants: int, seed: int) -> list[list[int]]:
    """Arms for every twin in turn, each arm as often as the others (Qualtrics'
    "Evenly Present Elements")."""
    total = max(0, participants * count)
    if child_count <= 0 or total <= 0:
        return []
    bag = [index % child_count for index in range(total)]
    digest = hashlib.sha256(f"{seed}:static-even-presentation:{flow}:{child_count}:{count}:{participants}".encode("utf-8")).hexdigest()
    random.Random(int(digest[:16], 16)).shuffle(bag)
    schedule: list[list[int]] = []
    cursor = 0
    for _participant in range(participants):
        picks: list[int] = []
        for _slot in range(count):
            swap_at = cursor
            for candidate in range(cursor, len(bag)):
                if bag[candidate] not in picks:
                    swap_at = candidate
                    break
            bag[cursor], bag[swap_at] = bag[swap_at], bag[cursor]
            picks.append(bag[cursor])
            cursor += 1
        schedule.append(picks)
    return schedule


def _take(node: dict[str, Any], child_count: int) -> int:
    return max(1, min(int(node.get("take") or 1), child_count))


def even_schedules(flow: list[dict[str, Any]], *, participants: int, seed: int) -> tuple[dict[str, list[list[int]]], dict[str, int]]:
    """Each evenly presenting randomizer's schedule, and how many randomizers of
    each kind the survey has."""
    plan: dict[str, list[list[int]]] = {}
    summary = {"even_presentation_randomizers": 0, "normal_randomizers": 0}

    def walk(nodes: list[dict[str, Any]]) -> None:
        for node in nodes:
            if not isinstance(node, dict):
                continue
            if node.get("type") == "randomize":
                children = node.get("children") or []
                if children:
                    take = _take(node, len(children))
                    arms = take >= len(children) and is_assignment_arms(children)
                    if node.get("evenly", True) is True and (take < len(children) or arms):
                        plan[flow_id(node)] = _even_schedule(
                            flow=flow_id(node), child_count=len(children), count=1 if arms else take, participants=max(0, participants), seed=seed
                        )
                        summary["even_presentation_randomizers"] += 1
                    elif take < len(children) or arms:
                        summary["normal_randomizers"] += 1
            for key in ("then", "else", "children"):
                if isinstance(node.get(key), list):
                    walk(node[key])

    walk(flow)
    return plan, summary


# ---------------------------------------------------------------- text


def fill_variables(text: str, variables: dict[str, Any]) -> str:
    """Each ${e://Field/...} of a variable that is set replaced by its value; the others kept."""
    if not text:
        return text

    def fill(match: re.Match[str]) -> str:
        value = embedded_field_value(match.group(1), variables)
        return match.group(0) if value is None else str(value)

    return EMBEDDED_FIELD.sub(fill, text)


def fill_loop(text: str, row: dict[str, Any] | None) -> str:
    """Each ${lm://...} of a repeat row filled (a field with no value kept)."""
    if not text or not row:
        return text
    fields = row.get("fields") or {}

    def sub(match: re.Match[str]) -> str:
        token = str(match.group(1)).strip()
        if token == "CurrentLoopNumber":
            return str(row.get("current_loop_number", 1))
        if token == "TotalLoops":
            return str(row.get("total_loops", 1))
        if token.startswith("Field/"):
            value = fields.get(token.split("/", 1)[1].strip())
            if value is not None and str(value).strip():
                return str(value)
        return match.group(0)

    return LOOP_TOKEN.sub(sub, text)


def unfilled(text: str) -> list[str]:
    """The variables and repeat values still piped in ``text``, as tokens without ${}."""
    if not text:
        return []
    return [f"e://Field/{field}" for field in EMBEDDED_FIELD.findall(text)] + [f"lm://{token}" for token in LOOP_TOKEN.findall(text)]


def render_value(value: Any, variables: dict[str, Any], *, seed: Any, respondent: Any, place: str) -> str:
    """A value as a variable is set to it: random values drawn (keyed by the twin and
    the place), then other variables filled in (blank when not set)."""
    text = str(value)
    if has_random_pipes(text):
        text = resolve_random_pipes_at(text, seed=seed, respondent=respondent, path=place)
    return EMBEDDED_FIELD.sub("", fill_variables(text, variables))


# ---------------------------------------------------------------- one twin


class _Twin:
    def __init__(self, planner: "Planner", index: int, respondent: str | None, initial: dict[str, Any] | None):
        self.planner = planner
        self.index = index
        self.respondent = respondent
        self.key = respondent if respondent is not None else index  # random values are keyed by the twin
        self.variables: dict[str, Any] = {
            str(key): str(value) for key, value in (initial or {}).items() if str(key).strip() and value is not None
        }
        self.assignments: list[dict[str, str]] = [
            {"source": "outside", "flow_id": "", "field": key, "value": value} for key, value in self.variables.items()
        ]
        self.records: list[dict[str, Any]] = []
        self.pending_hidden: dict[int, dict[str, list[str]]] = {}

    # ---- flow

    def walk(self, nodes: list[dict[str, Any]]) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for node in nodes:
            kind = node.get("type")
            if kind == "set_variables":
                self.assign(node)
            elif kind == "randomize":
                out += self.walk(self.randomize(node))
            elif kind == "group":
                out += self.walk(node.get("children") or [])
            elif kind == "branch":
                for condition, steps in branch_parts(node):
                    if only_ignored(condition) and _ends(steps):
                        # "If the device is a phone, end the survey": a twin has no device,
                        # and ignoring the condition must not end every twin's survey
                        continue
                    if holds(condition, VariablesOnly(self.variables)):
                        out += self.walk(steps)
            elif kind == "show_block":
                out += self.block(str(node.get("block")))
        return out

    def assign(self, node: dict[str, Any]) -> None:
        """Set a step's variables in order; a value is rendered when it is set."""
        flow = flow_id(node)
        rows = [a for a in node.get("assignments") or [] if not a.get("from_outside")]
        set_here = {str(a.get("variable", "") or "").strip() for a in rows}
        times: dict[str, int] = {}
        for assignment in rows:
            field = str(assignment.get("variable", "") or "").strip()
            value = assignment_value(assignment)
            if not (field and value):
                continue  # no value: an earlier or outside value stays
            times[field] = times.get(field, 0) + 1
            again = f"#{times[field]}" if times[field] > 1 else ""
            rendered = render_value(value, self.variables, seed=self.planner.seed, respondent=self.key, place=f"ed/{flow}/{field}{again}")
            self.variables[field] = rendered
            self.assignments.append({"source": "flow", "flow_id": flow, "field": field, "value": rendered})
            compact = field.replace(" ", "")
            if compact and compact != field and compact not in set_here:
                self.variables[compact] = rendered

    def randomize(self, node: dict[str, Any]) -> list[dict[str, Any]]:
        children = node.get("children") or []
        if not children:
            return []
        take = _take(node, len(children))
        flow = flow_id(node) or str(node.get("id") or "")
        planned = self.planner.schedules.get(flow)
        planned = list(planned[self.index % len(planned)]) if planned else None
        if take >= len(children):
            if is_assignment_arms(children):
                chosen = planned[0] % len(children) if planned else _independent_indexes(
                    flow=flow, participant_index=self.index, child_count=len(children), count=1, seed=self.planner.seed
                )[0]
                others = [child for position, child in enumerate(children) if position != chosen]
                self.planner.rng.shuffle(others)
                # the chosen arm runs last: its values are the ones that stay
                return others + [children[chosen]]
            selected = list(children)
            self.planner.rng.shuffle(selected)
            return selected
        if planned:
            return [children[position % len(children)] for position in planned[:take]]
        indexes = _independent_indexes(flow=flow, participant_index=self.index, child_count=len(children), count=take, seed=self.planner.seed)
        return [children[position] for position in indexes]

    # ---- blocks

    def block(self, block_id: str) -> list[dict[str, Any]]:
        block = self.planner.definition.blocks.get(block_id)
        if block is None:
            return []
        if block.get("kind") in {"conjoint", "maxdiff"}:
            tasks, records = experiment_questions(
                block, seed=self.planner.seed, twin=str(self.respondent) if self.respondent else f"pid_{self.index + 1}", twin_index=self.index
            )
            self.records += records
            copies = [{"block": block_id, "title": str(block.get("title") or block_id), "row": None, "questions": [q for task in tasks for q in task]}]
        else:
            copies = self.repeat(block)
        out = []
        for shown_block in copies:
            questions = shown_block["questions"]
            randomization = block.get("randomization") if block.get("kind") == "survey" else None
            if randomization and len(questions) > 1:
                if randomization.get("order") == "shuffle":
                    self.planner.rng.shuffle(questions)
                    shown_block["question_order"] = [q["question"]["id"] for q in questions]
                else:
                    shown_block["warning"] = "Advanced block question randomization was preserved but not executed."
            kept = []
            for shown in questions:
                question = shown["question"]
                condition = question.get("show_if")
                if condition:
                    if not reads_answers(condition) and not holds(condition, VariablesOnly(self.variables)):
                        continue
                if choices.has_item_conditions(question):
                    # options shown on a variable are decided here, with the values set so
                    # far, and left out once the options are ordered ("present 3 of 5"
                    # draws from all of them); options shown on an answer make a survey
                    # staged
                    hidden = choices.hidden_items(question, lambda c: holds(c, VariablesOnly(self.variables)))
                    choices.drop_item_conditions(question)
                    if hidden:
                        self.pending_hidden[id(shown)] = hidden
                map_text(question, lambda _place, text: fill_variables(text, self.variables))
                if condition and not reads_answers(condition):
                    question.pop("show_if", None)
                kept.append(shown)
            shown_block["questions"] = kept
            if kept:
                out.append(shown_block)
        return out

    def repeat(self, block: dict[str, Any]) -> list[dict[str, Any]]:
        """The block once per repeat row (Qualtrics Loop & Merge), or once."""
        native = self.planner.definition.native
        questions = [
            shown_question(self.planner.definition.questions[str(qid)], native=native, brand=self.planner.definition.brand)
            for qid in block.get("questions") or []
            if str(qid) in self.planner.definition.questions
        ]
        title = str(block.get("title") or "")
        repeat = block.get("repeat")
        if not repeat or not repeat.get("rows"):
            return [{"block": str(block["id"]), "title": title, "row": None, "questions": questions}]
        rows = list(repeat["rows"])
        if repeat.get("order") == "shuffle":
            self.planner.rng.shuffle(rows)
        if repeat.get("take"):
            rows = rows[: int(repeat["take"])]
        out = []
        for number, row in enumerate(rows, start=1):
            from ..spec.text import join_text

            context = {
                "row_id": str(row["id"]),
                "fields": {str(k): join_text(v) for k, v in (row.get("values") or {}).items()},
                "current_loop_number": number,
                "total_loops": len(rows),
            }
            copies = []
            for shown in questions:
                shown = copy.deepcopy(shown)
                question = shown["question"]
                question["id"] = f"{row['id']}_{question['id']}"
                for field in (question.get("response") or {}).get("fields") or [] if (question.get("response") or {}).get("type") == "group" else []:
                    source = field.get("source") if isinstance(field.get("source"), dict) else {}
                    if str(source.get("QuestionID", "") or "").strip():
                        source["QuestionID"] = f"{row['id']}_{str(source['QuestionID']).strip()}"
                map_text(question, lambda _place, text: fill_loop(text, context))
                for key in ("show_if", "skip"):
                    if key in question:
                        question[key] = map_condition_text(question[key], lambda text: fill_loop(text, context))
                _map_item_conditions(question, lambda text: fill_loop(text, context))
                shown["row"] = context
                copies.append(shown)
            out.append({"block": f"{block['id']}_{row['id']}", "title": f"{title} (Loop {number}/{len(rows)})", "row": context, "questions": copies})
        return out


def branch_parts(node: dict[str, Any]) -> list[tuple[dict[str, Any], list[dict[str, Any]]]]:
    """A branch as the steps it runs and when: its ``then`` steps when its condition
    holds; its ``else`` steps, decided after them, when the opposite holds."""
    when = node.get("when") or {"op": "true"}
    parts = [(when, node.get("then") or [])]
    if node.get("else"):
        try:
            other = negate(when)
        except ValueError:
            other = {"op": "not", "arg": when}
        parts.append((other, node["else"]))
    return parts


def _map_item_conditions(question: dict[str, Any], fn: Any) -> None:
    for condition_holder in _items_with_conditions(question):
        condition_holder["show_if"] = map_condition_text(condition_holder["show_if"], fn)


def _items_with_conditions(question: dict[str, Any]) -> list[dict[str, Any]]:
    response = question.get("response") if isinstance(question.get("response"), dict) else {}
    found = []
    for key in ("options", "items", "fields", "rows", "columns"):
        found += [item for item in response.get(key) or [] if isinstance(item, dict) and item.get("show_if")]
    row_response = response.get("row_response") if isinstance(response.get("row_response"), dict) else {}
    found += [item for item in row_response.get("options") or [] if isinstance(item, dict) and item.get("show_if")]
    return found


def _ends(nodes: list[dict[str, Any]]) -> bool:
    return any(
        isinstance(node, dict) and (node.get("type") == "end" or _ends(node.get("then") or []) or _ends(node.get("else") or []) or _ends(node.get("children") or []))
        for node in nodes
    )


# ---------------------------------------------------------------- every twin


class Planner:
    def __init__(self, definition: Definition, *, seed: Any, participants: int):
        self.definition = definition
        self.rng = random.Random(seed)
        self.seed = int(seed) if seed is not None else self.rng.getrandbits(64)
        self.schedules, self.summary = even_schedules(definition.flow, participants=participants, seed=self.seed)

    def copy_for(
        self,
        index: int,
        *,
        respondent: str | None = None,
        initial_variables: dict[str, Any] | None = None,
        borrow_unset: bool = False,
    ) -> tuple[dict[str, Any], list[str]]:
        twin = _Twin(self, index, respondent, initial_variables)
        blocks = twin.walk(self.definition.flow)
        questions = [shown for block in blocks for shown in block["questions"]]

        # A variable piped where it is not set (never on this path, or only later)
        # is blank, as for a Qualtrics respondent. With borrowing (the website's
        # behavior), one never set on this path takes the first value set anywhere.
        fills: dict[str, str] = {}
        never_set: list[str] = []
        borrowed_from: dict[str, Any] | None = None
        for token in _unfilled_tokens(questions):
            if not token.startswith("e://Field/"):
                continue
            field = token.removeprefix("e://Field/")
            if not borrow_unset or embedded_field_value(field, twin.variables) is not None:
                fills[field] = ""
                continue
            if borrowed_from is None:
                borrowed_from = _first_values(self.definition.flow)
            value = embedded_field_value(field, borrowed_from)
            if value is None:
                value = borrowed_from.get(field.replace(" ", ""))
            if value is None:
                fills[field] = ""
                never_set.append(token)
                continue
            value = render_value(value, twin.variables, seed=self.seed, respondent=twin.key, place=f"borrowed/{field}")
            twin.variables[field] = value
            fills[field] = value
        if fills:
            with_values = {field: value for field, value in fills.items() if str(value) != ""}
            for shown in questions:
                map_text(
                    shown["question"],
                    lambda place, text: fill_variables(text, with_values if place.startswith("Default") else fills),
                )

        # a repeat value piped outside its repeated block: a row chosen as before
        row = _fallback_row(self.definition, twin.variables)
        for shown in questions:
            map_text(shown["question"], lambda _place, text: fill_loop(text, row))
            question = shown["question"]
            if has_random_pipes(json.dumps(question, ensure_ascii=False)):
                qid = str(question.get("id", "") or "")
                map_text(question, lambda place, text: resolve_random_pipes_at(text, seed=self.seed, respondent=twin.key, path=f"q/{qid}/{place}"))
        for shown in questions:
            applied = choices.order_items(shown["question"], self.rng, index)
            if applied:
                shown["order"] = applied
        for block in blocks:
            kept = []
            for shown in block["questions"]:
                hidden = twin.pending_hidden.get(id(shown))
                if hidden:
                    if not choices.remove_items(shown["question"], hidden):
                        continue
                    shown["hidden"] = [item for ids in hidden.values() for item in ids]
                kept.append(shown)
            block["questions"] = kept
        copy_ = {"blocks": blocks, "variables": twin.variables, "assignments": twin.assignments}
        if twin.records:
            copy_["experiments"] = twin.records
        unresolved = _unfilled_tokens([shown for block in blocks for shown in block["questions"]])
        unresolved += [token for token in dict.fromkeys(never_set) if token not in unresolved]
        return json.loads(json.dumps(copy_, ensure_ascii=False)), unresolved


def _unfilled_tokens(questions: list[dict[str, Any]]) -> list[str]:
    found: set[str] = set()
    for shown in questions:
        for text in every_text(shown["question"]):
            found.update(unfilled(text))
    return list(found)


def _first_values(flow: list[dict[str, Any]]) -> dict[str, Any]:
    """The first value each variable is set to anywhere in the flow (for borrowing)."""
    values: dict[str, Any] = {}

    def walk(nodes: list[dict[str, Any]]) -> None:
        for node in nodes:
            if not isinstance(node, dict):
                continue
            if node.get("type") == "set_variables":
                for assignment in node.get("assignments") or []:
                    field = str(assignment.get("variable", "") or "")
                    value = assignment_value(assignment)
                    if assignment.get("from_outside") or not (field and value):
                        continue
                    values.setdefault(field, value)
                    compact = field.replace(" ", "")
                    if compact and compact != field:
                        values.setdefault(compact, value)
            for key in ("then", "else", "children"):
                if isinstance(node.get(key), list):
                    walk(node[key])

    walk(flow)
    return values


def _fallback_row(definition: Definition, variables: dict[str, Any]) -> dict[str, Any] | None:
    """The repeat row whose values fill a repeat value piped outside its block: the
    row the source named, else one whose ID a variable holds, else the first row
    of the first repeated block (as the template planner chose it)."""
    tables = []
    for block in definition.blocks.values():
        source = block.get("source") if isinstance(block.get("source"), dict) else {}
        options = source.get("options") if isinstance(source.get("options"), dict) else None
        if options is not None:
            looping = options.get("LoopingOptions")
            static = looping.get("Static") if isinstance(looping, dict) else None
            if not isinstance(static, dict) or not static:
                continue
            match = re.search(r"[?&]v=([^&]+)", str(looping.get("Locator", "")))
            total_raw = options.get("TotalRandSubset") or looping.get("TotalRandSubset")
            try:
                total = int(total_raw) if total_raw is not None else len(static)
            except Exception:
                total = len(static)
            tables.append({"static": static, "selected": match.group(1).strip() if match else "", "total": max(total, 1)})
        elif block.get("repeat"):
            from ..spec.text import join_text

            repeat = block["repeat"]
            static = {str(r["id"]): {k: join_text(v) for k, v in (r.get("values") or {}).items()} for r in repeat.get("rows") or []}
            if static:
                tables.append({"static": static, "selected": "", "total": max(int(repeat.get("take") or len(static)), 1)})
    if not tables:
        return None
    candidates = [str(value).strip() for value in variables.values() if str(value).strip()]
    for table in tables:
        static = table["static"]
        row_id = table["selected"] if table["selected"] and table["selected"] in static else next((c for c in candidates if c in static), "")
        if not row_id:
            continue
        row = static.get(row_id) if isinstance(static.get(row_id), dict) else {}
        positions = {key: position + 1 for position, key in enumerate(static)}
        return {"row_id": row_id, "fields": {str(k): str(v) for k, v in row.items()}, "current_loop_number": positions.get(row_id, 1), "total_loops": table["total"]}
    first = tables[0]["static"]
    row_id = next(iter(first))
    row = first.get(row_id) if isinstance(first.get(row_id), dict) else {}
    return {"row_id": str(row_id), "fields": {str(k): str(v) for k, v in row.items()}, "current_loop_number": 1, "total_loops": tables[0]["total"]}


def survey_copies(
    definition: Definition,
    count: int,
    *,
    seed: Any,
    respondent_ids: list[str] | None = None,
    initial_variables: dict[str, Any] | None = None,
    borrow_unset: bool = False,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Every twin's copy of the survey, in order, and what was drawn."""
    planner = Planner(definition, seed=seed, participants=count)
    copies: list[dict[str, Any]] = []
    unresolved: set[str] = set()
    for index in range(count):
        copy_, left = planner.copy_for(
            index,
            respondent=respondent_ids[index] if respondent_ids and index < len(respondent_ids) else None,
            initial_variables=initial_variables,
            borrow_unset=borrow_unset,
        )
        unresolved.update(left)
        copies.append(copy_)
    stats: dict[str, Any] = {
        "success": not unresolved,
        "num_participants": count,
        **planner.summary,
        "randomizer_assignment_mode": "mode_aware",
        "generated": len(copies),
        "unresolved_placeholders": list(unresolved),
    }
    if unresolved:
        stats["error"] = "Unresolved placeholders remain after QSF processing. This file likely relies on unsupported external piping/loop fields."
    return copies, stats


# ---------------------------------------------------------------- design cells


def _updates(node: dict[str, Any]) -> dict[str, Any]:
    """The values a set-variables step writes, as written (an empty value too)."""
    out: dict[str, Any] = {}
    for assignment in node.get("assignments") or []:
        field = str(assignment.get("variable", "") or "").strip()
        if field and not assignment.get("from_outside"):
            out[field] = assignment_value(assignment)
    return out


def _cells(nodes: list[dict[str, Any]], variables: dict[str, Any]) -> list[tuple[dict[str, int], dict[str, Any]]]:
    cells: list[tuple[dict[str, int], dict[str, Any]]] = [({}, dict(variables))]
    for node in nodes:
        if not isinstance(node, dict):
            continue
        kind = node.get("type")
        if kind == "set_variables":
            updates = _updates(node)
            if updates:
                cells = [(arms, {**values, **updates}) for arms, values in cells]
            continue
        if kind == "randomize":
            children = node.get("children") or []
            if not children:
                continue
            take = min(int(node.get("take") or 1), len(children))
            flow = flow_id(node)
            new: list[tuple[dict[str, int], dict[str, Any]]] = []
            if take >= len(children):
                if is_assignment_arms(children):
                    for arms, values in cells:
                        for arm, child in enumerate(children):
                            new.append(({**arms, flow: arm}, {**values, **_updates(child)}))
                else:
                    for arms, values in cells:
                        inner = _cells(children, values)
                        new += [({**arms, **sub}, dict(sub_values)) for sub, sub_values in inner] or [(arms, values)]
            elif take == 1:
                for arms, values in cells:
                    for arm, child in enumerate(children):
                        inner = _cells([child], values) or [({}, dict(values))]
                        new += [({**arms, flow: arm, **sub}, dict(sub_values)) for sub, sub_values in inner]
            else:
                for arms, values in cells:
                    for start in range(len(children)):
                        window = [children[(start + offset) % len(children)] for offset in range(take)]
                        inner = _cells(window, values) or [({}, dict(values))]
                        new += [({**arms, flow: start, **sub}, dict(sub_values)) for sub, sub_values in inner]
            cells = new
            continue
        if kind == "branch":
            for condition, steps in branch_parts(node):
                new = []
                for arms, values in cells:
                    taken = not (only_ignored(condition) and _ends(steps)) and holds(condition, VariablesOnly(values))
                    inner = _cells(steps, values) if taken and steps else []
                    new += [({**arms, **sub}, dict(sub_values)) for sub, sub_values in inner] or [(arms, values)]
                cells = new
            continue
        if kind == "group" and node.get("children"):
            new = []
            for arms, values in cells:
                inner = _cells(node["children"], values)
                new += [({**arms, **sub}, dict(sub_values)) for sub, sub_values in inner] or [(arms, values)]
            cells = new
    return cells


def design_cells(definition: Definition) -> int:
    """How many distinct randomizer assignments the flow can give a twin (branches
    decided on the variables each assignment sets)."""
    return len(_cells(definition.flow, {}))
