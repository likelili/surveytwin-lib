"""A question's internal ID is its identity, not its position (2026-09-29).

It survives reordering, editing, a draft round trip (with or without ``qid`` in
the payload), saving and loading; a removed or dropped question's ID is never
given to another question; and the whole survey is checked after every edit.
"""

from __future__ import annotations

import copy
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
WHY = "Why do you like ${q://QID1/ChoiceGroup/SelectedChoices}?"


def ids(survey):
    return [(q.export_tag, q.id) for q in survey.questions.values()]


def texts(survey):
    return {q.id: q.text for q in survey.questions.values()}


def asked(results, qid):
    return [r["question_text"] for r in results.rows if r["question_id"] == qid]


def run_one(survey, mini_bank):
    config = st.configure(bank=mini_bank, sample_size=2, seed=3, model=scripted_model())
    return st.run(st.plan(survey, config), image_checker=accept_all_images)


def without_qids(data):
    """What a web form that keeps only the website's fields sends back."""
    out = copy.deepcopy(data)
    for q in out["questions"]:
        q.pop("qid", None)
    for key in ("next_qid", "block_id", "block_name", "metadata"):
        out.pop(key, None)
    return out


# ---- the Fruit / Why / Color case -----------------------------------------------------------------


def test_fruit_why_color_reordered_converted_and_rebuilt_keeps_ids_targets_and_order(mini_bank, tmp_path):
    s = builder.build_survey(FRUIT)
    moved = builder.reorder_questions(s, order=["Q3", "Q1", "Q2"])  # Color first
    expected = [("Q3", "QID3"), ("Q1", "QID1"), ("Q2", "QID2")]
    assert ids(moved) == expected

    # Survey -> to_draft -> JSON (a form or a file) -> build_survey
    saved = json.loads(json.dumps(builder.to_draft(moved).to_dict()))
    rebuilt = builder.build_survey(saved)
    assert ids(rebuilt) == expected
    assert texts(rebuilt)["QID2"] == WHY  # Why still reads QID1, the fruit

    # Saving and loading, and the survey a run records
    loaded = st.Survey.from_dict(json.loads(json.dumps(rebuilt.to_dict())))
    assert ids(loaded) == expected
    results = run_one(loaded, mini_bank)
    assert asked(results, "QID2") == ["Why do you like Apple?"] * 2  # the fruit, not "Red"
    results.save(tmp_path / "run")
    assert ids(st.Results.load(tmp_path / "run").survey) == expected

    # A form that drops qid and renumbers the tags by position still keeps each question's ID.
    form = without_qids(saved)
    for n, q in enumerate(form["questions"], start=1):
        q["id"] = f"Q{n}"
    applied = builder.apply_draft(moved, form)
    assert ids(applied) == [("Q1", "QID3"), ("Q2", "QID1"), ("Q3", "QID2")]
    assert texts(applied)["QID2"] == WHY
    assert asked(run_one(applied, mini_bank), "QID2") == ["Why do you like Apple?"] * 2
    assert applied.edits[-1]["order_qids"] == ["QID3", "QID1", "QID2"]


def test_moving_qid1_to_the_last_position_keeps_it_qid1():
    # In the Fruit survey, Why pipes the fruit: moving the fruit last would ask Why first.
    s = builder.build_survey(FRUIT)
    with pytest.raises(st.UnsupportedEdit, match="Q2 \\(QID2\\) reads Q1 \\(QID1\\), which would come after it"):
        builder.reorder_questions(s, order=["Q2", "Q3", "Q1"])
    # Where nothing reads it, the fruit moves to the end and stays QID1 through every path.
    free = copy.deepcopy(FRUIT)
    free["questions"][1]["text"] = "Why do you like ${q://QID2/ChoiceGroup/SelectedChoices}?"
    free["questions"][1], free["questions"][2] = free["questions"][2], free["questions"][1]  # Fruit, Color (QID2), Why (Color)
    s = builder.build_survey(free)
    moved = builder.reorder_questions(s, order=["QID2", "QID3", "QID1"])
    assert [q.id for q in moved.questions.values()] == ["QID2", "QID3", "QID1"]
    for survey in (
        builder.build_survey(json.loads(json.dumps(builder.to_draft(moved).to_dict()))),
        st.Survey.from_dict(json.loads(json.dumps(moved.to_dict()))),
        builder.apply_draft(moved, without_qids(builder.to_draft(moved).to_dict())),
    ):
        assert [q.id for q in survey.questions.values()] == ["QID2", "QID3", "QID1"]
        assert survey.questions["QID1"].text == "Which fruit do you like most?"


# ---- a removed or dropped question's ID is never given again ----------------------------------------


def test_a_removed_questions_id_is_never_given_to_a_new_question_on_any_path(tmp_path, mini_bank):
    removed = builder.remove_questions(builder.build_survey(FRUIT), "Q3")  # Color, QID3
    new = {"id": "Q9", "type": "open_text", "text": "Anything else?"}

    def via_draft(survey):
        data = builder.to_draft(survey).to_dict()
        data["questions"].append(dict(new))
        return builder.build_survey(json.loads(json.dumps(data)))

    results = run_one(removed, mini_bank)
    results.save(tmp_path / "run")
    paths = {
        "add_questions": builder.add_questions(removed, dict(new)),
        "draft": via_draft(removed),
        "draft, then add_questions": builder.add_questions(builder.build_survey(builder.to_draft(removed)), dict(new)),
        "saved survey, then draft": via_draft(st.Survey.from_dict(removed.to_dict())),
        "a run's survey, then draft": via_draft(st.Results.load(tmp_path / "run").survey),
        "apply_draft": builder.apply_draft(removed, {"title": "Fruit", "questions": [
            *without_qids(builder.to_draft(removed).to_dict())["questions"], dict(new)]}),
    }
    for path, survey in paths.items():
        assert ids(survey)[-1] == ("Q9", "QID4"), path


def two_blocks():
    """Blocks BL_BUILDER (KEEP, QID3) and BL_TWO (DROP, QID4), from a QSF whose own two questions were removed."""
    data = write_qsf("Two", [{"id": "OLD1", "type": "open_text", "text": "Old one?"}, {"id": "OLD2", "type": "open_text", "text": "Old two?"}])
    blocks = next(e for e in data["SurveyElements"] if e["Element"] == "BL")["Payload"]
    blocks["1"]["BlockElements"] = [{"Type": "Question", "QuestionID": "QID1"}]
    blocks["2"] = {"Type": "Standard", "SubType": "", "Description": "Second", "ID": "BL_TWO", "BlockElements": [{"Type": "Question", "QuestionID": "QID2"}]}
    next(e for e in data["SurveyElements"] if e["Element"] == "FL")["Payload"]["Flow"].append({"ID": "BL_TWO", "Type": "Standard", "FlowID": "FL_3"})
    s = builder.add_questions(st.read_qsf(data), {"id": "KEEP", "type": "open_text", "text": "Keep me?"}, block_id="BL_BUILDER")
    s = builder.add_questions(s, {"id": "DROP", "type": "open_text", "text": "Drop me?"}, block_id="BL_TWO")
    return builder.remove_questions(s, ["OLD1", "OLD2"])


def test_a_dropped_questions_id_is_never_given_to_a_new_question():
    active = two_blocks().drop_blocks(["BL_TWO"])
    draft = builder.to_draft(active).to_dict()
    assert [q["qid"] for q in draft["questions"]] == ["QID3"] and draft["next_qid"] == 5
    draft["questions"].append({"id": "NEW", "type": "open_text", "text": "New?"})
    assert builder.planned_qids(draft) == ["QID3", "QID5"]  # never the dropped question's QID4
    assert ids(builder.apply_draft(active, without_qids(draft)))[-1] == ("NEW", "QID5")
    with pytest.raises(st.DraftError) as err:
        builder.apply_draft(active, {**draft, "questions": [*draft["questions"][:1], {"id": "DROP", "type": "open_text", "text": "Drop me?", "qid": "QID4"}]})
    assert [i.code for i in err.value.issues] == ["qid.retired"]
    with pytest.raises(st.DraftError) as err:
        builder.apply_draft(active, {**draft, "questions": [*draft["questions"][:1], {"id": "X", "type": "open_text", "text": "X?", "qid": "QID9"}]})
    assert "qid.unknown" in [i.code for i in err.value.issues]


# ---- what a draft carries, and what to_draft refuses --------------------------------------------


def test_a_draft_keeps_the_block_and_the_surveys_metadata():
    s = builder.build_survey(FRUIT, block_id="BL_MINE", block_name="Main block")
    rebuilt = builder.build_survey(json.loads(json.dumps(builder.to_draft(s).to_dict())))
    assert [(b.block_id, b.name) for b in rebuilt.preview_blocks()] == [("BL_MINE", "Main block")]
    q = st.read_qsf(write_qsf("Mixed", [{"id": "OLD", "type": "open_text", "text": "Old?"}]))
    mixed = builder.remove_questions(builder.add_questions(q, {"id": "NEW", "type": "open_text", "text": "New?"}), "OLD")
    rebuilt = builder.build_survey(builder.to_draft(mixed))
    assert rebuilt.metadata.get("SurveyLanguage") == mixed.metadata.get("SurveyLanguage") == "EN"
    assert ids(rebuilt) == [("NEW", "QID2")]


def test_to_draft_names_what_a_draft_cannot_hold():
    s = builder.build_survey(FRUIT)
    block = s.template["Elements"][0]
    twice = st.Survey({**s.template, "Elements": [block, copy.deepcopy(block)]}, kind="builder", authoring=s._authoring)
    with pytest.raises(st.UnsupportedEdit, match="block BL_BUILDER appears 2 times in the flow"):
        builder.to_draft(twice)
    wrapped = st.Survey({**s.template, "Elements": [{"Elements": [block]}]}, kind="builder", authoring=s._authoring)
    with pytest.raises(st.UnsupportedEdit, match="a flow element the builder cannot represent"):
        builder.to_draft(wrapped)
    services = st.Survey({**s.template, "WebServices": [{"URL": "https://example.org"}]}, kind="builder", authoring=s._authoring)
    with pytest.raises(st.UnsupportedEdit, match="the survey has WebServices"):
        builder.to_draft(services)
    edited = copy.deepcopy(s.to_dict())
    for q in edited["template"]["Elements"][0]["Questions"]:
        if q["QuestionID"] == "QID2":
            q["DisplayLogic"] = {"0": {"0": {"LogicType": "Question", "QuestionID": "QID1"}}}
    edited.pop("checksum_sha256"), edited.pop("definition_sha256")
    with pytest.raises(st.UnsupportedEdit, match="Q2 \\(QID2\\): DisplayLogic"):
        builder.to_draft(st.Survey.from_dict(edited))


def test_to_draft_refuses_a_draft_it_could_not_build_back():
    data = write_qsf("Two", [{"id": "FRUIT", "type": "single_choice", "text": "Fruit?", "options": ["Apple", "Pear"]},
                             {"id": "OLD", "type": "open_text", "text": "Old?"}])
    blocks = next(e for e in data["SurveyElements"] if e["Element"] == "BL")["Payload"]
    blocks["1"]["BlockElements"] = [{"Type": "Question", "QuestionID": "QID1"}]
    blocks["2"] = {"Type": "Standard", "SubType": "", "Description": "B", "ID": "BL_B", "BlockElements": [{"Type": "Question", "QuestionID": "QID2"}]}
    next(e for e in data["SurveyElements"] if e["Element"] == "FL")["Payload"]["Flow"].append({"ID": "BL_B", "Type": "Standard", "FlowID": "FL_3"})
    s = builder.add_questions(st.read_qsf(data), {"id": "WHY", "type": "open_text", "text": "Why ${q://QID1/ChoiceGroup/SelectedChoices}?"}, block_id="BL_B")
    s = builder.remove_questions(s, "OLD").drop_blocks(["BL_BUILDER"])
    with pytest.raises(st.UnsupportedEdit, match="could not be built back.*WHY text: The question pipes QID1"):
        builder.to_draft(s)


def test_the_qsf_refusal_counts_each_question_once():
    data = write_qsf("Twice", [{"id": "A", "type": "open_text", "text": "A?"}, {"id": "B", "type": "open_text", "text": "B?"}])
    flow = next(e for e in data["SurveyElements"] if e["Element"] == "FL")["Payload"]["Flow"]
    flow.append(dict(flow[0], FlowID="FL_3"))  # the block is shown twice
    with pytest.raises(st.UnsupportedEdit, match="2 question\\(s\\) have no draft form \\(read from a QSF or a survey file, or added with Survey.add_question\\): A, B$"):
        builder.to_draft(st.read_qsf(data))


# ---- every edit keeps the survey's own draft buildable ----------------------------------------------


def three():
    return builder.build_survey({"title": "T", "questions": [
        {"id": "Q1", "type": "single_choice", "text": "Pick one", "options": ["a", "b"]},
        {"id": "Q2", "type": "open_text", "text": "Why?"},
        {"id": "Q3", "type": "multi_select", "text": "Which?", "options": ["x", "y"]},
    ]})


def codes(err):
    return [(i.code, i.field) for i in err.value.issues]


def test_edits_check_pipes_in_every_field_a_draft_keeps():
    s = three()
    with pytest.raises(st.DraftError) as err:  # rows a single choice does not show, piping a missing question
        builder.add_questions(s, {"type": "single_choice", "text": "x", "options": ["a", "b"], "rows": ["${q://QID9/ChoiceTextEntryValue}"]})
    assert codes(err) == [("text.piping_unknown", "rows")]
    with pytest.raises(st.DraftError) as err:  # a later question, in a field a type change would show
        builder.update_question(s, "Q1", rows=["About ${q://QID3/ChoiceGroup/SelectedChoices}"])
    assert codes(err) == [("text.piping_later", "rows")]
    grid = builder.build_survey({"title": "T", "questions": [
        {"id": "Q1", "type": "single_choice", "text": "Pick one", "options": ["a", "b"]},
        {"id": "Q2", "type": "grid", "text": "Rate", "rows": ["About ${q://QID1/ChoiceGroup/SelectedChoices}"], "options": ["lo", "hi"]},
    ]})
    single = builder.update_question(grid, "Q2", type="single_choice")  # the rows stay in its draft
    with pytest.raises(st.DraftError) as err:
        builder.remove_questions(single, "Q1")
    assert codes(err) == [("text.piping_unknown", "rows")]
    with pytest.raises(st.DraftError) as err:
        builder.reorder_questions(single, move=("Q2", "up"))
    assert codes(err) == [("text.piping_later", "rows")]
    assert builder.build_survey(builder.to_draft(single))  # what was accepted still builds back


def test_a_question_cannot_pipe_itself():
    with pytest.raises(st.DraftError) as err:
        builder.build_survey({"title": "T", "questions": [{"id": "Q1", "type": "open_text", "text": "Said ${q://QID1/ChoiceTextEntryValue}?"}]})
    assert [i.code for i in err.value.issues] == ["text.piping_self"]


def test_the_cap_counts_only_questions_that_exist():
    data = write_qsf("Two", [{"id": f"A{i}", "type": "open_text", "text": f"A{i}?"} for i in range(1, 3)]
                     + [{"id": f"B{i}", "type": "open_text", "text": f"B{i}?"} for i in range(1, 3)])
    blocks = next(e for e in data["SurveyElements"] if e["Element"] == "BL")["Payload"]
    blocks["1"]["BlockElements"] = blocks["1"]["BlockElements"][:2]
    blocks["2"] = {"Type": "Standard", "SubType": "", "Description": "B", "ID": "BL_B",
                   "BlockElements": [{"Type": "Question", "QuestionID": "QID3"}, {"Type": "Question", "QuestionID": "QID4"}]}
    next(e for e in data["SurveyElements"] if e["Element"] == "FL")["Payload"]["Flow"].append({"ID": "BL_B", "Type": "Standard", "FlowID": "FL_3"})
    s = st.read_qsf(data).drop_blocks(["BL_B"])
    added = builder.add_questions(s, {"type": "open_text", "text": "One more?"}, block_id="BL_BUILDER", policy=builder.BuilderPolicy(max_questions=3))
    assert len(added.questions) == 3


def test_a_blank_id_never_gets_a_tag_whose_columns_clash():
    d = {"title": "T", "questions": [{"type": "multi_select", "text": "Which?", "options": ["a", "b"]}, {"id": "Q1_1", "type": "open_text", "text": "Why?"}]}
    assert builder.validate_draft(d) == []
    assert ids(builder.build_survey(d)) == [("Q2", "QID1"), ("Q1_1", "QID2")]
    s = builder.build_survey({"title": "T", "questions": [{"id": "Q1", "type": "open_text", "text": "A?"}, {"id": "Q2_1", "type": "open_text", "text": "B?"}]})
    assert ids(builder.add_questions(s, {"type": "multi_select", "text": "Which?", "options": ["a", "b"]}))[-1] == ("Q3", "QID3")


def test_a_removal_is_refused_only_when_it_leaves_nothing_to_answer():
    nothing = st.read_qsf(write_qsf("Text", [{"id": "T1", "type": "instruction", "text": "Hello."}, {"id": "T2", "type": "instruction", "text": "Bye."}]))
    assert ids(builder.remove_questions(nothing, "T2")) == [("T1", "QID1")]  # nothing to answer before, either
    s = builder.build_survey({"title": "T", "questions": [{"id": "Q1", "type": "instruction", "text": "Read."}, {"id": "Q2", "type": "open_text", "text": "Say?"}]})
    with pytest.raises(st.DraftError) as err:
        builder.remove_questions(s, "Q2")
    assert [i.code for i in err.value.issues] == ["survey.no_answerable"]


def test_an_edit_returns_its_warnings():
    s = three()
    edited = builder.update_question(s, "Q2", text="What is your age?", policy=builder.WEBSITE_POLICY)
    assert [w.code for w in edited.edit_warnings] == ["text.demographic"]
    added = builder.add_questions(s, {"type": "open_text", "text": "What is your age?"}, policy=builder.WEBSITE_POLICY)
    assert [w.code for w in added.edit_warnings] == ["text.demographic"]
    qsf_survey = st.read_qsf(write_qsf("Q", [{"id": "Q1", "type": "open_text", "text": "Anything?"}]))
    assert [w.code for w in builder.update_question(qsf_survey, "Q1", text="What is your age?", policy=builder.WEBSITE_POLICY).edit_warnings] == ["text.demographic"]
    assert builder.update_question(s, "Q2", text="What is your age?").edit_warnings == []  # no policy, no warning
    assert "edit_warnings" not in json.dumps(edited.to_dict())  # not part of the survey's definition


def test_the_edit_log_says_which_question_moved_by_its_internal_id():
    s = builder.reorder_questions(three(), order=["Q3", "Q1", "Q2"])
    s = builder.update_question(builder.update_question(s, "Q3", id="COLOR"), "Q1", id="Q3")
    entry = next(e for e in s.edits if e["op"] == "reorder_questions")
    assert entry["order_qids"] == ["QID3", "QID1", "QID2"]
    swapped = builder.reorder_questions(three(), swap=("Q1", "Q2"))
    assert swapped.edits[-1]["swap_qids"] == [["QID1"], ["QID2"]]
    moved = builder.reorder_questions(three(), move=("Q3", "up"))
    assert moved.edits[-1]["move_qid"] == "QID3"


# ---- refine and generate ------------------------------------------------------------------------------


# ---- apply_draft ---------------------------------------------------------------------------------------


def test_apply_draft_matches_by_qid_content_then_tag_and_keeps_the_history():
    s = builder.update_question(builder.build_survey(FRUIT), "Q3", required=True)
    data = builder.to_draft(s).to_dict()
    assert builder.apply_draft(s, data) is s  # nothing changed
    form = without_qids(data)
    form["questions"][0]["text"] = "Which fruit is best?"  # same tag, new text
    form["questions"][2]["id"] = "COLOR"  # same content, new tag
    form["questions"].append({"id": "Q4", "type": "open_text", "text": "Anything else?"})
    applied = builder.apply_draft(s, form)
    assert ids(applied) == [("Q1", "QID1"), ("Q2", "QID2"), ("COLOR", "QID3"), ("Q4", "QID4")]
    assert [e["op"] for e in applied.edits] == ["update_question", "apply_draft"]
    entry = applied.edits[-1]
    assert entry["added"] == [{"id": "Q4", "qid": "QID4"}] and entry["removed"] == []
    assert [(c["qid"], c["fields"]) for c in entry["changed"]] == [("QID1", ["text"]), ("QID3", ["id"])]
    assert entry["changed"][1]["renamed_from"] == "Q3"
    fewer = builder.apply_draft(applied, {"title": "Fruit", "questions": without_qids(builder.to_draft(applied).to_dict())["questions"][:3]})
    assert fewer.edits[-1]["removed"] == [{"id": "Q4", "qid": "QID4"}]
    again = builder.apply_draft(fewer, {"title": "Fruit", "questions": [*builder.to_draft(fewer).to_dict()["questions"],
                                                                        {"id": "Q5", "type": "open_text", "text": "More?"}]})
    assert ids(again)[-1] == ("Q5", "QID5")  # QID4 stays retired


def test_apply_draft_applies_the_whole_survey_rules():
    s = builder.build_survey(FRUIT)
    form = without_qids(builder.to_draft(s).to_dict())
    with pytest.raises(st.DraftError) as err:
        builder.apply_draft(s, {**form, "questions": [dict(q, type="instruction", options=[]) for q in form["questions"]]})
    assert "survey.no_answerable" in [i.code for i in err.value.issues]
    with pytest.raises(st.DraftError) as err:
        builder.apply_draft(s, {**form, "questions": form["questions"][1:]})  # Why without the fruit it pipes
    assert [(i.code, i.field) for i in err.value.issues] == [("text.piping_unknown", "text")]
    with pytest.raises(st.UnsupportedEdit):
        builder.apply_draft(st.read_qsf(write_qsf("Q", FRUIT["questions"])), form)  # no draft form


# ---- dropping blocks keeps something to answer ------------------------------------------------------------


def text_and_question_qsf():
    data = write_qsf("Two", [{"id": "INTRO", "type": "instruction", "text": "Hello."}, {"id": "ASK", "type": "open_text", "text": "Say?"}])
    blocks = next(e for e in data["SurveyElements"] if e["Element"] == "BL")["Payload"]
    blocks["1"]["BlockElements"] = [{"Type": "Question", "QuestionID": "QID1"}]
    blocks["2"] = {"Type": "Standard", "SubType": "", "Description": "Q", "ID": "BL_Q", "BlockElements": [{"Type": "Question", "QuestionID": "QID2"}]}
    next(e for e in data["SurveyElements"] if e["Element"] == "FL")["Payload"]["Flow"].append({"ID": "BL_Q", "Type": "Standard", "FlowID": "FL_3"})
    return data


def test_dropping_or_excluding_every_answerable_question_is_refused():
    data = text_and_question_qsf()
    for attempt in (
        lambda: st.read_qsf(data).drop_blocks(["BL_Q"]),
        lambda: st.read_qsf(data, exclude_blocks=["BL_Q"]),
        lambda: st.read_qsf(data, exclude_questions=["QID2"]),
        lambda: builder.build_survey(FRUIT).drop_blocks(["BL_BUILDER"]),
    ):
        with pytest.raises(st.DraftError) as err:
            attempt()
        assert [i.code for i in err.value.issues] == ["survey.no_answerable"]
    assert st.read_qsf(data).drop_blocks(["BL_BUILDER"]).check().ok


def test_check_is_not_ok_when_nothing_can_be_answered():
    nothing = st.read_qsf(write_qsf("Text", [{"id": "T1", "type": "instruction", "text": "Hello."}]))
    check = nothing.check()
    assert not check.ok
    assert [f.feature for f in check.findings if f.feature == "survey.no_answerable"] == ["survey.no_answerable"]
    assert "Nothing for a twin to answer" in str(check) and str(check).endswith("Needs attention before a run")


# ---- review of the above (2026-09-29) --------------------------------------------------------------


def test_a_new_question_that_takes_a_deleted_questions_tag_gets_a_new_id():
    """The website's form reuses a tag after a delete; the deleted question's ID
    must not pass to the new one (and a pipe to it must not quietly move)."""
    s = builder.build_survey(FRUIT)
    kept = builder.to_draft(s).to_dict()  # qids kept: a question without one is new
    kept["questions"] = kept["questions"][:2] + [{"id": "Q3", "type": "single_choice", "text": "Which season?", "options": ["Summer", "Winter"]}]
    assert ids(builder.apply_draft(s, kept))[-1] == ("Q3", "QID4")
    form = without_qids(builder.to_draft(s).to_dict())  # no qids: matched by tag only within the same type
    form["questions"] = form["questions"][:2] + [{"id": "Q3", "type": "open_text", "text": "Anything else?"}]
    assert ids(builder.apply_draft(s, form))[-1] == ("Q3", "QID4")
    piped = without_qids(builder.to_draft(s).to_dict())
    piped["questions"][0]["text"] = "Which fruit is best?"  # same tag and type: the same question, with a warning
    applied = builder.apply_draft(s, piped)
    assert ids(applied)[0] == ("Q1", "QID1") and [w.code for w in applied.edit_warnings] == ["apply.piped_changed"]

