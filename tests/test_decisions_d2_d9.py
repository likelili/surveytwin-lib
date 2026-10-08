"""Rules settled on 2026-09-30 for logic a twin cannot meet, repairs, recorded
codes, choice logic and the text carried into a stage (D2 to D9 below).

D2: Qualtrics' built-in conditions a twin cannot have (device type, location,
quota, panel) are ignored for twins, as if the logic did not have them. A question
gated only on them is shown; with an answer condition, the answer decides; a
screen-out on them never ends a survey. Static and staged runs agree.
"""

from __future__ import annotations

import surveytwin as st
from fakes import accept_all_images
from flow_kit import answering, block, branch, embedded, end, group, mc, qsf, te, when, when_field
from surveytwin.spec.read_template import survey_from_template


def device(operator: str = "Is", value: str = "mobile") -> dict:
    return {"LogicType": "DeviceType", "Operator": operator, "LeftOperand": value, "Type": "Expression"}


def only(condition: dict) -> dict:
    return {"0": {"0": condition, "Type": "If"}, "Type": "BooleanExpression"}


def with_answer(join: str) -> dict:
    logic = when("QID1", "SelectableChoice/1", "Selected")
    logic["0"]["1"] = dict(device(), Conjuction=join)
    return logic


def run(data, mini_bank, model, *, mode="auto", n=1):
    config = st.configure(bank=mini_bank, sample_size=n, seed=3, model=model)
    plan = st.plan(st.read_qsf(data), config, mode=mode)
    return plan, st.run(plan, image_checker=accept_all_images)


def asked(results):
    return [r["question_id"] for r in results.rows]


def test_a_question_gated_only_on_device_type_is_shown_in_both_modes(mini_bank):
    data = qsf({"BL_A": ("A", ["QID1", "QID2"])},
               {"QID1": te("QID1", "Your view?"), "QID2": te("QID2", "Mobile only?", display_logic=only(device()))},
               [block("BL_A")])
    for mode in ("static", "staged"):
        plan, results = run(data, mini_bank, answering(lambda q: None), mode=mode)
        assert asked(results) == ["QID1", "QID2"], mode  # static hid it, staged showed it
    assert any("ignored for twins" in f.message for f in st.read_qsf(data).check().findings)


def test_with_an_answer_condition_the_answer_decides(mini_bank):
    for join in ("And", "Or"):
        data = qsf({"BL_A": ("A", ["QID1", "QID2"])},
                   {"QID1": mc("QID1", "Do you shop online?", ["Yes", "No"]),
                    "QID2": te("QID2", "Where?", display_logic=with_answer(join))},
                   [block("BL_A")])
        for answer, expected in (("Yes", ["QID1", "QID2"]), ("No", ["QID1"])):
            _plan, results = run(data, mini_bank, answering(lambda q, a=answer: a if q["id"] == "QID1" else None))
            assert asked(results) == expected, (join, answer)  # "No or mobile" showed it in staged runs


def test_a_screen_out_on_device_type_never_ends_a_survey(mini_bank):
    blocks = {"BL_A": ("A", ["QID1"]), "BL_M": ("Main", ["QID3"])}
    questions = {"QID1": te("QID1", "Your view?"), "QID3": te("QID3", "Last?")}
    plan, results = run(qsf(blocks, questions, [block("BL_A"), branch(only(device()), end()), block("BL_M")]),
                        mini_bank, answering(lambda q: None))
    assert plan.mode == "staged" and asked(results) == ["QID1", "QID3"]


def test_a_screen_out_on_device_type_that_shows_a_message_first_is_skipped_whole(mini_bank):
    blocks = {"BL_A": ("A", ["QID1"]), "BL_S": ("Sorry", ["QID2"]), "BL_M": ("Main", ["QID3"])}
    questions = {"QID1": te("QID1", "Your view?"), "QID2": te("QID2", "Sorry, this study is for desktop only. Why?"),
                 "QID3": te("QID3", "Last?")}
    for body in ([block("BL_S"), end()], [embedded(term="mobile"), end()], [group(block("BL_S"), end())]):
        _plan, results = run(qsf(blocks, questions, [block("BL_A"), branch(only(device()), *body), block("BL_M")]),
                             mini_bank, answering(lambda q: None))
        assert asked(results) == ["QID1", "QID3"]  # every twin's survey ended here, after the message
        assert "term" not in (results.rows[-1].get("embedded_data") or {})


def test_device_or_false_is_switched_off_and_no_operator_is_called_unsupported():
    switched = {"0": {"0": dict(device(), Type="Expression"),
                      "1": {"LogicType": "BooleanValue", "Value": "false", "Type": "Expression", "Conjuction": "Or"},
                      "Type": "If"}, "Type": "BooleanExpression"}
    data = qsf({"BL_A": ("A", ["QID1", "QID2"])},
               {"QID1": te("QID1", "Your view?"), "QID2": te("QID2", "Parked", display_logic=switched)}, [block("BL_A")])
    messages = [f.message for f in st.read_qsf(data).check().findings]
    assert any("switched off for every respondent" in m and "QID2" in m for m in messages)
    assert not any("Unsupported logic operator" in m for m in messages)  # the condition is ignored, whatever its operator


def test_a_branch_gated_on_device_type_is_taken_in_both_modes(mini_bank):
    blocks = {"BL_A": ("A", ["QID1"]), "BL_D": ("Desktop", ["QID2"]), "BL_M": ("Main", ["QID3"])}
    questions = {"QID1": te("QID1", "Your view?"), "QID2": te("QID2", "Desktop follow-up?"), "QID3": te("QID3", "Last?")}
    flow = [block("BL_A"), branch(only(device("Is", "desktop")), block("BL_D")), block("BL_M")]
    for mode in ("static", "staged"):
        _plan, results = run(qsf(blocks, questions, flow), mini_bank, answering(lambda q: None), mode=mode)
        assert asked(results) == ["QID1", "QID2", "QID3"], mode  # static skipped the desktop block


# ---- D6: every loop row is asked ------------------------------------------------------------------


def test_every_row_of_a_long_loop_is_asked_in_both_modes(mini_bank):
    rows = {str(i): {"1": f"Item {i}"} for i in range(1, 13)}
    loop = {"Looping": "Static", "LoopingOptions": {"Static": rows, "Randomization": "None"}}
    data = qsf({"BL_L": ("Items", ["QID1"], loop)}, {"QID1": te("QID1", "About ${lm://Field/1}?")}, [block("BL_L")])
    for mode in ("static", "staged"):
        _plan, results = run(data, mini_bank, answering(lambda q: None), mode=mode)
        assert asked(results) == [f"{i}_QID1" for i in range(1, 13)], mode  # rows 11 and 12 were never asked


def test_the_plan_shows_how_many_loop_rows_a_twin_is_asked(mini_bank):
    rows = {str(i): {"1": f"Item {i}"} for i in range(1, 26)}
    loop = {"Looping": "Static", "LoopingOptions": {"Static": rows, "Randomization": "None"}}
    data = qsf({"BL_L": ("Items", ["QID1", "QID2"], loop)},
               {"QID1": te("QID1", "About ${lm://Field/1}?"), "QID2": te("QID2", "Why ${lm://Field/1}?")}, [block("BL_L")])
    the_plan, _results = run(data, mini_bank, answering(lambda q: None), mode="static")
    assert the_plan.loops == [{"block_id": "BL_L", "name": "Items", "rows": 25, "questions": 2, "answers_per_row": 2}]
    assert "loop: Items (BL_L) asks 25 rows of 2 questions (2 answers a row), up to 50 answers per twin" in str(the_plan)


# ---- D9: the text shown after the twin's last answer opens the next call ----------------------------


def db(qid: str, text: str) -> dict:
    return {"QuestionID": qid, "DataExportTag": qid, "QuestionText": text, "QuestionType": "DB", "Selector": "TB",
            "ChoiceOrder": [], "Validation": {"Settings": {"Type": "None"}}}




BRAND = {"QID1": mc("QID1", "Which brand do you use?", ["Acme", "Zeta"]),
         "QID2": db("QID2", "Your brand just doubled its price."),
         "QID3": te("QID3", "After reading the article, how likely are you to keep buying ${q://QID1/ChoiceGroup/SelectedChoices}?"),
         "QID9": db("QID9", "I agree to take part in this study.")}


def staged_prompts(data, mini_bank):
    import json as _json

    prompts: list[str] = []

    def reply(request):
        prompts.append(request.user)
        text = request.user
        start = text.rindex("Questions JSON:\n") + len("Questions JSON:\n")
        questions = _json.loads(text[start: text.index("\n\nRespond using JSON schema", start)])
        answers = []
        for q in questions:
            value = (q.get("options") or ["fine"])[0]
            answers.append({"question_id": q["id"], "answer_value": value, "answer_label": value, "answer_raw": value})
        return st.Reply(text=_json.dumps({"answers": answers}), input_tokens=1, output_tokens=1)

    _plan, results = run(data, mini_bank, st.Model.from_function(reply, name="rec"), mode="staged")
    return prompts, results


def test_an_article_shown_after_the_last_answer_opens_the_next_call(mini_bank):
    data = qsf({"BL_C": ("Consent", ["QID9"]), "BL_1": ("Brand", ["QID1"]), "BL_2": ("Article", ["QID2"]), "BL_3": ("Outcome", ["QID3"])},
               BRAND, [block("BL_C"), block("BL_1"), block("BL_2"), block("BL_3")])
    prompts, results = staged_prompts(data, mini_bank)
    assert len(prompts) == 2
    first, second = prompts
    assert "I agree to take part" in first and "doubled its price" in first
    # the outcome question comes with the article, labelled as before; the consent page is not repeated
    stage_text = second[second.index("Current stage survey text:"):]
    assert "doubled its price" in stage_text
    assert stage_text.index("doubled its price") < stage_text.index("Q1:") < stage_text.index("keep buying Acme")
    assert "I agree to take part" not in second
    assert [r["question_id"] for r in results.rows] == ["QID1", "QID3"]


def test_text_the_next_call_shows_anyway_is_not_shown_twice(mini_bank):
    data = qsf({"BL_A": ("A", ["QID1", "QID2", "QID3"])}, BRAND, [block("BL_A")])
    prompts, _results = staged_prompts(data, mini_bank)
    assert len(prompts) == 2
    assert prompts[1].count("doubled its price") == 1


def test_text_shown_before_the_last_answer_is_not_carried(mini_bank):
    later = te("QID4", "And ${q://QID3/ChoiceTextEntryValue}?")
    data = qsf({"BL_1": ("Brand", ["QID1"]), "BL_2": ("Article", ["QID2"]), "BL_3": ("Outcome", ["QID3"]), "BL_4": ("More", ["QID4"])},
               {**BRAND, "QID4": later}, [block("BL_1"), block("BL_2"), block("BL_3"), block("BL_4")])
    prompts, _results = staged_prompts(data, mini_bank)
    assert len(prompts) == 3
    assert "doubled its price" in prompts[1] and "doubled its price" not in prompts[2]


# ---- D7: display logic on single options, matrix statements and scale points ------------------------


def choice_payload(data, qid):
    return next(e for e in data["SurveyElements"] if e.get("PrimaryAttribute") == qid)["Payload"]


def options_offered(prompts, qid):
    import json as _json

    for text in prompts:
        start = text.rindex("Questions JSON:\n") + len("Questions JSON:\n")
        for q in _json.loads(text[start: text.index("\n\nRespond using JSON schema", start)]):
            if q["id"] == qid:
                return q.get("options")
    return None


def picking(prompts, choices):
    """A model that answers each question with ``choices[id]`` (or its first option) and keeps every prompt."""
    import json as _json

    def reply(request):
        prompts.append(request.user)
        text = request.user
        start = text.rindex("Questions JSON:\n") + len("Questions JSON:\n")
        answers = []
        for q in _json.loads(text[start: text.index("\n\nRespond using JSON schema", start)]):
            value = choices.get(q["id"]) or (q.get("options") or ["fine"])[0]
            answers.append({"question_id": q["id"], "answer_value": value, "answer_label": value, "answer_raw": value})
        return st.Reply(text=_json.dumps({"answers": answers}), input_tokens=1, output_tokens=1)

    return st.Model.from_function(reply, name="pick")


def values_row(results, tmp_path):
    import csv as _csv

    rows = list(_csv.reader(results.to_qualtrics_csv(tmp_path / "values.csv", values=True).open(encoding="utf-8")))
    return dict(zip(rows[0], rows[3]))


def fruit_survey(same_block=False):
    questions = {"QID1": mc("QID1", "Fruit?", ["Apple", "Pear"]), "QID2": mc("QID2", "More?", ["More apples", "More pears", "Neither"])}
    blocks = {"BL_A": ("A", ["QID1", "QID2"])} if same_block else {"BL_A": ("A", ["QID1"]), "BL_B": ("B", ["QID2"])}
    data = qsf(blocks, questions, [block(b) for b in blocks])
    choices = choice_payload(data, "QID2")["Choices"]
    choices["1"]["DisplayLogic"] = when("QID1", "SelectableChoice/1", "Selected")
    choices["2"]["DisplayLogic"] = when("QID1", "SelectableChoice/2", "Selected")
    return data


def test_an_option_shown_on_an_earlier_answer_is_offered_only_then(mini_bank, tmp_path):
    data = fruit_survey()
    assert st.read_qsf(data).check() is not None
    prompts: list[str] = []
    the_plan, results = run(data, mini_bank, picking(prompts, {"QID1": "Pear"}))
    assert the_plan.mode == "staged"  # options shown on an answer make the survey staged
    assert options_offered(prompts, "QID2") == ["More pears", "Neither"]
    assert [r["answer_label"] for r in results.rows] == ["Pear", "More pears"]
    assert values_row(results, tmp_path)["QID2"] == "2"  # the option's own code, not its place among those shown


def test_the_question_waits_for_an_answer_in_its_own_block(mini_bank):
    prompts: list[str] = []
    _plan, results = run(fruit_survey(same_block=True), mini_bank, picking(prompts, {"QID1": "Apple"}))
    assert len(prompts) == 2
    assert options_offered(prompts[1:], "QID2") == ["More apples", "Neither"]
    assert [r["answer_label"] for r in results.rows] == ["Apple", "More apples"]


def test_matrix_statements_shown_on_a_multi_select_answer(mini_bank, tmp_path):
    many = mc("QID1", "Which did you give to?", ["Red Cross", "Oxfam", "UNICEF"])
    many["Selector"] = "MAVR"
    matrix = {"QID2": {"QuestionID": "QID2", "DataExportTag": "QID2", "QuestionText": "How much do you trust each?", "QuestionType": "Matrix",
                       "Selector": "Likert", "SubSelector": "SingleAnswer", "Configuration": {"QuestionDescriptionOption": "UseText"},
                       "Validation": {"Settings": {"ForceResponse": "OFF", "Type": "None"}},
                       "Choices": {str(i): {"Display": n, "DisplayLogic": when("QID1", f"SelectableChoice/{i}", "Selected")}
                                   for i, n in enumerate(["Red Cross", "Oxfam", "UNICEF"], 1)},
                       "ChoiceOrder": [1, 2, 3], "Answers": {"1": {"Display": "Low"}, "2": {"Display": "High"}}, "AnswerOrder": [1, 2]}}
    data = qsf({"BL_A": ("A", ["QID1"]), "BL_B": ("B", ["QID2"])}, {"QID1": many, **matrix}, [block("BL_A"), block("BL_B")])
    prompts: list[str] = []
    _plan, results = run(data, mini_bank, picking(prompts, {"QID1_1": "Yes", "QID1_2": "No", "QID1_3": "Yes"}))
    assert [r["question_id"] for r in results.rows if r["question_id"].startswith("QID2")] == ["QID2_1", "QID2_3"]
    row = values_row(results, tmp_path)
    assert row["QID2_1"] and row["QID2_3"] and row["QID2_2"] == ""  # Oxfam's statement was not shown


def test_a_question_left_with_no_option_is_not_shown(mini_bank):
    data = fruit_survey()
    choice_payload(data, "QID2")["Choices"]["3"]["DisplayLogic"] = {"0": {"0": {"LogicType": "BooleanValue", "Value": "false", "Type": "Expression"},
                                                                          "Type": "If"}, "Type": "BooleanExpression"}
    choice_payload(data, "QID1")["Choices"]["3"] = {"Display": "Plum"}
    choice_payload(data, "QID1")["ChoiceOrder"] = [1, 2, 3]
    prompts: list[str] = []
    _plan, results = run(data, mini_bank, picking(prompts, {"QID1": "Plum"}))
    assert [r["question_id"] for r in results.rows] == ["QID1"]


def test_an_option_shown_on_embedded_data_is_decided_in_a_static_run(mini_bank, tmp_path):
    questions = {"QID1": mc("QID1", "Which offer?", ["Offer A", "Offer B", "No thanks"])}
    data = qsf({"BL_A": ("A", ["QID1"])}, questions, [embedded(arm="b"), block("BL_A")])
    choices = choice_payload(data, "QID1")["Choices"]
    choices["1"]["DisplayLogic"] = when_field("arm", "a")
    choices["2"]["DisplayLogic"] = when_field("arm", "b")
    prompts: list[str] = []
    the_plan, results = run(data, mini_bank, picking(prompts, {}))
    assert the_plan.mode == "static"
    assert options_offered(prompts, "QID1") == ["Offer B", "No thanks"]
    assert values_row(results, tmp_path)["QID1"] == "2"


# ---- D5: the values file codes the option chosen, from the options shown ----------------------------


def test_an_option_that_pipes_an_answer_is_coded_in_the_values_file(mini_bank, tmp_path):
    questions = {"QID1": te("QID1", "Which fee surprised you?"),
                 "QID2": mc("QID2", "How fair is it?", ["The ${q://QID1/ChoiceTextEntryValue} fee is fair", "It is not fair"])}
    payload = questions["QID2"]
    payload["RecodeValues"] = {"1": "5", "2": "9"}
    data = qsf({"BL_A": ("A", ["QID1"]), "BL_B": ("B", ["QID2"])}, questions, [block("BL_A"), block("BL_B")])
    prompts: list[str] = []
    _plan, results = run(data, mini_bank, picking(prompts, {"QID1": "booking"}))
    assert options_offered(prompts, "QID2") == ["The booking fee is fair", "It is not fair"]
    assert [a.code for a in results.answers] == ["", "5"]
    row = values_row(results, tmp_path)
    assert row["QID2"] == "5"  # held the option text: the options as written never match what the twin was shown


def test_a_rejected_answer_records_no_code(mini_bank, tmp_path):
    data = fruit_survey()
    prompts: list[str] = []
    _plan, results = run(data, mini_bank, picking(prompts, {"QID1": "Banana"}))
    assert [(a.status, a.code) for a in results.answers][0] == ("out_of_range", "")


# ---- D4: a rerun marks the answers an answer it repaired had shaped ---------------------------------


def test_a_rerun_marks_the_answers_given_to_text_from_the_repaired_answer(mini_bank, tmp_path):
    import json as _json

    questions = {"QID1": mc("QID1", "Which fruit?", ["Apple", "Banana"]),
                 "QID2": te("QID2", "Why do you like ${q://QID1/ChoiceGroup/SelectedChoices}?"),
                 "QID3": mc("QID3", "Anything else?", ["Yes", "No"], display_logic=when("QID1", "SelectableChoice/2", "Selected")),
                 "QID4": te("QID4", "Your age?")}
    data = qsf({"BL_A": ("A", ["QID1"]), "BL_B": ("B", ["QID2", "QID3", "QID4"])}, questions, [block("BL_A"), block("BL_B")])
    first = {"QID1": "Grape", "QID2": "sweet", "QID3": "No", "QID4": "40"}
    config = st.configure(bank=mini_bank, sample_size=1, seed=3, model=answering(lambda q: first.get(q["id"])))
    results = st.run(st.plan(st.read_qsf(data), config), image_checker=accept_all_images)
    assert [r["question_id"] for r in results.rows] == ["QID1", "QID2", "QID4"]  # Grape is no option: QID3 hidden
    results.save(tmp_path / "run")

    def repair(request):
        qid = request.user[request.user.index('"id": "') + 7:].split('"')[0]
        return _json.dumps({"answers": [{"question_id": qid, "answer_value": "Banana", "answer_label": "Banana", "answer_raw": "Banana"}]})

    repaired = st.Results.load(tmp_path / "run").rerun_flagged(model=st.Model.from_function(repair, name="repair"))
    marks = {a.question_id: a.shaped_by_repaired for a in repaired.answers}
    assert marks == {"QID1": [], "QID2": ["QID1"], "QID4": []}  # "Why do you like ?" was asked of Grape
    assert any("1 answer(s) were given to text" in w for w in repaired.warnings)
    long_rows = list(__import__("csv").DictReader((repaired.save(tmp_path / "fixed") / "long.csv").open(encoding="utf-8")))
    assert {r["question_id"]: r["shaped_by_repaired"] for r in long_rows} == {"QID1": "", "QID2": "QID1", "QID4": ""}


def test_what_shapes_each_question():
    from surveytwin.answers.repair_marks import answers_shaping
    from surveytwin.runtime.instrument_plan import parse_template

    questions = {"QID1": mc("QID1", "Fruit?", ["Apple", "Pear"]), "QID2": te("QID2", "Why ${e://Field/fruit}?"),
                 "QID3": te("QID3", "Shown on a branch"), "QID4": te("QID4", "Unrelated")}
    data = qsf({"BL_A": ("A", ["QID1"]), "BL_B": ("B", ["QID2"]), "BL_C": ("C", ["QID3"]), "BL_D": ("D", ["QID4"])}, questions,
               [block("BL_A"), embedded(fruit="${q://QID1/ChoiceGroup/SelectedChoices}"), block("BL_B"),
                branch(when("QID1", "SelectableChoice/1", "Selected"), block("BL_C")), block("BL_D")])
    assert answers_shaping(survey_from_template(parse_template(data, None))) == {"QID2": {"QID1"}, "QID3": {"QID1"}}


def test_the_check_names_each_loop_with_its_rows():
    rows = {str(i): {"1": f"Item {i}"} for i in range(1, 26)}
    loop = {"Looping": "Static", "LoopingOptions": {"Static": rows, "Randomization": "None"}}
    data = qsf({"BL_L": ("Items", ["QID1"], loop)}, {"QID1": te("QID1", "About ${lm://Field/1}?")}, [block("BL_L")])
    found = [f for f in st.read_qsf(data).check().findings if f.feature == "block_option.loop_and_merge"]
    assert any("Items (BL_L) asks 25 row(s) of 1 question(s) (1 answer(s) a row), up to 25 answers per twin" in f.message for f in found)


def test_the_loop_cost_counts_the_answers_a_row_asks_for(mini_bank):
    rows = {str(i): {"1": f"Item {i}"} for i in range(1, 4)}
    loop = {"Looping": "Static", "LoopingOptions": {"Static": rows, "Randomization": "None"}}
    matrix = {"QuestionID": "QID3", "DataExportTag": "QID3", "QuestionText": "Rate ${lm://Field/1}", "QuestionType": "Matrix",
              "Selector": "Likert", "SubSelector": "SingleAnswer", "Configuration": {"QuestionDescriptionOption": "UseText"},
              "Validation": {"Settings": {"ForceResponse": "OFF", "Type": "None"}},
              "Choices": {str(i): {"Display": f"Aspect {i}"} for i in range(1, 5)}, "ChoiceOrder": [1, 2, 3, 4],
              "Answers": {"1": {"Display": "Low"}, "2": {"Display": "High"}}, "AnswerOrder": [1, 2]}
    timing = {"QuestionID": "QID1", "DataExportTag": "QID1", "QuestionText": "Timing", "QuestionType": "Timing", "Selector": "PageTimer",
              "Choices": {"1": {"Display": "First Click"}}, "Validation": {"Settings": {"Type": "None"}}}
    off = te("QID4", "Parked", display_logic={"0": {"0": {"LogicType": "BooleanValue", "Value": "false", "Type": "Expression"},
                                                         "Type": "If"}, "Type": "BooleanExpression"})
    empty = {"Looping": "Static", "LoopingOptions": {"Static": {}, "Randomization": "None"}}
    data = qsf({"BL_L": ("Items", ["QID1", "QID2", "QID3", "QID4"], loop), "BL_E": ("Empty", ["QID5"], empty)},
               {"QID1": timing, "QID2": te("QID2", "About ${lm://Field/1}?"), "QID3": matrix, "QID4": off, "QID5": te("QID5", "Once")},
               [block("BL_L"), block("BL_E")])
    the_plan, results = run(data, mini_bank, answering(lambda q: None), mode="static")
    # the text question and the matrix's 4 statements, in each of 3 rows; the empty table asks its block once
    assert the_plan.loops == [{"block_id": "BL_L", "name": "Items", "rows": 3, "questions": 2, "answers_per_row": 5}]
    assert sum(1 for r in results.rows if r["question_id"] != "QID5") == 15


def test_a_stage_that_makes_no_model_call_keeps_the_carried_text_for_the_next(mini_bank):
    brand = mc("QID1", "Which brand do you use?", ["Acme", "Zeta"])
    article = db("QID2", "Your brand just doubled its price.")
    prolific = te("QID5", "Please enter your Prolific ID", display_logic=when("QID1", "SelectableChoice/1", "Selected"))
    outcome = te("QID3", "Thanks ${q://QID5/ChoiceTextEntryValue}. After reading the article, will you keep buying?")
    data = qsf({"BL_1": ("Brand", ["QID1"]), "BL_2": ("Article", ["QID2"]), "BL_P": ("ID", ["QID5"]), "BL_3": ("Outcome", ["QID3"])},
               {"QID1": brand, "QID2": article, "QID5": prolific, "QID3": outcome},
               [block("BL_1"), block("BL_2"), block("BL_P"), block("BL_3")])
    prompts, results = staged_prompts(data, mini_bank)
    assert [r["question_id"] for r in results.rows] == ["QID1", "QID5", "QID3"]
    assert len(prompts) == 2  # the ID box is filled without a call
    assert "doubled its price" in prompts[1][prompts[1].index("Current stage survey text:"):]  # it was lost at the ID stage


def test_carried_text_brings_its_image_once(mini_bank):
    article = db("QID2", "Your brand just doubled its price.")
    article["QuestionText"] = 'Your brand just doubled its price. <img src="https://example.org/ad.png">'
    data = qsf({"BL_1": ("Brand", ["QID1"]), "BL_2": ("Article", ["QID2"]), "BL_3": ("Outcome", ["QID3"])},
               BRAND, [block("BL_1"), block("BL_2"), block("BL_3")])
    choice_payload(data, "QID2")["QuestionText"] = article["QuestionText"]
    seen: list[list[str]] = []

    def reply(request):
        import json as _json
        seen.append(list(request.images or []))
        text = request.user
        start = text.rindex("Questions JSON:\n") + len("Questions JSON:\n")
        qs = _json.loads(text[start: text.index("\n\nRespond using JSON schema", start)])
        answers = [{"question_id": q["id"], "answer_value": (q.get("options") or ["fine"])[0], "answer_label": (q.get("options") or ["fine"])[0],
                    "answer_raw": "x"} for q in qs]
        return st.Reply(text=_json.dumps({"answers": answers}), input_tokens=1, output_tokens=1)

    config = st.configure(bank=mini_bank, sample_size=1, seed=3, model=st.Model.from_function(reply, name="img", images=True), carry_stage_images=True)
    st.run(st.plan(st.read_qsf(data), config, mode="staged"), image_checker=accept_all_images)
    assert len(seen) == 2 and len(seen[1]) == 1  # sent twice at the second stage


# ---- review of D7 --------------------------------------------------------------------------------


def _fruit_survey_read(same_block=True):
    return st.read_qsf(fruit_survey(same_block=same_block))


def test_the_builder_refuses_edits_that_would_hand_option_logic_to_other_options():
    import pytest
    from surveytwin import builder

    survey = _fruit_survey_read()
    with pytest.raises(st.UnsupportedEdit, match="display logic on its items"):
        builder.update_question(survey, "QID2", options=["More pears", "Neither"])
    with pytest.raises(st.UnsupportedEdit):
        builder.remove_questions(survey, ["QID1"])  # its options' logic reads QID1
    with pytest.raises(st.UnsupportedEdit):
        builder.reorder_questions(survey, ["QID2", "QID1"], block_id="BL_A")  # QID2's options read QID1


def test_option_logic_reading_a_question_the_survey_does_not_have_is_named():
    data = fruit_survey()
    choice_payload(data, "QID2")["Choices"]["3"]["DisplayLogic"] = when("QID77", "SelectableChoice/1", "Selected")
    messages = [f.message for f in st.read_qsf(data).check().findings if f.feature == "logic.missing_question"]
    assert any("QID77" in m for m in messages)


def test_text_entry_form_fields_follow_their_display_logic(mini_bank, tmp_path):
    form = {"QuestionID": "QID1", "DataExportTag": "QID1", "QuestionText": "Your details", "QuestionType": "TE", "Selector": "FORM",
            "Configuration": {"QuestionDescriptionOption": "UseText"}, "Validation": {"Settings": {"ForceResponse": "OFF", "Type": "None"}},
            "Choices": {"1": {"Display": "Name"}, "2": {"Display": "Company", "DisplayLogic": when_field("employed", "yes")},
                        "3": {"Display": "City"}}, "ChoiceOrder": [1, 2, 3]}
    data = qsf({"BL_A": ("A", ["QID1"])}, {"QID1": form}, [embedded(employed="no"), block("BL_A")])
    prompts: list[str] = []
    the_plan, results = run(data, mini_bank, picking(prompts, {}))
    assert the_plan.mode == "static"
    assert [r["question_id"] for r in results.rows] == ["QID1_1", "QID1_3"]  # the Company field was asked


def test_both_modes_draw_the_options_first_and_hide_after(mini_bank):
    options = ["A", "B", "C", "D", "E"]
    question = mc("QID1", "Pick one", options)
    question["Randomization"] = {"Type": "Subset", "TotalRandSubset": 3}
    data = qsf({"BL_A": ("A", ["QID1"])}, {"QID1": question}, [embedded(hide="yes"), block("BL_A")])
    choice_payload(data, "QID1")["Choices"]["1"]["DisplayLogic"] = when_field("hide", "no")
    counts = {}
    for mode in ("static", "staged"):
        for n in range(6):
            prompts: list[str] = []
            config = st.configure(bank=mini_bank, sample_size=1, seed=n, model=picking(prompts, {}))
            plan = st.plan(st.read_qsf(data), config, mode=mode)
            st.run(plan, image_checker=accept_all_images)
            offered = options_offered(prompts, "QID1")
            assert "A" not in offered
            counts.setdefault(mode, set()).add(len(offered))
    assert counts["static"] == counts["staged"] == {2, 3}  # static hid first, then drew 3 of the 4 left


def test_a_default_on_a_hidden_scale_point_is_not_offered():
    from surveytwin.runtime.choice_logic import hide_choices

    question = {"QuestionID": "QID1", "QuestionType": "Matrix", "Rows": ["a"], "Columns": ["Low", "Mid", "High"],
                "AnswerDisplayLogic": {"2": {"x": 1}}, "DefaultEntries": [{"ItemID": "1", "SubID": "2"}, {"ItemID": "1", "SubID": "3"}]}
    shown = hide_choices(question, lambda logic: False)
    assert shown["Columns"] == ["Low", "High"] and shown["ColumnsID"] == ["1", "3"]
    assert shown["DefaultEntries"] == [{"ItemID": "1", "SubID": "3"}]


def test_option_logic_reading_a_loop_value_is_decided_in_both_modes(mini_bank):
    rows = {"1": {"1": "Coffee"}, "2": {"1": "Tea"}}
    loop = {"Looping": "Static", "LoopingOptions": {"Static": rows, "Randomization": "None"}}
    question = mc("QID1", "How do you take ${lm://Field/1}?", ["With milk", "Black"])
    data = qsf({"BL_L": ("Drinks", ["QID1"], loop)}, {"QID1": question}, [embedded(drink="Tea"), block("BL_L")])
    choice_payload(data, "QID1")["Choices"]["1"]["DisplayLogic"] = {
        "0": {"0": {"LogicType": "EmbeddedField", "LeftOperand": "drink", "Operator": "EqualTo", "RightOperand": "${lm://Field/1}",
                    "Type": "Expression"}, "Type": "If"}, "Type": "BooleanExpression"}
    for mode in ("static", "staged"):
        prompts: list[str] = []
        run(data, mini_bank, picking(prompts, {}), mode=mode)
        assert options_offered(prompts, "1_QID1") == ["Black"], mode  # "Tea" is not "Coffee"
        assert options_offered(prompts, "2_QID1") == ["With milk", "Black"], mode


def test_a_survey_saved_without_option_logic_is_not_said_to_apply_it():
    survey = _fruit_survey_read(same_block=False)
    saved = survey.to_dict()
    for block_ in saved["template"]["Elements"]:
        for q in block_.get("Questions") or []:
            q.pop("ChoiceDisplayLogic", None)
    saved["definition_sha256"] = ""  # (a save made before the reader kept option logic: its digest matched its template)
    old = st.Survey.from_dict(saved)
    messages = [f.message for f in old.check().findings if f.feature == "question_option.choice_display_logic"]
    assert messages and all("is not applied" in m for m in messages)


# ---- review of D4 --------------------------------------------------------------------------------


def _marks(data, answered, repaired):
    """The marks for one twin who answered ``answered`` and had ``repaired`` repaired."""
    from surveytwin.answers.repair_marks import shaped_by_repaired
    from surveytwin.runtime.instrument_plan import parse_template

    rows = [{"persona_id": "p1", "question_id": q, "answer_label": "x", **({"answer_repaired": "y"} if q in repaired else {})}
            for q in answered]
    return {qid: sources for (_twin, qid), sources in shaped_by_repaired(rows, survey_from_template(parse_template(data, None))).items()}


YES_NO = ["Yes", "No"]


def test_a_screen_out_on_the_repaired_answer_marks_what_comes_after():
    data = qsf({"BL_A": ("A", ["QID1"]), "BL_B": ("B", ["QID2"])},
               {"QID1": mc("QID1", "Consent?", YES_NO), "QID2": te("QID2", "Your view?")},
               [block("BL_A"), branch(when("QID1", "SelectableChoice/2", "Selected"), end()), block("BL_B")])
    assert _marks(data, ["QID1", "QID2"], ["QID1"]) == {"QID2": ["QID1"]}


def test_a_field_set_inside_a_branch_on_the_answer_is_traced():
    data = qsf({"BL_A": ("A", ["QID1"]), "BL_B": ("B", ["QID2"])},
               {"QID1": mc("QID1", "Employed?", YES_NO), "QID2": te("QID2", "Your job?", display_logic=when_field("eligible", "1"))},
               [block("BL_A"), branch(when("QID1", "SelectableChoice/1", "Selected"), embedded(eligible="1")), block("BL_B")])
    assert _marks(data, ["QID1", "QID2"], ["QID1"]) == {"QID2": ["QID1"]}


def test_skips_mark_only_the_questions_they_can_jump_past():
    def survey(destination):
        questions = {"QID1": mc("QID1", "Continue?", YES_NO), "QID2": te("QID2", "a"), "QID3": te("QID3", "b"), "QID4": te("QID4", "c"),
                     "QID5": te("QID5", "d")}
        data = qsf({"BL_A": ("A", ["QID1", "QID2", "QID3", "QID4"]), "BL_B": ("B", ["QID5"])}, questions, [block("BL_A"), block("BL_B")])
        blocks = next(e for e in data["SurveyElements"] if e["Element"] == "BL")["Payload"]
        blocks["1"]["BlockElements"][0]["SkipLogic"] = [{
            "SkipLogicID": 1, "ChoiceLocator": "q://QID1/SelectableChoice/2", "Condition": "Selected", "SkipToDestination": destination,
            "SkipToDescription": destination, "Locator": "q://QID1/SelectableChoice/2", "QuestionID": "QID1", "SkipToDestinationType": "Question"}]
        return data
    everyone = ["QID1", "QID2", "QID3", "QID4", "QID5"]
    assert _marks(survey("QID3"), everyone, ["QID1"]) == {"QID2": ["QID1"]}  # QID3 is shown either way
    assert _marks(survey("ENDOFBLOCK"), everyone, ["QID1"]) == {q: ["QID1"] for q in ("QID2", "QID3", "QID4")}
    assert _marks(survey("ENDOFSURVEY"), everyone, ["QID1"]) == {q: ["QID1"] for q in everyone[1:]}


def test_a_field_counts_as_it_stood_where_it_was_read():
    data = qsf({"BL_A": ("A", ["QID1"]), "BL_B": ("B", ["QID2"]), "BL_C": ("C", ["QID3"])},
               {"QID1": mc("QID1", "Fruit?", ["Apple", "Pear"]), "QID2": te("QID2", "About ${e://Field/fruit}?"),
                "QID3": te("QID3", "Now ${e://Field/fruit}?")},
               [block("BL_A"), embedded(fruit="none yet"), block("BL_B"), embedded(fruit="${q://QID1/ChoiceGroup/SelectedChoices}"), block("BL_C")])
    assert _marks(data, ["QID1", "QID2", "QID3"], ["QID1"]) == {"QID3": ["QID1"]}


def test_copied_fields_names_with_spaces_and_text_above_are_traced():
    data = qsf({"BL_A": ("A", ["QID1"]), "BL_B": ("B", ["QID2", "QID3", "QID4"])},
               {"QID1": mc("QID1", "Colour?", ["Red", "Blue"]), "QID2": te("QID2", "Why ${e://Field/shown%20colour}?"),
                "QID3": db("QID3", "You picked ${q://QID1/ChoiceGroup/SelectedChoices}."), "QID4": te("QID4", "Anything else?")},
               [block("BL_A"), embedded(colour="${q://QID1/ChoiceGroup/SelectedChoices}"), embedded(**{"shown colour": "${e://Field/colour}"}),
                block("BL_B")])
    assert _marks(data, ["QID1", "QID2", "QID4"], ["QID1"]) == {"QID2": ["QID1"], "QID4": ["QID1"]}


def test_a_pipe_to_one_loop_row_names_only_that_row():
    rows = {"1": {"1": "Coffee"}, "2": {"1": "Tea"}}
    loop = {"Looping": "Static", "LoopingOptions": {"Static": rows, "Randomization": "None"}}
    data = qsf({"BL_L": ("Drinks", ["QID5"], loop), "BL_B": ("B", ["QID6"])},
               {"QID5": mc("QID5", "Like ${lm://Field/1}?", YES_NO), "QID6": te("QID6", "About coffee: ${q://1_QID5/ChoiceGroup/SelectedChoices}")},
               [block("BL_L"), block("BL_B")])
    assert _marks(data, ["1_QID5", "2_QID5", "QID6"], ["2_QID5"]) == {}
    assert _marks(data, ["1_QID5", "2_QID5", "QID6"], ["1_QID5"]) == {"QID6": ["1_QID5"]}


def test_a_question_shown_because_the_flagged_answer_hid_another_is_marked():
    data = qsf({"BL_A": ("A", ["QID1", "QID2", "QID3"])},
               {"QID1": mc("QID1", "Fruit?", ["Apple", "Banana"]),
                "QID2": mc("QID2", "Ripe?", YES_NO, display_logic=when("QID1", "SelectableChoice/2", "Selected")),
                "QID3": te("QID3", "Why not?", display_logic=when("QID2", "SelectableChoice/1", "NotSelected"))},
               [block("BL_A")])
    # QID1 was Grape (flagged), so QID2 was hidden and QID3 shown; QID1 is repaired to Banana
    assert _marks(data, ["QID1", "QID3"], ["QID1"]) == {"QID3": ["QID1"]}
