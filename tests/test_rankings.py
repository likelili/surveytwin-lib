"""A ranking is checked as a whole and never renumbered.

Ranks given twice, a skipped rank, or a rank no item can have: every item of the
ranking is flagged, each with the rank the model gave, for a rerun after the run.
A valid ranking keeps its ranks as given. A rerun asks the ranking again with its
rule, and does not store a repair that is still not a ranking.
"""

from __future__ import annotations

import json

import surveytwin as st
from conftest import TESTS
from fakes import answer_questions

BANK = str(TESTS.parent / "examples" / "example-bank")


def ranking_survey(**response) -> st.Survey:
    return st.read_survey({
        "schema_version": "0.1",
        "id": "r",
        "title": "Drinks",
        "questions": [{
            "id": "rank",
            "content": [{"type": "text", "text": "Rank these drinks."}],
            "response": {"type": "ranking", "items": [{"id": "a", "label": "Coffee"}, {"id": "b", "label": "Tea"}, {"id": "c", "label": "Juice"}], **response},
        }],
        "blocks": [{"id": "main", "kind": "survey", "questions": ["rank"]}],
        "flow": [{"type": "show_block", "block": "main"}],
    })


def ranking(given: dict[str, str], calls: list[str] | None = None) -> st.Model:
    """A model that gives each item the rank in ``given`` (blank when absent)."""

    def reply(request: st.ModelRequest) -> str:
        if calls is not None:
            calls.append(request.user)
        answers = answer_questions(request.user)
        for answer in answers:
            value = given.get(answer["question_id"].removeprefix("rank_"), "")
            answer["answer_value"] = answer["answer_label"] = answer["answer_raw"] = value
        return json.dumps({"answers": answers})

    return st.Model.from_function(reply, name="ranker")


def run(survey: st.Survey, given: dict[str, str]) -> st.Results:
    return st.run(st.plan(survey, st.configure(bank=BANK, sample_size=1, seed=1, model=ranking(given))))


def by_item(results: st.Results) -> dict[str, tuple]:
    return {a.question_id.removeprefix("rank_"): (a.status, a.value, a.raw) for a in results.answers}


def test_a_valid_ranking_keeps_its_ranks_as_given():
    assert by_item(run(ranking_survey(), {"a": "2", "b": "1", "c": "3"})) == {
        "a": (None, "2", "2"), "b": (None, "1", "1"), "c": (None, "3", "3"),
    }


def test_a_rank_given_twice_flags_the_whole_ranking_and_keeps_the_ranks_given():
    assert by_item(run(ranking_survey(), {"a": "1", "b": "1", "c": "2"})) == {
        "a": ("out_of_range", "", "1"), "b": ("out_of_range", "", "1"), "c": ("out_of_range", "", "2"),
    }


def test_a_skipped_rank_flags_the_whole_ranking():
    items = by_item(run(ranking_survey(), {"a": "1", "b": "3"}))
    assert {status for status, _value, _raw in items.values()} == {"out_of_range"}
    assert (items["a"][2], items["b"][2], items["c"][2]) == ("1", "3", "")


def test_a_rank_no_item_can_have_flags_the_whole_ranking_with_its_words():
    items = by_item(run(ranking_survey(), {"a": "2.5", "b": "1", "c": "3"}))
    assert {status for status, _value, _raw in items.values()} == {"out_of_range"}
    assert items["a"][2] == "2.5"


def test_an_unranked_item_is_skipped_unless_fewer_items_may_be_ranked():
    assert by_item(run(ranking_survey(), {"a": "1", "b": "2"}))["c"] == ("skipped", "", "")
    partial = by_item(run(ranking_survey(min_ranked=2), {"a": "1", "b": "2"}))
    assert partial == {"a": (None, "1", "1"), "b": (None, "2", "2"), "c": (None, "", "")}


def test_a_rerun_asks_the_ranking_again_with_its_rule():
    results = run(ranking_survey(), {"a": "1", "b": "1", "c": "2"})
    calls: list[str] = []
    fixed = results.rerun_flagged(model=ranking({"a": "2", "b": "1", "c": "3"}, calls))
    assert "Assign this item a unique rank from 1 to 3" in calls[0]
    assert fixed.flagged() == []
    assert {a.question_id: a.repair["value"] for a in fixed.answers} == {"rank_a": "2", "rank_b": "1", "rank_c": "3"}


def test_a_repair_that_is_still_not_a_ranking_is_not_stored():
    results = run(ranking_survey(), {"a": "1", "b": "1", "c": "2"})
    fixed = results.rerun_flagged(model=ranking({"a": "1", "b": "1", "c": "3"}))
    assert len(fixed.flagged()) == 3 and not any(a.repair for a in fixed.answers)
    assert any("ranking(s) were still not a valid ranking" in w for w in fixed.warnings)
