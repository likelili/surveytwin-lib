"""Problems another review found in the builder and the staged runtime (2026-09-28):
a draft round trip that renumbered questions, to_draft bringing back dropped
questions, an update that left nothing to answer, and a block that never started
because a question in it pipes an earlier answer from the same block."""

from __future__ import annotations

import json

import pytest

import surveytwin as st
from fakes import accept_all_images, scripted_model
from qsf_writer import write_qsf
from surveytwin import builder

FRUIT = {"title": "Fruit", "questions": [
    {"id": "Q1", "type": "single_choice", "text": "Which fruit do you like most?", "options": ["Apple", "Banana"]},
    {"id": "Q2", "type": "open_text", "text": "Why do you like ${q://QID1/ChoiceGroup/SelectedChoices}?"},
    {"id": "Q3", "type": "single_choice", "text": "Which color do you like most?", "options": ["Red", "Blue"]},
]}


def qids(survey):
    return [(q.export_tag, q.id) for q in survey.questions.values()]


# ---- 1. internal IDs survive reordering, to_draft, saving and building --------------------------


def test_a_round_trip_keeps_every_internal_id_and_what_pipes_read(mini_bank):
    s = builder.build_survey(FRUIT)
    moved = builder.reorder_questions(s, order=["Q3", "Q1", "Q2"])
    assert qids(moved) == [("Q3", "QID3"), ("Q1", "QID1"), ("Q2", "QID2")]
    draft = builder.to_draft(moved)
    assert [q.qid for q in draft.questions] == ["QID3", "QID1", "QID2"]
    saved = json.loads(json.dumps(draft.to_dict()))  # what a web form or a file keeps
    assert saved["questions"][0]["qid"] == "QID3"
    rebuilt = builder.build_survey(saved)
    assert qids(rebuilt) == qids(moved)
    config = st.configure(bank=mini_bank, sample_size=2, seed=3, model=scripted_model())
    results = st.run(st.plan(rebuilt, config), image_checker=accept_all_images)
    fruit = {r["persona_id"]: r["answer_label"] for r in results.rows if r["question_id"] == "QID1"}
    for row in (r for r in results.rows if r["question_id"] == "QID2"):
        assert fruit[row["persona_id"]] in row["question_text"]  # still the fruit, not the color


def test_new_questions_get_ids_no_question_has_or_pipes():
    draft = builder.to_draft(builder.remove_questions(builder.build_survey(FRUIT), "Q3"))
    data = draft.to_dict()
    data["questions"].append({"id": "Q9", "type": "open_text", "text": "Anything else?"})
    assert builder.planned_qids(data) == ["QID1", "QID2", "QID4"]  # never the removed Q3's QID3
    data["questions"].insert(0, {"id": "Q8", "type": "open_text", "text": "Say ${q://QID7/ChoiceGroup/SelectedChoices}."})
    issues = builder.validate_draft(data)
    assert [(i.code, i.question_index) for i in issues if i.severity == "error"] == [("text.piping_unknown", 0)]
    assert builder.planned_qids(data) == ["QID8", "QID1", "QID2", "QID9"]  # new ones above the piped QID7 too
    legacy = {"title": "T", "questions": [{"type": "open_text", "text": "a"}, {"type": "open_text", "text": "${q://QID1/ChoiceTextEntryValue}"}]}
    assert builder.planned_qids(legacy) == ["QID1", "QID2"] and builder.validate_draft(legacy) == []  # by position, as before


def test_internal_id_rules():
    def codes(questions):
        return [(i.code, i.field) for i in builder.validate_draft({"title": "T", "questions": questions}) if i.severity == "error"]

    assert codes([{"type": "open_text", "text": "a", "qid": "Q1"}]) == [("field.invalid", "qid")]
    assert codes([{"type": "open_text", "text": "a", "qid": "QID2"}, {"type": "open_text", "text": "b", "qid": "QID2"}]) == [("qid.duplicate", "qid")]
    assert codes([{"type": "open_text", "text": "${q://QID2/ChoiceTextEntryValue}", "qid": "QID1"},
                  {"type": "open_text", "text": "b", "qid": "QID2"}]) == [("text.piping_later", "text")]
    s = builder.build_survey(FRUIT)
    with pytest.raises(st.DraftError) as err:
        builder.add_questions(s, {"type": "open_text", "text": "Copied", "qid": "QID1"})
    assert err.value.issues[0].code == "qid.not_allowed"
    whole = builder.to_draft(s).questions[2].to_dict()
    assert builder.update_question(s, "Q3", {**whole, "text": "Which colour?"}).questions["QID3"].text == "Which colour?"
    with pytest.raises(st.DraftError) as err:
        builder.update_question(s, "Q3", {**whole, "qid": "QID1"})
    assert err.value.issues[0].code == "qid.fixed"


# ---- 2. to_draft is the survey as it is now, and refuses what a draft cannot hold ------------------


def two_block_survey():
    data = write_qsf("Two", [{"id": "OLD1", "type": "open_text", "text": "Old one?"}, {"id": "OLD2", "type": "open_text", "text": "Old two?"}])
    blocks = next(e for e in data["SurveyElements"] if e["Element"] == "BL")["Payload"]
    blocks["1"]["BlockElements"] = [{"Type": "Question", "QuestionID": "QID1"}]
    blocks["2"] = {"Type": "Standard", "SubType": "", "Description": "Second", "ID": "BL_TWO", "BlockElements": [{"Type": "Question", "QuestionID": "QID2"}]}
    flow = next(e for e in data["SurveyElements"] if e["Element"] == "FL")["Payload"]["Flow"]
    flow.append({"ID": "BL_TWO", "Type": "Standard", "FlowID": "FL_3"})
    s = st.read_qsf(data)
    s = builder.add_questions(s, {"id": "KEEP", "type": "open_text", "text": "Keep me?"}, block_id="BL_BUILDER")
    s = builder.add_questions(s, {"id": "DROP", "type": "open_text", "text": "Drop me?"}, block_id="BL_TWO")
    return builder.remove_questions(s, ["OLD1", "OLD2"])


def test_to_draft_leaves_dropped_blocks_out():
    s = two_block_survey()
    with pytest.raises(st.UnsupportedEdit, match="it has 2 blocks"):
        builder.to_draft(s)  # would flatten two blocks into one list
    active = s.drop_blocks(["BL_TWO"])
    draft = builder.to_draft(active)
    assert [q.id for q in draft.questions] == ["KEEP"]
    assert [q.export_tag for q in builder.build_survey(draft).questions.values()] == ["KEEP"]
    with pytest.raises(st.DraftError) as err:  # dropping every block leaves nothing to answer
        s.drop_blocks(["BL_BUILDER", "BL_TWO"])
    assert [i.code for i in err.value.issues] == ["survey.no_answerable"]


def test_to_draft_refuses_flow_a_draft_cannot_hold():
    s = builder.build_survey(FRUIT)
    looped = st.Survey(
        {**s.template, "Elements": [{**s.template["Elements"][0], "Options": {"RandomizeQuestions": "RandomWithXPerPage"}}]},
        kind="builder",
        authoring=s._authoring,
    )
    with pytest.raises(st.UnsupportedEdit, match="randomizes its questions"):
        builder.to_draft(looped)


# ---- 3. an update cannot leave nothing to answer --------------------------------------------------


def test_an_update_cannot_leave_the_survey_with_nothing_to_answer():
    s = builder.build_survey({"title": "T", "questions": [
        {"id": "Q1", "type": "instruction", "text": "Read this."},
        {"id": "Q2", "type": "open_text", "text": "What do you think?"},
    ]})
    with pytest.raises(st.DraftError) as err:
        builder.update_question(s, "Q2", type="instruction")
    assert err.value.issues[0].code == "survey.no_answerable"
    assert builder.update_question(s, "Q1", type="open_text", text="Anything first?")


# ---- 4. piping an earlier answer from the same block ----------------------------------------------


def run_one(survey, mini_bank):
    config = st.configure(bank=mini_bank, sample_size=2, seed=3, model=scripted_model())
    return st.run(st.plan(survey, config), image_checker=accept_all_images)


def test_a_question_piping_an_earlier_answer_in_its_block_is_asked_after_it(mini_bank):
    """The website's runtime asks nothing here: it waits on the whole block."""
    results = run_one(st.read_qsf(write_qsf("Fruit", FRUIT["questions"][:2])), mini_bank)
    assert not [w for w in results.warnings if "stalled" in w]
    for twin in results.twins:
        rows = [r for r in results.rows if r["persona_id"] == twin["id"]]
        assert [r["question_id"] for r in rows] == ["QID1", "QID2"]
        assert rows[0]["answer_label"] in rows[1]["question_text"]


def test_piping_a_later_question_of_the_block_is_empty_and_waits_for_nothing(mini_bank):
    questions = [
        {"id": "Q1", "type": "open_text", "text": "Before you pick: [${q://QID2/ChoiceGroup/SelectedChoices}] Any thoughts?"},
        {"id": "Q2", "type": "single_choice", "text": "Pick one.", "options": ["Apple", "Banana"]},
        {"id": "Q3", "type": "instruction", "text": "You picked ${q://QID2/ChoiceGroup/SelectedChoices}."},
        {"id": "Q4", "type": "open_text", "text": "Anything else?"},
    ]
    results = run_one(st.read_qsf(write_qsf("Order", questions)), mini_bank)
    assert not [w for w in results.warnings if "stalled" in w]
    rows = [r for r in results.rows if r["persona_id"] == results.twins[0]["id"]]
    assert [r["question_id"] for r in rows] == ["QID1", "QID2", "QID4"]
    assert "[]" in rows[0]["question_text"]  # Q2 had no answer yet, as in Qualtrics


# ---- fifth review -------------------------------------------------------------------


def test_an_image_reference_of_0_on_a_built_question_without_an_image():
    resolve = lambda ref: f"https://example.org/img/{ref}.png"  # noqa: E731
    s = builder.build_survey({"title": "T", "questions": [{"id": "Q1", "type": "single_choice", "text": "Pick", "options": ["a", "b"]}]},
                             image_resolver=resolve)
    assert [m["src"] for m in builder.update_question(s, "Q1", image_resolver=resolve, image_id=0).questions["QID1"].media] == [
        "https://example.org/img/0.png"]
    assert builder.update_question(s, "Q1", image_id="") is s  # the same (no) image: nothing to do
