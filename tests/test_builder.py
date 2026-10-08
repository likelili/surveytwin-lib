"""surveytwin.builder: drafts, validation, conversion, building, and a built survey
running through the same pipeline as a QSF. Several tests are adapted from the
website's test_v3_survey_draft.py."""

from __future__ import annotations

import base64
import copy
import json
import tempfile
from pathlib import Path

import pytest

import surveytwin as st
from fakes import accept_all_images, scripted_model
from qsf_writer import write_qsf
from surveytwin import builder
from surveytwin.prompts.instrument import collect_instrument_questions, render_qsf_prompt_sequence

ALL_TYPES = [
    {"id": "Q1", "type": "instruction", "text": "You will see a new product concept."},
    {"id": "Q2", "type": "single_choice", "text": "First reaction?", "options": ["Negative", "Neutral", "Positive"], "required": True},
    {"id": "Q3", "type": "multi_select", "text": "Which features matter?", "options": ["Price", "Speed", "Privacy", "Design"], "min_selections": 1, "max_selections": 2, "required": True},
    {"id": "Q4", "type": "scale", "text": "It is useful.", "options": builder.SCALE_PRESETS["agreement_5"]},
    {"id": "Q5", "type": "grid", "text": "Rate each statement.", "rows": ["Easy to use", "Trustworthy"], "options": ["Low", "Medium", "High"]},
    {"id": "Q6", "type": "open_text", "text": "Why?", "min_chars": 5, "max_chars": 200},
    {"id": "Q7", "type": "number", "text": "Price you would pay?", "min_value": 0, "max_value": 100, "decimals": 0, "required": True},
]


def draft(questions=None, title="Concept test"):
    return {"title": title, "questions": copy.deepcopy(ALL_TYPES if questions is None else questions)}


def runtime(survey):
    return {q["id"]: q for q in collect_instrument_questions(survey.template["Elements"])}


# ---- the draft format ----------------------------------------------------------------------


def test_frontend_payload_round_trip():
    payload = {
        "title": "Self-checkout attitudes",
        "questions": [
            {"id": "Q1", "type": "instruction", "text": "Look at this store layout.", "required": False, "options": [], "rows": [],
             "image_id": "img_3f1c0a", "image_name": "layout.png", "image_bytes": 482113},
            {"id": "Q2", "type": "multi_select", "text": "Which lanes have you used?", "required": True,
             "options": ["Self-checkout", "Staffed lane"], "rows": [], "min_selections": 1, "max_selections": None},
            {"id": "Q3", "type": "number", "text": "Minutes you would wait?", "required": False, "options": [], "rows": [],
             "min_value": 0, "max_value": 100, "decimals": 0},
        ],
    }
    d = builder.SurveyDraft.from_dict(payload)
    out = d.to_dict()
    assert list(out["questions"][0]) == list(builder.draft.DRAFT_FIELDS)  # all 18 fields, the website's order
    assert out["questions"][1]["min_selections"] == 1 and out["questions"][1]["step"] is None
    assert builder.SurveyDraft.from_dict(out).to_dict() == out
    assert json.loads(json.dumps(out)) == out


def test_lenient_values_like_the_website_model():
    d = builder.SurveyDraft.from_dict({"questions": [{"type": "open_text", "text": "x", "required": "false", "min_chars": "3", "max_chars": 10.0}]})
    q = d.questions[0]
    assert q.required is False and q.min_chars == 3 and q.max_chars == 10


def test_unknown_fields_are_reported_not_used():
    issues = builder.validate_draft({"questions": [{"type": "open_text", "text": "x", "colour": "red"}], "theme": "dark"})
    assert {(i.field, i.severity) for i in issues} == {("colour", "warning"), ("theme", "warning")}


# ---- validation ----------------------------------------------------------------------------


def codes(issues):
    return {(i.code, i.field) for i in issues if i.severity == "error"}


def test_structural_errors_name_the_question_and_field():
    issues = builder.validate_draft(draft([
        {"id": "Q1", "type": "single_choice", "text": "", "options": ["A"]},
        {"id": "Q2", "type": "multi_select", "text": "Pick", "options": ["A", "B"], "min_selections": 2, "max_selections": 1},
        {"id": "Q3", "type": "number", "text": "Count", "min_value": 10, "max_value": 5},
        {"id": "Q4", "type": "grid", "text": "Rate", "rows": ["x", "X"], "options": ["lo", "hi"]},
    ]))
    by = {(i.question_id, i.code, i.field) for i in issues}
    assert ("Q1", "text.empty", "text") in by
    assert ("Q1", "options.too_few", "options") in by
    assert ("Q2", "min_selections.above_max", "min_selections") in by
    assert ("Q3", "min_value.not_below_max", "min_value") in by
    assert ("Q4", "rows.duplicate", "rows") in by
    dup = next(i for i in issues if i.code == "rows.duplicate")
    assert dup.item_index == 1 and dup.question_index == 3
    assert set(dup.to_dict()) == {"question_id", "question_index", "severity", "message", "field", "item_index", "code"}


def test_website_messages_are_kept():
    issues = builder.validate_draft(draft([{"id": "Q1", "type": "single_choice", "text": "x", "options": ["A", "a"]}]))
    assert "Options must be unique." in {i.message for i in issues}
    empty = builder.validate_draft({"title": "t", "questions": []})
    assert [i.message for i in empty] == ["The survey has no questions."]
    only_instruction = builder.validate_draft(draft([{"id": "Q1", "type": "instruction", "text": "Read this."}]))
    assert "The survey has no answerable questions." in {i.message for i in only_instruction}


def test_single_answerable_question_is_valid():
    assert builder.validate_draft(draft([{"type": "open_text", "text": "Anything else?"}])) == []


def test_one_sided_number_range_is_checked():
    issues = builder.validate_draft(draft([{"id": "Q1", "type": "number", "text": "x", "min_value": 150}]))
    assert any("unset maximum means 100" in i.message for i in issues)


def test_wrong_kinds_of_values():
    issues = builder.validate_draft(draft([{"id": "Q1", "type": "number", "text": "x", "decimals": 7, "step": 0}]))
    assert codes(issues) >= {("field.invalid", "decimals"), ("field.invalid", "step")}
    issues = builder.validate_draft(draft([{"id": "Q1", "type": "ranking", "text": "x"}]))
    assert ("type.unknown", "type") in codes(issues)


def test_ids_duplicate_reserved_and_column_clash():
    issues = builder.validate_draft(draft([
        {"id": "Q1", "type": "open_text", "text": "a"},
        {"id": "Q1", "type": "open_text", "text": "b"},
        {"id": "QID9", "type": "open_text", "text": "c"},
        {"id": "Q4", "type": "multi_select", "text": "d", "options": ["x", "y"]},
        {"id": "Q4_2", "type": "open_text", "text": "e"},
    ]))
    assert {c for c, f in codes(issues) if f == "id"} == {"id.duplicate", "id.reserved", "id.column_clash"}


def test_website_policy_is_opt_in():
    long_draft = draft([{"type": "open_text", "text": f"Q {i}?"} for i in range(41)], title="")
    assert builder.validate_draft(long_draft) == []
    issues = builder.validate_draft(long_draft, policy=builder.WEBSITE_POLICY)
    assert {i.code for i in issues} >= {"survey.too_many", "title.required"}
    demographic = draft([{"type": "single_choice", "text": "What is your age?", "options": ["<30", "30+"]}])
    assert builder.validate_draft(demographic) == []
    warnings = builder.validate_draft(demographic, policy=builder.WEBSITE_POLICY)
    assert [i.code for i in warnings] == ["text.demographic"] and warnings[0].severity == "warning"
    agency = draft([{"type": "open_text", "text": "Tell me about your agency."}])
    assert builder.validate_draft(agency, policy=builder.WEBSITE_POLICY) == []  # whole words only


def test_assign_ids_keeps_given_ids_and_fills_blanks():
    d = builder.assign_ids(draft([
        {"id": "", "type": "open_text", "text": "a"},
        {"id": "Q1", "type": "open_text", "text": "b"},
        {"id": "Q7", "type": "open_text", "text": "c"},
        {"type": "open_text", "text": "d"},
    ]))
    assert [q.id for q in d.questions] == ["Q2", "Q1", "Q7", "Q3"]  # a blank never takes Q1 from a later question


# ---- building ------------------------------------------------------------------------------


def test_build_survey_expands_to_the_expected_runtime_questions():
    survey = builder.build_survey(draft())
    assert survey.kind == "builder" and survey.qsf is None
    units = runtime(survey)
    assert "QID1" not in units  # the instruction has nothing to answer
    assert set(units) == {"QID2", "QID3_1", "QID3_2", "QID3_3", "QID3_4", "QID4", "QID5_1", "QID5_2", "QID6", "QID7"}
    assert units["QID2"]["options"] == ["Negative", "Neutral", "Positive"] and units["QID2"]["force_response"] is True
    assert units["QID4"]["type"] == "single_choice"
    assert units["QID5_1"]["type"] == "matrix" and units["QID5_1"]["options"] == ["Low", "Medium", "High"]
    assert units["QID3_1"]["multi_answer_min_choices"] == 1 and units["QID3_1"]["multi_answer_max_choices"] == 2
    assert units["QID6"]["text_min_chars"] == 5 and units["QID6"]["text_max_chars"] == 200
    assert units["QID7"]["type"] == "numeric" and units["QID7"]["numeric_min"] == 0 and units["QID7"]["numeric_max"] == 100


def test_grid_stays_one_matrix_question():
    survey = builder.build_survey(draft())
    grid = survey.questions["QID5"]
    assert grid.type == "Matrix" and grid.export_tag == "Q5" and grid.rows == ["Easy to use", "Trustworthy"]


def test_ids_become_export_tags():
    survey = builder.build_survey(draft())
    assert [(q.id, q.export_tag) for q in survey.questions.values()] == [(f"QID{i}", f"Q{i}") for i in range(1, 8)]


def test_constraints_render_in_prompts():
    survey = builder.build_survey(draft())
    config = st.configure(bank="examples/example-bank", sample_size=1, model=scripted_model())
    text = st.plan(survey, config).preview_prompt().text
    for phrase in ("Select between 1 and 2 options", "Response required.", "between 5 and 200 characters", "between 0 and 100"):
        assert phrase in text


def test_instruction_text_appears_in_the_prompt_sequence():
    survey = builder.build_survey(draft())
    assert "You will see a new product concept." in render_qsf_prompt_sequence(survey.template["Elements"])


def test_build_refuses_an_invalid_draft_with_its_issues():
    with pytest.raises(st.DraftError) as info:
        builder.build_survey(draft([{"id": "Q1", "type": "single_choice", "text": "x", "options": ["only"]}]))
    assert [(i.question_id, i.field) for i in info.value.issues if i.severity == "error"] == [("Q1", "options")]
    assert "Choice questions need at least 2 options." in str(info.value)


def test_rationale_never_reaches_the_template():
    d = draft()
    d["questions"][1]["rationale"] = "Measures first impressions."
    survey = builder.build_survey(d)
    assert "first impressions" not in json.dumps(survey.template)
    assert builder.to_draft(survey).questions[1].rationale == "Measures first impressions."


def test_the_draft_passed_in_is_not_changed():
    d = draft([{"type": "open_text", "text": "x"}])
    before = copy.deepcopy(d)
    builder.build_survey(d)
    assert d == before


def test_block_id_and_name():
    survey = builder.build_survey(draft(), block_id="BL_MAIN", block_name="Main block")
    assert [(b.block_id, b.name) for b in survey.preview_blocks()] == [("BL_MAIN", "Main block")]


def test_check_and_preview_of_a_built_survey():
    survey = builder.build_survey(draft())
    check = survey.check()
    assert check.mode == "static" and check.ok and check.findings == []
    blocks = survey.preview_blocks()
    assert blocks[0].answerable == 6 and blocks[0].question_counts == {"DB": 1, "MC": 3, "Matrix": 1, "TE": 1, "Slider": 1}


# ---- images --------------------------------------------------------------------------------


def test_image_resolver_attaches_question_media():
    d = draft()
    d["questions"][0].update({"image_id": "img_abc", "image_name": "poster.png"})
    survey = builder.build_survey(d, image_resolver=lambda ref: "data:image/png;base64,ZZ" if ref == "img_abc" else None)
    # the file name is not sent to the twin: it stays in the draft
    assert survey.questions["QID1"].media == [{"kind": "image", "src": "data:image/png;base64,ZZ"}]
    assert builder.to_draft(survey).questions[0].image_name == "poster.png"
    assert survey.unresolved_media == []


def test_image_only_instruction_gets_placeholder_text():
    d = draft([{"type": "instruction", "text": "", "image_id": "https://img.example/ad.png"}, {"type": "open_text", "text": "Thoughts?"}])
    assert builder.validate_draft(d) == []
    survey = builder.build_survey(d)
    assert survey.questions["QID1"].text == "[Image stimulus attached]"


def test_default_resolver_urls_data_uris_and_files(tmp_path):
    png = tmp_path / "ad.png"
    png.write_bytes(b"\x89PNG fake")
    assert builder.default_image_resolver("https://img.example/a.png") == "https://img.example/a.png"
    assert builder.default_image_resolver("data:image/gif;base64,R0") == "data:image/gif;base64,R0"
    assert builder.default_image_resolver(str(png)) == "data:image/png;base64," + base64.b64encode(b"\x89PNG fake").decode()
    assert builder.default_image_resolver("img_not_mine") is None


def test_unresolved_image_is_kept_visible_and_stops_the_run(mini_bank):
    d = draft([{"type": "instruction", "text": "Look.", "image_id": "img_missing", "image_name": "a.png"}, {"type": "open_text", "text": "Thoughts?"}])
    survey = builder.build_survey(d)
    assert survey.unresolved_media[0]["reference"] == "img_missing"
    check = survey.check()
    assert not check.ok and [i["status"] for i in check.images] == ["unresolved"]
    config = st.configure(bank=mini_bank, sample_size=1, model=scripted_model())
    with pytest.raises(st.ImagesRefused, match="img_missing"):
        st.run(st.plan(survey, config))
    accepted = st.configure(bank=mini_bank, sample_size=1, model=scripted_model(), accept_missing_images=True)
    results = st.run(st.plan(survey, accepted))
    assert "img_missing" in results.provenance["images_left_out"]
    assert any("not found" in w for w in results.warnings)


def test_a_failing_resolver_counts_as_missing():
    def broken(ref):
        raise OSError("storage offline")

    d = draft([{"type": "open_text", "text": "x", "image_id": "img_1"}])
    survey = builder.build_survey(d, image_resolver=broken)
    assert "storage offline" in survey.unresolved_media[0]["detail"]


# ---- the same pipeline as a QSF --------------------------------------------------------------


def test_built_survey_runs_and_exports_with_its_ids(mini_bank, tmp_path):
    survey = builder.build_survey(draft())
    config = st.configure(bank=mini_bank, sample_size=2, seed=3, model=scripted_model())
    results = st.run(st.plan(survey, config))
    assert results.provenance["survey_kind"] == "builder"
    out = results.save(tmp_path / "run")
    import csv

    rows = list(csv.reader((out / "responses_labels.csv").open(encoding="utf-8")))
    assert [c for c in rows[0] if c.startswith("Q")] == ["Q2", "Q3_1", "Q3_2", "Q3_3", "Q3_4", "Q4", "Q5_1", "Q5_2", "Q6", "Q7_1"]
    assert '{"ImportId":"QID6_TEXT"}' in rows[2]


def test_equivalent_builder_and_qsf_surveys_match(tmp_path):
    built = builder.build_survey(draft())
    read = st.read_qsf(write_qsf("Concept test", ALL_TYPES))
    assert collect_instrument_questions(built.template["Elements"]) == collect_instrument_questions(read.template["Elements"])
    config = st.configure(bank="examples/example-bank", sample_size=3, seed=4, model=scripted_model())
    plan_b, plan_q = st.plan(built, config), st.plan(read, config)
    assert [plan_b.preview_prompt(t.id).text for t in plan_b.twins] == [plan_q.preview_prompt(t.id).text for t in plan_q.twins]
    res_b, res_q = st.run(plan_b), st.run(plan_q)
    assert res_b.rows == res_q.rows
    out_b, out_q = res_b.save(tmp_path / "b"), res_q.save(tmp_path / "q")
    for name in ("responses_labels.csv", "responses_values.csv", "responses_repaired.csv", "long.csv", "wide.csv"):
        assert (out_b / name).read_bytes() == (out_q / name).read_bytes(), name
    for folder in ("answers", "questions"):
        assert all((out_b / folder / f.name).read_bytes() == f.read_bytes() for f in (out_q / folder).iterdir())
    report_b = json.loads((out_b / "report.json").read_text(encoding="utf-8"))
    report_q = json.loads((out_q / "report.json").read_text(encoding="utf-8"))
    for section in ("fidelity", "distributions", "coverage", "validity"):
        assert report_b[section] == report_q[section], section


def test_plan_head_and_staged_mode_work_on_a_built_survey():
    survey = builder.build_survey(draft())
    config = st.configure(bank="examples/example-bank", sample_size=3, seed=4, model=scripted_model())
    plan = st.plan(survey, config, mode="staged")
    assert plan.mode == "staged"
    results = st.run(plan.head(1), image_checker=accept_all_images)
    assert results.answers


def test_command_line_accepts_a_draft_file(tmp_path, capsys):
    from surveytwin.cli import main

    path = tmp_path / "draft.json"
    path.write_text(json.dumps(draft()), encoding="utf-8")
    assert main(["check", str(path)]) == 0
    assert "Execution mode: static" in capsys.readouterr().out
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps(draft([{"type": "single_choice", "text": "x", "options": ["one"]}])), encoding="utf-8")
    assert main(["check", str(bad)]) == 2
    assert "Choice questions need at least 2 options." in capsys.readouterr().err
