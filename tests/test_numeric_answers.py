"""A number the question does not allow is recorded as given and flagged, never moved.

Out of range, off the step, or with more decimals than allowed: the answer keeps
the model's reply in ``raw``, has no value, and is flagged ``out_of_range`` for
the report and a rerun after the run. A number the question allows is only
written in the question's format (7.0 for a whole number is 7).
"""

from __future__ import annotations

import json

import pytest

import surveytwin as st
from conftest import TESTS
from surveytwin.answers.executor import _coerce_numeric

BANK = str(TESTS.parent / "examples" / "example-bank")


def number_survey(**response) -> st.Survey:
    return st.read_survey({
        "schema_version": "0.1",
        "id": "n",
        "title": "Numbers",
        "questions": [{
            "id": "hours",
            "content": [{"type": "text", "text": "How many hours did you sleep?"}],
            "response": {"type": "number", **response},
        }],
        "blocks": [{"id": "main", "kind": "survey", "questions": ["hours"]}],
        "flow": [{"type": "show_block", "block": "main"}],
    })


def answering(value: str) -> st.Model:
    def reply(request: st.ModelRequest) -> str:
        return json.dumps({"answers": [{"question_id": "hours", "answer_value": value, "answer_label": value, "answer_raw": value}]})

    return st.Model.from_function(reply, name=f"says-{value}")


def answer_to(survey: st.Survey, value: str) -> st.Answer:
    results = st.run(st.plan(survey, st.configure(bank=BANK, sample_size=1, seed=1, model=answering(value))))
    [answer] = results.answers
    return answer


def test_a_number_above_the_range_is_kept_and_flagged_not_moved_to_the_end():
    answer = answer_to(number_survey(min=0, max=10, step=1), "99.6")
    assert (answer.status, answer.value, answer.raw) == ("out_of_range", "", "99.6")


def test_a_rerun_after_the_run_repairs_it():
    survey = number_survey(min=0, max=10, step=1)
    results = st.run(st.plan(survey, st.configure(bank=BANK, sample_size=1, seed=1, model=answering("99.6"))))
    fixed = results.rerun_flagged(model=answering("7"))
    [answer] = fixed.answers
    assert answer.raw == "99.6" and answer.repair and fixed.flagged() == []


@pytest.mark.parametrize(
    "settings, given, expected",
    [
        ({"min": 0, "max": 10, "step": 1}, "-1", None),  # below the lowest value
        ({"min": 0, "max": 10, "step": 1}, "7.5", None),  # not on the step
        ({"min": 0, "max": 100, "step": 5}, "12", None),  # not on the step of 5
        ({"min": 0, "max": 100, "step": 5}, "15", "15"),
        ({"min": 0, "max": 10, "decimals": 1}, "3.25", None),  # more decimals than allowed
        ({"min": 0, "max": 10, "decimals": 1}, "3.2", "3.2"),
        ({"min": 0, "max": 10, "step": 1}, "7.0", "7"),  # written in the question's format
        ({"min": 0, "max": 10, "step": 1}, "10", "10"),  # the ends are allowed
    ],
)
def test_what_a_number_question_allows(settings, given, expected):
    question = {
        "id": "q",
        "type": "numeric",
        "numeric_min": settings.get("min"),
        "numeric_max": settings.get("max"),
        "numeric_step": settings.get("step"),
        "numeric_decimals": settings.get("decimals"),
    }
    answer = _coerce_numeric({"answer_value": given, "answer_label": given, "answer_raw": given}, question)
    if expected is None:
        assert answer["answer_status"] == "out_of_range" and answer["answer_value"] == "" and answer["answer_raw"] == given
    else:
        assert not answer.get("answer_status") and answer["answer_value"] == expected
