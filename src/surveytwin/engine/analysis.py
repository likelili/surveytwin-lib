"""Whether a survey runs static or staged, and what it uses, from its definition.

A survey runs staged when later content depends on an earlier answer: a
condition reads an answer, text pipes one, the survey can end early, a skip
jumps on an answer, or a conjoint or MaxDiff block asks each task in a model
call of its own. ``analyze`` says so, lists the survey's traits (randomizers,
loops, conditions no twin can meet...) and the warnings a plan shows. Its
report is the one the template analysis it replaced gave, word for word, so
plans, checks, manifests and reports are unchanged.
"""

from __future__ import annotations

import json
import re
from typing import Any

from ..runtime.logic_compare import is_boolean_condition, is_ignored_condition
from . import render
from .conditions import OPERATOR_NAMES, decide
from .definition import Definition, assignment_value
from .realize import shown_question

_DECIDED = {"Question", "EmbeddedField", "BooleanValue"}
_Q_PIPE_RE = re.compile(r"\$\{q://[^}]+\}")
_Q_PIPE_PARTS_RE = re.compile(r"\$\{q://([^}]+)\}")
_Q_LOCATOR_RE = re.compile(r"q://[^/]+/([^/}]+)")
_SUPPORTED_OPERATORS = {
    "Contains",
    "Displayed",
    "DoesNotContain",
    "DoesNotMatchRegex",
    "Empty",
    "EqualTo",
    "GreaterThan",
    "GreaterThanOrEqual",
    "GreaterThanOrEqualTo",
    "IsEmpty",
    "IsNotEmpty",
    "LessThan",
    "LessThanOrEqual",
    "LessThanOrEqualTo",
    "MatchesRegex",
    "NotDisplayed",
    "NotEmpty",
    "NotEqualTo",
    "NotSelected",
    "Selected",
}
_SUPPORTED_LOCATORS = {
    "ChoiceDisplayed",
    "ChoiceNumericEntryValue",
    "ChoiceTextEntryValue",
    "NumericEntryValue",
    "QuestionDisplayed",
    "QuestionText",
    "SelectableChoice",
    "SelectedChoicesCount",
    "SelectedValue",
    "TextEntryValue",
}
_FIELD_LOCATORS = {"text_entry": "ChoiceTextEntryValue", "number_entry": "ChoiceNumericEntryValue", "selected_count": "SelectedChoicesCount"}
ENTRY_PIPE_KINDS = frozenset({"SelectedValue", "ChoiceTextEntryValue", "TextEntryValue", "ChoiceNumericEntryValue", "NumericEntryValue"})
CHOICE_GROUP_PIPE_FORMS = frozenset(
    {"SelectedChoices", "SelectedChoicesRecode", "SelectedChoicesTextEntry", "UnselectedChoices", "AllChoices", "DisplayedChoices", "NotDisplayedChoices", "SelectedAnswers"}
)
QUESTION_PIPE_KINDS = frozenset({"QuestionText", "ChoiceDescription", "ChoiceGroup", "SelectedChoices", "SelectedChoice", "SelectedChoicesCount"}) | ENTRY_PIPE_KINDS


def _strings(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [text for item in value for text in _strings(item)]
    if isinstance(value, dict):
        return [text for item in value.values() for text in _strings(item)]
    return []


def _dicts(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, dict):
        return [value] + [d for child in value.values() for d in _dicts(child)]
    if isinstance(value, list):
        return [d for child in value for d in _dicts(child)]
    return []


def _atoms(condition: Any) -> list[dict[str, Any]]:
    if not isinstance(condition, dict):
        return []
    if condition.get("op") in {"and", "or"}:
        return [atom for arg in condition.get("args") or [] for atom in _atoms(arg)]
    if condition.get("op") == "not":
        return _atoms(condition.get("arg"))
    return [condition]


# ---------------------------------------------------------------- what a condition reads


def _sources(condition: Any) -> set[str]:
    """What a condition is decided on, named as Qualtrics names it (Question,
    EmbeddedField, BooleanValue, DeviceType...)."""
    found: set[str] = set()
    for atom in _atoms(condition):
        op = atom.get("op")
        if op in {"true", "false"}:
            found.add("BooleanValue")
        elif op in {"selected", "not_selected", "displayed", "not_displayed"}:
            found.add("Question")
        elif op in OPERATOR_NAMES:
            found.add("EmbeddedField" if (atom.get("left") or {}).get("scope") == "variable" else "Question")
        elif op == "source":
            for item in _dicts(atom.get("qualtrics")):
                source = str(item.get("LogicType", "") or "").strip()
                if source:
                    found.add(source)
                if str(item.get("LeftOperand", "") or "").strip().startswith("q://"):
                    found.add("Question")
    return found


def _operators(condition: Any) -> set[str]:
    found: set[str] = set()
    for atom in _atoms(condition):
        op = atom.get("op")
        if op in {"selected", "not_selected"}:
            found.add("Selected" if op == "selected" else "NotSelected")
        elif op in {"displayed", "not_displayed"}:
            found.add("Displayed" if op == "displayed" else "NotDisplayed")
        elif op in OPERATOR_NAMES:
            found.add(OPERATOR_NAMES[op])
        elif op == "source":
            found |= {
                str(item.get("Operator", "") or "").strip()
                for item in _dicts(atom.get("qualtrics"))
                if not is_ignored_condition(item) and str(item.get("Operator", "") or "").strip()
            }
    return found


def _locators(condition: Any) -> set[str]:
    found: set[str] = set()
    for atom in _atoms(condition):
        op = atom.get("op")
        if op in {"selected", "not_selected"}:
            found.add("SelectableChoice")
        elif op in {"displayed", "not_displayed"}:
            found.add("QuestionDisplayed")
        elif op in OPERATOR_NAMES and (atom.get("left") or {}).get("scope") == "answer":
            found.add(_FIELD_LOCATORS.get(str(atom["left"].get("field")), ""))
        elif op == "source":
            for item in _dicts(atom.get("qualtrics")):
                if is_ignored_condition(item):
                    continue
                match = _Q_LOCATOR_RE.match(str(item.get("LeftOperand", "") or "").strip())
                if match:
                    found.add(match.group(1).strip())
    return {locator for locator in found if locator}


def always_false(condition: Any) -> bool:
    """A condition decided by literal true/false alone (conditions no twin can meet
    left out) that comes out false: what it gates is shown to nobody."""
    atoms = _atoms(condition)
    literal = []
    for atom in atoms:
        if atom.get("op") == "source":
            literal += [c for c in _dicts(atom.get("qualtrics")) if "LogicType" in c and not is_ignored_condition(c)]
        else:
            literal.append(atom)
    if not literal or not all(a.get("op") in {"true", "false"} or is_boolean_condition(a) for a in literal):
        return False

    class _Literal:
        def atom(self, _condition: dict[str, Any]) -> bool:
            return True

        def source(self, _condition: dict[str, Any]) -> bool:
            return True

    result = decide(condition, _Literal())  # type: ignore[arg-type]
    return not (True if result is None else result)


# ---------------------------------------------------------------- the survey's parts, in flow order


def _placements(definition: Definition) -> list[tuple[str, dict[str, Any], list[dict[str, Any]]]]:
    """Every flow step in order: ("branch", node, [condition, steps] parts), and so
    on; a block's questions as shown questions."""
    from ..results.layout import experiment_skeleton

    out: list[tuple[str, dict[str, Any], Any]] = []

    def walk(nodes: list[dict[str, Any]]) -> None:
        for node in nodes:
            if not isinstance(node, dict):
                continue
            kind = node.get("type")
            if kind == "show_block":
                block = definition.blocks.get(str(node.get("block")))
                if block is None:
                    continue
                if block.get("kind") in {"conjoint", "maxdiff"}:
                    out.append(("block", block, experiment_skeleton(block)))
                    continue
                questions = [
                    shown_question(definition.questions[str(qid)], native=definition.native)
                    for qid in block.get("questions") or []
                    if str(qid) in definition.questions
                ]
                if questions:
                    out.append(("block", block, questions))
                continue
            out.append((kind, node, None))
            for key in ("then", "else", "children"):
                if isinstance(node.get(key), list):
                    walk(node[key])

    walk(definition.flow)
    return out


def _token_strings(value: Any) -> list[str]:
    """Every text in ``value``, piped values as their tokens (``${q://QID1/...}``)."""
    from ..spec.text import join_text

    if isinstance(value, str):
        return [value]
    if isinstance(value, dict) and isinstance(value.get("parts"), list):
        return [join_text(value)]
    if isinstance(value, dict):
        if value.get("op") and "left" in value and isinstance(value.get("left"), dict):
            # a comparison: the value compared with, then the rest
            return _token_strings(value.get("right")) + [t for k, v in value.items() if k not in {"right", "left"} for t in _token_strings(v)]
        return [t for item in value.values() for t in _token_strings(item)]
    if isinstance(value, list):
        return [t for item in value for t in _token_strings(item)]
    return []


def _choice_randomization(question: dict[str, Any]) -> str:
    """'advanced', 'basic' or '' for a question's own randomization (as its source wrote it)."""
    source = question.get("source") if isinstance(question.get("source"), dict) else {}
    raw = source.get("randomization")
    if isinstance(raw, dict) and raw:
        kind = str(raw.get("Type", "") or "").strip().lower()
        if raw.get("Advanced"):
            return "advanced"
        return "basic" if kind and kind not in {"none", "no", "off"} else ""
    randomization = (question.get("response") or {}).get("randomization") if isinstance(question.get("response"), dict) else None
    if not randomization:
        return ""
    return "advanced" if randomization.get("order") in {"positions", "source"} else "basic"


def _item_conditions(question: dict[str, Any]) -> list[Any]:
    response = question.get("response") if isinstance(question.get("response"), dict) else {}
    found: list[Any] = []
    for key in ("options", "items", "fields", "rows", "columns"):
        found += [item["show_if"] for item in response.get(key) or [] if isinstance(item, dict) and item.get("show_if")]
    row_response = response.get("row_response") if isinstance(response.get("row_response"), dict) else {}
    found += [item["show_if"] for item in row_response.get("options") or [] if isinstance(item, dict) and item.get("show_if")]
    return found


def _item_conditions_apply(shown: dict[str, Any]) -> bool:
    """Whether display conditions on single items are applied (not for a side-by-side question)."""
    return render.Shown(shown).kind != "group"


def _type_name(shown: dict[str, Any]) -> str:
    source = shown["question"].get("source") if isinstance(shown["question"].get("source"), dict) else {}
    if source.get("raw_type"):
        return str(source["raw_type"]).strip()
    return {
        "choice": "MC", "text": "TE", "form": "TE", "grid": "Matrix", "grid_text": "Matrix", "grid_sum": "Matrix",
        "slider": "Slider", "allocation": "CS", "ranking": "Rank", "group": "SBS", "instruction": "DB",
    }.get(render.Shown(shown).kind or "", str(source.get("type") or ""))


def analyze(definition_dict: dict[str, Any]) -> dict[str, Any]:
    """Static or staged, the survey's traits, and the warnings a plan shows."""
    definition = Definition(definition_dict)
    traits = {
        "branch": False, "randomizer": False, "group": False, "end": False, "q_piping": False, "question_logic": False,
        "embedded_logic": False, "display_logic": False, "skip": False,
    }
    basic = advanced = 0
    unsupported_sources: set[str] = set()
    unsupported_operators: set[str] = set()
    unsupported_locators: set[str] = set()
    hidden: list[str] = []
    hidden_labels: list[str] = []
    choice_logic: list[str] = []
    choice_logic_not_applied: list[str] = []
    matrices: set[str] = set()
    pipe_texts: list[str] = []

    def note_condition(condition: Any) -> set[str]:
        sources = _sources(condition)
        unsupported_sources.update(s for s in sources if s not in _DECIDED)
        unsupported_operators.update(o for o in _operators(condition) if o not in _SUPPORTED_OPERATORS)
        unsupported_locators.update(loc for loc in _locators(condition) if loc not in _SUPPORTED_LOCATORS)
        return sources

    steps = _placements(definition)
    top = definition.flow
    terminal_end = top[-1] if top and isinstance(top[-1], dict) and top[-1].get("type") == "end" else None
    for kind, node, questions in steps:
        if kind == "branch":
            traits["branch"] = True
            from .static import branch_parts

            for condition, _steps in branch_parts(node):
                sources = note_condition(condition)
                if "Question" in sources:
                    traits["question_logic"] = True
                if "EmbeddedField" in sources:
                    traits["embedded_logic"] = True
                pipe_texts += [t for t in _token_strings(condition) if "${q://" in t]
        elif kind == "randomize":
            traits["randomizer"] = True
        elif kind == "group":
            source = node.get("source") if isinstance(node.get("source"), dict) else {}
            if "element_type" not in source:
                traits["group"] = True
        elif kind == "end":
            if node is not terminal_end:
                traits["end"] = True
        elif kind == "set_variables":
            pipe_texts += [str(assignment_value(a)) for a in node.get("assignments") or [] if "${q://" in str(assignment_value(a))]
        elif kind == "block":
            for shown in questions:
                question = shown["question"]
                view = render.Shown(shown)
                qid = str(question.get("id", "") or "").strip()
                if view.kind in {"grid", "grid_text", "grid_sum"}:
                    matrices.add(qid)
                texts = [t for t in _token_strings(question) if "${q://" in t]
                pipe_texts += texts
                condition = question.get("show_if")
                if condition:
                    sources = note_condition(condition)
                    if "Question" in sources:
                        traits["display_logic"] = True
                        traits["question_logic"] = True
                    if "EmbeddedField" in sources:
                        traits["embedded_logic"] = True
                    if always_false(condition):
                        hidden.append(qid)
                        tag = str(question.get("export_name", "") or "").strip()
                        hidden_labels.append(f"{tag} ({qid})" if tag else qid)
                items = _item_conditions(question)
                if items:
                    tag = str(question.get("export_name", "") or "").strip()
                    label = f"{tag} ({qid})" if tag and tag != qid else qid
                    if not _item_conditions_apply(shown):
                        typed = f"{label}, {_type_name(shown)}"
                        if typed not in choice_logic_not_applied:
                            choice_logic_not_applied.append(typed)
                    else:
                        if label not in choice_logic:
                            choice_logic.append(label)
                        for item in items:
                            sources = note_condition(item)
                            if "Question" in sources:
                                traits["question_logic"] = True
                            if "EmbeddedField" in sources:
                                traits["embedded_logic"] = True
                if texts:
                    traits["q_piping"] = True
                if question.get("skip"):
                    traits["skip"] = True
                    for rule in question["skip"]:
                        when = rule.get("when") or {}
                        if when.get("op") == "source":
                            unsupported_operators.update(
                                str(item.get("Operator", "") or "").strip()
                                for item in _dicts(when.get("qualtrics"))
                                if not is_ignored_condition(item) and str(item.get("Operator", "") or "").strip() not in _SUPPORTED_OPERATORS
                                and str(item.get("Operator", "") or "").strip()
                            )
                            for item in _dicts(when.get("qualtrics")):
                                match = _Q_LOCATOR_RE.match(str(item.get("LeftOperand", "") or "").strip())
                                if match and not is_ignored_condition(item) and match.group(1).strip() not in _SUPPORTED_LOCATORS:
                                    unsupported_locators.add(match.group(1).strip())
                randomized = _choice_randomization(question)
                if randomized == "advanced":
                    advanced += 1
                elif randomized == "basic":
                    basic += 1

    # any piped answer anywhere makes the survey staged
    traits["q_piping"] = any(_Q_PIPE_RE.search(t) for t in pipe_texts) or any(_Q_PIPE_RE.search(t) for t in _token_strings(definition_dict))
    unfilled = _unfilled(pipe_texts, matrices)
    loops = loop_blocks(definition)
    experiments = experiment_blocks(definition)
    task_boundaries = any(e["unit"] == "task" for e in experiments)
    requires_staging = traits["question_logic"] or traits["q_piping"] or traits["end"] or traits["skip"] or task_boundaries
    warnings = _warnings(
        definition_dict, traits, requires_staging, experiments, basic, advanced, hidden, hidden_labels, unfilled, loops,
        choice_logic_not_applied, unsupported_sources, unsupported_operators, unsupported_locators,
    )
    return {
        "requires_staging": requires_staging,
        "execution_mode": "staged" if requires_staging else "static",
        "traits": {
            "branch_logic": traits["branch"],
            "question_logic": traits["question_logic"],
            "embedded_data_logic": traits["embedded_logic"],
            "question_display_logic": traits["display_logic"],
            "skip_logic": traits["skip"],
            "question_piped_text": traits["q_piping"],
            "randomizer": traits["randomizer"],
            "group": traits["group"],
            "end_survey": traits["end"],
            "basic_choice_randomization": basic,
            "advanced_choice_randomization": advanced,
            "unsupported_logic_sources": sorted(unsupported_sources),
            "questions_hidden_for_everyone": hidden,
            "unsupported_logic_operators": sorted(unsupported_operators),
            "unsupported_question_locators": sorted(unsupported_locators),
            "unfilled_pipe_forms": unfilled,
            "loops": loops,
            "choice_display_logic": choice_logic,
            "choice_display_logic_not_applied": choice_logic_not_applied,
            # (only for surveys with conjoint or MaxDiff blocks, so other analyses are unchanged)
            **({"experiments": experiments, "task_boundaries": task_boundaries} if experiments else {}),
        },
        "warnings": warnings,
    }


def _unfilled(texts: list[str], matrices: set[str]) -> list[str]:
    """Each ${q://...} form the runtime does not fill, as written."""
    found: list[str] = []
    for text in texts:
        for expression in _Q_PIPE_PARTS_RE.findall(text):
            parts = [part.strip() for part in expression.split("/") if part.strip()]
            kind = parts[1] if len(parts) > 1 else ""
            form = parts[2] if len(parts) > 2 else ""
            source = re.sub(r"^\d+_", "", parts[0] if parts else "").split("#", 1)[0]
            filled = kind in QUESTION_PIPE_KINDS and (
                (kind != "ChoiceGroup" or form in CHOICE_GROUP_PIPE_FORMS)
                and (kind != "ChoiceDescription" or bool(form))
                and not (source in matrices and kind == "ChoiceGroup" and form != "SelectedAnswers")
                and not (source in matrices and kind == "SelectedChoicesCount")
            )
            token = "${q://" + expression + "}"
            if not filled and token not in found:
                found.append(token)
    return found


def loop_blocks(definition: Definition, excluded_block_ids: Any = ()) -> list[dict[str, Any]]:
    """Each repeated block a twin can be shown, with the rows it asks (all, or the
    random subset's size), its questions per row and the answers a row asks for."""
    excluded = {str(b) for b in excluded_block_ids or ()}
    found: list[dict[str, Any]] = []
    seen: set[str] = set()
    for kind, block, questions in _placements(definition):
        if kind != "block" or block.get("kind") != "survey":
            continue
        block_id = str(block.get("id") or "")
        repeat = block.get("repeat") or {}
        if block_id in excluded or block_id in seen or not repeat.get("rows"):
            continue
        rows = len(repeat["rows"])
        if repeat.get("take"):
            rows = min(rows, int(repeat["take"]))
        asked = [
            shown for shown in questions
            if render.Shown(shown).kind not in {None, "instruction"} and not always_false(shown["question"].get("show_if"))
        ]
        answers = len(render.prompt_questions(asked))
        seen.add(block_id)
        found.append({"block_id": block_id, "name": str(block.get("title") or block_id), "rows": rows, "questions": len(asked), "answers_per_row": answers})
    return found


def experiment_blocks(definition: Definition) -> list[dict[str, Any]]:
    """Each conjoint or MaxDiff block: its kind, tasks per twin, and how they are asked."""
    out = []
    for kind, block, _questions in _placements(definition):
        if kind != "block" or block.get("kind") not in {"conjoint", "maxdiff"}:
            continue
        design = block.get("design") or {}
        tasks = (
            max((len(v.get("tasks") or []) for v in design.get("versions") or []), default=0)
            if design.get("mode") == "supplied"
            else int(design.get("tasks_per_participant") or 1)
        )
        out.append({"block_id": str(block["id"]), "kind": str(block["kind"]), "tasks": tasks, "unit": str((block.get("administration") or {}).get("unit") or "block")})
    return out


def _warnings(
    definition: dict[str, Any], traits: dict[str, bool], requires_staging: bool, experiments: list[dict[str, Any]], basic: int, advanced: int,
    hidden: list[str], hidden_labels: list[str], unfilled: list[str], loops: list[dict[str, Any]], not_applied: list[str],
    sources: set[str], operators: set[str], locators: set[str],
) -> list[str]:
    warnings: list[str] = []
    if requires_staging:
        warnings.append("QSF uses answer-dependent flow or question piping; simulation will run in staged passes.")
    if traits["q_piping"]:
        warnings.append("Survey question piped text (${q://...}) will be resolved from prior staged answers.")
    if traits["question_logic"]:
        warnings.append("Question-based branch/display logic will be evaluated after prior answers exist.")
    if traits["skip"]:
        warnings.append("Question skip logic will be evaluated after the skip-origin answer exists.")
    if traits["randomizer"]:
        warnings.append(
            "Survey-flow randomizers will choose deterministic per-persona arms, "
            "balance Evenly Present elements when configured, and store arm metadata."
        )
    if traits["end"]:
        warnings.append("End-of-survey flow elements can terminate staged execution early.")
    for experiment in experiments:
        warnings.append(
            f"{experiment['kind'].capitalize() if experiment['kind'] == 'conjoint' else 'MaxDiff'} block {experiment['block_id']}: "
            f"{experiment['tasks']} task(s) drawn for each twin from the block's rules, "
            + ("each asked in a model call of its own." if experiment["unit"] == "task" else "all asked together.")
        )
    text = json.dumps(definition, ensure_ascii=False)
    if '"scope": "random"' in text or "${rand://" in text:
        warnings.append(
            "Random-value piped text (${rand://...}) is drawn per simulated respondent and place "
            "from the run seed, the same value in static and staged runs; it does not require staged passes."
        )
    if _random_in_condition_values(definition):
        warnings.append("Random-value piped text in a condition's value (${rand://...}) is compared as written, not drawn.")
    if basic:
        warnings.append(f"{basic} question(s) use choice randomization; each simulated respondent gets a deterministic per-persona presentation order.")
    if advanced:
        warnings.append(
            f"{advanced} question(s) use advanced choice randomization; "
            "fixed positions, randomized/subset slots, and undisplayed items are executed per "
            "persona (reversal groups are preserved but not executed)."
        )
    if hidden:
        warnings.append(
            f"{len(hidden)} question(s) are switched off for every respondent "
            "(display logic \"If False\"): "
            + ", ".join(hidden_labels[:12])
            + (" ..." if len(hidden_labels) > 12 else "")
            + ". They are not asked and appear as empty columns, as in a Qualtrics export."
        )
    if unfilled:
        warnings.append(
            "Piped text in a form the simulation does not fill (${q://...}): "
            + ", ".join(unfilled[:8])
            + (" ..." if len(unfilled) > 8 else "")
            + ". The twin sees the question's answer there instead (nothing, for a multi-select or matrix)."
        )
    if loops:
        warnings.append(
            "Loop & Merge asks every row: "
            + "; ".join(
                f"{loop['name']} ({loop['block_id']}) asks {loop['rows']} row(s) of {loop['questions']} question(s) "
                f"({loop['answers_per_row']} answer(s) a row), up to {loop['rows'] * loop['answers_per_row']} answers per twin"
                for loop in loops[:8]
            )
            + (" ..." if len(loops) > 8 else "")
            + "."
        )
    if not_applied:
        warnings.append(
            "Display logic on single items of these questions is not applied, so every item is shown: "
            + ", ".join(not_applied[:8])
            + (" ..." if len(not_applied) > 8 else "")
            + "."
        )
    if sources:
        warnings.append(
            "Conditions on "
            + ", ".join(sorted(sources))
            + " are ignored for twins, which have no device, location or quota: the rest of the logic decides, "
            "a question gated only on them is shown, and a screen-out on them never ends a survey."
        )
    if operators:
        warnings.append("Unsupported logic operator(s) detected: " + ", ".join(sorted(operators)) + ". These conditions will be treated conservatively.")
    if locators:
        warnings.append("Unsupported question-logic locator(s) detected: " + ", ".join(sorted(locators)) + ". These conditions will be treated conservatively.")
    return warnings


def _random_in_condition_values(definition: dict[str, Any]) -> bool:
    """Whether a condition compares with a random value (``${rand://...}`` in its value)."""

    def in_value(right: Any) -> bool:
        if isinstance(right, dict):
            return any(isinstance(p, dict) and (p.get("ref") or {}).get("scope") == "random" for p in right.get("parts") or []) or "${rand://" in json.dumps(right)
        return isinstance(right, str) and "${rand://" in right

    for item in _dicts(definition):
        if "right" in item and "op" in item and in_value(item["right"]):
            return True
        if isinstance(item.get("RightOperand"), str) and re.match(r"^[^\"]*\$\{rand://", item["RightOperand"]):
            return True
    return False


def undrawable_random_tokens(definition_dict: dict[str, Any]) -> list[tuple[str, str]]:
    """(where, token) for every random-value token (``${rand://...}``) that cannot
    be drawn, in a value a flow step sets or in a question (its conditions aside),
    in flow order: ``FL <flow ID> <variable>`` or ``SQ <question ID>``."""
    from ..runtime.random_pipes import random_token_problems
    from .definition import flow_id

    definition = Definition(definition_dict)
    found: list[tuple[str, str]] = []

    def question_texts(value: Any) -> list[str]:
        if isinstance(value, dict) and not isinstance(value.get("parts"), list):
            return [t for key, item in value.items() if key not in {"show_if", "skip"} for t in question_texts(item)]
        if isinstance(value, list):
            return [t for item in value for t in question_texts(item)]
        return _token_strings(value)

    def walk(nodes: list[Any]) -> None:
        for node in nodes:
            if not isinstance(node, dict):
                continue
            if node.get("type") == "set_variables":
                for assignment in node.get("assignments") or []:
                    if isinstance(assignment, dict) and not assignment.get("from_outside"):
                        where = f"FL {flow_id(node)} {assignment.get('variable', '')}"
                        found.extend((where, token) for token in random_token_problems(str(assignment_value(assignment))))
            elif node.get("type") == "show_block":
                block = definition.blocks.get(str(node.get("block")))
                for qid in (block or {}).get("questions") or []:
                    question = definition.questions.get(str(qid))
                    if question is not None:
                        found.extend((f"SQ {qid}", t) for text in question_texts(question) for t in random_token_problems(text))
            for key in ("then", "else", "children"):
                if isinstance(node.get(key), list):
                    walk(node[key])

    walk(definition.flow)
    return list(dict.fromkeys(found))
