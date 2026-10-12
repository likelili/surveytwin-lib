"""Same-block answer-dependent piping, as a runtime acceptance case (raised on
2026-09-29).

A block may hold an earlier question and a later one that pipes its answer. The
earlier question is asked first; the dependent content is prepared only after the
answer exists; the block is never held back as a whole, and unresolved content is
never shown. Each test runs through the public API (read_qsf, plan, run, exports)
with a scripted model that records every prompt it gets.
"""

from __future__ import annotations

import csv
import json
import re

import surveytwin as st
from fakes import accept_all_images, asked_questions
from flow_kit import LOOP_COFFEE_TEA, block, mc, qsf, te, when


class Recorder:
    """A scripted model: answers each question by ``rule(question, prompt)`` (or the
    first option / a short text) and keeps every prompt."""

    def __init__(self, rule=None, *, by_label=False):
        self.rule = rule or (lambda q, prompt: None)
        self.by_label = by_label
        self.prompts: list[str] = []
        self.images: list[list[str]] = []

    def model(self) -> st.Model:
        def reply(request):
            text = request.user
            self.prompts.append(text)
            self.images.append(list(request.images))
            questions = asked_questions(text)
            # each question's text as the survey prints it ("Q2:\n<text>")
            texts = {f"Q{n}": body.strip() for n, body in re.findall(r"(?m)^Q(\d+):\n(.+)$", current_text(text))}
            answers = []
            for q in questions:
                value = self.rule(q, text)
                if value is None:
                    value = (q.get("options") or [None])[0] or f"answer to {texts.get(q.get('printed_as'), q['id'])}"
                qid = q.get("printed_as", q["id"]) if self.by_label else q["id"]  # a model that names answers by the printed labels
                answers.append({"question_id": qid, "answer_value": value, "answer_label": value, "answer_raw": value})
            return st.Reply(text=json.dumps({"answers": answers}), input_tokens=1, output_tokens=1)

        return st.Model.from_function(reply, name="recorder", images=True)


def run(data, recorder, mini_bank, *, n=2, max_stages=None):
    options = {"max_stages": max_stages} if max_stages else {}
    config = st.configure(bank=mini_bank, sample_size=n, seed=3, model=recorder.model(), **options)
    plan = st.plan(st.read_qsf(data), config)
    return plan, st.run(plan, image_checker=accept_all_images)


def rows_of(results, twin_id):
    return [r for r in results.rows if r["persona_id"] == twin_id]


def current_text(prompt):
    """The part of a staged prompt that shows this stage's survey."""
    if "Current stage survey text:\n" in prompt:
        prompt = prompt.split("Current stage survey text:\n", 1)[1]
    return re.split(r"Questions JSON|Answer each question under", prompt, maxsplit=1)[0]


def asked_ids(prompt):
    return [q["id"] for q in asked_questions(prompt)]


def labels_csv(results, tmp_path):
    path = results.write_exports(tmp_path / "out", report=False)["csv_labels"]
    with open(path, encoding="utf-8") as f:
        header, description, _import_ids, *rows = list(csv.reader(f))
    return dict(zip(header, description)), [dict(zip(header, r)) for r in rows]


FRUIT = qsf(
    {"BL_A": ("Fruit", ["QID1", "QID2", "QID3"])},
    {"QID1": mc("QID1", "Which fruit do you like most?", ["Apple", "Banana"]),
     "QID2": te("QID2", "Why do you like ${q://QID1/ChoiceGroup/SelectedChoices}?"),
     "QID3": te("QID3", "What goes with ${q://QID1/ChoiceGroup/SelectedChoices}?")},
    [block("BL_A")],
)


def banana(q, _prompt):
    return "Banana" if q["id"] == "QID1" else None


def test_the_earlier_question_is_asked_first_and_the_dependent_text_shown_resolved(mini_bank, tmp_path):
    recorder = Recorder(banana)
    plan, results = run(FRUIT, recorder, mini_bank)
    assert plan.mode == "staged"
    assert not results.warnings
    per_twin = [recorder.prompts[i:i + 2] for i in range(0, len(recorder.prompts), 2)]
    assert len(per_twin) == 2
    for first, second in per_twin:
        assert asked_ids(first) == ["QID1"]  # the block is not held back behind QID2
        assert "Why do you like" not in first and "${" not in first  # nothing unresolved is shown
        assert asked_ids(second) == ["QID2", "QID3"]
        assert "Why do you like Banana?" in current_text(second) and "${" not in second
        # the answered question is listed with the earlier answers, not asked again
        assert "Which fruit do you like most?" not in current_text(second)
        assert "Which fruit do you like most?\n   Previous answer: Banana" in second
        assert re.findall(r"(?m)^Q\d+:$", current_text(second)) == ["Q1:", "Q2:"]
    for twin in results.twins:
        rows = {r["question_id"]: r for r in rows_of(results, twin["id"])}
        assert [r["question_id"] for r in rows_of(results, twin["id"])] == ["QID1", "QID2", "QID3"]
        assert rows["QID1"]["answer_label"] == "Banana" and twin["status"] == "complete"
        assert rows["QID2"]["question_text"] == "Why do you like Banana?"
        assert rows["QID2"]["answer_label"] == "answer to Why do you like Banana?"
    descriptions, csv_rows = labels_csv(results, tmp_path)
    # the column header is the question as written, the same for every twin
    assert descriptions["QID2"] == "Why do you like ${q://QID1/ChoiceGroup/SelectedChoices}?"
    assert [r["QID1"] for r in csv_rows] == ["Banana", "Banana"]
    assert [r["QID2"] for r in csv_rows] == ["answer to Why do you like Banana?"] * 2


def test_answers_named_by_the_printed_labels_land_on_the_questions_asked(mini_bank):
    """A later stage labels only what it asks, so "Q1" is QID2 there, as the answers
    are matched. It listed the answered question as Q1 and QID2 as Q2, and an answer
    named "Q2" went to QID3."""
    _plan, results = run(FRUIT, Recorder(banana, by_label=True), mini_bank)
    for twin in results.twins:
        rows = {r["question_id"]: r for r in rows_of(results, twin["id"])}
        assert rows["QID2"]["answer_label"] == "answer to Why do you like Banana?"
        assert rows["QID3"]["answer_label"] == "answer to What goes with Banana?"
        assert not rows["QID2"].get("answer_status")


def test_a_pipe_to_a_later_question_of_the_block_stays_as_the_twin_saw_it(mini_bank):
    data = qsf(
        {"BL_A": ("Home", ["QID1", "QID2", "QID3"])},
        {"QID1": mc("QID1", "Where do you work?", ["Where I live now [${q://QID2/ChoiceTextEntryValue}]", "Somewhere else"]),
         "QID2": te("QID2", "Where do you live?"),
         "QID3": te("QID3", "Why ${q://QID2/ChoiceTextEntryValue}?")},
        [block("BL_A")],
    )
    recorder = Recorder(lambda q, _p: "Paris" if q["id"] == "QID2" else None)
    _plan, results = run(data, recorder, mini_bank, n=1)
    first, second = recorder.prompts
    assert "Where I live now []" in first  # QID2 had no answer yet, as in Qualtrics
    assert "[Paris]" not in second and asked_ids(second) == ["QID3"]
    assert "Why Paris?" in current_text(second)


def test_the_other_pipe_forms_are_filled_as_qualtrics_fills_them(mini_bank):
    data = qsf(
        {"BL_A": ("Forms", ["QID1", "QID2"])},
        {"QID1": mc("QID1", "Which fruit?", ["Apple", "Banana", "Cherry"]),
         "QID2": te("QID2", "R=[${q://QID1/ChoiceGroup/SelectedChoicesRecode}] U=[${q://QID1/ChoiceGroup/UnselectedChoices}] "
                            "A=[${q://QID1/ChoiceGroup/AllChoices}] N=[${q://QID1/SelectedChoicesCount}]")},
        [block("BL_A")],
    )
    next(e for e in data["SurveyElements"] if e.get("PrimaryAttribute") == "QID1")["Payload"]["RecodeValues"] = {"1": "10", "2": "20", "3": "30"}
    _plan, results = run(data, Recorder(lambda q, _p: "Banana" if q["id"] == "QID1" else None), mini_bank, n=1)
    text = next(r["question_text"] for r in results.rows if r["question_id"] == "QID2")
    assert text == "R=[20] U=[Apple, Cherry] A=[Apple, Banana, Cherry] N=[1]"  # was [Banana] four times


def test_a_form_the_runtime_does_not_fill_is_named_before_the_run():
    data = qsf(
        {"BL_A": ("Forms", ["QID1", "QID2"])},
        {"QID1": mc("QID1", "Which fruit?", ["Apple", "Banana"]), "QID2": te("QID2", "Said ${q://QID1/ChoiceGroup/SomethingElse}?")},
        [block("BL_A")],
    )
    check = st.read_qsf(data).check()
    assert any("${q://QID1/ChoiceGroup/SomethingElse}" in f.message and f.feature == "piped_text.question" for f in check.findings)


def test_an_answer_the_data_does_not_hold_pipes_blank(mini_bank):
    """An answer that is not one of the options is blanked in the data
    (out_of_range); piped text showed the rejected text ("Why Grape?")."""
    recorder = Recorder(lambda q, _p: "Grape" if q["id"] == "QID1" else None)
    _plan, results = run(FRUIT, recorder, mini_bank, n=1)
    rows = {r["question_id"]: r for r in results.rows}
    assert rows["QID1"]["answer_status"] == "out_of_range" and rows["QID1"]["answer_label"] == ""
    assert rows["QID2"]["question_text"] == "Why do you like ?"


def test_loop_rows_and_split_columns_get_the_question_as_written_as_their_header(mini_bank, tmp_path):
    data = qsf(
        {"BL_L": ("Drinks", ["QID1", "QID2"], LOOP_COFFEE_TEA)},
        {"QID1": mc("QID1", "Do you drink ${lm://Field/1}?", ["Yes", "No"]),
         "QID2": te("QID2", "You said ${q://QID1/ChoiceGroup/SelectedChoices} about ${lm://Field/1}. Why?")},
        [block("BL_L")],
    )
    twins = iter(["Yes", "No", "No", "Yes"])  # the two twins answer differently

    def rule(q, _prompt):
        return next(twins) if q["id"].endswith("_QID1") else None

    _plan, results = run(data, Recorder(rule), mini_bank)
    seen = sorted({r["question_text"] for r in results.rows if r["question_id"] == "1_QID2"})
    assert seen == ["You said No about Coffee. Why?", "You said Yes about Coffee. Why?"]
    descriptions, _rows = labels_csv(results, tmp_path)
    assert descriptions["1_QID2"] == "You said ${q://QID1/ChoiceGroup/SelectedChoices} about Coffee. Why?"  # was one twin's answer
    assert descriptions["2_QID2"] == "You said ${q://QID1/ChoiceGroup/SelectedChoices} about Tea. Why?"


def test_a_twin_that_finishes_at_the_last_allowed_stage_is_complete(mini_bank):
    _plan, results = run(FRUIT, Recorder(banana), mini_bank, n=1, max_stages=2)
    assert results.twins[0]["status"] == "complete" and not results.warnings  # was "incomplete", stopped at the limit
    assert len(results.rows) == 3


def test_plan_warns_when_the_survey_needs_more_stages_than_allowed(mini_bank):
    chain = qsf(
        {"BL_A": ("Chain", ["QID1", "QID2", "QID3", "QID4"])},
        {"QID1": te("QID1", "Name a fruit."), "QID2": te("QID2", "Why ${q://QID1/ChoiceTextEntryValue}?"),
         "QID3": te("QID3", "And ${q://QID2/ChoiceTextEntryValue}?"), "QID4": te("QID4", "So ${q://QID3/ChoiceTextEntryValue}?")},
        [block("BL_A")],
    )
    config = st.configure(bank=mini_bank, sample_size=1, seed=3, model=Recorder().model(), max_stages=2)
    plan = st.plan(st.read_qsf(chain), config)
    assert any("needs about 4 stages" in w and "max_stages=9" in w for w in plan.warnings)
    enough = st.plan(st.read_qsf(chain), st.configure(bank=mini_bank, sample_size=1, seed=3, model=Recorder().model(), max_stages=4))
    assert not any("stages" in w for w in enough.warnings)


# ---- review of the above (2026-09-29) --------------------------------------------------------------


def payload_of(data, qid):
    return next(e for e in data["SurveyElements"] if e.get("PrimaryAttribute") == qid)["Payload"]


def test_a_later_stage_still_sends_the_image_of_the_question_it_follows(mini_bank):
    data = qsf(
        {"BL_A": ("Ad", ["QID1", "QID2"])},
        {"QID1": mc("QID1", 'Look at this ad. <img src="https://example.org/ad.png"> Do you find it appealing?', ["Yes", "No"]),
         "QID2": te("QID2", "What in the ad made you say ${q://QID1/ChoiceGroup/SelectedChoices}?")},
        [block("BL_A")],
    )
    recorder = Recorder(lambda q, _p: "Yes" if q["id"] == "QID1" else None)
    run(data, recorder, mini_bank, n=1)
    assert recorder.images == [["https://example.org/ad.png"], ["https://example.org/ad.png"]]  # it was [] at stage 2
    assert "Visual context for an earlier question" in recorder.prompts[1]


def test_a_rejected_answer_selects_nothing_in_logic_either(mini_bank):
    data = qsf(
        {"BL_A": ("A", ["QID1", "QID2", "QID3"])},
        {"QID1": mc("QID1", "Which fruit?", ["Apple", "Banana"]),
         "QID2": te("QID2", "You picked ${q://QID1/ChoiceGroup/SelectedChoices}. Why?", display_logic=when("QID1", "SelectableChoice/2", "Selected")),
         "QID3": te("QID3", "Anything else?")},
        [block("BL_A")],
    )
    _plan, results = run(data, Recorder(lambda q, _p: "Banana, because it is sweet" if q["id"] == "QID1" else None), mini_bank, n=1)
    assert [r["question_id"] for r in results.rows] == ["QID1", "QID3"]  # the data holds no answer: as unanswered
    assert results.rows[0]["answer_status"] == "out_of_range"


def with_write_in(question, key):
    question["Choices"][key]["TextEntry"] = "true"
    return question


def test_a_label_names_the_answer_of_a_question_with_a_write_in_box(mini_bank):
    data = qsf(
        {"BL_A": ("A", ["QID1", "QID2"])},
        {"QID1": with_write_in(mc("QID1", "Where do you live?", ["City", "Town", "Other (specify)"]), "3"), "QID2": te("QID2", "How long?")},
        [block("BL_A")],
    )
    _plan, results = run(data, Recorder(lambda q, _p: "Town" if q["id"] == "QID1" else None, by_label=True), mini_bank, n=1)
    rows = {r["question_id"]: r for r in results.rows}
    assert rows["QID1"]["answer_label"] == "Town" and not rows["QID1"].get("answer_status")  # it was skipped


def test_write_ins_pipe_their_typed_text_and_are_not_counted_as_choices(mini_bank):
    multi = with_write_in(mc("QID1", "Which fruits do you eat?", ["Apple", "Banana", "Other"]), "3")
    multi["Selector"] = "MAVR"
    single = with_write_in(mc("QID5", "Where do you live?", ["City", "Other (specify)"]), "2")
    data = qsf(
        {"BL_A": ("A", ["QID1", "QID5"]), "BL_B": ("B", ["QID2"])},
        {"QID1": multi, "QID5": single,
         "QID2": te("QID2", "N=[${q://QID1/SelectedChoicesCount}] R=[${q://QID1/ChoiceGroup/SelectedChoicesRecode}] "
                            "T=[${q://QID1/ChoiceTextEntryValue/3}] L=[${q://QID5/ChoiceTextEntryValue/2}]")},
        [block("BL_A"), block("BL_B")],
    )
    payload_of(data, "QID1")["RecodeValues"] = {"1": "10", "2": "20", "3": "30"}

    def rule(q, _prompt):
        if q["id"] == "QID1_3_TEXT":
            return "Mango"
        if q["id"] == "QID5_2_TEXT":
            return "Village"
        if q["id"] == "QID5":
            return "Other (specify)"
        if q["id"].startswith("QID1_"):
            return {"QID1_1": "Yes", "QID1_2": "No", "QID1_3": "Yes"}[q["id"]]
        return None

    _plan, results = run(data, Recorder(rule), mini_bank, n=1)
    text = next(r["question_text"] for r in results.rows if r["question_id"] == "QID2")
    assert text == "N=[2] R=[10, 30] T=[Mango] L=[Village]"  # was N=[3] R=[10, 30, 3_TEXT] T=[1] L=[Other (specify)]


def test_a_loop_rows_choices_read_from_another_block(mini_bank):
    data = qsf(
        {"BL_L": ("Drinks", ["QID1"], LOOP_COFFEE_TEA), "BL_B": ("After", ["QID2"])},
        {"QID1": mc("QID1", "How is ${lm://Field/1}?", ["${lm://Field/1} is great", "${lm://Field/1} is bad"]),
         "QID2": te("QID2", "N=[${q://1_QID1/SelectedChoicesCount}] U=[${q://1_QID1/ChoiceGroup/UnselectedChoices}]")},
        [block("BL_L"), block("BL_B")],
    )
    _plan, results = run(data, Recorder(lambda q, _p: (q.get("options") or [None])[-1] if "QID1" in q["id"] else None), mini_bank, n=1)
    text = next(r["question_text"] for r in results.rows if r["question_id"] == "QID2")
    assert text == "N=[1] U=[Coffee is great]"  # was N=[0] and every choice unselected


def test_a_matrix_pipes_its_answers_and_its_unfilled_forms_are_named(mini_bank):
    matrix = {
        "QuestionID": "QID1", "DataExportTag": "QID1", "QuestionText": "Which devices do you use where?", "QuestionType": "Matrix",
        "Selector": "Likert", "SubSelector": "MultipleAnswer", "Configuration": {"QuestionDescriptionOption": "UseText"},
        "Choices": {"1": {"Display": "Work"}, "2": {"Display": "Home"}}, "ChoiceOrder": [1, 2],
        "Answers": {"1": {"Display": "Laptop"}, "2": {"Display": "Phone"}}, "AnswerOrder": [1, 2],
        "Validation": {"Settings": {"ForceResponse": "OFF", "Type": "None"}},
    }
    data = qsf(
        {"BL_A": ("A", ["QID1"]), "BL_B": ("B", ["QID2"])},
        {"QID1": matrix, "QID2": te("QID2", "Work=[${q://QID1/ChoiceGroup/SelectedAnswers/1}] D=[${q://QID1/ChoiceGroup/DisplayedChoices}]")},
        [block("BL_A"), block("BL_B")],
    )
    check = st.read_qsf(data).check()
    assert any("${q://QID1/ChoiceGroup/DisplayedChoices}" in f.message for f in check.findings)
    assert not any("SelectedAnswers" in f.message for f in check.findings if "does not fill" in f.message)
    _plan, results = run(data, Recorder(lambda q, _p: "Yes" if q["id"] == "QID1_1_1" else ("No" if q["id"].startswith("QID1_") else None)), mini_bank, n=1)
    assert next(r["question_text"] for r in results.rows if r["question_id"] == "QID2").startswith("Work=[Laptop]")  # was Work=[]


def test_loop_rows_of_split_columns_name_their_row_and_write_ins_say_text(mini_bank, tmp_path):
    multi = mc("QID2", "When do you drink ${lm://Field/1}?", ["Morning", "Evening"])
    multi["Selector"] = "MAVR"
    data = qsf(
        {"BL_L": ("Drinks", ["QID2", "QID6"], LOOP_COFFEE_TEA)},
        {"QID2": multi, "QID6": with_write_in(mc("QID6", "Where do you buy ${lm://Field/1}?", ["Shop", "Other"]), "2")},
        [block("BL_L")],
    )
    _plan, results = run(data, Recorder(), mini_bank, n=1)
    descriptions, _rows = labels_csv(results, tmp_path)
    assert descriptions["1_QID2_1"] == "When do you drink Coffee? - Morning" and descriptions["2_QID2_1"] == "When do you drink Tea? - Morning"
    assert descriptions["2_QID6_2_TEXT"] == "Where do you buy Tea? - Other - Text"
