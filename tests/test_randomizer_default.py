"""A randomizer balances its arms across twins unless it says otherwise.

In a survey written in the standard format, a ``randomize`` step without
``evenly`` presents its arms evenly; ``"evenly": false`` draws each twin's arms
independently. A QSF says which it uses, and is read as it says: a randomizer
with no even presentation set stays independent.
"""

from __future__ import annotations

import copy
import json
from collections import Counter

import surveytwin as st
from conftest import TESTS, qsf
from fakes import scripted_model

BANK = str(TESTS.parent / "examples" / "example-bank")


def arms_survey(**randomize) -> dict:
    def question(qid: str, text: str) -> dict:
        return {"id": qid, "content": [{"type": "text", "text": text}], "response": {"type": "choice", "mode": "single", "options": [{"id": "y", "label": "Yes"}, {"id": "n", "label": "No"}]}}

    return {
        "schema_version": "0.1",
        "id": "arms",
        "title": "Two messages",
        "questions": [question("qa", "Did message A persuade you?"), question("qb", "Did message B persuade you?")],
        "blocks": [{"id": "arm_a", "kind": "survey", "questions": ["qa"]}, {"id": "arm_b", "kind": "survey", "questions": ["qb"]}],
        "flow": [{"id": "message", "type": "randomize", "take": 1, "children": [{"type": "show_block", "block": "arm_a"}, {"type": "show_block", "block": "arm_b"}], **randomize}],
    }


def arm_counts(data: dict, twins: int) -> Counter:
    results = st.run(st.plan(st.read_survey(data), st.configure(bank=BANK, sample_size=twins, seed=3, model=scripted_model())))
    return Counter(a.question_id for a in results.answers)


def test_a_written_randomizer_is_even_by_default():
    survey = st.read_survey(arms_survey())
    assert "evenly" not in survey.standard["flow"][0]  # the survey keeps what was written
    randomizer = next(e for e in survey.template["Elements"] if e.get("ElementType") == "Randomizer")
    assert randomizer["EvenPresentation"] is True
    assert arm_counts(arms_survey(), 6) == Counter({"qa": 3, "qb": 3})


def test_evenly_false_draws_each_twin_independently():
    data = arms_survey(evenly=False)
    randomizer = next(e for e in st.read_survey(data).template["Elements"] if e.get("ElementType") == "Randomizer")
    assert randomizer["EvenPresentation"] is False
    counts = arm_counts(data, 8)
    assert sum(counts.values()) == 8  # every twin saw one arm, drawn on its own


def test_a_qsf_randomizer_without_even_presentation_stays_independent(tmp_path):
    data = json.loads(qsf("kitchen_sink_supported.qsf").read_text(encoding="utf-8"))
    unset = copy.deepcopy(data)

    def drop_even(node):
        for child in node.get("Flow", []) if isinstance(node, dict) else []:
            if child.get("Type") == "BlockRandomizer":
                child.pop("EvenPresentation", None)
            drop_even(child)

    for element in unset["SurveyElements"]:
        if element.get("Element") == "FL":
            drop_even(element["Payload"])
    survey = st.read_qsf(unset)
    randomizers = []

    def collect(nodes):
        for node in nodes:
            if node.get("type") == "randomize":
                randomizers.append(node)
            for key in ("then", "else", "children"):
                collect(node.get(key) or [])

    collect(survey.standard["flow"])
    assert randomizers and all(node["evenly"] is False for node in randomizers)
    # and the template the QSF reader gives is unchanged: no even presentation written in
    template_randomizers = []

    def walk(elements):
        for element in elements:
            if element.get("ElementType") == "Randomizer":
                template_randomizers.append(element)
            walk(element.get("Elements") or [])

    walk(survey.template["Elements"])
    assert template_randomizers and all("EvenPresentation" not in element for element in template_randomizers)
