"""Embedded data and random values: the survey keeps each
rule, a twin's run keeps the values it was given, and static and staged runs
resolve both the same way."""

from __future__ import annotations

import json

import pytest

import surveytwin as st
from fakes import accept_all_images
from flow_kit import answering, block, branch, embedded, mc, qsf, te, when, when_field


def run(survey, mini_bank, mode, *, n=3, rule=lambda q: None, embedded_data=None):
    config = st.configure(bank=mini_bank, sample_size=n, seed=5, model=answering(rule), embedded_data=embedded_data)
    return st.run(st.plan(survey, config, mode=mode), image_checker=accept_all_images)


def by_twin(results):
    out: dict[str, dict[str, dict]] = {}
    for row in results.rows:
        out.setdefault(row["persona_id"], {})[row["question_id"]] = row
    return out


def both_modes(survey, mini_bank, **kwargs):
    static, staged = (by_twin(run(survey, mini_bank, mode, **kwargs)) for mode in ("static", "staged"))
    return static, staged


def texts(rows):
    return {qid: row["question_text"] for qid, row in rows.items()}


def test_a_chained_field_holds_the_value_it_quotes_in_both_modes(mini_bank):
    blocks = {"BL_A": ("A", ["QID1"])}
    questions = {"QID1": mc("QID1", "Derived ${e://Field/derived}", ["A ${e://Field/derived}", "B"])}
    survey = st.read_qsf(qsf(blocks, questions, [embedded(base="30"), embedded(derived="${e://Field/base}"), block("BL_A")]))
    static, staged = both_modes(survey, mini_bank)
    assert static == staged or {k: texts(v) for k, v in static.items()} == {k: texts(v) for k, v in staged.items()}
    for rows in static.values():
        assert rows["QID1"]["question_text"] == "Derived 30" and rows["QID1"]["answer_label"] == "A 30"
        assert rows["QID1"]["embedded_data"]["derived"] == "30"


def test_a_field_set_later_is_blank_before_it_is_set_and_overwrites_show_in_order(mini_bank):
    blocks = {"BL_A": ("A", ["QID1"]), "BL_B": ("B", ["QID2"])}
    questions = {"QID1": te("QID1", "Arm ${e://Field/arm}; later [${e://Field/later}]"), "QID2": te("QID2", "Arm ${e://Field/arm}; later ${e://Field/later}")}
    flow = [embedded(arm="control"), block("BL_A"), embedded(arm="treated", later="L"), block("BL_B")]
    survey = st.read_qsf(qsf(blocks, questions, flow))
    static, staged = both_modes(survey, mini_bank)
    for rows in list(static.values()) + list(staged.values()):
        assert texts(rows) == {"QID1": "Arm control; later []", "QID2": "Arm treated; later L"}


def test_an_answer_dependent_assignment_is_piped_later(mini_bank):
    blocks = {"BL_A": ("A", ["QID1"]), "BL_B": ("B", ["QID2"])}
    questions = {"QID1": mc("QID1", "Pick a fruit.", ["Apple", "Banana"]), "QID2": te("QID2", "You chose ${e://Field/fruit}.")}
    flow = [block("BL_A"), embedded(fruit="${q://QID1/ChoiceGroup/SelectedChoices}"), block("BL_B")]
    results = run(st.read_qsf(qsf(blocks, questions, flow)), mini_bank, "auto", rule=lambda q: "Banana" if q.get("options") else None)
    for rows in by_twin(results).values():
        assert rows["QID2"]["question_text"] == "You chose Banana."
    assert all(a["assignments"][-1] == {"source": "flow", "flow_id": a["assignments"][-1]["flow_id"], "field": "fruit", "value": "Banana"} for a in results.audit)


def test_outside_values_are_used_counted_by_check_and_a_misspelled_key_is_named(mini_bank):
    blocks = {"BL_A": ("A", ["QID1"])}
    questions = {"QID1": te("QID1", "Hello ${e://Field/panel}")}
    survey = st.read_qsf(qsf(blocks, questions, [block("BL_A")]))
    assert any("'panel'" in f.message for f in survey.check().findings)
    check = survey.check(embedded_data={"panel": "prolific", "pannel": "x"})
    assert not any("'panel' is read" in f.message for f in check.findings)
    assert any("supplies 'pannel'" in f.message for f in check.findings)
    config = st.configure(bank=mini_bank, sample_size=2, seed=5, model=answering(lambda q: None), embedded_data={"panel": "prolific", "pannel": "x"})
    plan = st.plan(survey, config)
    assert any("'pannel'" in w or "pannel" in w for w in plan.warnings)
    results = st.run(plan, image_checker=accept_all_images)
    assert {r["question_text"] for r in results.rows} == {"Hello prolific"}
    assert results.audit[0]["assignments"][0] == {"source": "outside", "flow_id": "", "field": "panel", "value": "prolific"}


def test_a_random_value_assigned_once_is_the_same_everywhere_and_in_both_modes(mini_bank):
    blocks = {"BL_A": ("A", ["QID1"]), "BL_B": ("B", ["QID2"])}
    questions = {"QID1": te("QID1", "Your offer is ${e://Field/offer}."), "QID2": te("QID2", "Again: ${e://Field/offer}; a new roll ${rand://int/1:1000}")}
    survey = st.read_qsf(qsf(blocks, questions, [embedded(offer="${rand://int/10:99}"), block("BL_A"), block("BL_B")]))
    static, staged = both_modes(survey, mini_bank, n=4)
    assert {k: texts(v) for k, v in static.items()} == {k: texts(v) for k, v in staged.items()}
    for rows in static.values():
        offer = rows["QID1"]["embedded_data"]["offer"]
        assert rows["QID1"]["question_text"] == f"Your offer is {offer}." and rows["QID2"]["question_text"].startswith(f"Again: {offer}; a new roll ")
    assert len({rows["QID1"]["embedded_data"]["offer"] for rows in static.values()}) > 1  # drawn per twin
    assert "${rand://int/10:99}" in json.dumps(survey.template)  # the survey keeps the rule


@pytest.mark.parametrize("token, check_value", [("${rand://bool}", lambda v: v in {"0", "1"}),
                                                ("${rand://float/0:1}", lambda v: len(v.split(".")[1]) == 13)])
def test_random_formats_follow_qualtrics(mini_bank, token, check_value):
    survey = st.read_qsf(qsf({"BL_A": ("A", ["QID1"])}, {"QID1": te("QID1", "x ${e://Field/r}")}, [embedded(r=token), block("BL_A")]))
    results = run(survey, mini_bank, "auto", n=4)
    assert all(check_value(r["embedded_data"]["r"]) for r in results.rows)


def test_random_tokens_that_cannot_be_drawn_are_reported_before_the_run(mini_bank):
    survey = st.read_qsf(qsf({"BL_A": ("A", ["QID1"])}, {"QID1": te("QID1", "A ${rand://pick/a,b} B ${rand://int/5}")}, [block("BL_A")]))
    findings = [f for f in survey.check().findings if f.level == "Not supported" and f.feature == "piped_text.random"]
    assert [f.source for f in findings] == ["SQ QID1", "SQ QID1"]
    config = st.configure(bank=mini_bank, sample_size=1, seed=5, model=answering(lambda q: None))
    assert any("cannot be drawn" in w for w in st.plan(survey, config).warnings)


def test_static_runs_decide_embedded_logic_as_staged_runs_do(mini_bank):
    """GreaterThanOrEqual, Empty and numeric EqualTo are Qualtrics' names; the static
    evaluator did not know the first two and compared EqualTo as text."""
    blocks = {"BL_A": ("A", ["QID1"]), "BL_B": ("B", ["QID2"]), "BL_C": ("C", ["QID3"]), "BL_D": ("D", ["QID4"])}
    questions = {q: te(q, f"{q}?") for q in ("QID1", "QID2", "QID3", "QID4")}
    ge = when_field("score", "70")
    ge["0"]["0"]["Operator"] = "GreaterThanOrEqual"
    empty = when_field("missing", "")
    empty["0"]["0"]["Operator"] = "Empty"
    flow = [embedded(score="75", level="2.0"), block("BL_A"),
            branch(ge, block("BL_B")), branch(empty, block("BL_C")), branch(when_field("level", "2"), block("BL_D"))]
    survey = st.read_qsf(qsf(blocks, questions, flow))
    assert survey.check().mode == "static"
    static, staged = both_modes(survey, mini_bank)
    for rows in list(static.values()) + list(staged.values()):
        assert sorted(rows) == ["QID1", "QID2", "QID3", "QID4"]


def test_panel_and_link_fields_are_declared_not_set():
    data = qsf({"BL_A": ("A", ["QID1"])}, {"QID1": te("QID1", "ID ${e://Field/PROLIFIC_PID}")}, [block("BL_A")])
    flow = next(e for e in data["SurveyElements"] if e["Element"] == "FL")["Payload"]["Flow"]
    flow.insert(0, {"Type": "EmbeddedData", "FlowID": "FL_9", "EmbeddedData": [
        {"Description": "PROLIFIC_PID", "Type": "Recipient", "Field": "PROLIFIC_PID", "VariableType": "String", "DataVisibility": [], "AnalyzeText": False},
        {"Description": "arm", "Type": "Custom", "Field": "arm", "VariableType": "Nominal", "DataVisibility": [], "Value": "A"}]})
    survey = st.read_qsf(data)
    element = next(e for e in survey.template["Elements"] if e.get("ElementType") == "EmbeddedData")
    assert element["Declared"][0]["Field"] == "PROLIFIC_PID" and "Value" not in element["Declared"][0]
    assert element["EmbeddedData"] == [{"Field": "arm", "Value": "A", "Description": "arm", "Type": "Custom", "VariableType": "Nominal", "DataVisibility": []}]
    fields = {f.name: f for f in survey.embedded_fields}
    assert fields["PROLIFIC_PID"].source == "from a panel or the survey link" and fields["arm"].variable_type == "Nominal"
    finding = next(f for f in survey.check().findings if "PROLIFIC_PID" in f.message)
    assert finding.level == "Partial" and "survey link" in finding.message


def test_a_saved_run_keeps_the_rules_and_a_rerun_shows_what_the_twin_saw(mini_bank, tmp_path):
    blocks = {"BL_A": ("A", ["QID1"])}
    questions = {"QID1": mc("QID1", "Price ${e://Field/price}?", ["Buy at ${e://Field/price}", "Skip"])}
    survey = st.read_qsf(qsf(blocks, questions, [embedded(price="${rand://int/10:99}"), block("BL_A")]))
    config = st.configure(bank=mini_bank, sample_size=2, seed=5, model=answering(lambda q: "Nothing I recognise"))
    results = st.run(st.plan(survey, config), image_checker=accept_all_images)
    assert all(r["answer_status"] for r in results.rows)  # every answer flagged, so a rerun re-asks it
    results.save(tmp_path / "run")
    loaded = st.Results.load(tmp_path / "run")
    saved = json.dumps(loaded.run["definition"])
    # the rules, not one twin's values: the draw and the variable as references
    assert '{"scope": "random", "kind": "int", "min": 10, "max": 99}' in saved and '{"scope": "variable", "variable": "price"}' in saved
    seen = []

    def repair(request):
        seen.append(request.user)
        start = request.user.index('"options": [') + len('"options": [')
        first = json.loads("[" + request.user[start:request.user.index("]", start)] + "]")[0]
        qid = request.user[request.user.index('"id": "') + 7:].split('"')[0]
        return json.dumps({"answers": [{"question_id": qid, "answer_value": first, "answer_label": first, "answer_raw": first}]})

    repaired = loaded.rerun_flagged(model=st.Model.from_function(repair, name="repair"))
    assert seen and all("${" not in text for text in seen)  # options and context as the twin saw them
    for row in repaired.rows:
        price = row["embedded_data"]["price"]
        assert row["answer_repaired"] == f"Buy at {price}"


def test_a_survey_saves_and_rebuilds_without_losing_meaning(mini_bank, tmp_path):
    from conftest import qsf as fixture
    from surveytwin import builder

    read = st.read_qsf(fixture("kitchen_sink_supported.qsf")).drop_blocks(["BL_KS_MC_1"])
    edited = builder.update_question(read, "QID44", text="Would you like one more question?")
    built = builder.build_survey({"title": "T", "questions": [{"id": "Q1", "type": "open_text", "text": "Offer ${rand://int/1:9}?"}]})
    for survey in (edited, built):
        again = st.Survey.from_dict(json.loads(json.dumps(survey.to_dict())))
        assert again.checksum == survey.checksum and again.kind == survey.kind
        assert again.template == survey.template and again.exclude_blocks == survey.exclude_blocks and again.edits == survey.edits
    tampered = edited.to_dict()
    tampered["template"]["Metadata"]["SurveyName"] = "changed"
    with pytest.raises(st.SurveyError, match="checksum"):
        st.Survey.from_dict(tampered)

    config = st.configure(bank=mini_bank, sample_size=2, seed=5, model=answering(lambda q: None))
    first = st.plan(built, config)
    results = st.run(first, image_checker=accept_all_images)
    results.save(tmp_path / "run")
    assert "survey.json" in json.loads((tmp_path / "run" / "checksums.json").read_text())
    loaded = st.Results.load(tmp_path / "run")
    again = st.plan(loaded.survey, config)
    assert [again.preview_prompt(t.id).text for t in again.twins] == [first.preview_prompt(t.id).text for t in first.twins]


def _static_copy(template, *, seed=5, respondent=None, borrow=False):
    """One twin's static copy of a template (read into the standard form), and the
    piped text left unfilled."""
    from surveytwin.engine.definition import Definition
    from surveytwin.engine.static import survey_copies
    from surveytwin.spec.read_template import survey_from_template

    definition = Definition(survey_from_template(template, origin="qsf"))
    copies, stats = survey_copies(definition, 1, seed=seed, respondent_ids=[respondent] if respondent else None, borrow_unset=borrow)
    return copies[0], stats["unresolved_placeholders"]


def _shown(copy):
    """A copy's shown questions by ID (the standard question as the twin saw it)."""
    return {shown["question"]["id"]: shown["question"] for block in copy["blocks"] for shown in block["questions"]}


def _text(question):
    return question["content"][0]["text"]


def _asked(copy):
    from surveytwin.engine import render

    return {item["id"]: item for item in render.prompt_questions([shown for block in copy["blocks"] for shown in block["questions"]])}


def test_the_website_fallback_fills_only_the_fields_it_borrows():
    """With borrowing on (the website's static behavior, used for parity), a field
    never set on the twin's path takes the first value set anywhere. It then
    refilled every placeholder with the twin's final values, so a field set later
    showed its later value too early; only the borrowed field is filled now, and
    any other placeholder is blank, in options as in the question text."""
    answer_dependent = {"0": {"0": {"LogicType": "Question", "QuestionID": "QID9", "Operator": "Selected",
                                    "LeftOperand": "q://QID9/SelectableChoice/1", "Type": "Expression"}, "Type": "If"},
                        "Type": "BooleanExpression"}
    template = {"Elements": [
        {"ElementType": "Branch", "FlowID": "FL_1", "BranchLogic": answer_dependent,
         "Elements": [{"ElementType": "EmbeddedData", "FlowID": "FL_2", "EmbeddedData": [{"Field": "side", "Value": "a good idea"}]}]},
        {"ElementType": "Block", "BlockID": "BL_A", "Questions": [
            {"QuestionID": "QID1", "QuestionType": "MC", "QuestionText": "UBI is ${e://Field/side}; later [${e://Field/later}]; group [${e://Field/group}]",
             "Options": ["Pick ${e://Field/later}", "No"]}]},
        {"ElementType": "EmbeddedData", "FlowID": "FL_3", "EmbeddedData": [{"Field": "later", "Value": "L"}]},
        {"ElementType": "Block", "BlockID": "BL_B", "Questions": [{"QuestionID": "QID2", "QuestionType": "TE", "QuestionText": "Later ${e://Field/later}"}]},
    ]}
    participant, unresolved = _static_copy(template, seed=42, borrow=True)
    first, second = (block["questions"][0]["question"] for block in participant["blocks"])
    assert _text(first) == "UBI is a good idea; later []; group []" and [o["label"] for o in first["response"]["options"]] == ["Pick ", "No"]
    assert _text(second) == "Later L"
    assert participant["variables"]["side"] == "a good idea" and unresolved == ["e://Field/group"]
    blank, unresolved = _static_copy(template, seed=42, borrow=False)
    assert _text(blank["blocks"][0]["questions"][0]["question"]) == "UBI is ; later []; group []" and unresolved == []


# ---- review of c89fbf8: every string, piped condition values, repeated rows, trimmed names ----


def _t_static(template, respondent="p1"):
    return _static_copy(template, seed=5, respondent=respondent)[0]




def _t_staged(template, respondent="p1"):
    from surveytwin.engine.definition import Definition
    from surveytwin.engine.staged import StagedRuntime
    from surveytwin.spec.read_template import survey_from_template

    runtime = StagedRuntime(Definition(survey_from_template(template)), seed=5, persona_count=1)
    state = runtime.new_state(persona_id=respondent, persona_index=0)
    shown = {}
    while not state.done:
        stage = runtime.render_next_stage(state)
        if not stage.questions:
            break
        shown.update({q["id"]: q for q in stage.questions})
        runtime.apply_answers(state, questions=stage.questions, answers=[
            {"question_id": q["id"], **dict.fromkeys(("answer_value", "answer_label", "answer_raw"), "ok")} for q in stage.questions])
    return shown, state


def _t_ed(flow_id, *rows):
    return {"ElementType": "EmbeddedData", "FlowID": flow_id, "EmbeddedData": [{"Field": f, "Value": v} for f, v in rows]}


def _t_q(qid, text, **extra):
    return {"QuestionID": qid, "QuestionText": text, "QuestionType": "TE", "Settings": {"Selector": "SL"}, **extra}


def _t_gt(field, value):
    return {"0": {"0": {"LogicType": "EmbeddedField", "LeftOperand": field, "Operator": "GreaterThan", "RightOperand": value, "Type": "Expression"},
                  "Type": "If"}, "Type": "BooleanExpression"}


def test_static_copies_fill_every_string_a_twin_sees():
    """Default answers, write-in labels and side-by-side rows kept the raw token in
    static copies; a text question whose default pipes a field was taken as answered
    with nothing instead of asked with that default (the staged runtime asks it)."""

    template = {"Elements": [
        _t_ed("FL_1", ("answer", "7"), ("brand", "Acme")),
        {"ElementType": "Block", "BlockID": "BL_A", "Questions": [
            _t_q("QID1", "How many ideas?", DefaultChoices=["${e://Field/answer}"], DefaultEntries=[{"Kind": "text", "Value": "${e://Field/answer}"}]),
            {"QuestionID": "QID2", "QuestionText": "Which brand?", "QuestionType": "MC", "Options": ["${e://Field/brand}", "Other than ${e://Field/brand}"],
             "OptionsID": ["1", "2"], "Settings": {"Selector": "SAVR"}, "TextEntryOptions": [{"OptionID": "2", "OptionText": "Other than ${e://Field/brand}"}]},
            {"QuestionID": "QID3", "QuestionText": "Rate", "QuestionType": "SBS", "sub_questions": [
                {"id": "1", "text": "The ${e://Field/brand} logo", "QuestionID": "QID3#1", "Options": ["Bad", "Good"], "Rows": ["${e://Field/brand} red", "Blue"]}]},
            _t_q("QID4", "Later?", DefaultChoices=["[${e://Field/later}]"])]},
        _t_ed("FL_2", ("later", "L")),
    ]}
    participant = _t_static(template)
    q = _shown(participant)
    assert q["QID1"]["response"]["default"] == "7"
    assert [o["label"] for o in q["QID2"]["response"]["options"] if o.get("write_in")] == ["Other than Acme"]
    assert [r["label"] for r in q["QID3"]["response"]["fields"][0]["response"]["rows"]] == ["Acme red", "Blue"]
    assert q["QID4"]["response"]["default"] == "[${e://Field/later}]"  # no value there: kept, so the box is filled from outside
    asked = _asked(participant)
    assert not asked["QID1"].get("prefill_only") and asked["QID1"].get("default_text") == "7"
    assert asked["QID4"].get("prefill_only") is True


def test_a_conditions_value_that_pipes_a_field_is_decided_one_way():
    above = _t_gt("score", "${e://Field/cutoff}")
    for score, expected in (("30", ["QID1"]), ("70", ["QID1", "QID2", "QID3"])):
        template = {"Elements": [
            _t_ed("FL_1", ("score", score), ("cutoff", "50")),
            {"ElementType": "Block", "BlockID": "BL_A", "Questions": [_t_q("QID1", "Start"), _t_q("QID2", "Above the cutoff", DisplayLogic=above)]},
            {"ElementType": "Branch", "FlowID": "FL_2", "BranchLogic": above,
             "Elements": [{"ElementType": "Block", "BlockID": "BL_B", "Questions": [_t_q("QID3", "Above the cutoff too")]}]},
        ]}
        assert sorted(_shown(_t_static(template))) == expected, score
        assert sorted(_t_staged(template)[0]) == expected, score


def test_a_field_set_twice_in_one_element_draws_twice_the_same_in_both_modes():
    template = {"Elements": [
        _t_ed("FL_1", ("roll", "${rand://int/1:1000000}"), ("first", "${e://Field/roll}"), ("roll", "${rand://int/1:1000000}"), ("score ", "75")),
        {"ElementType": "Block", "BlockID": "BL_A", "Questions": [_t_q("QID1", "First ${e://Field/first}, second ${e://Field/roll}")]},
        {"ElementType": "Branch", "FlowID": "FL_2", "BranchLogic": _t_gt("score", "50"),
         "Elements": [{"ElementType": "Block", "BlockID": "BL_B", "Questions": [_t_q("QID2", "High score")]}]},
    ]}
    participant = _t_static(template)
    values = participant["variables"]
    assert values["first"] != values["roll"] and values["score"] == "75"
    static_text = _text(_shown(participant)["QID1"])
    shown, state = _t_staged(template)
    assert sorted(shown) == ["QID1", "QID2"] and not state.warnings
    assert shown["QID1"]["text"] == static_text  # the same draws in both modes
    assert [a["field"] for a in participant["assignments"]] == ["roll", "first", "roll", "score"]


def test_a_text_or_number_answer_is_re_asked_without_other_twins_answers_as_options():
    """With no option list, a rerun offered the answers other twins gave as options,
    so a twin re-answering a text or number question saw other twins' free text; and
    a recorded empty option list (a text question) was treated as missing."""
    from surveytwin.answers.rerun import build_rerun_context

    def row(pid, qid, value, qtype, status=None):
        r = {"persona_id": pid, "question_id": qid, "question_text": "?", "question_type": qtype,
             "answer_raw": value, "answer_value": value, "answer_label": value}
        if status:
            r.update({"answer_value": "", "answer_label": "", "answer_status": status})
        return r

    rows = [row("p0", "Q1", "", "open_text", "skipped"), row("p0", "Q2", "", "numeric", "skipped"), row("p0", "Q3", "", "single_choice", "skipped"),
            *[row(f"p{i}", "Q1", f"my own words {i}", "open_text") for i in (1, 2)],
            *[row(f"p{i}", "Q2", str(10 * i), "numeric") for i in (1, 2)],
            row("p1", "Q3", "Yes", "single_choice"), row("p2", "Q3", "No", "single_choice")]
    questions, _prior, flagged = build_rerun_context(rows, "p0", definition=None)
    options = {q["id"]: q["options"] for q in questions}
    assert flagged == ["Q1", "Q2", "Q3"] and options["Q1"] == [] and options["Q2"] == [] and set(options["Q3"]) == {"Yes", "No"}
    questions, _prior, _flagged = build_rerun_context(rows, "p0", definition=None, shown={"Q1": {"options": [], "context": ""}})
    assert {q["id"]: q["options"] for q in questions}["Q1"] == []


def test_a_random_value_in_a_conditions_value_is_compared_as_written_in_both_modes():
    always_one = {"0": {"0": {"LogicType": "EmbeddedField", "LeftOperand": "x", "Operator": "EqualTo", "RightOperand": "${rand://int/1:1}", "Type": "Expression"},
                        "Type": "If"}, "Type": "BooleanExpression"}
    template = {"Elements": [
        _t_ed("FL_1", ("x", "1")),
        {"ElementType": "Block", "BlockID": "BL_A", "Questions": [_t_q("QID1", "Start")]},
        {"ElementType": "Branch", "FlowID": "FL_2", "BranchLogic": always_one,
         "Elements": [{"ElementType": "Block", "BlockID": "BL_B", "Questions": [_t_q("QID2", "Drawn")]}]}]}
    assert sorted(_shown(_t_static(template))) == ["QID1"]
    assert sorted(_t_staged(template)[0]) == ["QID1"]
    from surveytwin.engine.analysis import analyze
    from surveytwin.spec.read_template import survey_from_template

    assert any("condition's value" in w for w in analyze(survey_from_template(template))["warnings"])


def test_loop_values_are_filled_in_every_string_of_a_static_loop():
    loop = {"Looping": "Static", "LoopingOptions": {"Static": {"1": {"1": "coffee"}, "2": {"1": "tea"}}, "Randomization": "None"}}
    question = {"QuestionID": "QID5", "QuestionText": "About ${lm://Field/1}", "QuestionType": "MC", "Options": ["Like", "Other than ${lm://Field/1}"],
                "OptionsID": ["1", "2"], "Settings": {"Selector": "SAVR"},
                "TextEntryOptions": [{"OptionID": "2", "OptionText": "Other than ${lm://Field/1}"}]}
    box = {"QuestionID": "QID6", "QuestionText": "Your ${lm://Field/1}", "QuestionType": "TE", "Settings": {"Selector": "SL"},
           "DefaultChoices": ["my ${lm://Field/1}"]}
    template = {"Elements": [{"ElementType": "Block", "BlockID": "BL_L", "Questions": [question, box], "Options": loop}]}
    participant, unresolved = _static_copy(template)
    rows = _shown(participant)
    assert [o["label"] for o in rows["1_QID5"]["response"]["options"] if o.get("write_in")] == ["Other than coffee"]
    assert rows["2_QID6"]["response"]["default"] == "my tea"
    assert unresolved == []


def test_a_text_box_prefilled_from_the_survey_link_is_not_asked_in_either_mode():
    """"Please insert your Prolific ID" (Misinfo): a twin has no ID, so the box is
    taken as filled from outside (blank) and not asked, in both modes."""

    prolific = _t_q("QID1", "Please, insert your Prolific ID", DefaultChoices=["${e://Field/PROLIFIC_PID}"],
                    DefaultEntries=[{"Kind": "text", "Value": "${e://Field/PROLIFIC_PID}"}])
    template = {"Elements": [{"ElementType": "Block", "BlockID": "BL_A", "Questions": [prolific, _t_q("QID2", "How old are you?")]}]}
    static = _asked(_t_static(template))
    assert static["QID1"].get("prefill_only") is True and static["QID1"].get("prefilled_answer") == ""
    shown, _state = _t_staged(template)
    assert shown["QID1"].get("prefill_only") is True and not shown["QID2"].get("prefill_only")
