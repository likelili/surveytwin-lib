"""Conjoint and MaxDiff blocks: drawing each twin's tasks, asking them, recording them.

Draws depend only on the run's seed, the twin and the block, so they repeat
exactly; rules (constraints, duplicates, balance) hold in every draw; an
impossible design is refused before anything runs; best and worst must differ.
"""

from __future__ import annotations

import base64
import copy
import csv
import json
import random
from collections import Counter
from pathlib import Path

import pytest

import surveytwin as st
from conftest import TESTS
from fakes import answer_questions, asked_questions, scripted_model

from surveytwin.experiments.design import (
    ConjointRules,
    DesignError,
    design_rng,
    draw_conjoint,
    draw_maxdiff,
)
from surveytwin.experiments.tasks import compile_block, expand_block

EXAMPLES = TESTS.parent / "examples" / "survey-format"
COFFEE = json.loads((EXAMPLES / "coffee-conjoint.json").read_text())
FEATURES = json.loads((EXAMPLES / "feature-maxdiff.json").read_text())


def coffee_block() -> dict:
    return copy.deepcopy(next(b for b in COFFEE["blocks"] if b["kind"] == "conjoint"))


def features_block() -> dict:
    return copy.deepcopy(FEATURES["blocks"][0])


def survey_with(block: dict, **top) -> dict:
    return {"schema_version": "0.1", "id": "s", "blocks": [block], "flow": [{"type": "show_block", "block": block["id"]}], **top}


def configure(**kwargs):
    kwargs.setdefault("sample_size", 2)
    kwargs.setdefault("seed", 7)
    kwargs.setdefault("model", scripted_model())
    return st.configure(bank=str(TESTS.parent / "examples" / "example-bank"), **kwargs)


def codes(error) -> set[str]:
    return {f.code for f in error.findings if f.level == "error"}


# ---- conjoint draws -----------------------------------------------------------------------


def test_every_drawn_profile_keeps_the_rules_and_the_fixed_levels():
    block = coffee_block()
    rules = ConjointRules(block)
    for twin in range(150):
        design = draw_conjoint(block, design_rng(1, f"t{twin}", "coffee"))
        assert len(design["tasks"]) == 6
        for task in design["tasks"]:
            assert [a["id"] for a in task["alternatives"]] == ["mcd", "dunkin", "starbucks"]
            for alternative in task["alternatives"]:
                levels = alternative["levels"]
                assert rules.valid(levels)
                assert levels["brand"] == alternative["id"]
                # the applicability table: Starbucks never at 3.00, McDonald's and Dunkin never at 6.00
                assert (levels["brand"], levels["price"]) not in {("starbucks", "p300"), ("mcd", "p600"), ("dunkin", "p600")}
                assert not (levels["brand"] == "starbucks" and levels["seating"] == "none" and levels["promotion"] == "upgrade")


def test_a_level_of_weight_zero_is_never_drawn():
    block = coffee_block()
    block["attributes"][1]["levels"][1]["weight"] = 0  # "No seating"
    seen = {a["levels"]["seating"] for t in range(60) for task in draw_conjoint(block, design_rng(2, f"t{t}", "coffee"))["tasks"] for a in task["alternatives"]}
    assert "none" not in seen and seen == {"seats", "wifi"}


def _generic(levels_per_attribute: int, duplicates: str, tasks: int = 4, count: int = 2) -> dict:
    return {
        "id": "g",
        "kind": "conjoint",
        "attributes": [
            {"id": f"a{i}", "name": f"A{i}", "levels": [{"id": f"l{j}", "label": f"L{i}{j}"} for j in range(levels_per_attribute)]}
            for i in range(2)
        ],
        "alternatives": {"mode": "generic", "count": count},
        "design": {"mode": "generated", "tasks_per_participant": tasks, "duplicates": duplicates},
    }


def test_no_task_shows_two_identical_profiles():
    block = _generic(2, "within_task")
    for t in range(80):
        for task in draw_conjoint(block, design_rng(3, f"t{t}", "g"))["tasks"]:
            a, b = (alt["levels"] for alt in task["alternatives"])
            assert a != b


def test_within_participant_a_twin_never_sees_a_profile_twice():
    block = _generic(3, "within_participant", tasks=4)  # 9 profiles, 8 needed
    for t in range(40):
        profiles = [tuple(sorted(a["levels"].items())) for task in draw_conjoint(block, design_rng(4, f"t{t}", "g"))["tasks"] for a in task["alternatives"]]
        assert len(profiles) == len(set(profiles)) == 8


def test_an_impossible_design_is_refused_when_the_survey_is_read():
    block = coffee_block()
    block["constraints"]["forbidden_profiles"].append({"brand": "mcd"})  # McDonald's can never appear
    with pytest.raises(st.SurveyFormatError) as caught:
        st.read_survey(survey_with(block))
    assert "experiment.infeasible" in codes(caught.value)
    too_many = _generic(2, "within_participant", tasks=3)  # 4 profiles, 6 needed
    with pytest.raises(st.SurveyFormatError) as caught:
        st.read_survey(survey_with(too_many))
    assert "experiment.infeasible" in codes(caught.value)


def test_a_draw_that_keeps_failing_stops_instead_of_loosening_a_rule():
    block = _generic(2, "within_task", count=2)
    block["constraints"] = {"forbidden_profiles": [{"a0": "l0", "a1": "l0"}, {"a0": "l0", "a1": "l1"}, {"a0": "l1", "a1": "l0"}]}
    block["design"]["max_attempts"] = 3  # one valid profile: two different ones never come
    with pytest.raises(DesignError, match="no valid profile|duplicate"):
        draw_conjoint(block, design_rng(5, "t", "g"))


def test_a_twin_gets_the_same_tasks_every_time_and_twins_differ():
    element = compile_block(coffee_block())
    first, records = expand_block(element, seed=9, twin="ann")
    again, records_again = expand_block(copy.deepcopy(element), seed=9, twin="ann")
    other, other_records = expand_block(element, seed=9, twin="bob")
    assert first == again and records == records_again
    assert records != other_records
    # by default the block's six tasks are asked together; with unit "task", one block each
    assert len(first) == 1 and len(first[0]["Questions"]) == 6 and "StageBreak" not in first[0]
    one_by_one = coffee_block()
    one_by_one["administration"] = {"unit": "task"}
    blocks, task_records = expand_block(compile_block(one_by_one), seed=9, twin="ann")
    assert [b["BlockID"] for b in blocks] == [f"coffee_task{n}" for n in range(1, 7)] and all(b["StageBreak"] for b in blocks)
    assert task_records == records  # the same draws either way


def test_the_fixed_attribute_stays_first_and_the_rest_shuffle_by_twin():
    element = compile_block(coffee_block())
    orders = set()
    for twin in ("a", "b", "c", "d", "e", "f"):
        _, records = expand_block(element, seed=3, twin=twin)
        assert {tuple(r["attribute_order"]) for r in records} == {tuple(records[0]["attribute_order"])}  # once per twin
        assert records[0]["attribute_order"][0] == "brand"
        orders.add(tuple(records[0]["attribute_order"]))
    assert len(orders) > 1


def test_a_supplied_design_is_assigned_by_position_and_checked():
    block = coffee_block()
    block["design"] = {
        "mode": "supplied",
        "assignment": "by_index",
        "versions": [
            {"id": "v1", "tasks": [{"id": "1", "alternatives": [
                {"id": "mcd", "levels": {"brand": "mcd", "seating": "seats", "promotion": "none", "price": "p300"}},
                {"id": "dunkin", "levels": {"brand": "dunkin", "seating": "wifi", "promotion": "discount", "price": "p450"}},
                {"id": "starbucks", "levels": {"brand": "starbucks", "seating": "seats", "promotion": "none", "price": "p600"}},
            ]}]},
            {"id": "v2", "tasks": [{"id": "1", "alternatives": [
                {"id": "mcd", "levels": {"brand": "mcd", "seating": "none", "promotion": "upgrade", "price": "p450"}},
                {"id": "dunkin", "levels": {"brand": "dunkin", "seating": "seats", "promotion": "none", "price": "p300"}},
                {"id": "starbucks", "levels": {"brand": "starbucks", "seating": "wifi", "promotion": "upgrade", "price": "p450"}},
            ]}]},
        ],
    }
    st.read_survey(survey_with(block))
    element = compile_block(block)
    assert [r["version"] for i in range(4) for r in expand_block(element, seed=1, twin=f"t{i}", twin_index=i)[1]] == ["v1", "v2", "v1", "v2"]
    broken = copy.deepcopy(block)
    broken["design"]["versions"][0]["tasks"][0]["alternatives"][2]["levels"]["price"] = "p300"  # Starbucks at 3.00
    with pytest.raises(st.SurveyFormatError) as caught:
        st.read_survey(survey_with(broken))
    assert "experiment.constraints" in codes(caught.value)


@pytest.mark.parametrize(
    "change, code",
    [
        (lambda b: b["constraints"]["applicability"][0].update(row_levels=["p300", "p450"]), "experiment.constraints"),
        (lambda b: b["alternatives"]["items"][0].update(fixed_levels={"brand": "costa"}), "reference.experiment"),
        (lambda b: b["response"]["outside_option"].update(id="mcd"), "id.duplicate"),
        (lambda b: b["response"]["outside_option"].update(label="Dunkin"), "label.duplicate"),
        (lambda b: b["presentation"]["attribute_order"]["fixed_positions"].update(size=2), "reference.experiment"),
    ],
)
def test_conjoint_problems_are_errors(change, code):
    block = coffee_block()
    change(block)
    with pytest.raises(st.SurveyFormatError) as caught:
        st.read_survey(survey_with(block))
    assert code in codes(caught.value)


# ---- MaxDiff draws --------------------------------------------------------------------------


def test_each_task_shows_distinct_items_and_no_subset_twice():
    block = features_block()
    block["design"]["generator"] = "uniform_subsets"
    for t in range(60):
        tasks = draw_maxdiff(block, design_rng(6, f"t{t}", "features"))["tasks"]
        subsets = [frozenset(task["items"]) for task in tasks]
        assert all(len(task["items"]) == 4 == len(set(task["items"])) for task in tasks)
        assert len(set(subsets)) == len(subsets)


def test_balanced_subsets_show_every_item_within_one_of_the_others():
    block = features_block()
    for t in range(60):
        counts = Counter(i for task in draw_maxdiff(block, design_rng(7, f"t{t}", "features"))["tasks"] for i in task["items"])
        assert len(counts) == 8 and max(counts.values()) - min(counts.values()) <= 1


@pytest.mark.parametrize(
    "change, code",
    [
        (lambda b: b["items"].append({"id": "price2", "label": "low price"}), "label.duplicate"),
        (lambda b: b["response"].update(worst_label="Most important"), "experiment.response"),
        (lambda b: b["design"].update(items_per_task=9), "experiment.infeasible"),
    ],
)
def test_maxdiff_problems_are_errors(change, code):
    block = features_block()
    change(block)
    with pytest.raises(st.SurveyFormatError) as caught:
        st.read_survey(survey_with(block))
    assert code in codes(caught.value)


def test_a_block_shown_twice_or_a_task_id_taken_is_refused():
    data = survey_with(features_block())
    data["flow"].append({"type": "show_block", "block": "features"})
    with pytest.raises(st.SurveyFormatError) as caught:
        st.read_survey(data)
    assert "experiment.shown_twice" in codes(caught.value)
    data = survey_with(features_block())
    data["questions"] = [{"id": "features_t1_best", "content": [{"type": "text", "text": "x"}], "response": {"type": "text"}}]
    data["blocks"].append({"id": "other", "kind": "survey", "questions": ["features_t1_best"]})
    data["flow"].append({"type": "show_block", "block": "other"})
    with pytest.raises(st.SurveyFormatError) as caught:
        st.read_survey(data)
    assert "id.duplicate" in codes(caught.value)


# ---- asking the tasks -------------------------------------------------------------------------


def _recording_model(choose=None):
    calls: list[str] = []

    def reply(request: st.ModelRequest) -> st.Reply:
        calls.append(request.user)
        answers = answer_questions(request.user) if "Respond using JSON schema" in request.user else []
        if choose:
            answers = choose(request.user, answers)
        return st.Reply(text=json.dumps({"answers": answers}), input_tokens=10, output_tokens=10)

    return st.Model.from_function(reply, name="recording", images=True), calls


def test_by_default_all_tasks_of_a_block_are_asked_in_one_call_without_staging():
    model, calls = _recording_model()
    plan = st.plan(st.read_survey(EXAMPLES / "coffee-conjoint.json"), configure(sample_size=1, model=model))
    assert plan.mode == "static"  # drawing profiles needs no earlier answer
    results = st.run(plan)
    assert len(calls) == 1 and all(f'"id": "coffee_t{n}"' in calls[0] for n in range(1, 7))
    assert len(results.audit[0]["experiments"]) == 6 and len(results.answers) == 7


def test_with_unit_task_each_conjoint_task_is_asked_in_a_call_of_its_own():
    data = copy.deepcopy(COFFEE)
    next(b for b in data["blocks"] if b["kind"] == "conjoint")["administration"] = {"unit": "task"}
    model, calls = _recording_model()
    plan = st.plan(st.read_survey(data), configure(sample_size=1, model=model))
    assert plan.mode == "staged"
    results = st.run(plan)
    per_call = [sum(f'"id": "coffee_t{n}"' in text for n in range(1, 7)) for text in calls]
    assert per_call.count(1) == 6 and max(per_call) == 1
    assert len(results.audit[0]["experiments"]) == 6


def test_the_answer_names_the_alternative_shown_whatever_its_position(tmp_path):
    def pick_second(user, answers):
        for a in answers:
            if a["question_id"].startswith("coffee_t"):
                options = next(q["options"] for q in asked_questions(user) if q["id"] == a["question_id"])
                a["answer_value"] = a["answer_label"] = a["answer_raw"] = options[1]
        return answers

    model, _ = _recording_model(pick_second)
    results = st.run(st.plan(st.read_survey(EXAMPLES / "coffee-conjoint.json"), configure(sample_size=2, model=model)))
    rows = results.exposures["conjoint"]
    chosen = [r for r in rows if r["chosen"] == "1"]
    assert len(chosen) == 12 and all(r["position"] == 2 for r in chosen)
    for row in chosen:  # the chosen option is the alternative recorded at that position, levels and all
        answer = next(a for a in results.answers if a.twin_id == row["twin_id"] and a.question_id == row["question_id"])
        assert answer.label == row["label"] and row["brand"] == row["alternative"]
    results.save(tmp_path / "run")
    with (tmp_path / "run" / "conjoint_exposures.csv").open() as f:
        saved = list(csv.DictReader(f))
    assert len(saved) == len(rows) == 2 * 6 * 4
    assert st.Results.load(tmp_path / "run").exposures == results.exposures


def test_the_outside_option_can_be_chosen():
    def pick_none(user, answers):
        for a in answers:
            if a["question_id"].startswith("coffee_t"):
                a["answer_value"] = a["answer_label"] = a["answer_raw"] = "None of these"
        return answers

    model, _ = _recording_model(pick_none)
    results = st.run(st.plan(st.read_survey(EXAMPLES / "coffee-conjoint.json"), configure(sample_size=1, model=model)))
    outside = [r for r in results.exposures["conjoint"] if r["outside"] == "1"]
    assert len(outside) == 6 and all(r["chosen"] == "1" for r in outside)


def test_a_conjoint_task_shows_its_alternatives_as_a_table():
    from surveytwin.experiments.tasks import conjoint_tasks

    block = coffee_block()
    tasks, _ = conjoint_tasks(block, seed=7, twin="t1", twin_index=0)
    names = {a["id"]: a["name"] for a in block["attributes"]}
    for task in tasks:
        record = task["record"]
        assert record["layout"] == "table"
        lines = task["text"].split("\n")
        assert lines[:2] == ["Which of these coffee shops would you go to?", ""]
        # a column per alternative, in the order shown; a row per attribute, in the order drawn
        assert lines[2] == "| | " + " | ".join(a["label"] for a in record["alternatives"]) + " |"
        assert lines[3] == "|---|---|---|---|"
        assert lines[4:] == [
            "| " + " | ".join([names[attribute], *(a["levels"][attribute]["label"] for a in record["alternatives"])]) + " |"
            for attribute in record["attribute_order"]
        ]
        assert task["with_pictures"] == "" and task["media"] == []


def test_the_list_layout_gives_each_alternative_a_line_per_attribute():
    from surveytwin.experiments.tasks import conjoint_tasks

    block = coffee_block()
    block["presentation"]["layout"] = "list"
    task = conjoint_tasks(block, seed=7, twin="t1", twin_index=0)[0][0]
    first = task["record"]["alternatives"][0]
    assert task["record"]["layout"] == "list"
    assert task["text"].split("\n")[2:4] == [first["label"], f"- Brand: {first['levels']['brand']['label']}"]
    block["presentation"]["layout"] = "grid"
    with pytest.raises(st.SurveyFormatError):
        st.read_survey(survey_with(block))


def test_a_level_with_a_bar_or_lines_stays_in_its_cell():
    from surveytwin.experiments.tasks import conjoint_tasks

    block = coffee_block()
    seating = next(a for a in block["attributes"] if a["id"] == "seating")
    for level in seating["levels"]:
        level["label"] = level["label"] + " | indoors\nand out"
    task = conjoint_tasks(block, seed=7, twin="t1", twin_index=0)[0][0]
    row = next(line for line in task["text"].split("\n") if line.startswith("| Seating |"))
    assert row.count(" | indoors; and out") == 0 and row.count(" \\| indoors; and out") == 3
    assert len(row.replace("\\|", "").split("|")) == 6  # still four cells


def test_a_level_picture_is_sent_in_its_cell():
    import json as _json

    data = copy.deepcopy(COFFEE)
    block = next(b for b in data["blocks"] if b["kind"] == "conjoint")
    brand = next(a for a in block["attributes"] if a["id"] == "brand")
    png = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAQAAAADCAIAAAA7ljmRAAAAE0lEQVR4nGM8ISfHAANMcBY6BwAm2gEKUjNC5gAAAABJRU5ErkJggg==")
    level_pictures = {  # a picture per level, told apart by bytes after its end
        level["id"]: "data:image/png;base64," + base64.b64encode(png + level["id"].encode()).decode("ascii")
        for level in brand["levels"]
    }
    for level in brand["levels"]:
        level["media"] = [{"source": level_pictures[level["id"]]}]
    requests = []

    def reply(request: st.ModelRequest) -> st.Reply:
        requests.append(request)
        return st.Reply(text=_json.dumps({"answers": answer_questions(request.user)}), input_tokens=10, output_tokens=10)

    st.run(st.plan(st.read_survey(data), configure(sample_size=1, model=st.Model.from_function(reply, name="r", images=True))))
    parts = requests[0].content
    pictures = [n for n, part in enumerate(parts) if part["type"] == "image"]
    assert len(pictures) == 6 * 3  # each task's three brand cells
    for n in pictures:  # each right after its cell's label, the table going on after it
        before = parts[n - 1]["text"]
        label = before[before.rindex("[Image "):]
        brand_shown = label.split("Title: ")[1].split(",")[0]
        assert parts[n]["url"] == level_pictures[next(lv["id"] for lv in brand["levels"] if lv["label"] == brand_shown)]
        assert before[: before.rindex("[Image ")].rstrip().endswith(f"| {brand_shown}")
        assert parts[n + 1]["text"].startswith(" |")


def test_static_and_staged_runs_draw_the_same_tasks():
    data = copy.deepcopy(FEATURES)
    data["blocks"][0]["administration"] = {"unit": "task"}
    staged_plan = st.plan(st.read_survey(data), configure())
    assert staged_plan.mode == "staged"
    staged = st.run(staged_plan)
    plan = st.plan(st.read_survey(EXAMPLES / "feature-maxdiff.json"), configure())
    assert plan.mode == "static"
    static = st.run(plan)

    def tasks(results):
        return {(r["persona_id"], e["task"]): [i["item"] for i in e["items"]] for r in results.audit for e in r["experiments"]}

    assert tasks(staged) == tasks(static) and len(tasks(static)) == 12


def _options(user: str, question_id: str) -> list[str]:
    """The options a request lists for a question."""
    return next(q["options"] for q in asked_questions(user) if q["id"] == question_id)


def _worst_is(pick):
    """A choice that answers each worst question with ``pick(its options)``."""

    def choose(user, answers):
        for a in answers:
            if a["question_id"].endswith("_worst"):
                a["answer_value"] = a["answer_label"] = a["answer_raw"] = pick(_options(user, a["question_id"]))
        return answers

    return choose


@pytest.mark.parametrize("unit, call_count", [("block", 1), ("task", 6)])
def test_best_and_worst_are_checked_after_the_answers_and_never_asked_again(unit, call_count):
    from surveytwin.answers.executor import BEST_WORST_RULE

    model, calls = _recording_model()  # always the first option, for best and worst
    data = copy.deepcopy(FEATURES)
    data["blocks"][0]["administration"] = {"unit": unit}
    results = st.run(st.plan(st.read_survey(data), configure(sample_size=1, model=model)))
    assert len(calls) == call_count  # no call of its own to ask a pair again
    assert all(BEST_WORST_RULE in c for c in calls)  # the rule goes with the questions
    flagged = results.flagged()
    assert len(flagged) == 12 and all(a.status == "skipped" and a.value == "" and a.raw for a in flagged)
    assert all(r["best"] == "" and r["worst"] == "" for r in results.exposures["maxdiff"])
    assert any("named the same item twice" in w and "for a rerun to ask again" in w for w in results.warnings)


def test_a_rerun_asks_a_pair_that_named_one_item_twice_with_its_rule():
    from surveytwin.answers.executor import BEST_WORST_RULE

    model, _ = _recording_model()  # every pair names one item twice
    results = st.run(st.plan(st.read_survey(EXAMPLES / "feature-maxdiff.json"), configure(sample_size=1, model=model)))
    assert len(results.flagged()) == 12
    fixer, calls = _recording_model(_worst_is(lambda options: options[-1]))
    fixed = results.rerun_flagged(model=fixer)
    assert len(calls) == 1 and BEST_WORST_RULE in calls[0]
    assert fixed.flagged() == [] and fixed.provenance["reruns"][-1]["repaired"] == 12
    rows = fixed.exposures["maxdiff"]
    for task in range(1, 7):
        mine = [r for r in rows if r["task"] == task]
        assert [r["best"] for r in mine].count("1") == 1 and [r["worst"] for r in mine].count("1") == 1
        assert next(r for r in mine if r["best"] == "1")["item"] != next(r for r in mine if r["worst"] == "1")["item"]


def test_a_repair_that_names_one_item_twice_is_not_stored():
    model, _ = _recording_model()  # every pair names one item twice, in the run and in the rerun
    results = st.run(st.plan(st.read_survey(EXAMPLES / "feature-maxdiff.json"), configure(sample_size=1, model=model)))
    fixed = results.rerun_flagged(model=_recording_model()[0])
    assert len(fixed.flagged()) == 12 and not any(a.repair for a in fixed.answers)
    assert fixed.provenance["reruns"][-1]["repaired"] == 0
    assert any("6 best/worst pair(s) named the same item twice when asked again" in w for w in fixed.warnings)


def test_a_repair_may_not_name_the_item_its_pair_already_names():
    model, _ = _recording_model(_worst_is(lambda options: "Something else"))  # worst is out of range
    results = st.run(st.plan(st.read_survey(EXAMPLES / "feature-maxdiff.json"), configure(sample_size=1, model=model)))
    flagged = results.flagged()
    assert len(flagged) == 6 and all(a.question_id.endswith("_worst") and a.status == "out_of_range" for a in flagged)
    fixed = results.rerun_flagged(model=_recording_model()[0])  # the first item: the one best names
    assert len(fixed.flagged()) == 6 and not any(a.repair for a in fixed.answers)
    assert any("named the same item twice when asked again" in w for w in fixed.warnings)
    other = results.rerun_flagged(model=_recording_model(_worst_is(lambda options: options[-1]))[0])
    assert other.flagged() == []


def test_the_anchor_follow_up_is_asked_with_its_task():
    data = copy.deepcopy(FEATURES)
    data["blocks"][0]["anchor"] = {
        "id": "importance",
        "content": [{"type": "text", "text": "Considering the same features, how many are important to you?"}],
        "response": {"type": "choice", "mode": "single", "options": [{"id": "all", "label": "All of them"}, {"id": "some", "label": "Some of them"}, {"id": "none", "label": "None of them"}]},
    }
    model, calls = _recording_model()
    results = st.run(st.plan(st.read_survey(data), configure(sample_size=1, model=model)))
    assert all(('"features_t%d_anchor"' % n) in next(c for c in calls if '"features_t%d_best"' % n in c) for n in range(1, 7))
    assert {r["anchor"] for r in results.exposures["maxdiff"]} == {"All of them"}


def test_an_order_drawn_once_per_twin_is_the_same_in_every_task():
    block = coffee_block()
    block["presentation"]["alternative_order"] = {"mode": "shuffle", "scope": "participant"}
    element = compile_block(block)
    orders = set()
    for twin in ("a", "b", "c", "d", "e", "f"):
        _, records = expand_block(element, seed=4, twin=twin)
        mine = {tuple(a["alternative"] for a in r["alternatives"]) for r in records}
        assert len(mine) == 1  # the same order in all six tasks
        orders |= mine
    assert len(orders) > 1  # and it differs between twins
    md = features_block()
    md["presentation"]["item_order"] = {"mode": "shuffle", "scope": "participant"}
    _, records = expand_block(compile_block(md), seed=4, twin="a")
    before: set[tuple[str, str]] = set()
    for record in records:  # two items shown together always come in the same order
        shown = [i["item"] for i in record["items"]]
        before |= {(a, b) for n, a in enumerate(shown) for b in shown[n + 1:]}
    assert not any((b, a) in before for a, b in before)


def test_the_standard_form_of_a_survey_with_an_experiment_keeps_the_block_definition():
    survey = st.read_survey(EXAMPLES / "coffee-conjoint.json")
    trimmed = survey.drop_blocks(["start"])
    standard = trimmed.standard
    assert [b["kind"] for b in standard["blocks"]] == ["conjoint"]
    assert standard["blocks"][0]["attributes"] == coffee_block()["attributes"]
    assert not any(q["id"].startswith("coffee_t") for q in standard["questions"])
    from surveytwin.spec.compile import compile_survey

    assert compile_survey(standard)["Elements"] == trimmed.template["Elements"]


def test_builder_edits_leave_experiment_blocks_alone():
    from surveytwin import builder

    survey = st.read_survey(EXAMPLES / "coffee-conjoint.json")
    with pytest.raises(st.UnsupportedEdit, match="conjoint block"):
        builder.update_question(survey, "coffee_t1", {"text": "Something else"})
    with pytest.raises(st.UnsupportedEdit, match="conjoint block"):
        builder.remove_questions(survey, ["coffee_t2"])
