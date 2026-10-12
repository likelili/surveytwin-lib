"""Prompt version 5: the instruction a prompt starts with, and where each
question's answer details go (``surveytwin.prompts.layout``).

"inline", the default, puts them under each question ("Answer as:") and lists
the IDs to answer once; "after" lists them after the survey without repeating
each question's text; "json", the layout before, repeats every question in a
Questions JSON list. All three give every answer detail.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import surveytwin as st
from fakes import scripted_model
from surveytwin.prompts import layout

ROOT = Path(__file__).resolve().parent.parent
BANK = str(ROOT / "examples" / "example-bank")
SURVEY = ROOT / "examples" / "example_survey.qsf"


def _prompt(selected: str = "inline") -> str:
    previous = layout.LAYOUT
    layout.LAYOUT = selected
    try:
        plan = st.plan(st.read_qsf(SURVEY), st.configure(bank=BANK, sample_size=1, seed=7, model=scripted_model()))
        return plan.preview_prompt().text
    finally:
        layout.LAYOUT = previous


def test_the_prompt_starts_with_the_funhouse_instruction_and_keeps_the_answer_rules():
    text = _prompt()
    assert st.prompts.PROMPT_VERSION == "5"
    assert "as if you are the person described in the 'Persona profile'" in st.prompts.DEFAULT_SYSTEM_INSTRUCTION
    # a profile is not always survey answers (a summary, demographics only)
    assert "past responses to questions" in st.prompts.DEFAULT_SYSTEM_INSTRUCTION
    assert "survey responses" not in st.prompts.DEFAULT_SYSTEM_INSTRUCTION
    assert f"{st.prompts.DEFAULT_SYSTEM_INSTRUCTION} {st.prompts.SIMULATION_FORMAT_CONTRACT}" in text


def test_each_question_has_its_answer_details_under_it():
    text = _prompt()
    assert layout.LAYOUT == "inline"
    assert "Questions JSON" not in text and "[[answers" not in text
    survey_text = text.split("Answer each question under", 1)[0]
    q1 = survey_text.split("Q1:\n", 1)[1].split("\n\nQ2:", 1)[0]
    assert "\nAnswer as:\n" in q1
    asked = st.prompts.asked_questions(text)
    assert [q["id"] for q in asked] == [q["id"] for q in st.prompts.asked_questions(_prompt("json"))]
    assert {q["row"] for q in asked if q["id"].startswith("QID1_")} >= {"Apple", "Banana"}


@pytest.mark.parametrize("selected", ["after", "inline"])
def test_every_answer_detail_is_given_without_repeating_the_question_text(selected):
    full = {q["id"]: q for q in st.prompts.asked_questions(_prompt("json"))}
    asked = st.prompts.asked_questions(_prompt(selected))
    assert [q["id"] for q in asked] == list(full)
    for question in asked:
        assert "text" not in question
        assert {k: v for k, v in full[question["id"]].items() if k != "text"}.items() <= question.items()


def test_a_survey_text_saved_without_markers_gets_the_questions_json():
    """A run saved by an older version holds survey text without the markers;
    asking it again still gives every question's details, in the Questions JSON."""
    from surveytwin.answers import executor

    previous = layout.LAYOUT
    layout.LAYOUT = "json"
    try:
        plan = st.plan(st.read_qsf(SURVEY), st.configure(bank=BANK, sample_size=1, seed=7, model=scripted_model()))
        old_text = plan.preview_prompt().text.split("Survey sequence:\n", 1)[1].split("\n\nQuestions JSON:", 1)[0]
        questions = st.prompts.asked_questions(plan.preview_prompt().text)
    finally:
        layout.LAYOUT = previous
    assert "[[answers" not in old_text
    _system, _persona, prompt = executor.build_simulation_prompt_parts("(persona)", [{**q, "text": "?"} for q in questions], survey_sequence_text=old_text)
    assert "Questions JSON:\n" in prompt and "Answer as:" not in prompt
    assert [q["id"] for q in st.prompts.asked_questions(prompt)] == [q["id"] for q in questions]
