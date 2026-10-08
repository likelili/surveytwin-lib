"""The standard survey format: written surveys, QSF read through it, and its checks.

A QSF is read into the standard format and the survey runs from that form;
compiled back, it gives exactly the QSF reader's template on the test QSFs. A
survey written in the format is checked field by field before anything runs,
and runs as written.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

import surveytwin as st
from conftest import QSF_DIR, TESTS, qsf
from fakes import scripted_model

from surveytwin.prompts.instrument import collect_instrument_questions
from surveytwin.qsf.html_text import html_text
from surveytwin.runtime.instrument_plan import build_instrument_from_template, parse_template
from surveytwin.spec.compile import compile_survey
from surveytwin.spec.model import check_structure, json_schema
from surveytwin.spec.read_template import survey_from_template
from surveytwin.spec.validate import check_survey

ROOT = TESTS.parent
COFFEE = ROOT / "examples" / "survey-format" / "coffee-habits.json"


def configure(bank, **kwargs):
    kwargs.setdefault("sample_size", 2)
    kwargs.setdefault("seed", 5)
    kwargs.setdefault("model", scripted_model())
    return st.configure(bank=bank, **kwargs)


def minimal(**question) -> dict:
    """A one-question survey in the standard format."""
    q = {
        "id": "q1",
        "content": [{"type": "text", "text": "Pick one."}],
        "response": {"type": "choice", "mode": "single", "options": [{"id": "a", "label": "A"}, {"id": "b", "label": "B"}]},
    }
    q.update(question)
    return {
        "schema_version": "0.1",
        "id": "s",
        "questions": [q],
        "blocks": [{"id": "b1", "kind": "survey", "questions": ["q1"]}],
        "flow": [{"type": "show_block", "block": "b1"}],
    }


def codes(findings) -> set[str]:
    return {f.code for f in findings if f.level == "error"}


# ---- a QSF goes through the standard format -------------------------------------------


@pytest.mark.parametrize("path", sorted(QSF_DIR.glob("*.qsf")), ids=lambda p: p.name)
def test_every_test_qsf_compiles_from_its_standard_form_to_the_readers_template(path):
    data = json.loads(path.read_text(encoding="utf-8"))
    template = parse_template(data)
    standard = survey_from_template(template, qsf=data)
    assert check_structure(standard) == []
    assert [f for f in check_survey(standard) if f.level == "error"] == []
    assert compile_survey(json.loads(json.dumps(standard))) == template


def test_read_qsf_runs_the_template_compiled_from_the_standard_form():
    survey = st.read_qsf(qsf("kitchen_sink_supported.qsf"))
    standard = survey.standard
    assert standard["schema_version"] == "0.1" and standard["source"]["format"] == "qsf"
    assert compile_survey(standard) == survey.template == parse_template(json.loads(qsf("kitchen_sink_supported.qsf").read_text()))
    # a question's text is kept as the QSF wrote it, HTML included
    assert any(item.get("format") == "html" for q in standard["questions"] for item in q["content"])


def test_the_standard_fields_alone_give_the_same_prompts():
    """Without anything kept from the QSF settings, the engine prepares the same questions."""
    data = json.loads(qsf("TEST_Matrix.qsf").read_text())
    template = parse_template(data)
    standard = survey_from_template(template, qsf=data)
    bare = {k: v for k, v in copy.deepcopy(standard).items() if k != "source"}
    bare["source"] = {"format": "qsf"}  # HTML read from a QSF is laid out as the QSF reader does
    for question in bare["questions"]:
        question.pop("source", None)
    for block in bare["blocks"]:
        block.pop("source", None)
    old = build_instrument_from_template(template, 3, seed=4, respondent_ids=["a", "b", "c"])
    new = build_instrument_from_template(compile_survey(bare), 3, seed=4, respondent_ids=["a", "b", "c"])
    strip = lambda inst: [  # noqa: E731
        ([{k: v for k, v in q.items() if k != "text_validation_type"} for q in i["questions"]], i["prompt_sequence_text"]) for i in inst["instances"]
    ]
    assert strip(old) == strip(new)


def test_a_survey_with_dropped_blocks_is_read_into_the_standard_format_as_it_now_is():
    whole = st.read_qsf(qsf("kitchen_sink_supported.qsf"))
    survey = whole.drop_blocks([whole.blocks[1]["block_id"]])
    shown = {q["id"] for q in survey.standard["questions"]}
    assert shown == set(survey.questions) and shown < set(whole.questions)
    assert compile_survey(survey.standard) == survey.template


# ---- a written survey runs as written -------------------------------------


def consent_survey() -> dict:
    """A consent question whose "no" ends the survey, then questions that read it."""
    return {
        "schema_version": "0.1",
        "id": "consent",
        "title": "Consent",
        "questions": [
            {
                "id": "consent",
                "content": [{"type": "text", "text": "Do you agree to take part?"}],
                "response": {"type": "choice", "mode": "single", "options": [{"id": "yes", "label": "Yes"}, {"id": "no", "label": "No"}]},
            },
            {"id": "cups", "content": [{"type": "text", "text": "How many cups of coffee do you drink a day?"}], "response": {"type": "number"}},
            {
                "id": "why",
                "show_if": {"op": "selected", "question": "consent", "option": "yes"},
                "content": [{"type": "text", "text": {"parts": [
                    {"text": "You answered "},
                    {"ref": {"scope": "answer", "question": "consent", "field": "selected_choices"}},
                    {"text": ". Why do you take part?"},
                ]}}],
                "response": {"type": "text"},
            },
        ],
        "blocks": [{"id": "intro", "kind": "survey", "questions": ["consent"]}, {"id": "main", "kind": "survey", "questions": ["cups", "why"]}],
        "flow": [
            {"type": "show_block", "block": "intro"},
            {"id": "screen_out", "type": "branch", "when": {"op": "not_selected", "question": "consent", "option": "yes"}, "then": [{"id": "stop", "type": "end"}]},
            {"type": "show_block", "block": "main"},
        ],
    }


def test_a_written_survey_is_planned_as_written(mini_bank):
    written = consent_survey()
    plan = st.plan(st.read_survey(written), configure(mini_bank))
    assert plan.definition == written  # nothing added or rewritten on the way


def test_dropping_a_written_surveys_consent_block_removes_its_screen_out(mini_bank):
    """Its question IDs are the survey's own (not Qualtrics' QID12): the screen-out
    that reads only the dropped question goes, and what reads it is reported."""
    survey = st.read_survey(consent_survey()).drop_blocks(["intro"])
    assert survey.standard["flow"] == [{"type": "show_block", "block": "main"}]
    report = survey.drop_report
    assert report.pruned_branches == ["FL screen_out"]
    assert [(d.kind, d.location, d.reads) for d in report.references] == [("display_logic", "SQ why", "consent"), ("piping", "SQ why", "consent")]
    results = st.run(st.plan(survey, configure(mini_bank)))
    # every twin is asked the rest; "why" is shown only to those who said yes, so to none
    assert {(r["persona_id"], r["question_id"]) for r in results.rows} == {(t, "cups") for t in {r["persona_id"] for r in results.rows}}
    assert len({r["persona_id"] for r in results.rows}) == 2


def test_a_written_survey_exports_its_definition_as_written(mini_bank, tmp_path):
    results = st.run(st.plan(st.read_survey(COFFEE), configure(mini_bank)))
    paths = results.write_exports(tmp_path / "written", report=False, text_files=False)
    written = json.loads(paths["survey_definition"].read_text())
    assert written == results.run["definition"] == json.loads(COFFEE.read_text())
    assert (tmp_path / "written" / "question_logic.json").is_file()  # the report still reads it
    manifest = json.loads(paths["manifest"].read_text())
    assert "survey_definition.json" in {f["file_name"] for f in manifest["files"]}
    qsf_results = st.run(st.plan(st.read_qsf(qsf("TEST_MC.qsf")), configure(mini_bank)))
    assert "survey_definition" not in qsf_results.write_exports(tmp_path / "qsf", report=False, text_files=False)
    assert not (tmp_path / "qsf" / "survey_definition.json").exists()


def test_block_previews_of_a_written_survey_name_what_reads_their_questions():
    previews = {b.block_id: b for b in st.read_survey(consent_survey()).preview_blocks()}
    assert previews["intro"].screenout and not previews["main"].screenout
    assert previews["intro"].referenced_by == [
        "branch at FL screen_out reads consent", "display_logic at SQ why reads consent", "piping at SQ why reads consent",
    ]


def test_a_branch_with_else_loses_only_the_part_that_shows_nothing():
    survey = st.read_survey(COFFEE).drop_blocks(["screen", "goodbye"])
    (branch,) = survey.standard["flow"]
    assert branch["id"] == "drinkers" and branch["then"] and "else" not in branch
    assert survey.drop_report.pruned_branches == ["FL drinkers_else"]


# ---- tables keep every row and cell ---------------------------------------


def test_a_table_keeps_every_header_row_cell_and_the_text_around_it():
    html = (
        "<p>Compare the offers.</p><table><thead><tr><th>Plan</th><th>Price</th></tr></thead>"
        "<tbody><tr><td>Basic</td><td>$5</td></tr><tr><td>Plus<br>with support</td><td>$9</td></tr>"
        "<tr><td>&nbsp;</td><td></td></tr></tbody></table><p>Which would you pick?</p>"
    )
    assert html_text(html) == (
        "Compare the offers.\n\n| Plan | Price |\n|---|---|\n| Basic | $5 |\n| Plus; with support | $9 |\n\nWhich would you pick?"
    )


def test_a_table_with_one_row_of_values_reads_as_heading_value_pairs():
    html = (
        "<p>Read this post.</p><table><thead><tr><th>Source</th><th>Number of Likes</th></tr></thead>"
        "<tbody><tr><td>BBC<nobr> News</nobr></td><td>200<nobr> likes</nobr></td></tr></tbody></table>"
        "<p>Would you share it?</p>"
    )
    assert html_text(html) == "Read this post.\n\nSource: BBC News. Number of Likes: 200 likes\n\nWould you share it?"
    # a single row with no heading keeps its cells apart
    assert html_text("<table><tr><td>Left</td><td></td><td>Right</td></tr></table>") == "Left | Right"


@pytest.mark.parametrize(
    "table, text",
    [
        # labels down the first column: one record read down the table
        ("<tr><th>Brand</th><td>Acme</td></tr><tr><th>Price</th><td>$5</td></tr>", "Brand: Acme. Price: $5"),
        ("<tr><th>Style</th><td></td></tr><tr><th>Color</th><td>Black</td></tr><tr><th>Price</th><td>$1,200</td></tr>", "Style. Color: Black. Price: $1,200"),
        ("<tr><th>Brand</th><td>A</td><td>B</td></tr><tr><th>Price</th><td>$5</td><td>$6</td></tr>", "Brand: A | B\nPrice: $5 | $6"),
        # nothing marks the headings: two rows stay rows, never paired up
        ("<tr><td>Brand</td><td>Acme</td></tr><tr><td>Price</td><td>$5</td></tr>", "Brand | Acme\nPrice | $5"),
        # a heading row (all <th>, or <thead>) with one row of values: pairs
        ("<tr><th>Brand</th><th>Price</th></tr><tr><td>Acme</td><td>$5</td></tr>", "Brand: Acme. Price: $5"),
        ("<thead><tr><td>Brand</td><td>Price</td></tr></thead><tr><td>Acme</td><td>$5</td></tr>", "Brand: Acme. Price: $5"),
        # headings across and down: a table
        ("<tr><th>Item</th><th>A</th><th>B</th></tr><tr><th>Price</th><td>$5</td><td>$6</td></tr>", "| Item | A | B |\n|---|---|---|\n| Price | $5 | $6 |"),
    ],
)
def test_which_cells_are_headings_comes_from_the_markup(table, text):
    assert html_text(f"<table>{table}</table>") == text


def test_a_qsf_question_with_a_table_reaches_the_prompt_line_by_line():
    from flow_kit import block, mc
    from flow_kit import qsf as build

    text = "Read this:<table><tr><td>Name</td><td>Age</td></tr><tr><td>Ann</td><td>31</td></tr><tr><td>Bo</td><td>42</td></tr></table>Who is older?"
    survey = st.read_qsf(build({"BL_1": ("Main", ["QID1"])}, {"QID1": mc("QID1", text, ["Ann", "Bo"])}, [block("BL_1")]))
    question = survey.template["Elements"][0]["Questions"][0]
    assert question["KeepLines"] is True
    assert "| Name | Age |\n|---|---|\n| Ann | 31 |\n| Bo | 42 |" in question["QuestionText"]
    entry = collect_instrument_questions(survey.template["Elements"])[0]
    assert entry["text"].startswith("Read this:\n\n| Name | Age |")


# ---- question text keeps its lines -----------------------------------------


def test_qsf_question_text_keeps_its_paragraphs_line_breaks_and_lists(mini_bank):
    from flow_kit import block, mc
    from flow_kit import qsf as build

    text = (
        "<div>Please read the following carefully.</div><div><br></div>"
        "<p>You will see\nthree   offers.<br>Each lasts <b>one</b> week.</p>"
        "<ul><li>Offer A</li><li>Offer B</li></ul>Which do you prefer?"
    )
    survey = st.read_qsf(build({"BL_1": ("Main", ["QID1"])}, {"QID1": mc("QID1", text, ["A", "B"])}, [block("BL_1")]))
    (question,) = st.plan(survey, configure(mini_bank)).twin_surveys[0]["questions"]
    # as a browser shows it: the source's own line break is a space
    assert question["text"] == (
        "Please read the following carefully.\n\nYou will see three offers.\nEach lasts one week.\n\n- Offer A\n- Offer B\n\nWhich do you prefer?"
    )
    # the template (the website's form, question_logic.json) has the same lines
    written = survey.template["Elements"][0]["Questions"][0]
    assert written["QuestionText"] == question["text"].removesuffix(" Response required.") and written["KeepLines"] is True


def test_written_html_text_is_laid_out_as_qsf_text_is(mini_bank):
    html = "<div>Line one\ncontinues.</div><div>Line two</div><ol><li>first</li><li>second</li></ol>"
    survey = st.read_survey(minimal(content=[{"type": "text", "text": html, "format": "html"}]))
    (question,) = st.plan(survey, configure(mini_bank)).twin_surveys[0]["questions"]
    assert question["text"] == "Line one continues.\nLine two\n\n1. first\n2. second"


# ---- a survey written in the standard format --------------------------------------------


def test_the_coffee_example_reads_and_runs(mini_bank):
    survey = st.read_survey(COFFEE)
    assert survey.kind == "standard" and survey.name == "Coffee habits"
    assert survey.standard == json.loads(COFFEE.read_text())
    plan = st.plan(survey, configure(mini_bank))
    assert plan.mode == "staged"
    results = st.run(plan)
    answers = [a for a in results.answers if a.question_id == "drinks_coffee"]
    assert answers and all(a.value == "Yes" for a in answers)  # the scripted model picks the first option
    asked = {a.question_id for a in results.answers if a.status != "not_shown"}
    assert {"cups", "importance_price", "own_cup", "1_rate_drink", "2_rate_drink", "3_rate_drink"} <= asked
    assert "goodbye_non_drinker" not in asked


def test_a_written_survey_saves_and_loads_with_its_definition():
    survey = st.read_survey(COFFEE)
    again = st.Survey.from_dict(json.loads(json.dumps(survey.to_dict())))
    assert again.kind == "standard" and again.standard == survey.standard and again.checksum == survey.checksum
    assert again.template == survey.template


def test_written_text_is_never_guessed_to_be_a_number():
    survey = st.read_survey(minimal(response={"type": "text"}, content=[{"type": "text", "text": "How many pets do you have?"}], presentation={"control": "single_line"}))
    entry = collect_instrument_questions(survey.template["Elements"])[0]
    assert entry["type"] == "open_text"


def test_written_text_keeps_its_lines():
    survey = st.read_survey(minimal(content=[{"type": "text", "text": "First line.\nSecond line."}]))
    entry = collect_instrument_questions(survey.template["Elements"])[0]
    assert entry["text"] == "First line.\nSecond line."


def test_a_lone_number_is_one_numeric_answer():
    survey = st.read_survey(minimal(response={"type": "number", "min": 0, "max": 10, "step": 1}))
    entries = collect_instrument_questions(survey.template["Elements"])
    assert [e["id"] for e in entries] == ["q1"]
    assert entries[0]["numeric_min"] == 0 and entries[0]["numeric_max"] == 10


def test_else_runs_when_the_condition_does_not_hold(mini_bank):
    data = json.loads(COFFEE.read_text())
    survey = st.read_survey(data)
    branch = next(e for e in survey.template["Elements"] if e.get("FlowID") == "drinkers_else")
    assert branch["ElementType"] == "Branch" and branch["Elements"][0]["BlockID"] == "goodbye"
    # the opposite of "selected yes"
    condition = branch["BranchLogic"]["0"]["0"]
    assert condition["Operator"] == "NotSelected" and condition["LeftOperand"] == "q://drinks_coffee/SelectableChoice/yes"


# ---- checks: every problem with its path ------------------------------------------------


def test_unknown_fields_and_wrong_types_are_errors_with_paths():
    data = minimal()
    data["questions"][0]["respone"] = {}
    data["questions"][0]["response"]["options"][0]["label"] = 5
    with pytest.raises(st.SurveyFormatError) as caught:
        st.read_survey(data)
    paths = {f.path for f in caught.value.findings}
    assert "$.questions[0].respone" in paths and "$.questions[0].response.options[0].label" in paths


def test_an_unsupported_schema_version_is_refused():
    data = minimal()
    data["schema_version"] = "9"
    with pytest.raises(st.SurveyFormatError, match="schema_version"):
        st.read_survey(data)


@pytest.mark.parametrize(
    "change, code",
    [
        (lambda d: d["flow"].append({"type": "show_block", "block": "nope"}), "reference.block"),
        (lambda d: d["blocks"][0]["questions"].append("ghost"), "reference.question"),
        (lambda d: d["questions"][0].update(show_if={"op": "selected", "question": "q1", "option": "zz"}), "reference.condition"),
        (lambda d: d["questions"][0]["response"]["options"].append({"id": "c", "label": "a"}), "label.duplicate"),
        (lambda d: d["questions"][0]["response"]["options"].append({"id": "a", "label": "C"}), "id.duplicate"),
        (lambda d: d["questions"][0]["response"].update(mode="multiple", min_selected=2, max_selected=1), "response.limits"),
        (lambda d: d["questions"][0].update(skip=[{"when": {"op": "selected", "question": "q1", "option": "a"}, "to": "q1"}]), "reference.skip"),
        (lambda d: d["questions"][0]["response"].update(default=["zz"]), "reference.default"),
        (lambda d: d["blocks"][0].update(repeat={"rows": [{"id": "2", "values": {}}, {"id": "1", "values": {}}]}), "repeat.order"),
        (lambda d: d["questions"][0].update(response={"type": "matrix", "mode": "per_row", "rows": [{"id": "r", "label": "R"}]}), "response.mode"),
    ],
)
def test_problems_of_meaning_are_errors(change, code):
    data = minimal()
    change(data)
    with pytest.raises(st.SurveyFormatError) as caught:
        st.read_survey(data)
    assert code in codes(caught.value.findings)


def test_an_else_whose_then_steps_change_its_condition_is_refused():
    data = minimal()
    data["questions"].append({"id": "q2", "content": [{"type": "text", "text": "Say something."}], "response": {"type": "text"}})
    data["blocks"].append({"id": "b2", "kind": "survey", "questions": ["q2"]})
    data["flow"].append(
        {
            "id": "br",
            "type": "branch",
            "when": {"op": "eq", "left": {"scope": "variable", "variable": "x"}, "right": "1"},
            "then": [{"id": "set", "type": "set_variables", "assignments": [{"variable": "x", "value": "2"}]}],
            "else": [{"type": "show_block", "block": "b2"}],
        }
    )
    with pytest.raises(st.SurveyFormatError) as caught:
        st.read_survey(data)
    assert "branch.else" in codes(caught.value.findings)


def test_warnings_do_not_stop_a_survey():
    data = minimal(content=[{"type": "text", "text": {"parts": [{"text": "Hi "}, {"ref": {"scope": "variable", "variable": "name"}}]}}])
    survey = st.read_survey(data)
    assert [w.code for w in survey.read_warnings] == ["variable.unset"]


def test_a_qsf_export_is_pointed_to_read_qsf():
    with pytest.raises(st.SurveyError, match="read_qsf"):
        st.read_survey(qsf("TEST_MC.qsf"))


# ---- the schema file and the command line -----------------------------------------------


def test_the_published_json_schema_is_current():
    published = json.loads((ROOT / "docs" / "reference" / "survey-format.schema.json").read_text())
    assert published == json_schema()


def test_convert_writes_a_survey_that_reads_back(tmp_path, capsys):
    from surveytwin.cli import main

    out = tmp_path / "survey.json"
    assert main(["convert", str(qsf("TEST_FlowBehavior.qsf")), "-o", str(out)]) == 0
    again = st.read_survey(out)
    assert again.template == st.read_qsf(qsf("TEST_FlowBehavior.qsf")).template
    capsys.readouterr()
    assert main(["schema"]) == 0
    assert json.loads(capsys.readouterr().out) == json_schema()
