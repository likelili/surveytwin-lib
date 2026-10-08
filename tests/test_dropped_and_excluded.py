"""A dropped or excluded question does not exist, the same way on both paths
(2026-09-29). Questions excluded when the survey is read (``exclude_questions``)
get what dropped blocks get: a skip rule that jumped to one goes on to the next
kept question, as for a question never shown; a screen-out that reads only
excluded questions is removed. The report finds dropped blocks by ID, since two
blocks can share a name, and choice display logic is never said to count as never
shown (it is not applied at all)."""

from __future__ import annotations

import json

import surveytwin as st
from fakes import accept_all_images, scripted_model
from flow_kit import answering, block, branch, end, mc, qsf, te, when  # noqa: F401


def run(survey, mini_bank, model=None, n=2):
    config = st.configure(bank=mini_bank, sample_size=n, seed=3, model=model or scripted_model())
    return st.run(st.plan(survey, config), image_checker=accept_all_images)


def asked(results, twin_id):
    return [r["question_id"] for r in results.rows if r["persona_id"] == twin_id]


def skipping_block(destination="QID3"):
    """One block: "Yes" on QID1 skips to ``destination``; then QID2, QID3, QID4."""
    data = qsf({"BL_A": ("A", ["QID1", "QID2", "QID3", "QID4"])},
               {"QID1": mc("QID1", "Skip ahead?", ["Yes", "No"]), "QID2": te("QID2", "Skipped?"),
                "QID3": te("QID3", "Destination?"), "QID4": te("QID4", "After?")},
               [block("BL_A")])
    payload = next(e for e in data["SurveyElements"] if e["Element"] == "BL")["Payload"]
    payload["1"]["BlockElements"][0]["SkipLogic"] = [{
        "SkipLogicID": 1, "ChoiceLocator": "q://QID1/SelectableChoice/1", "Condition": "Selected",
        "SkipToDestination": destination, "Locator": "q://QID1/SelectableChoice/1", "QuestionID": "QID1",
    }]
    return data


def test_a_skip_to_an_excluded_question_goes_on_to_the_next_kept_question(mini_bank):
    everything = run(st.read_qsf(skipping_block()), mini_bank)
    assert asked(everything, everything.twins[0]["id"]) == ["QID1", "QID3", "QID4"]  # Yes skips QID2
    survey = st.read_qsf(skipping_block(), exclude_questions=["QID3"])
    results = run(survey, mini_bank)
    for twin in results.twins:
        assert asked(results, twin["id"]) == ["QID1", "QID4"]  # was ["QID1"]: the rest of the block was lost
    assert survey.drop_report.moved_skips == ["SQ QID1 to QID4 (was QID3)"]
    assert "Excluded when read (1): QID3" in str(survey.drop_report)
    assert any(f.source == "SQ QID1" and "excluded when the survey was read; it goes on to QID4" in f.message for f in survey.check().findings)
    last = st.read_qsf(skipping_block("QID4"), exclude_questions=["QID4"])  # nothing kept after it
    assert last.drop_report.moved_skips == ["SQ QID1 to ENDOFBLOCK (was QID4)"]


def screener():
    blocks = {"BL_C": ("Screener", ["QID1"]), "BL_M": ("Main", ["QID2"])}
    questions = {"QID1": mc("QID1", "Are you eligible?", ["Yes", "No"]), "QID2": te("QID2", "Your view?")}
    flow = [block("BL_C"), branch(when("QID1", "SelectableChoice/1", "NotSelected"), end(), flow_id="FL_SCREEN"), block("BL_M")]
    return qsf(blocks, questions, flow)


def test_a_screen_out_that_reads_only_an_excluded_question_is_removed_as_for_a_dropped_block(mini_bank):
    dropped = st.read_qsf(screener()).drop_blocks(["BL_C"])
    excluded = st.read_qsf(screener(), exclude_questions=["QID1"])
    for survey in (dropped, excluded):
        assert survey.drop_report.pruned_branches == ["FL FL_SCREEN"]
        assert not any(e.get("ElementType") == "Branch" for e in survey.template["Elements"])
        results = run(survey, mini_bank)
        for twin in results.twins:
            assert asked(results, twin["id"]) == ["QID2"]  # was nothing: "not selected" ended every survey
    assert "Branches that read only dropped or excluded questions" in str(excluded.drop_report)
    assert any(f.source == "FL FL_SCREEN" and "excluded when the survey was read" in f.message for f in excluded.check().findings)


def test_a_branch_that_runs_a_block_stays_when_its_question_is_excluded(mini_bank):
    blocks = {"BL_C": ("Consent", ["QID1"]), "BL_M": ("Main", ["QID2"])}
    questions = {"QID1": mc("QID1", "Do you consent?", ["Yes", "No"]), "QID2": te("QID2", "Your view?")}
    flow = [block("BL_C"), branch(when("QID1", "SelectableChoice/1", "Selected"), block("BL_M"), flow_id="FL_GATE")]
    survey = st.read_qsf(qsf(blocks, questions, flow), exclude_questions=["QID1"])
    assert survey.drop_report.pruned_branches == []  # excluding a gate never deletes survey content


def two_attention_checks():
    blocks = {"BL_A": ("Attention Check", ["QID1"]), "BL_B": ("Attention Check", ["QID2", "QID3"]), "BL_M": ("Main", ["QID4"])}
    questions = {"QID1": mc("QID1", "Pick blue.", ["Blue", "Red"]), "QID2": mc("QID2", "Pick red.", ["Red", "Blue"]),
                 "QID3": te("QID3", "Comments?"), "QID4": te("QID4", "Your view?")}
    return qsf(blocks, questions, [block("BL_A"), block("BL_B"), block("BL_M")])


def test_the_report_finds_dropped_blocks_by_id_not_name(mini_bank, tmp_path):
    results = run(st.read_qsf(two_attention_checks()).drop_blocks(["BL_A"]), mini_bank)
    report = results.report()
    setting = report["setting"]
    assert "Attention Check" in setting["blocks"]  # the other block of that name was kept
    assert setting["excluded_blocks"] == ["Attention Check (BL_A)"] and setting["excluded_columns"] == 0
    assert {"QID2", "QID3"} <= {v.get("name") for v in report["distributions"]["variables"]}  # the kept block's answers
    files = results.write_exports(tmp_path / "out")
    html = files["report_html"].read_text(encoding="utf-8") if "report_html" in files else ""
    assert "kept in the CSVs" not in html
    both = run(st.read_qsf(two_attention_checks()).drop_blocks(["BL_A", "BL_B"]), mini_bank)
    manifest = json.loads((both.write_exports(tmp_path / "both")["manifest"]).read_text(encoding="utf-8"))
    assert manifest["config"]["qsf_block_exclusions"] == {"survey": ["BL_A", "BL_B"]}
    assert both.report()["setting"]["excluded_blocks"] == ["Attention Check (BL_A)", "Attention Check (BL_B)"]


def test_the_report_reads_every_exclusion_shape_runs_write():
    from surveytwin.results.report import excluded_block_ids_of, excluded_blocks_of

    assert excluded_block_ids_of({"qsf_block_exclusions": {"attachment-1": ["BL_A", "BL_B"]}}) == ["BL_A", "BL_B"]
    assert excluded_blocks_of({"qsf_block_exclusions": {"Consent": "BL_C"}}) == [("BL_C", "Consent")]  # an older library bundle
    # the website's validation-bundle driver: {block name: "ID1,ID2"}, one name for two blocks
    assert excluded_blocks_of({"qsf_block_exclusions": {"Attention Check": "BL_A1, BL_A2", "Consent": "BL_C"}}) == [
        ("BL_A1", "Attention Check"), ("BL_A2", "Attention Check"), ("BL_C", "Consent")]
    assert excluded_block_ids_of({}) == []


def test_choice_display_logic_reading_a_dropped_question_counts_it_as_never_shown(mini_bank):
    blocks = {"BL_A": ("A", ["QID1"]), "BL_B": ("B", ["QID2"])}
    questions = {"QID1": mc("QID1", "Fruit?", ["Apple", "Pear"]), "QID2": mc("QID2", "More?", ["More apples", "Something else"])}
    data = qsf(blocks, questions, [block("BL_A"), block("BL_B")])
    choices = next(e for e in data["SurveyElements"] if e.get("PrimaryAttribute") == "QID2")["Payload"]["Choices"]
    choices["1"]["DisplayLogic"] = when("QID1", "SelectableChoice/1", "Selected")
    survey = st.read_qsf(data).drop_blocks(["BL_A"])
    assert survey.drop_report.references == [] and [d.reads for d in survey.drop_report.choice_logic] == ["QID1"]
    config = st.configure(bank=mini_bank, sample_size=1, seed=3, model=scripted_model())
    plan = st.plan(survey, config)
    assert any(w.startswith("Display logic on 1 choice reads a dropped question; each counts as never shown") for w in plan.warnings)
    results = st.run(plan, image_checker=accept_all_images)
    # "Apple" was never selected, so "More apples" is not offered (the model takes the first option shown)
    assert [r["answer_label"] for r in results.rows if r["question_id"] == "QID2"] == ["Something else"]


# ---- review of the above (2026-09-29) --------------------------------------------------------------


def test_a_skip_is_moved_in_every_placement_of_a_block(mini_bank):
    data = skipping_block()
    blocks = next(e for e in data["SurveyElements"] if e["Element"] == "BL")["Payload"]
    blocks["2"] = {"Type": "Standard", "Description": "Group", "ID": "BL_G", "BlockElements": [{"Type": "Question", "QuestionID": "QID9"}]}
    data["SurveyElements"].append({"Element": "SQ", "PrimaryAttribute": "QID9", "Payload": mc("QID9", "Group?", ["GroupA", "GroupB"])})
    flow = next(e for e in data["SurveyElements"] if e["Element"] == "FL")["Payload"]
    flow["Flow"] = [block("BL_G"),
                    branch(when("QID9", "SelectableChoice/1", "Selected"), block("BL_A")),
                    branch(when("QID9", "SelectableChoice/2", "Selected"), block("BL_A"))]
    survey = st.read_qsf(data, exclude_questions=["QID3"])
    for group in ("GroupA", "GroupB"):
        results = run(survey, mini_bank, answering(lambda q, g=group: g if q["id"] == "QID9" else None), n=1)
        assert asked(results, results.twins[0]["id"]) == ["QID9", "QID1", "QID4"], group  # GroupB lost QID4
    assert not any("the rest of its block" in f.message for f in survey.check().findings)


def test_a_screen_out_reading_an_excluded_and_a_dropped_question_goes(mini_bank):
    blocks = {"BL_C": ("Consent", ["QID1"]), "BL_S": ("Eligible", ["QID5"]), "BL_M": ("Main", ["QID2"])}
    questions = {"QID1": mc("QID1", "Consent?", ["Yes", "No"]), "QID5": mc("QID5", "Eligible?", ["Yes", "No"]), "QID2": te("QID2", "Your view?")}
    either = when("QID1", "SelectableChoice/1", "NotSelected")
    either["0"]["1"] = dict(when("QID5", "SelectableChoice/1", "NotSelected")["0"]["0"], Conjuction="Or")
    flow = [block("BL_C"), block("BL_S"), branch(either, end(), flow_id="FL_SCREEN"), block("BL_M")]
    survey = st.read_qsf(qsf(blocks, questions, flow), exclude_questions=["QID1"]).drop_blocks(["BL_S"])
    assert survey.drop_report.pruned_branches == ["FL FL_SCREEN"]
    results = run(survey, mini_bank, n=1)
    assert asked(results, results.twins[0]["id"]) == ["QID2"]  # was nothing: the screen-out ended the survey


def test_exclude_questions_takes_internal_ids_or_one_questions_export_tag():
    data = screener()
    for element in data["SurveyElements"]:
        if element.get("PrimaryAttribute") == "QID1":
            element["Payload"]["DataExportTag"] = "eligible"
    assert st.read_qsf(data, exclude_questions=["eligible"]).exclude_questions == ["QID1"]
    import pytest

    with pytest.raises(st.SurveyError, match="no question 'QID77'"):
        st.read_qsf(data, exclude_questions=["QID77"])  # it stayed in the survey, reported as excluded


def test_questions_switched_off_with_if_false_are_not_answerable():
    blocks = {"BL_A": ("A", ["QID1"]), "BL_B": ("B", ["QID2"])}
    off = {"0": {"0": {"LogicType": "BooleanValue", "Value": "False", "Type": "Expression"}, "Type": "If"}, "Type": "BooleanExpression"}
    questions = {"QID1": te("QID1", "Your view?"), "QID2": te("QID2", "Parked question", display_logic=off)}
    import pytest

    with pytest.raises(st.DraftError) as err:
        st.read_qsf(qsf(blocks, questions, [block("BL_A"), block("BL_B")])).drop_blocks(["BL_A"])
    assert [i.code for i in err.value.issues] == ["survey.no_answerable"]
