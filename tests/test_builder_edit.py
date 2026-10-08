"""Editing surveys: add, update, remove, reorder. Each returns a new survey; the one
passed in and any plan made from it never change; IDs are never renumbered; a
survey read from a QSF keeps everything the edit does not name."""

from __future__ import annotations

import copy
import json

import pytest

import surveytwin as st
from conftest import qsf
from fakes import scripted_model
from surveytwin import builder
from surveytwin.builder.edit import _questions_by_qid


def base():
    return builder.build_survey({"title": "T", "questions": [
        {"type": "single_choice", "text": "A?", "options": ["x", "y"]},
        {"type": "open_text", "text": "B?"},
        {"type": "grid", "text": "C", "rows": ["r1", "r2"], "options": ["lo", "hi"]},
    ]})


def tags(survey):
    return [q.export_tag for q in survey.questions.values()]


def pairs(survey):
    return [(q.id, q.export_tag) for q in survey.questions.values()]


def kitchen():
    return st.read_qsf(qsf("kitchen_sink_supported.qsf"))


def without(template, qid):
    """The template with one question taken out of its block (for comparing the rest)."""
    out = copy.deepcopy(template)

    def walk(elements):
        for e in elements:
            if e.get("ElementType") == "Block":
                e["Questions"] = [q for q in e["Questions"] if q.get("QuestionID") != qid]
            walk(e.get("Elements") or [])

    walk(out["Elements"])
    return out


# ---- add ------------------------------------------------------------------------------------


def test_add_at_positions_with_new_ids():
    s = base()
    s2 = builder.add_questions(s, [{"type": "number", "text": "N?"}, {"id": "Z9", "type": "open_text", "text": "Z?"}], after_question_id="Q1")
    assert pairs(s2) == [("QID1", "Q1"), ("QID4", "Q4"), ("QID5", "Z9"), ("QID2", "Q2"), ("QID3", "Q3")]
    s3 = builder.add_questions(s2, {"type": "open_text", "text": "first"}, before_question_id="Q1")
    assert tags(s3)[:2] == ["Q5", "Q1"]
    s4 = builder.add_questions(s3, {"type": "open_text", "text": "last"})
    assert tags(s4)[-1] == "Q6" and list(s4.questions)[-1] == "QID7"
    assert s4.edits[0]["op"] == "add_questions" and len(s4.edits) == 3


def test_added_ids_never_collide():
    s = base()
    s2 = builder.remove_questions(s, "Q3")
    s3 = builder.add_questions(s2, {"type": "open_text", "text": "new"})
    assert pairs(s3)[-1] == ("QID4", "Q3")  # tag Q3 is free again, but QID3 is not reused
    with pytest.raises(st.DraftError) as info:
        builder.add_questions(s, {"id": "Q2", "type": "open_text", "text": "dup"})
    assert info.value.issues[0].code == "id.duplicate"
    with pytest.raises(st.DraftError):
        builder.add_questions(s, {"id": "Q3_1", "type": "open_text", "text": "clash"})  # grid Q3 has column Q3_1


def test_add_validates_each_question_with_its_position():
    with pytest.raises(st.DraftError) as info:
        builder.add_questions(base(), [{"type": "open_text", "text": "ok"}, {"type": "single_choice", "text": "bad", "options": ["one"]}])
    assert [(i.question_index, i.field) for i in info.value.issues if i.severity == "error"] == [(1, "options")]


def test_add_needs_a_block_when_there_are_several():
    with pytest.raises(st.SurveyError, match="pass block_id"):
        builder.add_questions(kitchen(), {"type": "open_text", "text": "x"})
    with pytest.raises(st.SurveyError, match="Unknown block"):
        builder.add_questions(kitchen(), {"type": "open_text", "text": "x"}, block_id="BL_NOPE")


def test_add_to_a_qsf_survey_keeps_everything_else():
    k = kitchen()
    k2 = builder.add_questions(k, {"type": "open_text", "text": "Anything else?"}, block_id="BL_KS_TE_4")
    new_qid = [q for q in k2.questions if q not in k.questions][0]
    assert new_qid == "QID69"  # after every QID in the QSF, not only those in the flow
    assert without(k2._base_template, new_qid) == k._base_template
    assert k2.check().mode == k.check().mode == "staged"
    assert k2.qsf == k.qsf and k2.checksum != k.checksum and k2.edited and not k.edited


# ---- update ---------------------------------------------------------------------------------


def test_update_a_built_question():
    s = base()
    s2 = builder.update_question(s, "Q2", text="B, changed?", required=True, id="B")
    q = s2.questions["QID2"]
    assert (q.text, q.export_tag, q.raw["Settings"]["ForceResponse"]) == ("B, changed?", "B", "ON")
    assert s2.edits[-1]["renamed_from"] == "Q2"
    s3 = builder.update_question(s2, "B", {"type": "single_choice", "options": ["yes", "no"]})
    assert s3.questions["QID2"].type == "MC"
    assert builder.to_draft(s3).questions[1].type == "single_choice"


def test_update_refuses_bad_values_with_the_field():
    with pytest.raises(st.DraftError) as info:
        builder.update_question(base(), "Q1", options=["only"])
    assert info.value.issues[0].field == "options"
    with pytest.raises(st.DraftError, match="Unknown field"):
        builder.update_question(base(), "Q1", colour="red")
    with pytest.raises(st.DraftError, match="No question"):
        builder.update_question(base(), "Q9", text="x")


def test_update_a_qsf_question_only_where_nothing_is_lost():
    k = kitchen()
    k2 = builder.update_question(k, "QID56", text="Which pet do you like most?", required=True)
    assert k2.questions["QID56"].text == "Which pet do you like most?"
    assert without(k2._base_template, "QID56") == without(k._base_template, "QID56")
    # relabeling in place keeps the positions logic relies on
    labels = k.questions["QID56"].options
    k3 = builder.update_question(k, "QID56", options=[label.upper() for label in labels])
    assert k3.questions["QID56"].options == [label.upper() for label in labels]
    # changing how many options there are would break the branches that read them
    with pytest.raises(st.UnsupportedEdit, match="branch at FL FL_14"):
        builder.update_question(k, "QID56", options=labels[:2])
    with pytest.raises(st.UnsupportedEdit, match="changing its type"):
        builder.update_question(k, "QID56", type="open_text")


def test_update_refuses_to_lose_qualtrics_features():
    k = kitchen()
    recoded = next(q for q in k.questions.values() if q.raw.get("RecodeValues") and q.type == "MC")
    with pytest.raises(st.UnsupportedEdit, match="recode values"):
        builder.update_question(k, recoded.id, options=["a", "b", "c", "d", "e"])
    write_in = next(q for q in k.questions.values() if q.raw.get("TextEntryOptions"))
    with pytest.raises(st.UnsupportedEdit, match="write-in options"):
        builder.update_question(k, write_in.id, options=["a", "b"])


def test_an_internal_id_names_its_own_question_even_when_a_tag_repeats_it(tmp_path):
    data = json.loads(qsf("TEST_MC.qsf").read_text(encoding="utf-8"))
    for element in data["SurveyElements"]:
        if element.get("Element") == "SQ" and element["Payload"]["QuestionID"] == "QID4":
            element["Payload"]["DataExportTag"] = "QID6"  # an export tag that is another question's ID
    s = st.read_qsf(data)
    edited = builder.update_question(s, "QID6", text="x")  # the internal ID wins (it used to be refused as ambiguous)
    assert edited.questions["QID6"].text == "x" and edited.questions["QID4"].text == s.questions["QID4"].text
    assert builder.update_question(s, "QID4", text="y").questions["QID4"].text == "y"


# ---- remove -----------------------------------------------------------------------------------


def test_remove_keeps_other_ids_and_refuses_an_empty_survey():
    s = base()
    s2 = builder.remove_questions(s, ["Q1", "Q3"])
    assert pairs(s2) == [("QID2", "Q2")]
    with pytest.raises(st.DraftError, match="no answerable questions"):
        builder.remove_questions(s2, "Q2")


def test_remove_refuses_while_logic_reads_the_question():
    with pytest.raises(st.UnsupportedEdit) as info:
        builder.remove_questions(kitchen(), "QID56")
    message = str(info.value)
    assert "branch at FL FL_14 reads" in message and "piping at SQ QID48" in message


def test_remove_refuses_a_skip_destination():
    s = st.read_qsf(qsf("TEST_FlowBehavior.qsf"))
    with pytest.raises(st.UnsupportedEdit, match="skip logic of .* jumps to"):
        builder.remove_questions(s, "QID9")


def test_remove_a_whole_block_by_its_questions():
    k = kitchen()
    qids = [q.id for q in k.questions.values() if q.block_id == "BL_KS_SL_7"]
    k2 = builder.remove_questions(k, qids)
    assert "BL_KS_SL_7" not in {b.block_id for b in k2.preview_blocks()}
    assert k2.edits[-1]["blocks_removed"] == ["BL_KS_SL_7"]


# ---- reorder ----------------------------------------------------------------------------------


def test_reorder_by_full_order_swap_and_move():
    s = builder.add_questions(base(), [{"id": "Q4", "type": "open_text", "text": "D"}, {"id": "Q5", "type": "open_text", "text": "E"}])
    assert tags(builder.reorder_questions(s, ["Q5", "Q4", "Q3", "Q2", "Q1"])) == ["Q5", "Q4", "Q3", "Q2", "Q1"]
    assert tags(builder.reorder_questions(s, swap=("Q1", "Q5"))) == ["Q5", "Q2", "Q3", "Q4", "Q1"]
    assert tags(builder.reorder_questions(s, swap=(["Q1", "Q2"], ["Q4", "Q5"]))) == ["Q4", "Q5", "Q3", "Q1", "Q2"]
    assert tags(builder.reorder_questions(s, swap=(["Q4", "Q5"], "Q1"))) == ["Q4", "Q5", "Q2", "Q3", "Q1"]
    assert tags(builder.reorder_questions(s, move=("Q3", "up"))) == ["Q1", "Q3", "Q2", "Q4", "Q5"]
    assert tags(builder.reorder_questions(s, move=("Q3", 1))) == ["Q1", "Q2", "Q4", "Q3", "Q5"]
    moved = builder.reorder_questions(s, ["Q5", "Q4", "Q3", "Q2", "Q1"])
    assert sorted(pairs(moved)) == sorted(pairs(s))  # nothing renumbered


def test_reorder_mistakes():
    s = builder.add_questions(base(), [{"id": "Q4", "type": "open_text", "text": "D"}])
    with pytest.raises(st.SurveyError, match="not consecutive"):
        builder.reorder_questions(s, swap=(["Q1", "Q3"], "Q4"))
    with pytest.raises(st.SurveyError, match="overlap"):
        builder.reorder_questions(s, swap=(["Q1", "Q2"], ["Q2", "Q3"]))
    with pytest.raises(st.SurveyError, match="every question"):
        builder.reorder_questions(s, ["Q2", "Q1"])
    with pytest.raises(st.SurveyError, match="already first"):
        builder.reorder_questions(s, move=("Q1", "up"))
    with pytest.raises(st.SurveyError, match="exactly one"):
        builder.reorder_questions(s, ["Q1"], move=("Q1", "down"))
    with pytest.raises(st.SurveyError, match="within one block"):
        builder.reorder_questions(kitchen(), swap=("QID56", "QID64"))


def test_reorder_refuses_to_break_logic_order():
    s = st.read_qsf(qsf("TEST_FlowBehavior.qsf"))
    # QID6's skip logic jumps forward to QID9 in the same block
    with pytest.raises(st.UnsupportedEdit, match="jump backwards to .*QID9"):
        builder.reorder_questions(s, ["QID9", "QID6", "QID7", "QID8"])
    ok = builder.reorder_questions(s, ["QID6", "QID8", "QID7", "QID9"])  # the jump still goes forward
    assert [q.id for q in ok.questions.values() if q.block_id == "BL_56lbbwE5KRumJCK"] == ["QID6", "QID8", "QID7", "QID9"]
    # piped text must keep reading an earlier question
    from qsf_writer import write_qsf

    piped = st.read_qsf(write_qsf("Piping", [
        {"id": "Q1", "type": "single_choice", "text": "Favourite fruit?", "options": ["Apple", "Pear"]},
        {"id": "Q2", "type": "open_text", "text": "Why ${q://QID1/ChoiceGroup/SelectedChoices}?"},
        {"id": "Q3", "type": "open_text", "text": "Anything else?"},
    ]))
    assert piped.check().mode == "staged"
    with pytest.raises(st.UnsupportedEdit, match=r"Q2 \(QID2\) reads Q1 \(QID1\), which would come after it"):
        builder.reorder_questions(piped, move=("Q1", "down"))
    assert [q.export_tag for q in builder.reorder_questions(piped, move=("Q3", "up")).questions.values()] == ["Q1", "Q3", "Q2"]


# ---- surveys and plans already made stay as they were -------------------------------------------


def test_edits_leave_the_input_survey_and_its_plans_unchanged(mini_bank):
    s = base()
    config = st.configure(bank=mini_bank, sample_size=1, model=scripted_model())
    plan = st.plan(s, config)
    before_template = copy.deepcopy(s._base_template)
    before_prompt = plan.preview_prompt().text
    s2 = builder.add_questions(s, {"type": "open_text", "text": "New question?"})
    s3 = builder.update_question(s2, "Q1", text="Changed?")
    builder.remove_questions(s3, "Q2")
    builder.reorder_questions(s3, move=("Q1", "down"))
    assert s._base_template == before_template and s.edits == []
    assert plan.preview_prompt().text == before_prompt
    assert "New question?" in st.plan(s2, config).preview_prompt().text


def test_edits_are_recorded_with_results(mini_bank):
    s = builder.update_question(base(), "Q1", text="Changed?")
    results = st.run(st.plan(s, st.configure(bank=mini_bank, sample_size=1, model=scripted_model())))
    assert results.provenance["survey_edits"] == [{"op": "update_question", "qid": "QID1", "question": "Q1", "fields": ["text"]}]


def test_edits_on_a_dropped_block_are_refused():
    k = kitchen().drop_blocks(["BL_KS_TE_4"])
    with pytest.raises(st.SurveyError, match="dropped"):
        builder.add_questions(k, {"type": "open_text", "text": "x"}, block_id="BL_KS_TE_4")


def test_unresolved_image_follows_its_question():
    s = builder.update_question(base(), "Q2", image_id="img_missing")
    assert [m["question"] for m in s.unresolved_media] == ["Q2"]
    s2 = builder.update_question(s, "Q2", text="still missing")
    assert [m["reference"] for m in s2.unresolved_media] == ["img_missing"]
    s3 = builder.update_question(s2, "Q2", image_id="https://img.example/ok.png")
    assert s3.unresolved_media == [] and s3.questions["QID2"].media[0]["src"] == "https://img.example/ok.png"
    s4 = builder.remove_questions(s, "Q2")
    assert s4.unresolved_media == []


def test_to_draft_round_trip_and_qsf_refusal():
    s = builder.reorder_questions(base(), move=("Q3", "up"))
    d = builder.to_draft(s)
    assert [q.id for q in d.questions] == ["Q1", "Q3", "Q2"]
    rebuilt = builder.build_survey(d)
    assert [q.export_tag for q in rebuilt.questions.values()] == ["Q1", "Q3", "Q2"]
    with pytest.raises(st.UnsupportedEdit, match="read from a QSF"):
        builder.to_draft(kitchen())
