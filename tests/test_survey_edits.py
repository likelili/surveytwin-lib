"""Editing a survey's definition: add, update, move, swap and remove questions;
add, remove and gather blocks.

Each edit changes only what it names in the definition (``Survey.standard``),
returns a new Survey, and is checked as ``read_survey`` checks a file. An edit
that would leave logic reading a question that is gone or asked later is refused.
"""

from __future__ import annotations

import base64
import json
from pathlib import Path

import pytest

import surveytwin as st
from conftest import TESTS, qsf
from fakes import answer_questions, scripted_model

BANK = str(TESTS.parent / "examples" / "example-bank")
COFFEE = TESTS.parent / "examples" / "survey-format" / "coffee-habits.json"
PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==")


def changed_places(a, b, path="$") -> list[str]:
    """The paths where two JSON values differ (a list of another length is one place)."""
    if type(a) is not type(b):
        return [path]
    if isinstance(a, dict):
        out = []
        for key in sorted(set(a) | set(b)):
            out += [f"{path}.{key}"] if key not in a or key not in b else changed_places(a[key], b[key], f"{path}.{key}")
        return out
    if isinstance(a, list):
        if len(a) != len(b):
            return [f"{path} (length)"]
        return [p for i, (x, y) in enumerate(zip(a, b)) for p in changed_places(x, y, f"{path}[{i}]")]
    return [] if a == b else [path]


def coffee() -> st.Survey:
    return st.read_survey(COFFEE)


def question(survey: st.Survey, qid: str) -> dict:
    return next(q for q in survey.standard["questions"] if q["id"] == qid)


def block(survey: st.Survey, block_id: str) -> dict:
    return next(b for b in survey.standard["blocks"] if b["id"] == block_id)


def run(survey: st.Survey, twins: int = 1, model: st.Model | None = None) -> st.Results:
    return st.run(st.plan(survey, st.configure(bank=BANK, sample_size=twins, seed=1, model=model or scripted_model())))


# ---- a new survey, built question by question -------------------------------------------------


def test_a_new_survey_is_filled_one_question_at_a_time_and_runs():
    survey = st.new_survey("Coffee habits")
    assert survey.standard == {"schema_version": "0.1", "id": "coffee_habits", "title": "Coffee habits", "questions": [], "blocks": [], "flow": []}
    survey = survey.add_question("single_choice", "Do you drink coffee?", options=["Yes", "No"], id="drinks", required=True)
    survey = survey.add_question("number", "How many cups a day?", min=0, max=12, show_if={"op": "selected", "question": "drinks", "option": "1"})
    assert survey.standard["blocks"] == [{"id": "main", "kind": "survey", "questions": ["drinks", "Q1"]}]
    assert survey.standard["flow"] == [{"type": "show_block", "block": "main"}]
    assert question(survey, "drinks")["response"] == {
        "type": "choice", "mode": "single", "options": [{"id": "1", "label": "Yes"}, {"id": "2", "label": "No"}], "required": True,
    }
    assert [e["op"] for e in survey.edits] == ["add_question", "add_question"]
    assert st.read_survey(survey.standard).standard == survey.standard  # a valid survey in the standard format
    results = run(survey)
    assert {a.question_id for a in results.answers} == {"drinks", "Q1"}
    assert [e["question"] for e in results.survey.edits] == ["drinks", "Q1"]


def test_every_question_type_is_written_in_the_standard_format_and_answered():
    survey = st.new_survey("Every type")
    survey = survey.add_question("instruction", "Welcome.")
    survey = survey.add_question("single_choice", "Pick one", options=["A", "B"], id="single")
    survey = survey.add_question("multiple_choice", "Pick some", options=["A", "B", "C"], max_selected=2, id="multi")
    survey = survey.add_question("text", "Say something", max_length=80, id="say")
    survey = survey.add_question("number", "A number", min=0, max=10, id="num")
    survey = survey.add_question("grid", "Rate", rows=["X", "Y"], scale="agreement_5", id="grid")
    survey = survey.add_question("number_grid", "Slide", rows=["X", "Y"], min=0, max=100, id="ngrid")
    survey = survey.add_question("text_grid", "Write", rows=["X", "Y"], columns=["Why", "How"], id="tgrid")
    survey = survey.add_question("sum_grid", "Split", rows=["Week", "Weekend"], columns=["Coffee", "Tea"], total=10, id="sgrid")
    survey = survey.add_question("form", "Your details", fields=["City", "Job"], id="form")
    survey = survey.add_question("ranking", "Rank", items=["A", "B", "C"], id="rank")
    survey = survey.add_question("allocation", "Allocate", items=["A", "B"], id="alloc")
    survey = survey.add_question("side_by_side", "Compare", rows=["Cats", "Dogs"],
                                 columns=[{"label": "Like", "scale": ["No", "Yes"]}, {"label": "Own", "scale": ["No", "Yes"]}], id="sbs")
    responses = {q["id"]: q.get("response") for q in survey.standard["questions"]}
    assert responses["Q1"] is None
    assert responses["grid"]["row_response"]["options"][0] == {"id": "1", "label": "Strongly disagree"}
    assert responses["ngrid"]["row_response"] == {"type": "number", "min": 0, "max": 100}
    assert responses["tgrid"]["cell_response"] == {"type": "text"}
    assert responses["sgrid"]["constraints"] == [{"type": "sum_equals", "axis": "row", "total": 10}]
    assert responses["alloc"]["total"] == 100
    assert [f["label"] for f in responses["sbs"]["fields"]] == ["Like", "Own"]
    assert st.read_survey(survey.standard).standard == survey.standard
    answered = {a.question_id for a in run(survey).answers}
    for qid in ("single", "multi_1", "say", "num", "grid_1", "ngrid_2", "tgrid_2_1", "sgrid_1_2", "form_2", "rank_3", "alloc_1", "sbs#2_1"):
        assert qid in answered


def test_a_question_takes_any_field_of_the_format_and_objects_for_its_lists():
    survey = st.new_survey("Fields").add_question(
        "single_choice",
        {"parts": [{"text": "Hello "}, {"ref": {"scope": "variable", "variable": "name"}}]},
        options=[{"id": "y", "label": "Yes", "code": 1}, "No", {"label": "Other", "write_in": True}],
        randomization={"order": "shuffle"},
        control="dropdown",
        export_name="greet",
    )
    q = question(survey, "Q1")
    assert q["export_name"] == "greet" and q["presentation"] == {"control": "dropdown"}
    assert q["content"] == [{"type": "text", "text": {"parts": [{"text": "Hello "}, {"ref": {"scope": "variable", "variable": "name"}}]}}]
    assert q["response"]["options"] == [{"id": "y", "label": "Yes", "code": 1}, {"id": "2", "label": "No"}, {"id": "3", "label": "Other", "write_in": True}]
    assert q["response"]["randomization"] == {"order": "shuffle"}
    assert [str(w) for w in survey.edit_warnings] == ["warning: $.questions[0].content[0].text.parts: variable 'name' is never set or declared; it reads as empty"]


def test_wrong_arguments_are_refused_with_what_is_allowed():
    survey = st.new_survey("T")
    with pytest.raises(st.SurveyError, match="'likert' is not a question type; the types are: instruction, single_choice"):
        survey.add_question("likert", "Q?")
    with pytest.raises(st.SurveyError, match="a single_choice question needs options"):
        survey.add_question("single_choice", "Q?")
    with pytest.raises(st.SurveyError, match="'colour' is not a setting of a text question; its settings are: required, min_length"):
        survey.add_question("text", "Q?", colour="red")
    with pytest.raises(st.SurveyError, match="needs text or a picture"):
        survey.add_question("text")
    with pytest.raises(st.SurveyFormatError, match=r"\$\.questions\[0\]\.response\.max_selected: max_selected 3 is more than the 2 options"):
        survey.add_question("multiple_choice", "Q?", options=["a", "b"], max_selected=3)
    with pytest.raises(st.SurveyFormatError, match="two options read 'a'"):
        survey.add_question("single_choice", "Q?", options=["a", "A"])


# ---- editing a survey written in the standard format ------------------------------------------


def test_adding_a_question_changes_only_the_question_and_its_block():
    survey = coffee()
    before = survey.standard
    edited = survey.add_question("single_choice", "Would you pay more for organic coffee?", options=["Yes", "No"],
                                 id="organic_more", after="own_cup")
    assert changed_places(before, edited.standard) == ["$.blocks[4].questions (length)", "$.questions (length)"]
    assert block(edited, "reaction")["questions"] == ["own_cup", "organic_more", "price_fair"]
    assert [q["id"] for q in edited.standard["questions"]].index("organic_more") == [q["id"] for q in before["questions"]].index("own_cup") + 1
    assert survey.standard == before and survey.edits == []  # the survey it was called on is unchanged
    assert edited.checksum != survey.checksum


def test_a_survey_with_several_blocks_needs_to_be_told_where():
    with pytest.raises(st.SurveyError, match="This survey has 7 blocks .*say where question 'Q11' goes with block=, after= or before="):
        coffee().add_question("text", "Anything else?")
    edited = coffee().add_question("text", "Anything else?", block="reaction", position=1)
    # (Q11: the survey's export names run to Q10, and a new ID never repeats one)
    assert block(edited, "reaction")["questions"] == ["Q11", "own_cup", "price_fair"]
    with pytest.raises(st.SurveyError, match="position is a number from 1 \\(first\\) to 3 \\(last\\) in block 'reaction', not 9"):
        coffee().add_question("text", "Anything else?", block="reaction", position=9)


def test_update_changes_only_what_is_given():
    survey = coffee()
    edited = survey.update_question("cups", text="How many cups on a weekday?", max=20, required=None)
    assert changed_places(survey.standard, edited.standard) == [
        "$.questions[2].content[0].text", "$.questions[2].response.max", "$.questions[2].response.required",
    ]
    assert "required" not in question(edited, "cups")["response"]
    edited = survey.update_question("chain_name", show_if=None)
    assert "show_if" not in question(edited, "chain_name")
    assert edited.edits[-1] == {"op": "update_question", "on": "definition", "question": "chain_name", "changed": ["show_if"]}


def test_update_keeps_pictures_and_text_apart():
    survey = st.new_survey("P").add_question("single_choice", "Which ad?", image="https://example.org/ad.png", options=["A", "B"])
    edited = survey.update_question("Q1", text="Which ad do you like?")
    assert question(edited, "Q1")["content"] == [{"type": "text", "text": "Which ad do you like?"}, {"type": "image", "source": "https://example.org/ad.png"}]
    assert question(edited.update_question("Q1", image=None), "Q1")["content"] == [{"type": "text", "text": "Which ad do you like?"}]


def test_a_new_type_keeps_what_it_can_and_is_refused_when_logic_would_read_it_differently():
    survey = coffee()
    multiple = survey.update_question("drinks_coffee", type="multiple_choice")
    response = question(multiple, "drinks_coffee")["response"]
    assert response["mode"] == "multiple" and [o["id"] for o in response["options"]] == ["yes", "no"] and response["required"] is True
    with pytest.raises(st.UnsupportedEdit, match="a text question would answer them differently:\n  the display condition of question 'chain_name' reads question 'where'"):
        survey.update_question("where", type="text")
    ranking = survey.update_question("budget", type="ranking")
    assert [i["id"] for i in question(ranking, "budget")["response"]["items"]] == ["beans", "cafe", "gear"]
    with pytest.raises(st.SurveyError, match="a question's ID cannot change"):
        survey.update_question("cups", id="cups2")


def test_removing_an_option_a_condition_names_is_refused():
    with pytest.raises(st.SurveyFormatError, match="question 'where' has no option 'chain'"):
        coffee().update_question("where", options=["At home", "At work"])


def test_moves_and_swaps_keep_logic_reading_earlier_questions():
    survey = coffee()
    moved = survey.move_question("budget", position=1)
    assert block(moved, "habits")["questions"] == ["budget", "cups", "where", "chain_name", "importance", "rank_features"]
    moved = survey.move_question("budget", block="reaction")
    assert block(moved, "reaction")["questions"] == ["own_cup", "price_fair", "budget"]
    assert "budget" not in block(moved, "habits")["questions"]
    swapped = survey.swap_questions("budget", "rank_features")
    assert block(swapped, "habits")["questions"][-2:] == ["rank_features", "budget"]
    with pytest.raises(st.UnsupportedEdit, match="the display condition of question 'chain_name' reads question 'where', which would be asked after it"):
        survey.move_question("where", after="chain_name")
    with pytest.raises(st.UnsupportedEdit, match="asked after it"):
        survey.swap_questions("where", "chain_name")


def test_a_question_that_logic_reads_is_not_removed():
    survey = coffee()
    with pytest.raises(st.UnsupportedEdit, match="remove_question\\('drinks_coffee'\\): other parts of the survey read this question.*\n  the condition of branch 'drinkers' reads question 'drinks_coffee'"):
        survey.remove_question("drinks_coffee")
    removed = survey.remove_question("rank_features")
    assert changed_places(survey.standard, removed.standard) == ["$.blocks[1].questions (length)", "$.questions (length)"]


def test_a_new_question_never_takes_the_id_of_one_removed():
    survey = st.new_survey("T").add_question("text", "One").add_question("text", "Two")
    assert [q["id"] for q in survey.standard["questions"]] == ["Q1", "Q2"]
    survey = survey.remove_question("Q2").add_question("text", "Three")
    assert [q["id"] for q in survey.standard["questions"]] == ["Q1", "Q3"]


def test_two_csv_columns_with_one_name_are_refused():
    with pytest.raises(st.SurveyFormatError, match="two CSV columns would be named 'Q1'"):
        coffee().add_question("text", "Another", export_name="Q1", block="reaction")


# ---- blocks -----------------------------------------------------------------------------------


def test_add_block_places_the_block_in_the_flow():
    survey = coffee()
    last = survey.add_block("feedback", title="Feedback")
    assert last.standard["flow"][-1] == {"type": "show_block", "block": "feedback"}
    assert last.standard["blocks"][-1] == {"id": "feedback", "kind": "survey", "title": "Feedback", "questions": []}
    inside = survey.add_block("feedback", after="reaction")
    assert [n.get("block") or n["type"] for n in inside.standard["flow"][1]["then"]] == ["habits", "randomize", "reaction", "feedback", "drinks"]
    first = survey.add_block("consent", position=1)
    assert first.standard["flow"][0] == {"type": "show_block", "block": "consent"}
    filled = inside.add_question("text", "Anything else?", block="feedback", id="more")
    assert block(filled, "feedback")["questions"] == ["more"]
    with pytest.raises(st.SurveyError, match="already has a block 'habits'"):
        survey.add_block("habits")


def test_a_block_is_removed_with_its_questions_unless_logic_reads_them():
    survey = coffee()
    removed = survey.remove_block("drinks")
    assert "drinks" not in [b["id"] for b in removed.standard["blocks"]]
    assert "rate_drink" not in [q["id"] for q in removed.standard["questions"]]
    assert [n.get("block") or n["type"] for n in removed.standard["flow"][1]["then"]] == ["habits", "randomize", "reaction"]
    with pytest.raises(st.UnsupportedEdit, match="the condition of branch 'drinkers' reads question 'drinks_coffee'"):
        survey.remove_block("screen")


def test_group_into_block_gathers_questions_after_the_first_ones_block():
    survey = coffee()
    grouped = survey.group_into_block(["budget", "rank_features"], "spending", title="Spending")
    assert block(grouped, "habits")["questions"] == ["cups", "where", "chain_name", "importance"]
    assert block(grouped, "spending") == {"id": "spending", "kind": "survey", "title": "Spending", "questions": ["budget", "rank_features"]}
    assert [n.get("block") or n["type"] for n in grouped.standard["flow"][1]["then"]] == ["habits", "spending", "randomize", "reaction", "drinks"]
    assert grouped.edits[-1] == {"op": "group_into_block", "on": "definition", "block": "spending", "questions": ["budget", "rank_features"]}
    elsewhere = survey.group_into_block(["rank_features", "budget"], "spending", after="reaction")
    assert block(elsewhere, "spending")["questions"] == ["rank_features", "budget"]
    assert [n.get("block") or n["type"] for n in elsewhere.standard["flow"][1]["then"]] == ["habits", "randomize", "reaction", "spending", "drinks"]


def test_group_into_block_refuses_what_would_change_how_questions_are_asked():
    survey = coffee()
    with pytest.raises(st.UnsupportedEdit, match="block 'drinks', which repeats \\(loop and merge\\)"):
        survey.group_into_block(["rate_drink"], "once")
    with pytest.raises(st.UnsupportedEdit, match="which would be asked after it"):
        survey.group_into_block(["where"], "later", after="reaction")
    arms = st.read_survey({
        "schema_version": "0.1", "id": "arms",
        "questions": [{"id": q, "content": [{"type": "text", "text": q}], "response": {"type": "text"}} for q in ("a1", "a2", "b1")],
        "blocks": [{"id": "arm_a", "kind": "survey", "questions": ["a1", "a2"]}, {"id": "arm_b", "kind": "survey", "questions": ["b1"]}],
        "flow": [{"id": "r", "type": "randomize", "take": 1, "children": [{"type": "show_block", "block": "arm_a"}, {"type": "show_block", "block": "arm_b"}]}],
    })
    with pytest.raises(st.SurveyError, match="block 'arm_a' is an arm of a randomizer"):
        arms.group_into_block(["a2"], "arm_c")
    # named explicitly, the new block is the arm asked for
    assert len(arms.group_into_block(["a2"], "arm_c", after="arm_b").standard["flow"][0]["children"]) == 3


# ---- surveys read from a QSF, dropped blocks, saving ------------------------------------------


def test_a_qsf_survey_is_edited_on_its_definition_and_keeps_everything_else():
    survey = st.read_qsf(qsf("kitchen_sink_supported.qsf"))
    first = survey.blocks[0]["block_id"]
    edited = survey.add_question("single_choice", "An added question?", options=["Yes", "No"], block=first, id="added")
    assert changed_places(survey.standard, edited.standard) == ["$.blocks[0].questions (length)", "$.questions (length)"]
    shown_first = next(i for i, e in enumerate(survey.template["Elements"]) if e.get("BlockID") == first)
    assert changed_places(survey.template, edited.template) == [f"$.Elements[{shown_first}].Questions (length)"]
    assert edited.kind == "qsf"
    renamed = survey.update_question("KS_MC_Q1", text="New words")  # an export name names its question
    # the HTML text read from the QSF is replaced by the plain text given
    assert changed_places(survey.standard, renamed.standard) == ["$.questions[0].content[0].format", "$.questions[0].content[0].text"]
    assert question(renamed, "QID56")["content"][0] == {"type": "text", "text": "New words"}


def test_dropped_blocks_are_not_edited():
    survey = coffee().drop_blocks(["habits"])
    with pytest.raises(st.SurveyError, match="Block 'habits' is dropped from this survey"):
        survey.add_question("text", "x", block="habits")
    with pytest.raises(st.SurveyError, match="Question 'cups' is in block 'habits', which is dropped"):
        survey.update_question("cups", text="y")
    edited = survey.update_question("own_cup", text="Changed")
    assert edited.exclude_blocks == ["habits"] and "cups" not in [q["id"] for q in edited.standard["questions"]]


def test_an_edited_survey_is_saved_and_loaded_with_its_definition():
    edited = coffee().add_question("text", "Anything else?", block="reaction").drop_blocks(["drinks"])
    again = st.Survey.from_dict(json.loads(json.dumps(edited.to_dict())))
    assert again.checksum == edited.checksum and again.standard == edited.standard and again.edits == edited.edits
    tampered = edited.to_dict()
    tampered["standard"]["questions"][0]["content"][0]["text"] = "changed"
    with pytest.raises(st.SurveyError, match="changed after saving"):
        st.Survey.from_dict(tampered)


def test_a_draft_built_survey_can_be_edited_on_its_definition():
    drafted = st.builder.build_survey({"title": "Draft", "questions": [
        {"id": "Q1", "type": "single_choice", "text": "Pick one", "options": ["a", "b"]},
    ]})
    edited = drafted.add_question("text", "Why?", id="why")
    assert [q["id"] for q in edited.standard["questions"]][-1] == "why"
    with pytest.raises(st.UnsupportedEdit, match="have no draft form .*why"):
        st.builder.to_draft(edited)


def test_build_survey_reads_a_whole_survey_in_the_standard_format():
    data = json.loads(COFFEE.read_text(encoding="utf-8"))
    assert st.builder.build_survey(data).standard == coffee().standard
    with pytest.raises(st.SurveyError, match="block_id apply to a draft"):
        st.builder.build_survey(data, block_id="X")


# ---- pictures ---------------------------------------------------------------------------------


def test_a_picture_file_an_edit_names_is_read_from_the_survey_folder(tmp_path: Path):
    folder = tmp_path / "study"
    (folder / "images").mkdir(parents=True)
    (folder / "images" / "ad.png").write_bytes(PNG)
    copy = folder / "study.json"
    copy.write_text(COFFEE.read_text(encoding="utf-8"), encoding="utf-8")
    survey = st.read_survey(copy)
    edited = survey.add_question("single_choice", "Do you like this ad?", image="images/ad.png", options=["Yes", "No"], block="reaction", id="ad")
    assert question(edited, "ad")["content"][1] == {"type": "image", "source": "images/ad.png"}
    assert list(edited._pictures) == ["images/ad.png"]
    with pytest.raises(st.SurveyFormatError, match="no picture file at .*images/missing.png"):
        survey.add_question("text", "x", image="images/missing.png", block="reaction")
    assert edited.update_question("ad", image=None)._pictures == {}  # a picture no longer named is let go
    blank = st.new_survey("Ads", folder=folder).add_question("instruction", image="images/ad.png")
    assert list(blank._pictures) == ["images/ad.png"]

    requests: list[st.ModelRequest] = []

    def reply(request: st.ModelRequest) -> st.Reply:
        requests.append(request)
        return st.Reply(text=json.dumps({"answers": answer_questions(request.user)}), input_tokens=1, output_tokens=1)

    run(edited.drop_blocks(["drinks"]), model=st.Model.from_function(reply, name="recording", images=True))
    sent = {url for request in requests for url in request.images}
    assert sent == {"data:image/png;base64," + base64.b64encode(PNG).decode("ascii")}


def test_a_qsf_question_given_another_kind_of_answer_keeps_no_old_settings():
    survey = st.read_qsf(qsf("kitchen_sink_supported.qsf"))
    edited = survey.update_question("QID1", type="text")
    q = question(edited, "QID1")
    assert q["response"] == {"type": "text", "required": True}
    assert q["source"] == {"format": "qsf"} and "presentation" not in q  # the old choice settings and layout went
    assert (edited.questions["QID1"].type, edited.questions["QID1"].selector) == ("TE", "ML")
    skipping = st.new_survey("Skip").add_question(
        "single_choice", "Go on?", options=["Yes", "No"], id="go", skip=[{"when": {"op": "selected", "question": "go", "option": "2"}, "to": "end_of_block"}],
    ).add_question("text", "Why?")
    with pytest.raises(st.UnsupportedEdit, match="skip rules test its options; give skip=None"):
        skipping.update_question("go", type="text")
    assert "skip" not in question(skipping.update_question("go", type="text", skip=None), "go")


def test_new_statements_replace_the_side_by_side_rows_the_twin_reads():
    survey = st.read_qsf(qsf("TEST_SBS.qsf"))
    edited = survey.update_question("QID43", rows=["Purr", "Bark", "Swim"])
    q = question(edited, "QID43")
    assert [[r["label"] for r in f["response"]["rows"]] for f in q["response"]["fields"]] == [["Purr", "Bark", "Swim"]] * 2
    assert "sbs" not in q["source"]  # the copy of the old statements went with them
    text = st.plan(edited, st.configure(bank=BANK, sample_size=1, seed=1, model=scripted_model())).preview_prompt().text
    assert "Cats - Swim" in text and "Dogs - Swim" in text
