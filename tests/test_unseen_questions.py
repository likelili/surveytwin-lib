"""The fourth review of the never-shown rule: a question shown
earlier stays shown when the flow passes another copy of its block, a loop row
is matched exactly, a staged run's post-stage display check sees earlier
answers, a blank answer is no number, and a screen-out is judged through groups."""

from __future__ import annotations

import pytest

import surveytwin as st
from fakes import accept_all_images
from flow_kit import LOOP_COFFEE_TEA, answering, block, branch, embedded, end, group, mc, qsf, randomizer, te, when, when_field


def run(data, model, mini_bank, n=4, drop=None):
    survey = st.read_qsf(data)
    if drop:
        survey = survey.drop_blocks(drop)
    config = st.configure(bank=mini_bank, sample_size=n, seed=2, model=model)
    return st.run(st.plan(survey, config), image_checker=accept_all_images)


def asked(results, twin_id):
    return [r["question_id"] for r in results.rows if r["persona_id"] == twin_id]


def smokes(question):
    return "Yes" if question["id"] == "QID1" else None


COUNTERBALANCED = {"BL_X": ("Smoking", ["QID1"]), "BL_Y": ("Ad", ["QID2"]), "BL_N": ("Never", ["QID3"]),
                   "BL_S": ("Smoker", ["QID4"]), "BL_Z": ("End", ["QID5"])}
QUESTIONS = {
    "QID1": mc("QID1", "Do you smoke?", ["Yes", "No"]),
    "QID2": te("QID2", "What did you think of the ad?"),
    "QID3": te("QID3", "Why have you never smoked?"),
    "QID4": te("QID4", "How many cigarettes a day?"),
    "QID5": te("QID5", "You said ${q://QID1/ChoiceGroup/SelectedChoices} to smoking. Any comments?"),
}


def counterbalanced(*after):
    """Two orders of the same two blocks, each in its own branch, then ``after``."""
    return [
        randomizer(1, embedded(order="1"), embedded(order="2")),
        branch(when_field("order", "1"), block("BL_X"), block("BL_Y")),
        branch(when_field("order", "2"), block("BL_Y"), block("BL_X")),
        *after,
    ]


def test_a_question_already_shown_waits_for_its_answer_past_another_copy_of_its_block(mini_bank):
    flow = counterbalanced(branch(when("QID1", "SelectableChoice/1", "NotSelected"), block("BL_N")),
                           branch(when("QID1", "SelectableChoice/1", "Selected"), block("BL_S")), block("BL_Z"))
    results = run(qsf(COUNTERBALANCED, QUESTIONS, flow), answering(smokes), mini_bank)
    for twin in results.twins:
        ids = asked(results, twin["id"])
        assert "QID3" not in ids and ids[-2:] == ["QID4", "QID5"], ids  # a smoker, in either order
    assert all("Yes to smoking" in r["question_text"] for r in results.rows if r["question_id"] == "QID5")


def test_a_screen_out_after_counterbalanced_blocks_reads_the_answer(mini_bank):
    flow = counterbalanced(branch(when("QID1", "SelectableChoice/1", "NotSelected"), end()), block("BL_S"))
    results = run(qsf(COUNTERBALANCED, QUESTIONS, flow), answering(smokes), mini_bank)
    for twin in results.twins:
        assert asked(results, twin["id"])[-1] == "QID4"  # smokers are not screened out


def test_an_arm_not_picked_does_not_undo_a_block_already_shown(mini_bank):
    flow = [block("BL_X"), randomizer(1, block("BL_X"), block("BL_Y")), branch(when("QID1", "SelectableChoice/1", "NotSelected"), end()), block("BL_S")]
    results = run(qsf(COUNTERBALANCED, QUESTIONS, flow), answering(smokes), mini_bank)
    for twin in results.twins:
        assert asked(results, twin["id"])[-1] == "QID4"


def test_a_loop_row_shown_in_the_picked_arm_is_piped(mini_bank):
    blocks = {"BL_L": ("Drinks", ["QID43"], LOOP_COFFEE_TEA), "BL_P": ("Follow-up", ["QID50"])}
    questions = {"QID43": mc("QID43", "How often do you drink ${lm://Field/1}?", ["Daily", "Weekly", "Never"]),
                 "QID50": te("QID50", "You drink coffee ${q://1_QID43/ChoiceGroup/SelectedChoices}. Why?")}
    flow = [randomizer(1, group(embedded(frame="health"), block("BL_L")), group(embedded(frame="taste"), block("BL_L"))), block("BL_P")]
    results = run(qsf(blocks, questions, flow), answering(lambda q: "Weekly" if "QID43" in q["id"] else None), mini_bank, n=2)
    assert {r["question_text"] for r in results.rows if r["question_id"] == "QID50"} == {"You drink coffee Weekly. Why?"}


def test_an_answer_shown_in_a_later_stage_is_kept(mini_bank):
    """The post-stage display check used only that stage's answers: "Have you paid
    one?" was asked and answered after "heard = Yes", then blanked as never shown,
    and "How much?" never came. The website does the same (2026-09-28)."""
    blocks = {"BL_F": ("Fee", ["QID1", "QID2", "QID3"])}
    questions = {
        "QID1": mc("QID1", "Have you heard of a resort fee?", ["Yes", "No"]),
        "QID2": mc("QID2", "Have you ever paid one?", ["Yes", "No"], display_logic=when("QID1", "SelectableChoice/1", "Selected")),
        "QID3": te("QID3", "How much did you pay?", display_logic=when("QID2", "SelectableChoice/1", "Selected")),
    }
    results = run(qsf(blocks, questions, [block("BL_F")]), answering(lambda q: "Yes" if q.get("options") else "20 dollars"), mini_bank, n=2)
    for twin in results.twins:
        rows = {r["question_id"]: r for r in results.rows if r["persona_id"] == twin["id"]}
        assert rows["QID2"]["answer_label"] == "Yes" and not rows["QID2"].get("not_shown")
        assert rows["QID3"]["answer_label"] == "20 dollars"


def test_a_blank_answer_is_no_number(mini_bank):
    blocks = {"BL_A": ("Age", ["QID1", "QID2"]), "BL_B": ("Rest", ["QID3"])}
    questions = {"QID1": te("QID1", "How old are you?"), "QID2": te("QID2", "Where do you live?"), "QID3": te("QID3", "Anything else?")}
    flow = [block("BL_A"), branch(when("QID1", "ChoiceTextEntryValue", "LessThan", "18"), end()), block("BL_B")]
    results = run(qsf(blocks, questions, flow), answering(lambda q: "" if q["id"] == "QID1" else None), mini_bank, n=2)
    for twin in results.twins:
        assert asked(results, twin["id"]) == ["QID1", "QID2", "QID3"]  # a blank age is not "less than 18"


@pytest.mark.parametrize("body, drop", [
    (lambda: [group(end())], ["BL_C"]),
    (lambda: [group(embedded(screened="1"), end())], ["BL_C"]),
    (lambda: [group(block("BL_BYE")), end()], ["BL_C", "BL_BYE"]),
    (lambda: [randomizer(1, block("BL_BYE")), end()], ["BL_C", "BL_BYE"]),
])
def test_a_screen_out_is_judged_through_groups_and_randomizers(body, drop):
    blocks = {"BL_C": ("Consent", ["QID1"]), "BL_BYE": ("Goodbye", ["QID9"]), "BL_M": ("Main", ["QID2"])}
    questions = {"QID1": mc("QID1", "Do you consent?", ["Yes", "No"]), "QID9": te("QID9", "Thank you anyway."), "QID2": te("QID2", "Your view?")}
    flow = [block("BL_C"), branch(when("QID1", "SelectableChoice/1", "NotSelected"), *body(), flow_id="FL_SCREEN"), block("BL_M")]
    survey = st.read_qsf(qsf(blocks, questions, flow)).drop_blocks(drop)
    assert survey.drop_report.pruned_branches == ["FL FL_SCREEN"]
    assert "read only dropped questions and run no block" in str(survey.drop_report)


# ---- fifth review -----------------------------------------------------------------


def test_a_staged_answer_is_kept_whatever_logic_showed_it(mini_bank):
    """A staged run no longer checks display logic again after each stage: the runtime
    decided it. The check's own evaluator read "(Q1 is not No) or (arm is treatment)"
    as an "and" and blanked the answer."""
    either = {"0": {"0": when("QID1", "SelectableChoice/2", "NotSelected")["0"]["0"], "Type": "If"},
              "1": {"0": when_field("arm", "treatment")["0"]["0"], "Type": "ElseIf"}, "Type": "BooleanExpression"}
    blocks = {"BL_A": ("A", ["QID1", "QID2"])}
    questions = {"QID1": mc("QID1", "Did you like it?", ["Yes", "No"]), "QID2": te("QID2", "Tell us more.", display_logic=either)}
    results = run(qsf(blocks, questions, [embedded(arm="treatment"), block("BL_A")]), answering(lambda q: "No" if q["id"] == "QID1" else "It was long"),
                  mini_bank, n=2)
    for twin in results.twins:
        rows = {r["question_id"]: r for r in results.rows if r["persona_id"] == twin["id"]}
        assert rows["QID2"]["answer_label"] == "It was long" and not rows["QID2"].get("not_shown")


def test_a_loop_row_left_out_is_read_without_a_warning(mini_bank):
    loop = {"Looping": "Static", "LoopingOptions": {"Static": {"1": {"1": "Coffee"}, "2": {"1": "Tea"}, "3": {"1": "Juice"}},
                                                   "Randomization": "Subset", "TotalRandSubset": 1}}
    blocks = {"BL_L": ("Drinks", ["QID5"], loop), "BL_P": ("After", ["QID6"])}
    questions = {"QID5": mc("QID5", "Do you drink ${lm://Field/1}?", ["Yes", "No"]),
                 "QID6": te("QID6", "No juice? [${q://3_QID5/ChoiceGroup/SelectedChoices}]", display_logic=when("3_QID5", "SelectableChoice/1", "NotSelected"))}
    results = run(qsf(blocks, questions, [block("BL_L"), block("BL_P")]), answering(lambda q: None), mini_bank, n=4)
    assert not [w for w in results.warnings if "Could not resolve" in w or "stalled" in w]
    left_out = [t["id"] for t in results.twins if "3_QID5" not in asked(results, t["id"])]
    assert left_out and all("QID6" in asked(results, t) for t in left_out)  # row 3 never shown: "not selected" is true


# ---- a question not reached yet ---------------------------------------------------


def test_logic_and_embedded_data_that_read_a_later_question_do_not_wait(mini_bank):
    """In Qualtrics a question not reached yet has no answer. The runtime used to
    wait for one: the twin's survey stopped there (no answers at all when that
    came first)."""
    blocks = {"BL_A": ("A", ["QID1"]), "BL_B": ("B", ["QID2"]), "BL_C": ("C", ["QID3"])}
    questions = {
        "QID1": te("QID1", "Early: [${e://Field/copied}]"),
        "QID2": mc("QID2", "Do you smoke?", ["Yes", "No"]),
        "QID3": te("QID3", "Anything else?"),
    }
    flow = [embedded(copied="${q://QID2/ChoiceGroup/SelectedChoices}"),
            branch(when("QID2", "SelectableChoice/1", "Selected"), end()),  # reads QID2 before it is asked: not taken
            block("BL_A"), block("BL_B"), block("BL_C")]
    results = run(qsf(blocks, questions, flow), answering(smokes_all), mini_bank, n=2)
    assert not [w for w in results.warnings if "stalled" in w]
    for twin in results.twins:
        rows = {r["question_id"]: r for r in results.rows if r["persona_id"] == twin["id"]}
        assert list(rows) == ["QID1", "QID2", "QID3"]
        assert rows["QID1"]["question_text"] == "Early: []"  # QID2 not answered yet when the field was set
        assert rows["QID3"]["embedded_data"].get("copied") == ""


def smokes_all(question):
    return "Yes" if question.get("options") else None


def test_a_decision_before_a_blocks_later_copy_stays_as_made():
    """[A: driver?] [if driver: B: own a car?] [C: which car? shown if own a car] [B again] [D]:
    a non-driver never sees C's question, although they answer B's copy later (the
    fifth review's known gap, closed: a later pass decides C the same way)."""
    blocks = {"BL_A": ("A", ["QID0"]), "BL_B": ("B", ["QID1"]), "BL_C": ("C", ["QID2"]), "BL_D": ("D", ["QID3"])}
    questions = {"QID0": mc("QID0", "Are you a driver?", ["Yes", "No"]), "QID1": mc("QID1", "Do you own a car?", ["Yes", "No"]),
                 "QID2": te("QID2", "Which car?", display_logic=when("QID1", "SelectableChoice/1", "Selected")), "QID3": te("QID3", "Last?")}
    flow = [block("BL_A"), branch(when("QID0", "SelectableChoice/1", "Selected"), block("BL_B")), block("BL_C"), block("BL_B"), block("BL_D")]
    from surveytwin.engine.definition import Definition
    from surveytwin.engine.staged import StagedRuntime

    survey = st.read_qsf(qsf(blocks, questions, flow))
    runtime = StagedRuntime(Definition(survey.standard), seed=1, persona_count=1)
    state = runtime.new_state(persona_id="p1", persona_index=0)
    asked = []
    while not state.done:
        stage = runtime.render_next_stage(state)
        if not stage.questions:
            break
        asked.append([q["id"] for q in stage.questions])
        runtime.apply_answers(state, questions=stage.questions, answers=[
            {"question_id": q["id"], **dict.fromkeys(("answer_value", "answer_label", "answer_raw"), "No" if q["id"] == "QID0" else "Yes")} for q in stage.questions])
    assert asked == [["QID0"], ["QID1", "QID3"]]
    assert not [w for w in state.warnings if "stalled" in w]


# ---- Loop & Merge: the plain question ID inside a loop is the current row (review of c89fbf8) ----


def _template_run(template, answer):
    from surveytwin.engine.definition import Definition
    from surveytwin.engine.staged import StagedRuntime
    from surveytwin.spec.read_template import survey_from_template

    runtime = StagedRuntime(Definition(survey_from_template(template)), seed=42, persona_count=1)
    state = runtime.new_state(persona_id="p1", persona_index=0)
    stages, texts = [], {}
    while not state.done:
        stage = runtime.render_next_stage(state)
        if not stage.questions:
            break
        stages.append([q["id"] for q in stage.questions])
        texts.update({q["id"]: q["text"] for q in stage.questions})
        runtime.apply_answers(state, questions=stage.questions, answers=[
            {"question_id": q["id"], **dict.fromkeys(("answer_value", "answer_label", "answer_raw"), answer(q["id"]))} for q in stage.questions])
    return stages, texts, state


def _t_block(block_id, questions, rows=0):
    element = {"ElementType": "Block", "BlockID": block_id, "BlockName": block_id, "Questions": questions}
    if rows:
        names = ["coffee", "tea", "juice"]
        element["Options"] = {"Looping": "Static", "Randomization": "None",
                              "LoopingOptions": {"Static": {str(i + 1): {"1": names[i]} for i in range(rows)}, "Randomization": "None"}}
    return element


def _t_choice(qid, text, options=("Yes", "No"), selector="SAVR", **extra):
    return {"QuestionID": qid, "QuestionText": text, "QuestionType": "MC", "Options": list(options),
            "OptionsID": [str(i + 1) for i in range(len(options))], "Settings": {"Selector": selector}, **extra}


def _t_text(qid, text, **extra):
    return {"QuestionID": qid, "QuestionText": text, "QuestionType": "TE", "Settings": {"Selector": "SL"}, **extra}


def _t_when(qid, locator, operator, loops=None):
    condition = {"LogicType": "Question", "QuestionID": qid, "Operator": operator, "LeftOperand": f"q://{qid}/{locator}", "Type": "Expression"}
    if loops:
        condition.update({"QuestionIsInLoop": "yes", "LoopAndMergeLoops": loops,
                          "LoopTarget": "none" if loops == "current" else "evaluateAllInArrayFunction"})
    return {"0": {"0": condition, "Type": "If"}, "Type": "BooleanExpression"}


def test_a_follow_up_inside_a_loop_reads_the_current_rows_answer():
    """Inside a loop, Qualtrics names the current row's answer by the plain question ID.
    Since entry 80 the runtime took it for a question never shown (the follow-up was
    never asked, its pipe was empty); before, it stalled."""
    skip = [{"ChoiceLocator": "q://QID5/SelectableChoice/2", "Condition": "Selected", "SkipToDestination": "ENDOFBLOCK"}]
    template = {"Elements": [
        _t_block("BL_L", [_t_choice("QID5", "Do you drink ${lm://Field/1}?", SkipLogic=skip),
                          _t_text("QID6", "How many cups of ${lm://Field/1} a day?", DisplayLogic=_t_when("QID5", "SelectableChoice/1", "Selected", "current")),
                          _t_text("QID7", "You said [${q://QID5/ChoiceGroup/SelectedChoices}] to ${lm://Field/1}. Why?")], rows=2),
        _t_block("BL_E", [_t_text("QID9", "Anything else?")])]}
    stages, texts, state = _template_run(template, lambda qid: {"1_QID5": "Yes", "2_QID5": "No"}.get(qid, "Two"))
    assert not state.warnings
    assert stages == [["1_QID5"], ["1_QID6", "1_QID7", "2_QID5"], ["QID9"]]  # a tea "No" skips the rest of its row
    assert texts["1_QID7"] == "You said [Yes] to coffee. Why?"


def test_an_in_all_loops_condition_holds_only_if_it_holds_in_every_row():
    done = lambda: _t_when("QID59", "SelectableChoice/1", "NotSelected", "BL_L,all")
    template = {"Elements": [
        _t_block("BL_L", [_t_text("QID2", "A word about ${lm://Field/1}?", DisplayLogic=done()),
                          _t_choice("QID59", "Or tick this if you are done:", ("I'm done", "Keep going"), DisplayLogic=done())], rows=3),
        _t_block("BL_E", [_t_text("QID9", "Anything else?")])]}
    stages, _texts, state = _template_run(template, lambda qid: "I'm done" if qid == "1_QID59" else "word")
    assert not state.warnings and stages == [["1_QID2", "1_QID59"], ["QID9"]]
    stages, _texts, state = _template_run(template, lambda qid: "Keep going" if qid.endswith("QID59") else "word")
    assert stages == [["1_QID2", "1_QID59"], ["2_QID2", "2_QID59"], ["3_QID2", "3_QID59", "QID9"]]


def test_logic_on_a_looped_multi_select_row_reads_its_choices():
    multi = _t_choice("QID5", "What do you add to ${lm://Field/1}?", ("Milk", "Sugar"), selector="MAVR")
    template = {"Elements": [_t_block("BL_L", [multi], rows=2),
                             _t_block("BL_E", [_t_text("QID9", "Milk in coffee: how much?", DisplayLogic=_t_when("1_QID5", "SelectableChoice/1", "Selected"))])]}
    stages, _texts, state = _template_run(template, lambda qid: "Yes" if qid == "1_QID5_1" else "No")
    assert stages[-1][-1] == "QID9" and not state.warnings


# ---- second review: skip destinations, never-asked questions, side-by-side, piped values ----


def test_a_skip_to_a_question_inside_a_loop_row_lands_in_the_same_row():
    skip = [{"ChoiceLocator": "q://QID5/SelectableChoice/2", "Condition": "Selected", "SkipToDestination": "QID7", "QuestionID": "QID5"}]
    template = {"Elements": [
        _t_block("BL_L", [_t_choice("QID5", "Do you drink ${lm://Field/1}?", SkipLogic=skip),
                          _t_text("QID6", "How many cups of ${lm://Field/1}?"), _t_text("QID7", "Anything about ${lm://Field/1}?")], rows=2),
        _t_block("BL_E", [_t_text("QID9", "Anything else?")])]}
    stages, _texts, state = _template_run(template, lambda qid: {"1_QID5": "No", "2_QID5": "Yes"}.get(qid, "ok"))
    assert not state.warnings
    assert stages == [["1_QID5"], ["1_QID7", "2_QID5"], ["2_QID6", "2_QID7", "QID9"]]


def test_logic_on_a_question_that_is_never_asked_does_not_wait():
    timing = {"QuestionID": "QID29", "QuestionText": "Timing", "QuestionType": "Timing", "is_timing": True, "Settings": {"Selector": "PageTimer"}}
    fast = _t_when("QID29", "ChoiceNumericEntryValue/3", "LessThan", "current")
    fast["0"]["0"]["RightOperand"] = "5"
    looped = {"Elements": [_t_block("BL_L", [timing, _t_text("QID22", "Why ${lm://Field/1}?"), _t_text("QID23", "Faster than 5 seconds?", DisplayLogic=fast)], rows=2),
                           _t_block("BL_E", [_t_text("QID9", "Anything else?")])]}
    stages, _texts, state = _template_run(looped, lambda qid: "ok")
    assert not [w for w in state.warnings if "stalled" in w]
    assert [q for stage in stages for q in stage] == ["1_QID22", "2_QID22", "QID9"]


def test_a_side_by_side_question_in_a_loop_is_answered_once_per_row():
    sbs = {"QuestionID": "QID35", "QuestionText": "Rate ${lm://Field/1}", "QuestionType": "SBS", "sub_questions": [
        {"id": "1", "text": "Taste", "QuestionID": "QID35#1", "question_type": "Matrix", "selector": "Likert", "subselector": "SingleAnswer",
         "Options": ["Bad", "Good"], "Rows": ["Hot", "Cold"]}]}
    stages, _texts, state = _template_run({"Elements": [_t_block("BL_L", [sbs], rows=2)]}, lambda qid: "Good")
    asked = [q for stage in stages for q in stage]
    assert len(asked) == len(set(asked)) == 4 and {qid.split("_", 1)[0] for qid in asked} == {"1", "2"} and not state.warnings


def test_a_value_that_pipes_an_answer_waits_for_it():
    code_is_answer = {"0": {"0": {"LogicType": "EmbeddedField", "LeftOperand": "code", "Operator": "EqualTo",
                                  "RightOperand": "${q://QID1/ChoiceTextEntryValue}", "Type": "Expression"}, "Type": "If"}, "Type": "BooleanExpression"}
    template = {"Elements": [
        {"ElementType": "EmbeddedData", "FlowID": "FL_ed", "EmbeddedData": [{"Field": "code", "Value": "abc"}]},
        _t_block("BL_A", [_t_text("QID1", "Your code?"), _t_text("QID4", "Same code again?", DisplayLogic=code_is_answer)]),
        {"ElementType": "Branch", "FlowID": "FL_b", "BranchLogic": code_is_answer, "Elements": [_t_block("BL_B", [_t_text("QID2", "Code accepted.")])]},
        _t_block("BL_C", [_t_text("QID3", "Last?")])]}
    stages, _texts, state = _template_run(template, lambda qid: "abc")
    assert stages == [["QID1"], ["QID4", "QID2", "QID3"]] and not state.warnings


def test_piped_question_text_inside_a_loop_is_the_rows_text():
    template = {"Elements": [_t_block("BL_L", [_t_choice("QID5", "Do you drink ${lm://Field/1}?"),
                                               _t_text("QID6", "You were asked: ${q://QID5/QuestionText} (choice 1: ${q://QID5/ChoiceDescription/1})")], rows=2)]}
    _stages, texts, _state = _template_run(template, lambda qid: "Yes")
    assert texts["1_QID6"] == "You were asked: Do you drink coffee? (choice 1: Yes)"
    assert texts["2_QID6"] == "You were asked: Do you drink tea? (choice 1: Yes)"
