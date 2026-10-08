"""Questions and branches switched off with a literal "If False".

Qualtrics authors park a question, or a whole branch, with display or branch
logic that is a literal condition, ``{"LogicType": "BooleanValue", "Value":
"False"}``. It has no ``Operator``, so the logic folder never compared anything
and the condition held: every twin was asked questions no human respondent
saw. "If False" is now decided by its value in both modes, in the post-stage
check, in the pre-run check and in the report; the switched-off question keeps
an empty export column, as in a Qualtrics export.
"""

from __future__ import annotations

import surveytwin as st
from fakes import accept_all_images
from flow_kit import answering, block, branch, embedded, end, mc, qsf, te
from surveytwin.answers.simulate import _apply_question_display_logic
from surveytwin.runtime.logic_compare import evaluate_logic_tree, logic_always_false


def literal(value: str) -> dict:
    return {"LogicType": "BooleanValue", "Value": value, "Type": "Expression"}


def if_(*conditions: dict) -> dict:
    group = {str(i): c for i, c in enumerate(conditions)}
    group["Type"] = "If"
    return {"0": group, "Type": "BooleanExpression", "inPage": False}


IF_FALSE = if_(literal("False"))
IF_TRUE = if_(literal("True"))


def field_is(field: str, value: str, **extra) -> dict:
    return {"LogicType": "EmbeddedField", "LeftOperand": field, "Operator": "EqualTo",
            "RightOperand": value, "Type": "Expression", **extra}


def asked_by_mode(survey, mini_bank, modes=("static", "staged")):
    """Question IDs the model was asked, per mode, over a 3-twin run."""
    out = {}
    for mode in modes:
        seen: list[str] = []

        def rule(q, seen=seen):
            seen.append(q["id"])
            return None

        config = st.configure(bank=mini_bank, sample_size=3, seed=5, model=answering(rule))
        results = st.run(st.plan(survey, config, mode=mode), image_checker=accept_all_images)
        out[mode] = (sorted(set(seen)), results)
    return out


def parked_survey():
    blocks = {"BL_A": ("Main", ["QID1", "QID2", "QID3", "QID4", "QID5"])}
    questions = {
        "QID1": mc("QID1", "How often do you contact customer service?", ["Never", "Often"]),
        "QID2": mc("QID2", "What is your gender?", ["Male", "Female"], display_logic=IF_FALSE),
        "QID3": te("QID3", "Parked open-end: describe your last call.", display_logic=IF_FALSE),
        "QID4": mc("QID4", "Kept on purpose with If True", ["Yes", "No"], display_logic=IF_TRUE),
        "QID5": te("QID5", "Anything else?"),
    }
    questions["QID2"]["DataExportTag"] = "S2"
    return st.read_qsf(qsf(blocks, questions, [block("BL_A")]))


def test_a_literal_condition_is_decided_by_its_value():
    def never(_condition):
        raise AssertionError("decide() must not see a literal")

    assert evaluate_logic_tree(IF_FALSE, never) is False
    assert evaluate_logic_tree(IF_TRUE, never) is True
    assert evaluate_logic_tree(literal("False"), never) is False
    assert logic_always_false(IF_FALSE) and not logic_always_false(IF_TRUE)
    assert not logic_always_false(if_(literal("False"), field_is("arm", "A", Conjuction="Or")))


def test_if_false_questions_are_asked_of_nobody_and_if_true_ones_are_asked_in_both_modes(mini_bank):
    for mode, (asked, results) in asked_by_mode(parked_survey(), mini_bank).items():
        assert asked == ["QID1", "QID4", "QID5"], mode
        answered = {row["question_id"] for row in results.rows if str(row.get("answer_value") or "").strip()}
        assert not answered & {"QID2", "QID3"}, mode


def test_if_false_or_a_real_condition_follows_the_real_condition(mini_bank):
    logic = if_(literal("False"), field_is("arm", "A", Conjuction="Or"))
    for arm, expected in (("A", ["QID1", "QID2"]), ("B", ["QID1"])):
        blocks = {"BL_A": ("Main", ["QID1", "QID2"])}
        questions = {"QID1": mc("QID1", "First", ["x", "y"]), "QID2": te("QID2", "Only in arm A", display_logic=logic)}
        survey = st.read_qsf(qsf(blocks, questions, [embedded(arm=arm), block("BL_A")]))
        for mode, (asked, _) in asked_by_mode(survey, mini_bank).items():
            assert asked == expected, (arm, mode)


def test_if_false_branches_never_run_and_literal_screen_outs_run_as_written(mini_bank):
    blocks = {"BL_A": ("A", ["QID1"]), "BL_B": ("B", ["QID2"]), "BL_C": ("C", ["QID3"])}
    questions = {"QID1": mc("QID1", "Start", ["a", "b"]), "QID2": te("QID2", "Parked block"), "QID3": te("QID3", "Live block")}
    flow = [block("BL_A"), branch(IF_FALSE, block("BL_B")), branch(IF_TRUE, block("BL_C"))]
    for mode, (asked, _) in asked_by_mode(st.read_qsf(qsf(blocks, questions, flow)), mini_bank).items():
        assert asked == ["QID1", "QID3"], mode

    def screened(logic):
        blocks = {"BL_A": ("A", ["QID1"]), "BL_B": ("B", ["QID2"])}
        questions = {"QID1": mc("QID1", "Start", ["a", "b"]), "QID2": te("QID2", "After the screen-out")}
        return st.read_qsf(qsf(blocks, questions, [block("BL_A"), branch(logic, end()), block("BL_B")]))

    assert asked_by_mode(screened(IF_FALSE), mini_bank, modes=("staged",))["staged"][0] == ["QID1", "QID2"]
    assert asked_by_mode(screened(IF_TRUE), mini_bank, modes=("staged",))["staged"][0] == ["QID1"]


def test_the_post_stage_check_decides_if_false():
    logic = if_(literal("False"), {"LogicType": "Question", "LeftOperand": "q://QID1/SelectableChoice/1",
                                   "Operator": "Selected", "QuestionID": "QID1", "Type": "Expression", "Conjuction": "Or"})
    questions = [
        {"id": "QID1", "text": "Pick", "type": "single_choice", "options": ["x", "y"], "option_ids": ["1", "2"]},
        {"id": "QID2", "text": "Follow-up", "type": "open_text", "options": [], "display_logic": logic},
        {"id": "QID3", "text": "Parked", "type": "open_text", "options": [], "display_logic": IF_FALSE},
    ]

    def answers(first):
        return [{"question_id": "QID1", "answer_value": first, "answer_label": first, "answer_raw": first},
                {"question_id": "QID2", "answer_value": "because", "answer_label": "because", "answer_raw": "because"},
                {"question_id": "QID3", "answer_value": "text", "answer_label": "text", "answer_raw": "text"}]

    kept = _apply_question_display_logic(answers=answers("x"), questions=questions, embedded_data={})
    assert kept[1]["answer_raw"] == "because" and kept[2]["answer_raw"] == ""
    assert _apply_question_display_logic(answers=answers("y"), questions=questions, embedded_data={})[1]["answer_raw"] == ""


def test_the_check_names_switched_off_questions_and_the_report_leaves_them_out(mini_bank, tmp_path):
    survey = parked_survey()
    messages = [f.message for f in survey.check().findings]
    assert not any("BooleanValue" in m for m in messages)
    assert any("switched off for every respondent" in m and "S2 (QID2)" in m for m in messages)

    config = st.configure(bank=mini_bank, sample_size=3, seed=5, model=answering(lambda q: None))
    results = st.run(st.plan(survey, config, mode="staged"), image_checker=accept_all_images)
    report = results.report(tmp_path / "report.json", format="json")
    names = [v["name"] for v in report["distributions"]["variables"]]
    assert "S2" not in names and not any(n.startswith("QID3") for n in names)
    assert "S2" in report["setting"]["hidden_for_everyone"]
    assert any("switched off for every respondent" in w for w in report["fidelity"]["warnings"])
