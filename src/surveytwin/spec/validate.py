"""Checks of meaning: what a well-formed survey must also get right before it runs.

``check_structure`` (model.py) checks shapes: types, required fields, known keys.
``check_survey`` runs that, then checks that references resolve (blocks, questions,
options, skip targets, piped answers), that limits are possible (at least what is
at most), that IDs are unique where answers are recorded by them, and that a
written survey gives the model no two options with the same label. Each problem
has a path and a code; warnings do not stop a survey, errors do.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterator

from .model import check_structure
from .schema import Problem
from .text import join_text, text_refs


@dataclass(frozen=True)
class Finding:
    """A problem (``level`` "error") or a warning in a survey definition."""

    path: str
    message: str
    code: str
    level: str = "error"

    def __str__(self) -> str:
        return f"{self.level}: {self.path}: {self.message}"


IMPORTED_FORMATS = {"qsf", "template", "survey_builder"}


def _ids(items: Any) -> list[str]:
    return [str(item.get("id")) for item in items or [] if isinstance(item, dict) and "id" in item]


def _duplicates(values: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for value in values:
        if value in seen and value not in out:
            out.append(value)
        seen.add(value)
    return out


def _conditions(value: Any, path: str) -> Iterator[tuple[dict[str, Any], str]]:
    """Every condition node in a condition tree, with its path."""
    if not isinstance(value, dict):
        return
    yield value, path
    for index, arg in enumerate(value.get("args") or []):
        yield from _conditions(arg, f"{path}.args[{index}]")
    if isinstance(value.get("arg"), dict):
        yield from _conditions(value["arg"], f"{path}.arg")


def _text_values(value: Any, path: str) -> Iterator[tuple[Any, str]]:
    """Every text value that may pipe something, with its path (labels, text, values)."""
    if isinstance(value, dict):
        if isinstance(value.get("parts"), list):
            yield value, path
            return
        for key, child in value.items():
            if key in {"source", "qualtrics"}:
                continue
            yield from _text_values(child, f"{path}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            yield from _text_values(child, f"{path}[{index}]")


def _repeat_order(rows: list[dict[str, Any]]) -> list[str]:
    ids = [str(row.get("id")) for row in rows]
    try:
        return sorted(ids, key=lambda item: int(item))
    except ValueError:
        return sorted(ids)


class _Checker:
    def __init__(self, survey: dict[str, Any]):
        self.survey = survey
        self.findings: list[Finding] = []
        source = survey.get("source") if isinstance(survey.get("source"), dict) else {}
        self.imported = source.get("format") in IMPORTED_FORMATS
        self.questions = {str(q.get("id")): q for q in survey.get("questions") or [] if isinstance(q, dict)}
        self.blocks = {str(b.get("id")): b for b in survey.get("blocks") or [] if isinstance(b, dict)}
        self.question_paths = {str(q.get("id")): f"$.questions[{i}]" for i, q in enumerate(survey.get("questions") or []) if isinstance(q, dict)}
        self.block_of: dict[str, str] = {}
        self.set_variables: set[str] = {str(v.get("id")) for v in survey.get("variables") or [] if isinstance(v, dict)}

    def error(self, path: str, message: str, code: str) -> None:
        self.findings.append(Finding(path, message, code, "error"))

    def warn(self, path: str, message: str, code: str) -> None:
        self.findings.append(Finding(path, message, code, "warning"))

    # ------------------------------------------------------------------ survey

    def run(self) -> list[Finding]:
        survey = self.survey
        for name, items, path in (
            ("question", survey.get("questions"), "$.questions"),
            ("block", survey.get("blocks"), "$.blocks"),
            ("variable", survey.get("variables"), "$.variables"),
        ):
            for dup in _duplicates(_ids(items)):
                self.error(path, f"two {name}s have the ID {dup!r}", "id.duplicate")
        for index, block in enumerate(survey.get("blocks") or []):
            self.block(block, f"$.blocks[{index}]")
        flow = survey.get("flow") or []
        if not flow and survey.get("questions"):
            self.error("$.flow", "the survey has questions but no flow, so nothing is asked", "flow.empty")
        self._flow_ids: list[str] = []
        shown: set[str] = set()
        self.flow(flow, "$.flow", shown)
        for dup in _duplicates(self._flow_ids):
            self.error("$.flow", f"two flow steps have the ID {dup!r}", "id.duplicate")
        for block_id in self.blocks:
            if block_id not in shown:
                self.warn(f"$.blocks[{list(self.blocks).index(block_id)}]", f"block {block_id!r} is never shown by the flow", "block.unused")
        for qid, path in self.question_paths.items():
            if qid not in self.block_of:
                self.warn(path, f"question {qid!r} is in no block, so it is never asked", "question.unused")
        for qid, question in self.questions.items():
            self.question(question, self.question_paths[qid])
        return self.findings

    def block(self, block: dict[str, Any], path: str) -> None:
        if block.get("kind", "survey") != "survey":
            return  # experiment blocks are checked by experiments.check_block
        for index, qid in enumerate(block.get("questions") or []):
            qid = str(qid)
            if qid not in self.questions:
                self.error(f"{path}.questions[{index}]", f"no question has the ID {qid!r}", "reference.question")
            elif qid in self.block_of and self.block_of[qid] != block.get("id"):
                self.error(f"{path}.questions[{index}]", f"question {qid!r} is already in block {self.block_of[qid]!r}", "reference.question")
            else:
                self.block_of[qid] = str(block.get("id"))
        repeat = block.get("repeat")
        if isinstance(repeat, dict):
            rows = [r for r in repeat.get("rows") or [] if isinstance(r, dict)]
            ids = [str(r.get("id")) for r in rows]
            for dup in _duplicates(ids):
                self.error(f"{path}.repeat.rows", f"two rows have the ID {dup!r}", "id.duplicate")
            if ids != _repeat_order(rows):
                self.error(
                    f"{path}.repeat.rows",
                    "rows run in the order of their IDs (numbers in numeric order); list them in that order: " + ", ".join(_repeat_order(rows)),
                    "repeat.order",
                )
            if repeat.get("take") and repeat.get("order") != "shuffle":
                self.error(f"{path}.repeat.take", "'take' draws rows at random; it needs order 'shuffle'", "repeat.take")

    def flow(self, nodes: list[Any], path: str, shown: set[str]) -> None:
        for index, node in enumerate(nodes):
            if not isinstance(node, dict):
                continue
            here = f"{path}[{index}]"
            if node.get("id"):
                self._flow_ids.append(str(node["id"]))
            kind = node.get("type")
            if kind == "show_block":
                block_id = str(node.get("block"))
                if block_id not in self.blocks:
                    self.error(f"{here}.block", f"no block has the ID {block_id!r}", "reference.block")
                shown.add(block_id)
            elif kind == "set_variables":
                for a_index, assignment in enumerate(node.get("assignments") or []):
                    if isinstance(assignment, dict) and assignment.get("variable"):
                        self.set_variables.add(str(assignment["variable"]))
                        self.refs(assignment.get("value"), f"{here}.assignments[{a_index}].value")
            elif kind == "branch":
                self.condition(node.get("when"), f"{here}.when")
                self.flow(node.get("then") or [], f"{here}.then", shown)
                if node.get("else"):
                    self.else_branch(node, here)
                    self.flow(node.get("else") or [], f"{here}.else", shown)
            elif kind in {"randomize", "group"}:
                children = node.get("children") or []
                if kind == "randomize" and isinstance(node.get("take"), int) and node["take"] > len(children):
                    self.error(f"{here}.take", f"takes {node['take']} of {len(children)} steps", "randomize.take")
                self.flow(children, f"{here}.children", shown)

    def else_branch(self, node: dict[str, Any], path: str) -> None:
        """'else' runs as a second branch on the opposite condition, decided after the
        'then' steps: the condition must have an opposite and the 'then' steps must
        not change what it reads."""
        from .conditions import condition_variables

        if any(n.get("op") == "source" for n, _ in _conditions(node.get("when"), "")):
            self.error(f"{path}.else", "a condition kept as written in the source cannot be reversed for 'else'; use two branches", "branch.else")
        read = condition_variables(node.get("when"))
        changed: set[str] = set()

        def walk(nodes: list[Any]) -> None:
            for child in nodes:
                if isinstance(child, dict):
                    for assignment in child.get("assignments") or []:
                        if isinstance(assignment, dict):
                            changed.add(str(assignment.get("variable")))
                    for key in ("then", "else", "children"):
                        walk(child.get(key) or [])

        walk(node.get("then") or [])
        both = sorted(read & changed)
        if both:
            self.error(f"{path}.else", f"the 'then' steps set {', '.join(both)}, which the condition reads; use two branches", "branch.else")

    # ------------------------------------------------------------------ references

    def options_of(self, qid: str) -> list[str] | None:
        """The option IDs a condition may name for a question (None: any is accepted)."""
        question = self.questions.get(qid)
        response = question.get("response") if isinstance(question, dict) else None
        if not isinstance(response, dict):
            return None
        rtype = response.get("type")
        if rtype == "choice":
            return _ids(response.get("options"))
        if rtype == "matrix":
            if response.get("mode") == "per_row" and (response.get("row_response") or {}).get("type") == "choice":
                return _ids((response.get("row_response") or {}).get("options"))
            return None
        if rtype in {"ranking", "allocation"}:
            return _ids(response.get("items"))
        if rtype == "form":
            return _ids(response.get("fields"))
        return None

    def condition(self, tree: Any, path: str) -> None:
        for node, here in _conditions(tree, path):
            op = node.get("op")
            if op in {"selected", "not_selected", "displayed", "not_displayed"}:
                qid = str(node.get("question"))
                if qid not in self.questions:
                    self.error(f"{here}.question", f"no question has the ID {qid!r}", "reference.condition")
                    continue
                option = node.get("option")
                options = self.options_of(qid)
                if option is not None and options is not None and str(option) not in options:
                    self.error(f"{here}.option", f"question {qid!r} has no option {option!r}", "reference.condition")
            left = node.get("left")
            if isinstance(left, dict):
                self.ref(left, f"{here}.left")
            right = node.get("right")
            if isinstance(right, dict):
                self.refs(right, f"{here}.right")

    def ref(self, ref: dict[str, Any], path: str) -> None:
        scope = ref.get("scope")
        if scope == "answer" and str(ref.get("question")) not in self.questions:
            self.error(f"{path}.question", f"no question has the ID {ref.get('question')!r}", "reference.pipe")
        elif scope == "variable" and str(ref.get("variable")) not in self.set_variables:
            self.warn(path, f"variable {ref.get('variable')!r} is never set or declared; it reads as empty", "variable.unset")

    def refs(self, value: Any, path: str) -> None:
        for text, here in _text_values(value, path):
            for index, ref in enumerate(text_refs(text)):
                self.ref(ref, f"{here}.parts")

    # ------------------------------------------------------------------ questions

    def question(self, question: dict[str, Any], path: str) -> None:
        self.refs(question.get("content"), f"{path}.content")
        self.condition(question.get("show_if"), f"{path}.show_if")
        response = question.get("response")
        if response is not None and question.get("kind") is not None:
            self.error(f"{path}.kind", f"a {question['kind']} question has no response", "question.kind")
        if isinstance(response, dict):
            self.refs(response, f"{path}.response")
            self.response(question, response, f"{path}.response")
        for index, rule in enumerate(question.get("skip") or []):
            self.skip(question, rule, f"{path}.skip[{index}]")

    def skip(self, question: dict[str, Any], rule: dict[str, Any], path: str) -> None:
        when = rule.get("when") or {}
        if when.get("op") not in {"selected", "not_selected", "source"}:
            self.error(f"{path}.when", "a skip rule tests whether an option was selected ('selected' or 'not_selected')", "skip.condition")
        self.condition(when, f"{path}.when")
        to = str(rule.get("to"))
        if to in {"end_of_block", "end_of_survey"}:
            return
        block_id = self.block_of.get(str(question.get("id")))
        block = self.blocks.get(block_id or "")
        order = [str(q) for q in (block or {}).get("questions") or []]
        if to not in self.questions:
            self.error(f"{path}.to", f"no question has the ID {to!r}", "reference.skip")
        elif to not in order or (str(question.get("id")) in order and order.index(to) <= order.index(str(question.get("id")))):
            self.error(f"{path}.to", f"a skip goes to a later question of the same block; {to!r} is not one", "reference.skip")

    def response(self, question: dict[str, Any], response: dict[str, Any], path: str) -> None:
        rtype = response.get("type")
        if rtype == "choice":
            self.items(response.get("options"), f"{path}.options", "option")
            options = _ids(response.get("options"))
            low, high = response.get("min_selected"), response.get("max_selected")
            if response.get("mode") == "single" and (low or high):
                self.error(path, "a single choice selects one option; 'min_selected'/'max_selected' are for 'multiple'", "response.limits")
            if low and high and low > high:
                self.error(path, f"min_selected {low} is more than max_selected {high}", "response.limits")
            if high and high > len(options):
                self.error(f"{path}.max_selected", f"max_selected {high} is more than the {len(options)} options", "response.limits")
            for index, option in enumerate(response.get("default") or []):
                if option not in options:
                    self.error(f"{path}.default[{index}]", f"no option has the ID {option!r}", "reference.default")
            if response.get("mode") == "single" and len(response.get("default") or []) > 1:
                self.error(f"{path}.default", "a single choice can preset one option", "reference.default")
            self.randomization(response.get("randomization"), options, f"{path}.randomization")
        elif rtype == "text":
            self.text_checks(response, path)
        elif rtype == "number":
            self.number(response, path)
        elif rtype == "form":
            self.items(response.get("fields"), f"{path}.fields", "field")
            self.text_checks(response.get("field_response") or {}, f"{path}.field_response")
        elif rtype == "matrix":
            self.items(response.get("rows"), f"{path}.rows", "row")
            mode = response.get("mode")
            if mode == "per_row":
                row_response = response.get("row_response")
                if not isinstance(row_response, dict):
                    self.error(path, "a 'per_row' grid needs 'row_response'", "response.mode")
                elif row_response.get("type") not in {"choice", "number"}:
                    self.error(f"{path}.row_response.type", "each row takes a 'choice' or a 'number'", "response.mode")
                else:
                    self.response(question, row_response, f"{path}.row_response")
                if response.get("columns") or response.get("cell_response"):
                    self.error(path, "'columns' and 'cell_response' are for 'per_cell' grids", "response.mode")
            elif mode == "per_cell":
                cell = response.get("cell_response")
                if not response.get("columns") or not isinstance(cell, dict):
                    self.error(path, "a 'per_cell' grid needs 'columns' and 'cell_response'", "response.mode")
                elif cell.get("type") not in {"text", "number"}:
                    self.error(f"{path}.cell_response.type", "each cell takes 'text' or a 'number'", "response.mode")
                else:
                    self.items(response.get("columns"), f"{path}.columns", "column")
                    self.response(question, cell, f"{path}.cell_response")
                    if cell.get("type") == "text" and response.get("constraints"):
                        self.error(f"{path}.constraints", "a sum applies to number cells", "response.mode")
                if response.get("row_response"):
                    self.error(path, "'row_response' is for 'per_row' grids", "response.mode")
            self.randomization(response.get("randomization"), _ids(response.get("rows")), f"{path}.randomization")
        elif rtype == "ranking":
            self.items(response.get("items"), f"{path}.items", "item")
            items = _ids(response.get("items"))
            ranks = [d.get("rank") for d in response.get("default") or []]
            for index, entry in enumerate(response.get("default") or []):
                if entry.get("item") not in items:
                    self.error(f"{path}.default[{index}].item", f"no item has the ID {entry.get('item')!r}", "reference.default")
            if _duplicates([str(r) for r in ranks]):
                self.error(f"{path}.default", "two items have the same preset rank", "reference.default")
            self.randomization(response.get("randomization"), items, f"{path}.randomization")
        elif rtype == "allocation":
            self.items(response.get("items"), f"{path}.items", "item")
            total = response.get("total", 100)
            if total is not None and total <= 0:
                self.error(f"{path}.total", "the total must be more than 0", "response.limits")
            low, high = response.get("min"), response.get("max")
            if low is not None and high is not None and low > high:
                self.error(path, f"min {low} is more than max {high}", "response.limits")
            self.randomization(response.get("randomization"), _ids(response.get("items")), f"{path}.randomization")
        elif rtype == "group":
            fields = response.get("fields") or []
            for dup in _duplicates(_ids(fields)):
                self.error(f"{path}.fields", f"two columns have the ID {dup!r}", "id.duplicate")
            for index, field in enumerate(fields):
                if isinstance(field.get("response"), dict):
                    self.response(question, field["response"], f"{path}.fields[{index}].response")
        elif rtype == "best_worst":
            self.error(f"{path}.type", "a best/worst response belongs to a MaxDiff block", "response.type")

    def items(self, items: Any, path: str, name: str) -> None:
        items = [item for item in items or [] if isinstance(item, dict)]
        for dup in _duplicates(_ids(items)):
            self.error(path, f"two {name}s have the ID {dup!r}", "id.duplicate")
        for index, item in enumerate(items):
            self.condition(item.get("show_if"), f"{path}[{index}].show_if")
        if self.imported or name not in {"option", "column", "item"}:
            return
        labels = [join_text(item.get("label", "")).strip().casefold() for item in items]
        for dup in _duplicates([label for label in labels if label]):
            self.error(path, f"two {name}s read {dup!r}; the model answers with the label, so labels must differ", "label.duplicate")

    def text_checks(self, response: dict[str, Any], path: str) -> None:
        low, high = response.get("min_length"), response.get("max_length")
        if low and high and low > high:
            self.error(path, f"min_length {low} is more than max_length {high}", "response.limits")
        number = response.get("number")
        if isinstance(number, dict) and number.get("min") is not None and number.get("max") is not None and number["min"] > number["max"]:
            self.error(f"{path}.number", "min is more than max", "response.limits")
        if number and response.get("format") not in (None, "number"):
            self.error(f"{path}.number", "number limits apply to the 'number' format", "response.limits")

    def number(self, response: dict[str, Any], path: str) -> None:
        low, high = response.get("min"), response.get("max")
        if low is not None and high is not None and low >= high:
            self.error(path, f"min {low} must be less than max {high}", "response.limits")
        step = response.get("step")
        if step is not None and step <= 0:
            self.error(f"{path}.step", "the step must be more than 0", "response.limits")

    def randomization(self, randomization: Any, ids: list[str], path: str) -> None:
        if not isinstance(randomization, dict):
            return
        known = set(ids)
        # An imported setting may name an option the survey no longer shows; the
        # engine leaves such IDs out, so it is only worth a warning there.
        report = self.warn if self.imported else self.error
        for key in ("shuffled", "subset", "hidden"):
            for index, item in enumerate(randomization.get(key) or []):
                if item not in known:
                    report(f"{path}.{key}[{index}]", f"no item has the ID {item!r}", "reference.randomization")
        for index, slot in enumerate(randomization.get("positions") or []):
            if isinstance(slot, str) and slot not in known:
                report(f"{path}.positions[{index}]", f"no item has the ID {slot!r}", "reference.randomization")
        take = randomization.get("take")
        if take and take > len(ids):
            self.error(f"{path}.take", f"takes {take} of {len(ids)}", "reference.randomization")
        if randomization.get("order") == "source" and "qualtrics" not in randomization:
            self.error(path, "a 'source' randomization keeps the source setting under 'qualtrics'", "randomization.source")


def check_survey(survey: Any) -> list[Finding]:
    """Every structural problem, then (when the structure is sound) every problem of meaning."""
    structural = check_structure(survey)
    if structural:
        return [Finding(p.path, p.message, p.code or "schema", "error") for p in structural]
    findings = _Checker(survey).run()
    from ..experiments import check_experiment_blocks

    findings += check_experiment_blocks(survey)
    return findings


def errors(findings: list[Finding]) -> list[Finding]:
    return [f for f in findings if f.level == "error"]


__all__ = ["Finding", "Problem", "check_survey", "errors"]
