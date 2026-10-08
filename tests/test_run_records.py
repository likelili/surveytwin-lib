"""What a run keeps about each twin, and what a saved run gives back (review of
c89fbf8): the options a twin was shown as asked, batch-collected runs recorded
like real-time ones, reruns that find matrix, multi-select and loop answers,
where each embedded field comes from, and saved surveys checked as a whole."""

from __future__ import annotations

import json

import pytest

import surveytwin as st
from fakes import accept_all_images
from flow_kit import LOOP_COFFEE_TEA, answering, block, embedded, mc, qsf, te


def _run(survey, mini_bank, *, mode="auto", rule=lambda q: None, n=2):
    config = st.configure(bank=mini_bank, sample_size=n, seed=5, model=answering(rule))
    return st.run(st.plan(survey, config, mode=mode), image_checker=accept_all_images)


def test_a_staged_twins_shown_options_are_the_ones_it_was_asked_with(mini_bank):
    """A block shown again in a later stage fills pipes of its later questions with
    answers given since; the record kept the later rendering instead of what the
    twin was asked."""
    blocks = {"BL_A": ("A", ["QID1", "QID2", "QID3"])}
    questions = {
        "QID1": mc("QID1", "Where do you work?", ["Where I live now [${q://QID2/ChoiceTextEntryValue}]", "Somewhere else"]),
        "QID2": te("QID2", "Where do you live?"),
        "QID3": te("QID3", "Why ${q://QID2/ChoiceTextEntryValue}?"),
    }
    results = _run(st.read_qsf(qsf(blocks, questions, [block("BL_A")])), mini_bank, mode="staged",
                   rule=lambda q: "Nothing I recognise" if q["id"] == "QID1" else "Paris")
    for record in results.audit:
        assert record["shown"]["QID1"]["options"] == ["Where I live now []", "Somewhere else"]


def test_a_batch_collected_run_keeps_each_twins_values_and_what_it_was_shown(mini_bank, monkeypatch):
    blocks = {"BL_A": ("A", ["QID2"])}
    questions = {"QID2": mc("QID2", "Price ${e://Field/price}?", ["Buy at ${e://Field/price}", "Skip"])}
    survey = st.read_qsf(qsf(blocks, questions, [embedded(price="${rand://int/10:99}"), block("BL_A")]))
    plan = st.plan(survey, st.configure(bank=mini_bank, sample_size=2, seed=5, model="gpt-4o-mini", batch=True))
    reply = json.dumps({"answers": [{"question_id": "QID2", "answer_value": "?", "answer_label": "?", "answer_raw": "?"}]})

    class FinishedBatch:
        on_usage = None

        async def poll_chat_batch_once(self, batch_id):
            return {"status": "completed"}

        async def collect_chat_batch_results(self, batch_id, **kwargs):
            return {str(index): reply for index in range(len(plan.twins))}

        async def close(self):
            pass

    monkeypatch.setattr(st.batch, "_client", lambda api_key: FinishedBatch())
    results = st.batch.collect("batch_1", plan)
    assert results.flagged()  # "?" is no option, so a rerun would re-ask it
    for record in results.audit:
        price = record["assignments"][0]["value"]
        assert record["assignments"] == [{"source": "flow", "flow_id": record["assignments"][0]["flow_id"], "field": "price", "value": price}]
        assert record["shown"]["QID2"]["options"] == [f"Buy at {price}", "Skip"]


def test_reruns_keep_what_matrix_multi_select_and_loop_answers_were_shown(mini_bank):
    """The record was kept only under question IDs, never under answer IDs (a loop
    row 1_QID5, a multi-select option QID4_1), so those answers were re-asked with
    the template's ${...} tokens."""
    loop = {**LOOP_COFFEE_TEA}
    blocks = {"BL_A": ("A", ["QID4"]), "BL_L": ("Drinks", ["QID6", "QID5"], loop)}
    multi = mc("QID4", "Which do you like, ${e://Field/name}?", ["Tea with ${e://Field/name}", "Coffee"])
    multi["Selector"] = "MAVR"
    about = {"QuestionID": "QID6", "DataExportTag": "QID6", "QuestionType": "DB", "Selector": "TB", "QuestionText": "About ${lm://Field/1}:"}
    questions = {"QID4": multi, "QID6": about, "QID5": te("QID5", "How often do you drink it?")}
    survey = st.read_qsf(qsf(blocks, questions, [embedded(name="Ana"), block("BL_A"), block("BL_L")]))
    results = _run(survey, mini_bank, mode="static", n=1)
    shown = results.audit[0]["shown"]
    assert any(key.startswith("QID4_") for key in shown), sorted(shown)
    assert {"1_QID5", "2_QID5"} <= set(shown), sorted(shown)
    assert all("${" not in json.dumps(value) for value in shown.values())


def test_embedded_fields_come_from_scripts_that_run_not_from_comments_or_placeholders():
    data = qsf({"BL_A": ("A", ["QID1"])}, {"QID1": te("QID1", "Hello ${e://Field/panel} ${e://Field/from_header} ${e://Field/commented}")}, [block("BL_A")])
    payload = next(e for e in data["SurveyElements"] if e["PrimaryAttribute"] == "QID1")["Payload"]
    payload["QuestionJS"] = "Qualtrics.SurveyEngine.addOnload(function(){\n/* Qualtrics.SurveyEngine.setEmbeddedData('commented', 1); */\n// setEmbeddedData('commented', 2)\nvar u = 'https://x.org';\n});"
    data["SurveyElements"].append({"Element": "SO", "PrimaryAttribute": "Survey Options",
                                   "Payload": {"Header": "<script>Qualtrics.SurveyEngine.setEmbeddedData('from_header', 'x');</script>"}})
    flow = next(e for e in data["SurveyElements"] if e["Element"] == "FL")["Payload"]["Flow"]
    flow.insert(0, {"Type": "EmbeddedData", "FlowID": "FL_9", "EmbeddedData": [
        {"Description": "Create New Field or Choose From Dropdown...", "Type": "Recipient", "Field": "Create New Field or Choose From Dropdown..."},
        {"Description": "panel", "Type": "Recipient", "Field": "panel"}]})
    fields = {f.name: f for f in st.read_qsf(data).embedded_fields}
    assert fields["from_header"].source == "set by JavaScript" and fields["from_header"].set_by_javascript == ["survey header"]
    assert fields["commented"].source == "never set"
    assert fields["panel"].source == "from a panel or the survey link"
    assert "Create New Field or Choose From Dropdown..." not in fields


def test_a_saved_survey_is_checked_as_a_whole(tmp_path):
    from conftest import qsf as fixture

    survey = st.read_qsf(fixture("kitchen_sink_supported.qsf"))
    saved = survey.to_dict()
    changed_text = json.loads(json.dumps(saved))
    first = next(q for b in changed_text["template"]["Elements"] if b.get("Questions") for q in b["Questions"])
    first["QuestionText"] = "A different question"
    with pytest.raises(st.SurveyError, match="definition"):
        st.Survey.from_dict(changed_text)  # an unedited QSF's checksum covers only the file
    dropped = json.loads(json.dumps(saved))
    dropped["exclude_blocks"] = ["BL_KS_MC_1"]
    with pytest.raises(st.SurveyError, match="definition"):
        st.Survey.from_dict(dropped)
    assert st.Survey.from_dict(json.loads(json.dumps(saved))).checksum == survey.checksum


def test_a_saved_runs_survey_is_its_own_and_a_damaged_one_does_not_stop_loading(mini_bank, tmp_path):
    survey_a = st.read_qsf(qsf({"BL_A": ("A", ["QID1"])}, {"QID1": te("QID1", "First survey?")}, [block("BL_A")]))
    survey_b = st.read_qsf(qsf({"BL_A": ("A", ["QID1"])}, {"QID1": te("QID1", "Second survey?")}, [block("BL_A")]))
    folder = tmp_path / "run"
    _run(survey_a, mini_bank).save(folder)
    run_b = _run(survey_b, mini_bank)
    run_b._survey = None  # a run without its survey, as an old folder loads
    run_b.save(folder)
    assert not (folder / "survey.json").exists()  # survey A's file is not run B's
    assert st.Results.load(folder).rows

    run_b2 = _run(survey_b, mini_bank)
    run_b2.save(folder)
    (folder / "survey.json").write_text(json.dumps(survey_a.to_dict()), encoding="utf-8")
    loaded = st.Results.load(folder, verify=False)
    with pytest.raises(st.ResultsError, match="not the survey this run asked"):
        loaded.survey
    (folder / "survey.json").write_text("{ damaged", encoding="utf-8")
    loaded = st.Results.load(folder, verify=False)
    assert loaded.rows  # the rows still load
    with pytest.raises(st.ResultsError, match="cannot be read"):
        loaded.survey


def test_a_head_run_records_the_plans_full_size(mini_bank):
    survey = st.read_qsf(qsf({"BL_A": ("A", ["QID1"])}, {"QID1": te("QID1", "Hi?")}, [block("BL_A")]))
    plan = st.plan(survey, st.configure(bank=mini_bank, sample_size=4, seed=5, model=answering(lambda q: None))).head(2)
    results = st.run(plan, image_checker=accept_all_images)
    assert results.run["population"] == 4 and len(results.run["twin_ids"]) == 2
