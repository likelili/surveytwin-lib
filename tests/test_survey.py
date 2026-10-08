"""Reading a QSF, the pre-run check, and dropping blocks."""

from __future__ import annotations

import copy
import json

import pytest

import surveytwin as st
from conftest import qsf


def test_read_from_path_bytes_and_dict_agree():
    path = qsf("TEST_MC.qsf")
    raw = path.read_bytes()
    a = st.read_qsf(path)
    b = st.read_qsf(raw)
    c = st.read_qsf(json.loads(raw))
    assert a.template == b.template == c.template
    assert a.checksum == b.checksum
    assert len(a.questions) == 6


@pytest.mark.parametrize(
    "content, message",
    [
        (b"not json", "JSON"),
        (json.dumps({"SurveyEntry": {}}).encode(), "SurveyElements"),
        (json.dumps({"SurveyEntry": {}, "SurveyElements": [{"Element": "SQ", "Payload": {}}]}).encode(), "flow"),
    ],
)
def test_read_rejects_what_is_not_a_qsf(content, message):
    with pytest.raises(st.QSFReadError) as info:
        st.read_qsf(content)
    assert message.lower() in str(info.value).lower()


def test_static_survey_check():
    check = st.read_qsf(qsf("TEST_MC.qsf")).check()
    assert check.mode == "static"
    assert check.staging_reasons == []
    assert check.ok


def test_staged_survey_check_names_reasons_and_features():
    check = st.read_qsf(qsf("kitchen_sink_supported.qsf")).check()
    assert check.mode == "staged"
    assert "skip logic" in check.staging_reasons
    features = {f.feature for f in check.findings}
    assert "execution.staged" in features
    assert "piped_text.question" in features
    assert "other" not in features
    levels = {f.feature: f.level for f in check.findings}
    assert levels["question_option.skip_logic"] == "Partial"


def test_offline_check_does_not_call_images_unreachable():
    check = st.read_qsf(qsf("kitchen_sink_supported.qsf")).check()
    assert check.images and all(i["status"] == "not_checked" for i in check.images)
    assert check.ok
    assert "links not tested" in str(check)


def test_image_checker_result_is_used():
    survey = st.read_qsf(qsf("kitchen_sink_supported.qsf"))
    check = survey.check(url_checker=lambda url: {"status": "auth_required", "detail": "HTTP 403"})
    assert {i["status"] for i in check.images} == {"auth_required"}
    assert not check.ok


def test_preview_blocks_counts_and_flags():
    blocks = {b.block_id: b for b in st.read_qsf(qsf("kitchen_sink_supported.qsf")).preview_blocks()}
    assert blocks["BL_KS_MC_1"].answerable == 11
    assert blocks["BL_KS_FB_13"].screenout
    assert any("QID56" in r for r in blocks["BL_KS_MC_1"].referenced_by)
    assert blocks["BL_KS_TG_10"].images


def test_drop_unknown_block_is_an_error():
    with pytest.raises(st.SurveyError):
        st.read_qsf(qsf("TEST_MC.qsf")).drop_blocks(["BL_NOPE"])


def test_a_dropped_question_no_longer_exists(mini_bank, tmp_path):
    """Dropped means gone: nothing asks it, no output lists it, and logic that
    reads it sees a question that was never shown (no answer)."""
    from fakes import accept_all_images, scripted_model

    full = st.read_qsf(qsf("kitchen_sink_supported.qsf"))
    survey = full.drop_blocks(["BL_KS_MC_1"])
    dropped = set(survey.drop_report.dropped_questions)
    assert "QID56" in dropped
    assert {d.reads for d in survey.drop_report.references} == {"QID56"}
    check = survey.check()
    assert check.ok and any("reads QID56" in r for r in check.reads_dropped)
    assert "counts as never shown" in str(survey.drop_report)
    assert not dropped & set(survey.questions)

    config = st.configure(bank=mini_bank, sample_size=3, seed=2, model=scripted_model())
    plan = st.plan(survey, config)  # no refusal: the dropped question simply does not exist
    results = st.run(plan, image_checker=accept_all_images)
    asked = {row["question_id"].split("_")[0] for row in results.rows}
    assert not asked & dropped
    # The branches that read QID56 ("is Cat selected") see no answer, so their blocks are not shown.
    branch_blocks = {"BL_KS_FB_14", "BL_5mvGQoBt7GcmAFo", "BL_8jFpZfwsRRTsiOi"}
    branch_qids = {q.id for q in full.questions.values() if q.block_id in branch_blocks}
    assert not asked & branch_qids
    out = results.save(tmp_path / "run")
    import csv

    imports = list(csv.reader((out / "responses_labels.csv").open(encoding="utf-8")))[2]
    import re

    columns = {re.split(r"[_#]", json.loads(c)["ImportId"])[0] for c in imports}
    assert not columns & dropped
    logic = json.loads((out / "question_logic.json").read_text(encoding="utf-8"))
    from surveytwin.builder.edit import _questions_by_qid

    assert not dropped & set(_questions_by_qid(logic))  # (branch conditions may still name them)
    assert "KS_MC_Q1" not in (out / "report.json").read_text(encoding="utf-8")
    assert "assumed_answers" not in results.provenance


def test_there_is_no_assumed_answer():
    with pytest.raises(TypeError):
        st.read_qsf(qsf("kitchen_sink_supported.qsf")).drop_blocks(["BL_KS_MC_1"], assume={"QID56": "Cat"})


def test_preview_marks_dropped_blocks_and_the_report_follows_edits():
    from surveytwin import builder

    survey = st.read_qsf(qsf("kitchen_sink_supported.qsf")).drop_blocks(["BL_KS_MC_1"])
    flags = {b.block_id: b.dropped for b in survey.preview_blocks()}
    assert flags["BL_KS_MC_1"] is True and flags["BL_KS_TE_4"] is False
    assert "DROPPED" in str(survey.preview_blocks())
    # the report is worked out from the survey as it is now, so an edit never leaves it stale
    edited = builder.update_question(survey, "QID48", text="A question that no longer pipes anything.")
    assert "piping at SQ QID48 reads QID56" in {f"{d.kind} at {d.location} reads {d.reads}" for d in survey.drop_report.references}
    assert "piping at SQ QID48 reads QID56" not in {f"{d.kind} at {d.location} reads {d.reads}" for d in edited.drop_report.references}


def test_excluded_questions_are_left_out_when_reading():
    survey = st.read_qsf(qsf("TEST_MC.qsf"), exclude_questions=["QID4"])
    assert "QID4" not in json_text(survey._base_template)


def json_text(value):
    import json

    return json.dumps(value)


def test_a_qsf_dict_is_not_a_template():
    raw = json.loads(qsf("TEST_MC.qsf").read_text(encoding="utf-8"))
    with pytest.raises(st.SurveyError, match="read_qsf"):
        st.Survey(raw)


def test_unset_embedded_data_field_is_reported():
    data = json.loads(qsf("TEST_MC.qsf").read_text(encoding="utf-8"))
    data = copy.deepcopy(data)
    for element in data["SurveyElements"]:
        if element.get("Element") == "SQ":
            element["Payload"]["QuestionText"] = "You are in group ${e://Field/Condition}. " + element["Payload"].get("QuestionText", "")
            break
    check = st.read_qsf(data).check()
    assert check.unset_embedded_fields == ["Condition"]
    assert any(f.feature == "piped_text.embedded_data" for f in check.findings)


def test_question_views():
    survey = st.read_qsf(qsf("TEST_MC.qsf"))
    question = next(iter(survey.questions.values()))
    assert question.id and question.type == "MC"
    assert question.options
    assert question.kind == "answerable"


def test_block_preview_prints_as_a_table():
    blocks = st.read_qsf(qsf("kitchen_sink_supported.qsf")).preview_blocks()
    assert isinstance(blocks, list) and len(blocks) == 12
    text = str(blocks)
    assert text.startswith("12 blocks, in flow order:")
    assert "BL_KS_FB_13" in text and "screen-out" in text
    assert "read by the rest of the survey: branch at FL FL_14 reads QID56" in text


def test_drop_report_prints_what_still_depends_on_the_dropped_block():
    text = str(st.read_qsf(qsf("kitchen_sink_supported.qsf")).drop_blocks(["BL_KS_MC_1"]).drop_report)
    assert "Dropped blocks: BL_KS_MC_1" in text
    assert "branch at FL FL_14 reads QID56" in text
