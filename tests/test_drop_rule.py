"""A dropped question no longer exists. Logic that reads one sees a
question never shown, and the rest of the survey is still asked, wherever that
logic sits; the same holds for a question the twin never sees for any other
reason. These cases come from the second and third reviews
(2026-09-28)."""

from __future__ import annotations

import copy
import json

import pytest

import surveytwin as st
from conftest import qsf
from fakes import accept_all_images, scripted_model

BASE = json.loads(qsf("kitchen_sink_supported.qsf").read_text(encoding="utf-8"))
LATER_BLOCKS = {"BL_KS_TG_10", "BL_KS_FB_12", "BL_KS_FB_13"}  # always shown, after the middle of the flow


def flow(data):
    return next(e for e in data["SurveyElements"] if e["Element"] == "FL")["Payload"]["Flow"]


def payload(data, qid):
    return next(e for e in data["SurveyElements"] if e.get("PrimaryAttribute") == qid)["Payload"]


def with_branch_mid_survey(operator="Selected"):
    """FL_14 ("if QID56 choice 1 is selected, show BL_KS_FB_14") moved to right after BL_KS_TE_4."""
    data = copy.deepcopy(BASE)
    items = flow(data)
    branch = next(f for f in items if f.get("FlowID") == "FL_14")
    items.remove(branch)
    items.insert(next(i for i, f in enumerate(items) if f.get("ID") == "BL_KS_TE_4") + 1, branch)
    branch["BranchLogic"]["0"]["0"]["Operator"] = operator
    return data


def run_dropped(data, mini_bank):
    survey = st.read_qsf(data).drop_blocks(["BL_KS_MC_1"])
    config = st.configure(bank=mini_bank, sample_size=3, seed=2, model=scripted_model())
    results = st.run(st.plan(survey, config), image_checker=accept_all_images)
    import re

    asked_ids = {re.sub(r"^\d+_", "", r["question_id"]).split("_")[0].split("#")[0] for r in results.rows}  # loop rows are "1_QID46"
    asked_blocks = {q.block_id for q in survey.questions.values() if q.id in asked_ids}
    return survey, results, asked_blocks


def assert_no_stall(results):
    # ("blocked" events are the normal pause between stages; a stall is a twin that
    # never gets past one, which the run reports with this warning.)
    assert not [w for w in results.warnings if "stalled" in w]
    assert not [a for a in results.audit if a.get("stopped_at_stage_limit")]


def test_a_branch_reading_a_dropped_question_is_not_taken_and_the_rest_is_asked(mini_bank):
    _survey, results, asked = run_dropped(with_branch_mid_survey("Selected"), mini_bank)
    assert_no_stall(results)
    assert "BL_KS_FB_14" not in asked  # "is selected" on a question never shown: false
    assert LATER_BLOCKS <= asked


def test_not_selected_on_a_dropped_question_is_true(mini_bank):
    _survey, results, asked = run_dropped(with_branch_mid_survey("NotSelected"), mini_bank)
    assert_no_stall(results)
    assert "BL_KS_FB_14" in asked and LATER_BLOCKS <= asked


@pytest.mark.parametrize("operator, shown", [("NotSelected", True), ("Selected", False)])
def test_display_logic_reading_a_dropped_question(mini_bank, operator, shown):
    data = copy.deepcopy(BASE)
    payload(data, "QID14")["DisplayLogic"] = {
        "0": {"0": {"LogicType": "Question", "QuestionID": "QID56", "QuestionIsInLoop": "no", "ChoiceLocator": "q://QID56/SelectableChoice/1",
                    "Operator": operator, "QuestionIDFromLocator": "QID56", "LeftOperand": "q://QID56/SelectableChoice/1", "Type": "Expression"},
              "Type": "If"},
        "Type": "BooleanExpression",
        "inPage": False,
    }
    _survey, results, asked = run_dropped(data, mini_bank)
    assert_no_stall(results)
    assert any(r["question_id"] == "QID14" for r in results.rows) is shown
    assert LATER_BLOCKS <= asked


@pytest.mark.parametrize("pipe", ["${q://QID56/ChoiceGroup/SelectedChoices}", "${q://QID56/QuestionText}", "${q://QID56/ChoiceDescription/1}"])
def test_piping_a_dropped_question_shows_nothing(mini_bank, pipe):
    data = copy.deepcopy(BASE)
    payload(data, "QID14")["QuestionText"] = f"You picked {pipe}. What is your full name?"
    _survey, results, asked = run_dropped(data, mini_bank)
    assert_no_stall(results)
    texts = [r["question_text"] for r in results.rows if r["question_id"] == "QID14"]
    assert texts and all("${q://" not in t for t in texts)
    assert LATER_BLOCKS <= asked


def test_embedded_data_piping_a_dropped_question(mini_bank):
    data = copy.deepcopy(BASE)
    items = flow(data)
    at = next(i for i, f in enumerate(items) if f.get("ID") == "BL_KS_TE_4")
    items.insert(at, {"Type": "EmbeddedData", "FlowID": "FL_900", "EmbeddedData": [
        {"Description": "picked", "Type": "Custom", "Field": "picked", "VariableType": "String", "DataVisibility": [], "AnalyzeText": False,
         "Value": "${q://QID56/ChoiceGroup/SelectedChoices}"}]})
    _survey, results, asked = run_dropped(data, mini_bank)
    assert_no_stall(results)
    assert {"BL_KS_TE_4"} | LATER_BLOCKS <= asked


def test_a_screen_out_whose_questions_are_all_dropped_goes_with_its_other_conditions(mini_bank):
    """The website's rule: a screen-out whose every question reference
    is dropped is removed, whatever else it tests. Kept and evaluated, "(consent not
    selected) and (panel is prolific)" would screen out every prolific twin."""
    for conjunction in ("And", "Or"):
        data = copy.deepcopy(BASE)
        items = flow(data)
        items.insert(0, {"Type": "EmbeddedData", "FlowID": "FL_905", "EmbeddedData": [{"Field": "pet", "Type": "Custom", "Value": "Dog"}]})
        branch = next(f for f in items if f.get("FlowID") == "FL_14")
        branch["Flow"] = [{"Type": "EndSurvey", "FlowID": "FL_901"}]  # a screen-out
        branch["BranchLogic"]["0"]["0"]["Operator"] = "NotSelected"
        branch["BranchLogic"]["0"]["1"] = {"LogicType": "EmbeddedField", "LeftOperand": "pet", "Operator": "EqualTo", "RightOperand": "Dog",
                                           "Type": "Expression", "Conjuction": conjunction}
        survey = st.read_qsf(data).drop_blocks(["BL_KS_MC_1"])
        assert "FL FL_14" in survey.drop_report.pruned_branches, conjunction
    config = st.configure(bank=mini_bank, sample_size=2, seed=2, model=scripted_model())
    results = st.run(st.plan(survey, config), image_checker=accept_all_images)
    import re

    asked_ids = {re.sub(r"^\d+_", "", r["question_id"]).split("_")[0].split("#")[0] for r in results.rows}  # loop rows are "1_QID46"
    assert LATER_BLOCKS <= {q.block_id for q in survey.questions.values() if q.id in asked_ids}  # nobody screened out


def test_static_and_staged_plans_use_the_same_survey():
    """A screen-out removed with the dropped block is gone from static plans too."""
    data = copy.deepcopy(BASE)
    survey = st.read_qsf(data).drop_blocks(["BL_KS_MC_1"])
    from surveytwin.runtime.instrument_plan import build_instrument_from_template

    from surveytwin.spec.compile import compile_survey

    staged = build_instrument_from_template(survey._full_template, 2, excluded_block_ids=survey.exclude_blocks, mode="staged")
    assert compile_survey(staged["definition"]) == survey.template


# ---- third review (2026-09-28): a question hidden because of a dropped one --------------------


def cond(qid, locator, operator, right=None):
    c = {"LogicType": "Question", "QuestionID": qid, "QuestionIsInLoop": "no", "ChoiceLocator": locator,
         "Operator": operator, "QuestionIDFromLocator": qid, "LeftOperand": locator, "Type": "Expression"}
    if right is not None:
        c["RightOperand"] = right
    return {"0": {"0": c, "Type": "If"}, "Type": "BooleanExpression", "inPage": False}


def test_logic_reading_a_question_hidden_by_a_dropped_one_is_decided(mini_bank):
    """QID14 is hidden (it shows only if the dropped QID56 was picked); QID64 shows
    only if QID14 has an answer, and QID45 pipes QID14's answer."""
    data = copy.deepcopy(BASE)
    payload(data, "QID14")["DisplayLogic"] = cond("QID56", "q://QID56/SelectableChoice/1", "Selected")
    payload(data, "QID64")["DisplayLogic"] = cond("QID14", "q://QID14/ChoiceTextEntryValue", "NotEmpty")
    payload(data, "QID45")["QuestionText"] = "You wrote ${q://QID14/ChoiceTextEntryValue}. Which animal image did you see earlier?"
    _survey, results, asked = run_dropped(data, mini_bank)
    assert_no_stall(results)
    ids = {r["question_id"] for r in results.rows}
    assert "QID14" not in ids and not any(i.startswith("QID64") for i in ids)  # never shown, so empty
    assert "QID45" in ids and LATER_BLOCKS <= asked
    assert all("${q://" not in r["question_text"] for r in results.rows if r["question_id"] == "QID45")


def test_a_question_in_a_branch_not_taken_counts_as_never_shown(mini_bank):
    data = with_branch_mid_survey("Selected")  # FL_14 (BL_KS_FB_14, QID48) is never taken
    payload(data, "QID44")["DisplayLogic"] = cond("QID48", "q://QID48/ChoiceTextEntryValue", "Empty")
    _survey, results, asked = run_dropped(data, mini_bank)
    assert_no_stall(results)
    assert any(r["question_id"] == "QID44" for r in results.rows) and LATER_BLOCKS <= asked


def test_a_question_in_a_randomizer_arm_not_picked_counts_as_never_shown(mini_bank):
    data = copy.deepcopy(BASE)
    next(f for f in flow(data) if f.get("FlowID") == "FL_10")["SubSet"] = 1  # one of four blocks per twin
    payload(data, "QID44")["DisplayLogic"] = cond("QID51", "q://QID51/ChoiceTextEntryValue", "NotEmpty")  # QID51 is in one arm
    survey = st.read_qsf(data)
    config = st.configure(bank=mini_bank, sample_size=4, seed=2, model=scripted_model())
    results = st.run(st.plan(survey, config), image_checker=accept_all_images)
    assert_no_stall(results)
    for twin in results.twins:
        ids = {r["question_id"] for r in results.rows if r["persona_id"] == twin["id"]}
        assert "QID45" in ids  # the rest of the survey is asked either way
        assert ("QID44" in ids) == ("QID51" in ids)


def test_without_any_drop_a_hidden_question_no_longer_stops_the_survey(mini_bank):
    """The website's own runtime stops here (2026-09-28): QID14 hidden by an answer, QID64 reads QID14."""
    data = copy.deepcopy(BASE)
    payload(data, "QID14")["DisplayLogic"] = cond("QID13", "q://QID13/ChoiceTextEntryValue", "EqualTo", "zzz")
    payload(data, "QID64")["DisplayLogic"] = cond("QID14", "q://QID14/ChoiceTextEntryValue", "NotEmpty")
    config = st.configure(bank=mini_bank, sample_size=3, seed=2, model=scripted_model())
    results = st.run(st.plan(st.read_qsf(data), config), image_checker=accept_all_images)
    assert_no_stall(results)
    assert {"QID54", "QID45"} <= {r["question_id"] for r in results.rows}


@pytest.mark.parametrize("operator", ["LessThan", "GreaterThanOrEqual"])
def test_a_number_comparison_on_a_question_never_shown_is_false(mini_bank, operator):
    data = copy.deepcopy(BASE)
    payload(data, "QID14")["DisplayLogic"] = cond("QID56", "q://QID56/ChoiceNumericEntryValue/1", operator, "1000")
    _survey, results, _asked = run_dropped(data, mini_bank)
    assert_no_stall(results)
    assert not any(r["question_id"] == "QID14" for r in results.rows)


def test_a_screen_out_whose_other_content_was_dropped_is_removed(mini_bank):
    """'If QID56 is not selected: BL_KS_FB_14, then End Survey' with both blocks dropped."""
    data = with_branch_mid_survey("NotSelected")
    branch = next(f for f in flow(data) if f.get("FlowID") == "FL_14")
    branch["Flow"] = branch["Flow"] + [{"Type": "EndSurvey", "FlowID": "FL_902"}]
    survey = st.read_qsf(data).drop_blocks(["BL_KS_MC_1", "BL_KS_FB_14"])
    assert "FL FL_14" in survey.drop_report.pruned_branches
    config = st.configure(bank=mini_bank, sample_size=2, seed=2, model=scripted_model())
    results = st.run(st.plan(survey, config), image_checker=accept_all_images)
    assert {"QID54", "QID45"} <= {r["question_id"] for r in results.rows}  # nobody screened out


def test_a_branch_that_only_sets_embedded_data_from_a_dropped_question_goes_with_it(mini_bank):
    """Nothing runs on a dropped question alone (as on the website): kept,
    "if QID56 is not selected: screened = 1" would flag every twin, and a later
    "if screened = 1: End Survey" would end every survey."""
    data = copy.deepcopy(BASE)
    items = flow(data)
    at = next(i for i, f in enumerate(items) if f.get("ID") == "BL_KS_TE_4")
    items.insert(at, {"Type": "Branch", "FlowID": "FL_903", "Description": "New Branch",
                      "BranchLogic": cond("QID56", "q://QID56/SelectableChoice/1", "NotSelected"),
                      "Flow": [{"Type": "EmbeddedData", "FlowID": "FL_904", "EmbeddedData": [
                          {"Description": "screened", "Type": "Custom", "Field": "screened", "VariableType": "String", "DataVisibility": [],
                           "AnalyzeText": False, "Value": "1"}]}]})
    survey = st.read_qsf(data).drop_blocks(["BL_KS_MC_1"])
    assert "FL FL_903" in survey.drop_report.pruned_branches
    config = st.configure(bank=mini_bank, sample_size=2, seed=2, model=scripted_model())
    results = st.run(st.plan(survey, config), image_checker=accept_all_images)
    assert {a["embedded_data"].get("screened") for a in results.audit} == {None}


def test_a_plain_drop_warns_once_per_run_not_once_per_twin(mini_bank):
    _survey, results, _asked = run_dropped(copy.deepcopy(BASE), mini_bank)
    assert not [w for w in results.warnings if "Could not resolve" in w]
    assert len([w for w in results.warnings if "reads a dropped question" in w]) == 1


def test_loop_pipes_and_loop_fields_are_reported():
    data = copy.deepcopy(BASE)
    payload(data, "QID45")["QuestionText"] = "You had ${q://1_QID43/ChoiceGroup/SelectedChoices}. Which animal image did you see earlier?"
    survey = st.read_qsf(data).drop_blocks(["BL_KS_FB_12"])
    assert ("piping", "SQ QID45", "QID43") in {(d.kind, d.location, d.reads) for d in survey.drop_report.references}
    data = copy.deepcopy(BASE)
    block = next(e for e in data["SurveyElements"] if e["Element"] == "BL")
    for b in (block["Payload"].values() if isinstance(block["Payload"], dict) else block["Payload"]):
        if b.get("ID") == "BL_KS_FB_12":
            b["Options"]["LoopingOptions"]["Static"]["1"]["1"] = "${q://QID56/ChoiceGroup/SelectedChoices}"
    survey = st.read_qsf(data).drop_blocks(["BL_KS_MC_1"])
    assert ("loop_fields", "BL BL_KS_FB_12", "QID56") in {(d.kind, d.location, d.reads) for d in survey.drop_report.references}


def test_display_logic_on_single_choices_is_reported():
    data = copy.deepcopy(BASE)
    choices = next(e for e in data["SurveyElements"] if e.get("PrimaryAttribute") == "QID45")["Payload"]["Choices"]
    choices["1"]["DisplayLogic"] = cond("QID56", "q://QID56/SelectableChoice/1", "Selected")
    check = st.read_qsf(data).check()
    assert any(f.feature == "question_option.choice_display_logic" and f.source == "SQ QID45" and f.level == "Partial"
               and "is applied" in f.message for f in check.findings)
    survey = st.read_qsf(data).drop_blocks(["BL_KS_MC_1"])
    # Choice display logic is listed apart from the logic of questions and branches.
    assert ("choice_display_logic", "SQ QID45 choice 1", "QID56") in {(d.kind, d.location, d.reads) for d in survey.drop_report.choice_logic}
    assert "choice_display_logic" not in {d.kind for d in survey.drop_report.references}
    assert "Display logic on choices that reads a dropped question (it counts as never shown" in str(survey.drop_report)


def test_logic_reading_a_question_the_survey_does_not_have(mini_bank):
    data = copy.deepcopy(BASE)
    payload(data, "QID14")["DisplayLogic"] = cond("QID999", "q://QID999/SelectableChoice/1", "Selected")
    survey = st.read_qsf(data)
    assert any(f.feature == "logic.missing_question" and "QID999" in f.message for f in survey.check().findings)
    config = st.configure(bank=mini_bank, sample_size=2, seed=2, model=scripted_model())
    plan = st.plan(survey, config)
    assert any("does not have" in w for w in plan.warnings)
    results = st.run(plan, image_checker=accept_all_images)
    assert_no_stall(results)
    ids = {r["question_id"] for r in results.rows}
    assert "QID14" not in ids and "QID45" in ids
