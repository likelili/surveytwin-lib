"""Regression tests for the independent review of the survey builder (2026-09-28).
Each test names the finding it pins down."""

from __future__ import annotations

import asyncio
import copy
import json

import pytest

import surveytwin as st
from conftest import qsf
from fakes import scripted_model
from qsf_writer import write_qsf
from surveytwin import builder
from surveytwin.builder.edit import _logic_references

STORE = {"img_7f3a": "data:image/png;base64," + "iVBORw0KGgo=" * 40}


def resolver(ref):
    return STORE.get(ref)


def built_with_image():
    return builder.build_survey({"title": "T", "questions": [
        {"id": "Q1", "type": "instruction", "text": "Look at this ad.", "image_id": "img_7f3a", "image_name": "ad.png"},
        {"id": "Q2", "type": "single_choice", "text": "Like it?", "options": ["Yes", "No"]},
    ]}, image_resolver=resolver)


# ---- images ---------------------------------------------------------------------------------


def test_c1_a_text_edit_keeps_a_resolved_image():
    s = built_with_image()
    s2 = builder.update_question(s, "Q1", text="Look at this advertisement.")
    assert s2.questions["QID1"].media == s.questions["QID1"].media and s2.unresolved_media == []
    assert s2.check().ok
    s3 = builder.update_question(s2, "Q1", image_name="poster.png")
    assert "title" not in s3.questions["QID1"].media[0]  # the file name is not sent to the twin
    assert builder.to_draft(s3).questions[0].image_name == "poster.png"
    s4 = builder.update_question(s3, "Q1", image_id="img_other")  # a new image is resolved again
    assert s4.questions["QID1"].media == [] and s4.unresolved_media[0]["reference"] == "img_other"


def test_c22_embedded_images_are_reported_as_sent():
    images = built_with_image().check().images
    assert [(i["status"], i["detail"]) for i in images] == [("supported", "embedded image")]


def test_c27_an_embedded_image_is_stored_once(mini_bank, tmp_path):
    s = built_with_image()
    results = st.run(st.plan(s, st.configure(bank=mini_bank, sample_size=3, model=scripted_model())))
    out = results.save(tmp_path / "run")
    text = (out / "results.json").read_text(encoding="utf-8")
    assert text.count(STORE["img_7f3a"]) == 1
    assert list(results.run["media"].values()) == [STORE["img_7f3a"]]


# ---- column names ---------------------------------------------------------------------------


def test_c5_column_clashes_are_found_both_ways():
    s = builder.build_survey({"title": "T", "questions": [{"id": "Q3_1", "type": "open_text", "text": "a"}]})
    with pytest.raises(st.DraftError, match="Q3_1"):  # the new question's column is an existing ID
        builder.add_questions(s, {"id": "Q3", "type": "multi_select", "text": "b", "options": ["x", "y"]})
    with pytest.raises(st.DraftError):
        builder.build_survey({"title": "T", "questions": [
            {"id": "Q3_1", "type": "open_text", "text": "a"},
            {"id": "Q3", "type": "multi_select", "text": "b", "options": ["x", "y"]},
        ]})


def test_c41_imported_columns_of_every_kind_count():
    k = st.read_qsf(qsf("TEST_RankOrder.qsf"))
    rank = next(q for q in k.questions.values() if q.raw.get("QuestionType") == "Rank")
    with pytest.raises(st.DraftError, match="appear twice"):
        builder.add_questions(k, {"id": f"{rank.export_tag}_1", "type": "open_text", "text": "x"}, block_id=rank.block_id)


# ---- edits on imported questions ------------------------------------------------------------------


def test_c8_text_limits_update_the_validation_type():
    s = st.read_qsf(write_qsf("T", [{"id": "Q1", "type": "open_text", "text": "Why?", "max_chars": 50}]))
    s2 = builder.update_question(s, "Q1", min_chars=10)
    settings = s2.questions["QID1"].raw["Settings"]
    assert (settings["MinChars"], settings["TotalChars"], settings["ValidationType"]) == ("10", "50", "CharRange")
    data = write_qsf("T", [{"id": "Q1", "type": "open_text", "text": "Your email?"}])
    data["SurveyElements"][2]["Payload"]["Validation"]["Settings"].update({"Type": "ContentType", "ContentType": "ValidEmail"})
    with pytest.raises(st.UnsupportedEdit, match="format"):
        builder.update_question(st.read_qsf(data), "Q1", max_chars=20)


def test_c9_more_features_block_option_changes():
    k = st.read_qsf(qsf("kitchen_sink_supported.qsf"))
    with pytest.raises(st.UnsupportedEdit, match="default answers"):
        builder.update_question(k, "QID7", options=["a", "b", "c"])
    data = write_qsf("T", [{"id": "Q1", "type": "single_choice", "text": "Pick", "options": ["a", "b", "c"]}])
    data["SurveyElements"][2]["Payload"]["Randomization"] = {"Advanced": {"FixedOrder": ["1", "2", "3"]}, "Type": "Advanced", "TotalRandSubset": ""}
    with pytest.raises(st.UnsupportedEdit, match="custom choice-randomization order"):
        builder.update_question(st.read_qsf(data), "Q1", options=["a", "b", "c", "d"])
    with pytest.raises(st.DraftError, match="random 3"):
        builder.update_question(k, "QID10", options=["a", "b"])


def test_c10_slider_step_follows_a_new_range():
    data = write_qsf("T", [{"id": "Q1", "type": "number", "text": "How many?", "min_value": 0, "max_value": 100}])
    data["SurveyElements"][2]["Payload"]["Configuration"]["SnapToGrid"] = True
    s = st.read_qsf(data)
    assert s.questions["QID1"].raw["Settings"]["NumericStep"] == 10
    s2 = builder.update_question(s, "Q1", max_value=50)
    assert s2.questions["QID1"].raw["Settings"]["NumericStep"] == 5
    with pytest.raises(st.DraftError, match="larger than the whole range"):
        builder.update_question(s, "Q1", step=200)


def test_c11_options_are_checked_against_selection_limits():
    s = st.read_qsf(write_qsf("T", [{"id": "Q1", "type": "multi_select", "text": "Pick", "options": ["a", "b", "c", "d"], "min_selections": 3}]))
    with pytest.raises(st.DraftError, match="selection limits"):
        builder.update_question(s, "Q1", options=["a", "b"])


def test_c13_piping_is_matched_exactly():
    template = {"Elements": [{"ElementType": "Block", "BlockID": "B", "Questions": [
        {"QuestionID": "QID1", "QuestionName": "Q1", "QuestionText": "a"},
        {"QuestionID": "QID2", "QuestionName": "Q2", "QuestionText": "${q://QID12/ChoiceGroup/SelectedChoices}"},
    ]}]}
    assert _logic_references(template, "QID1") == []


def test_c14_timing_questions_are_not_answerable():
    template = {"Metadata": {"SurveyName": "T"}, "Elements": [{"ElementType": "Block", "BlockID": "B", "BlockName": "B", "Questions": [
        {"QuestionID": "QID1", "QuestionName": "Q1", "QuestionText": "Pick", "QuestionType": "MC", "Options": ["a", "b"], "Settings": {"Selector": "SAVR"}},
        {"QuestionID": "QID2", "QuestionName": "Q2", "QuestionText": "", "QuestionType": "Timing", "is_timing": True, "Settings": {"Selector": "PageTimer"}},
    ]}]}
    with pytest.raises(st.DraftError, match="no answerable questions"):
        builder.remove_questions(st.Survey(template, kind="qsf"), "Q1")


def test_c15_a_built_question_needs_an_id():
    s = builder.build_survey({"title": "T", "questions": [{"id": "Q1", "type": "open_text", "text": "a"}]})
    with pytest.raises(st.DraftError, match="cannot be blank"):
        builder.update_question(s, "Q1", id="")


def test_c16_side_by_side_questions_are_not_renamed():
    s = st.read_qsf(qsf("TEST_SBS.qsf"))
    sbs = next(q for q in s.questions.values() if q.type == "SBS")
    with pytest.raises(st.UnsupportedEdit, match="side-by-side"):
        builder.update_question(s, sbs.id, id="NEW")


def test_c17_new_text_may_pipe_only_earlier_questions():
    s = builder.build_survey({"title": "T", "questions": [
        {"id": "Q1", "type": "single_choice", "text": "Pick", "options": ["a", "b"]},
        {"id": "Q2", "type": "open_text", "text": "Why?"},
    ]})
    ok = builder.update_question(s, "Q2", text="Why ${q://QID1/ChoiceGroup/SelectedChoices}?")
    assert ok.check().mode == "staged"
    with pytest.raises(st.DraftError, match="comes later"):
        builder.update_question(s, "Q1", text="Pick, given ${q://QID2/ChoiceTextEntryValue}")
    with pytest.raises(st.DraftError, match="not in this survey"):
        builder.update_question(s, "Q2", text="Why ${q://QID99/ChoiceGroup/SelectedChoices}?")


def test_c18_any_imported_question_can_change_its_image():
    k = st.read_qsf(qsf("kitchen_sink_supported.qsf"))
    rank = next(q for q in k.questions.values() if q.raw.get("QuestionType") == "Rank")
    k2 = builder.update_question(k, rank.id, image_id="https://img.example/shelf.png")
    assert k2.questions[rank.id].media[0]["src"] == "https://img.example/shelf.png"
    # the file name is not written into the question: the twin would read it
    k3 = builder.update_question(k, rank.id, image_id="https://img.example/shelf.png", image_name="cheap_version.png")
    assert k3.questions[rank.id].media == [{"kind": "image", "src": "https://img.example/shelf.png"}]
    k4 = builder.update_question(k2, rank.id, image_name="cheap_version.png")
    assert k4.questions[rank.id].media == k2.questions[rank.id].media
    missing = builder.update_question(k, rank.id, image_id="img_gone", image_name="cheap_version.png", image_resolver=lambda _ref: None)
    assert missing.unresolved_media[-1]["name"] == "cheap_version.png"  # it still names an image that did not resolve


def test_c19_c21_excluded_questions_do_not_get_in_the_way():
    s = st.read_qsf(qsf("TEST_MC.qsf"), exclude_questions=["QID4"])
    block = next(iter(s.questions.values())).block_id
    order = [q.id for q in s.questions.values() if q.block_id == block]
    reordered = builder.reorder_questions(s, list(reversed(order)))
    assert [q.id for q in reordered.questions.values() if q.block_id == block] == list(reversed(order))


# ---- drafts and the command line -----------------------------------------------------------


def test_c4_lenient_values_like_the_website():
    q = builder.DraftQuestion.from_dict({"type": "multi_select", "text": "x", "options": ["a", "b"], "required": "t", "min_selections": True})
    assert q.required is True and q.min_selections == 1
    assert builder.DraftQuestion.from_dict({"type": "open_text", "required": 0.0}).required is False


def test_c40_malformed_payloads_are_issues():
    assert [i.code for i in builder.validate_draft({"questions": "Q1"})] == ["draft.invalid"]
    assert [(i.code, i.question_index) for i in builder.validate_draft({"questions": [{"type": "open_text", "text": "a"}, 7]})] == [("question.invalid", 1)]
    with pytest.raises(st.DraftError):
        builder.build_survey(["not", "a", "draft"])


def test_c23_to_c26_command_line(tmp_path, capsys, mini_bank):
    from surveytwin.cli import main

    draft = {"title": "T", "colour": "red", "questions": [{"type": "open_text", "text": "Why?"}]}
    path = tmp_path / "draft.json"
    path.write_bytes(b"\xef\xbb\xbf" + json.dumps(draft).encode())  # a byte-order mark
    assert main(["check", str(path)]) == 0
    assert "Unknown field 'colour' is ignored" in capsys.readouterr().err
    assert main(["batch", "submit", "--out", str(tmp_path / "b")]) == 2
    assert "Give the survey" in capsys.readouterr().err


# ---- AI drafting ------------------------------------------------------------------------------


def run(coro):
    return asyncio.run(coro)


# ---- second review (2026-09-28) -------------------------------------------------------------


def test_r2_new_text_may_not_pipe_a_dropped_question():
    k = st.read_qsf(qsf("kitchen_sink_supported.qsf")).drop_blocks(["BL_KS_MC_1"])
    with pytest.raises(st.DraftError, match="was dropped"):
        builder.update_question(k, "QID14", text="You picked ${q://QID56/ChoiceGroup/SelectedChoices}. Why?")
    with pytest.raises(st.DraftError, match="was dropped"):
        builder.add_questions(k, {"type": "open_text", "text": "Re ${q://QID56/ChoiceGroup/SelectedChoices}"}, block_id="BL_KS_TE_4")


def test_r4_r9_removal_and_dropped_blocks():
    k = st.read_qsf(qsf("kitchen_sink_supported.qsf")).drop_blocks(["BL_KS_FB_14"])  # its QID48 pipes QID56
    with pytest.raises(st.UnsupportedEdit) as info:
        builder.remove_questions(k, "QID56")
    assert "QID48" not in str(info.value)  # a dropped question reads nothing
    with pytest.raises(st.SurveyError, match="dropped"):
        builder.remove_questions(k, "QID48")


def test_r6_r17_the_same_image_id_keeps_the_image():
    s = built_with_image()
    s2 = builder.update_question(s, "Q1", {**builder.to_draft(s).questions[0].to_dict(), "text": "Look again."})
    assert s2.questions["QID1"].media == s.questions["QID1"].media and s2.check().ok


def test_r7_objects_are_not_turned_into_text():
    issues = builder.validate_draft({"questions": [{"type": "single_choice", "text": {"en": "Pick"}, "options": ["a", {"x": 1}]}]})
    assert {(i.code, i.field) for i in issues if i.severity == "error"} >= {("field.invalid", "text"), ("field.invalid", "options")}


def test_r8_piping_in_options_is_checked():
    s = builder.build_survey({"title": "T", "questions": [
        {"id": "Q1", "type": "single_choice", "text": "Pick", "options": ["a", "b"]},
        {"id": "Q2", "type": "single_choice", "text": "Then", "options": ["x", "y"]},
    ]})
    with pytest.raises(st.DraftError, match="comes later"):
        builder.update_question(s, "Q1", options=["a", "b ${q://QID2/ChoiceGroup/SelectedChoices}"])


def test_r10_r18_stale_limits_stay_off():
    data = write_qsf("T", [{"id": "Q1", "type": "open_text", "text": "Why?", "max_chars": 15}])
    settings = data["SurveyElements"][2]["Payload"]["Validation"]["Settings"]
    settings.update({"Type": "TotalChar", "MinChars": "1"})  # a stale minimum, as real exports have
    s2 = builder.update_question(st.read_qsf(data), "Q1", max_chars=30)
    out = s2.questions["QID1"].raw["Settings"]
    assert out["ValidationType"] == "TotalChar" and out["TotalChars"] == "30"


def test_r14_export_column_names_are_reserved():
    issues = builder.validate_draft({"questions": [{"id": "TWIN_ID", "type": "open_text", "text": "a"}, {"id": "demo_age", "type": "open_text", "text": "b"}]})
    assert [i.code for i in issues] == ["id.reserved", "id.reserved"]


def test_r21_only_images_are_compacted():
    from surveytwin.run import _compact_media

    table = {}
    text = "data: " + "x" * 300
    assert _compact_media({"QuestionText": text}, table) == {"QuestionText": text} and table == {}


@pytest.mark.parametrize("spelling", [["--assume", "QID1=Yes"], ["--assume=QID1=Yes"], ["--assu", "QID1=Yes"]])
def test_r22_an_old_batch_with_assume_can_be_collected(tmp_path, monkeypatch, capsys, mini_bank, spelling):
    from surveytwin.cli import main

    out = tmp_path / "run"
    out.mkdir()
    argv = ["batch", "submit", str(qsf("TEST_MC.qsf")), "--bank", str(mini_bank), "--model", "gpt-4o-mini", *spelling, "--out", str(out)]
    (out / "batch_state.json").write_text(json.dumps({"batch_id": "batch_1", "argv": argv}), encoding="utf-8")
    seen = {}
    monkeypatch.setattr(st.batch, "collect", lambda batch_id, plan, **kw: seen.setdefault("plan", plan) and (_ for _ in ()).throw(st.BatchNotAvailable("stop here")))
    assert main(["batch", "collect", "--out", str(out)]) == 2  # got as far as collecting
    assert "plan" in seen and "--assume" in capsys.readouterr().err


# ---- third review (2026-09-28) ----------------------------------------------------------------

KS = json.loads(qsf("kitchen_sink_supported.qsf").read_text(encoding="utf-8"))


def ks_payload(data, qid):
    return next(e for e in data["SurveyElements"] if e.get("PrimaryAttribute") == qid)["Payload"]


def test_t7_a_dropped_questions_tag_names_nothing_once_another_question_has_it():
    s = st.read_qsf(qsf("kitchen_sink_supported.qsf"))
    s = builder.update_question(s, "QID56", id="Q1").drop_blocks(["BL_KS_MC_1"])  # a dropped question tagged Q1
    s2 = builder.add_questions(s, {"type": "open_text", "text": "Anything else?"}, block_id="BL_KS_TE_4")
    new_tag = s2.edits[-1]["questions"][0]["id"]
    assert new_tag != "Q1"  # blank IDs never take a dropped question's tag
    s3 = builder.update_question(s2, new_tag, text="Anything else to add?")
    assert s3.questions[s2.edits[-1]["questions"][0]["qid"]].text == "Anything else to add?"
    s4 = builder.add_questions(s, {"id": "KS_MC_Q2", "type": "open_text", "text": "Reuses a dropped tag."}, block_id="BL_KS_TE_4")
    s5 = builder.update_question(s4, "KS_MC_Q2", text="Still found by its tag.")  # QID1 (dropped) also had it
    assert builder.remove_questions(s5, "KS_MC_Q2")


def test_t9_objects_in_text_fields_are_errors_everywhere():
    s = st.read_qsf(qsf("kitchen_sink_supported.qsf"))
    for field in ("text", "id", "image_id", "image_name"):
        with pytest.raises(st.DraftError) as err:
            builder.update_question(s, "QID45", **{field: {"x": 1}})
        assert err.value.issues[0].code == "field.invalid" and err.value.issues[0].field == field
    b = builder.build_survey({"title": "T", "questions": [{"id": "Q1", "type": "open_text", "text": "Why?"}]})
    with pytest.raises(st.DraftError) as err:
        builder.update_question(b, "Q1", id={"x": 1})
    assert err.value.issues[0].field == "id"
    with pytest.raises(st.DraftError):
        builder.add_questions(b, {"id": ["Q9"], "type": "open_text", "text": "More?"})
    with pytest.raises(st.DraftError):
        builder.build_survey({"title": "T", "questions": [{"id": {"x": 1}, "type": "open_text", "text": "Why?"}]})
    assert [q.id for q in builder.assign_ids({"questions": [{"id": {"x": 1}, "type": "open_text"}]}).questions] == [{"x": 1}]


def test_t10_piping_already_there_does_not_block_other_edits():
    data = copy.deepcopy(KS)
    ks_payload(data, "QID45")["QuestionText"] = "You chose ${q://QID56/ChoiceGroup/SelectedChoices}. Which animal image did you see earlier?"
    s = st.read_qsf(data).drop_blocks(["BL_KS_MC_1"])
    s2 = builder.update_question(s, "QID45", options=["Kitten", "Puppy", "Neither", "Both"])  # text unchanged
    assert s2.questions["QID45"].options == ["Kitten", "Puppy", "Neither", "Both"]
    with pytest.raises(st.DraftError) as err:  # a new pipe of a dropped question is still refused
        builder.update_question(s, "QID44", text="You chose ${q://QID9/ChoiceGroup/SelectedChoices}. One more?")
    assert err.value.issues[0].code == "text.piping_dropped"
    with pytest.raises(st.DraftError) as err:  # also through a Loop & Merge pipe
        builder.update_question(s.drop_blocks(["BL_KS_FB_12"]), "QID44", text="You had ${q://1_QID43/ChoiceGroup/SelectedChoices}. One more?")
    assert err.value.issues[0].code == "text.piping_dropped"


def test_t13_logic_inside_a_dropped_block_does_not_limit_edits():
    data = copy.deepcopy(KS)
    ks_payload(data, "QID48")["DisplayLogic"] = {"0": {"0": {"LogicType": "Question", "QuestionID": "QID45", "QuestionIsInLoop": "no",
                                                            "ChoiceLocator": "q://QID45/SelectableChoice/1", "Operator": "Selected",
                                                            "QuestionIDFromLocator": "QID45", "LeftOperand": "q://QID45/SelectableChoice/1",
                                                            "Type": "Expression"}, "Type": "If"}, "Type": "BooleanExpression", "inPage": False}
    s = st.read_qsf(data)
    with pytest.raises(st.UnsupportedEdit, match="display logic of KS_RD_Q32") as err:
        builder.update_question(s, "QID45", options=["Cat", "Dog", "Neither", "Both", "Other"])
    assert "skip logic" not in str(err.value)  # QID44's skip logic only jumps to QID45; it reads none of its options
    s2 = builder.update_question(s.drop_blocks(["BL_KS_FB_14"]), "QID45", options=["Cat", "Dog", "Neither", "Both", "Other"])
    assert len(s2.questions["QID45"].options) == 5
    keep = "BL_KS_TE_4"
    only = s.drop_blocks([b["block_id"] for b in s.blocks if b["block_id"] != keep])
    added = builder.add_questions(only, {"type": "open_text", "text": "Anything else?"})  # the one block left
    assert added.edits[-1]["block_id"] == keep


# ---- fourth review (2026-09-28) ---------------------------------------------------------------


def test_f8_an_image_reference_of_0_is_text_on_every_path():
    s = st.read_qsf(qsf("kitchen_sink_supported.qsf"))
    resolve = lambda ref: f"https://example.org/img/{ref}.png"  # noqa: E731
    qsf_image = builder.update_question(s, "QID37", image_resolver=resolve, image_id=0).questions["QID37"].media
    b = builder.build_survey({"title": "T", "questions": [{"id": "Q1", "type": "single_choice", "text": "Pick", "options": ["a", "b"], "image_id": "cat"}]},
                             image_resolver=resolve)
    built_image = builder.update_question(b, "Q1", image_resolver=resolve, image_id=0).questions["QID1"].media
    assert [m["src"] for m in qsf_image] == [m["src"] for m in built_image] == ["https://example.org/img/0.png"]


def test_f9_logic_reading_a_missing_question_gives_the_right_reason():
    excluded = st.read_qsf(qsf("kitchen_sink_supported.qsf"), exclude_questions=["QID56"]).check()
    assert any(f.feature == "logic.missing_question" and "excluded when the survey was read" in f.message for f in excluded.findings)
    built = builder.build_survey({"title": "T", "questions": [{"id": "Q1", "type": "open_text", "text": "Why?"}]})
    built = builder.update_question(built, "Q1", text="Why?")  # a clean survey reports nothing
    assert not [f for f in built.check().findings if f.feature == "logic.missing_question"]


def test_f11_a_tag_with_a_stray_space_is_found_by_its_name():
    data = copy.deepcopy(KS)
    ks_payload(data, "QID45")["DataExportTag"] = "KS_QO_Q36\xa0"
    s = st.read_qsf(data)
    assert builder.update_question(s, "KS_QO_Q36", text="Which image did you see?").questions["QID45"].text == "Which image did you see?"
