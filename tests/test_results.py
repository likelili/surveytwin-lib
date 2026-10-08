"""Results: saving, loading, export files, text files, the report, reruns."""

from __future__ import annotations

import copy
import csv
import json

import pytest

import surveytwin as st
from conftest import qsf
from fakes import answer_questions, scripted_model
from surveytwin.results.results import _answer_from_row


def skip_first_question(request: st.ModelRequest) -> st.Reply:
    answers = answer_questions(request.user)
    return st.Reply(text=json.dumps({"answers": answers[1:]}), input_tokens=10, output_tokens=5)


@pytest.fixture
def results(mini_bank):
    config = st.configure(bank=mini_bank, sample_size=2, seed=5, model=scripted_model())
    return st.run(st.plan(st.read_qsf(qsf("TEST_TE.qsf")), config))


@pytest.fixture
def flagged_results(mini_bank):
    model = st.Model.from_function(skip_first_question, name="skipper")
    config = st.configure(bank=mini_bank, sample_size=2, seed=5, model=model)
    return st.run(st.plan(st.read_qsf(qsf("TEST_TE.qsf")), config))


def test_answers_view(results):
    answers = results.answers
    assert len(answers) == len(results.rows)
    first = answers[0]
    assert first.twin_id and first.question_id and first.label
    assert results.flagged() == []


def test_save_writes_the_website_files(results, tmp_path):
    out = results.save(tmp_path / "run")
    for name in ("responses_labels.csv", "responses_values.csv", "responses_repaired.csv", "long.csv", "wide.csv",
                 "manifest.json", "question_logic.json", "report.html", "report.json", "results.json",
                 "audit.jsonl", "checksums.json"):
        assert (out / name).is_file(), name
    twin_ids = sorted({a.twin_id for a in results.answers})
    assert sorted(p.stem for p in (out / "answers").glob("*.txt")) == twin_ids
    assert sorted(p.stem for p in (out / "questions").glob("*.txt")) == twin_ids


def test_qualtrics_csv_has_three_header_rows(results, tmp_path):
    path = results.to_qualtrics_csv(tmp_path / "labels.csv")
    rows = list(csv.reader(path.open(encoding="utf-8")))
    assert rows[0][0] == "TWIN_ID"
    assert rows[2][0] == '{"ImportId":"TWIN_ID"}'
    assert len(rows) == 3 + 2
    assert "demo_region" in rows[0]
    with pytest.raises(st.ResultsError):
        results.to_qualtrics_csv(tmp_path / "x.csv", values=True, repaired=True)


def test_text_files_hold_each_twins_answers(results, tmp_path):
    folder = results.to_text(tmp_path / "text")
    twin = results.answers[0].twin_id
    text = (folder / "answers" / f"{twin}.txt").read_text(encoding="utf-8")
    assert text.startswith(f"Participant ID: {twin}\n")
    assert "Answer: answer to" in text
    questions = (folder / "questions" / f"{twin}.txt").read_text(encoding="utf-8")
    assert "Answer:" not in questions


def test_load_round_trip(results, tmp_path):
    out = results.save(tmp_path / "run")
    loaded = st.Results.load(out)
    assert loaded.rows == json.loads(json.dumps(results.rows, default=str))
    assert loaded.provenance["run_seed"] == 5
    assert loaded.usage_totals == results.usage_totals


def test_load_detects_a_changed_file(results, tmp_path):
    out = results.save(tmp_path / "run")
    path = out / "responses_labels.csv"
    path.write_text(path.read_text(encoding="utf-8") + "extra\n", encoding="utf-8")
    with pytest.raises(st.ResultsError, match="checksum"):
        st.Results.load(out)
    assert st.Results.load(out, verify=False).rows


def test_load_refuses_a_folder_that_is_not_a_run(tmp_path):
    with pytest.raises(st.ResultsError):
        st.Results.load(tmp_path)


def test_report_json(results, tmp_path):
    data = results.report(tmp_path / "report.json", format="json")
    assert {"fidelity", "randomization", "coverage"} <= set(data)
    assert json.loads((tmp_path / "report.json").read_text(encoding="utf-8")) == data
    html = tmp_path / "report.html"
    results.report(html)
    assert "<h1>Simulation Report</h1>" in html.read_text(encoding="utf-8")


def test_summary_stats_and_pandas(results):
    stats = results.summary_stats()
    assert stats
    pd = pytest.importorskip("pandas")
    frame = results.to_pandas()
    assert isinstance(frame, pd.DataFrame) and len(frame) == len(results.rows)


def test_not_shown_is_its_own_status():
    row = {"persona_id": "p1", "question_id": "QID9", "answer_value": "", "not_shown": True}
    assert _answer_from_row(row).status == "not_shown"
    assert _answer_from_row({**row, "not_shown": False}).status is None


def test_flagged_answers_and_rerun(flagged_results, tmp_path):
    flagged = flagged_results.flagged()
    assert flagged and all(a.status == "skipped" for a in flagged)
    counts = flagged_results.rerun_counts()
    assert counts == {"twins": 2, "flagged_answers": len(flagged)}

    fixed = flagged_results.rerun_flagged(model=scripted_model())
    assert fixed.flagged() == []
    repaired = [a for a in fixed.answers if a.repair]
    assert len(repaired) == len(flagged)
    assert all(a.repair["method"] == "rerun_llm" for a in repaired)
    # the original answer is kept beside the repair
    assert all(a.status == "skipped" for a in repaired)
    assert fixed.provenance["reruns"][-1]["repaired"] == len(flagged)
    assert fixed.provenance["reruns"][-1]["library_version"] == st.__version__
    assert not any("was made with surveytwin" in w for w in fixed.warnings)
    assert len(fixed.usage) > len(flagged_results.usage)

    out = fixed.save(tmp_path / "run")
    assert (out / "responses_labels_raw.csv").is_file()
    repaired_csv = (out / "responses_repaired.csv").read_text(encoding="utf-8")
    assert "answer to" in repaired_csv


def test_a_run_made_with_another_version_says_so_when_asked_again(flagged_results):
    older = copy.copy(flagged_results)
    older.provenance = {**flagged_results.provenance, "library_version": "0.0.9"}
    fixed = older.rerun_flagged(model=scripted_model())
    assert f"This run was made with surveytwin 0.0.9; its flagged answers were asked again with {st.__version__}." in fixed.warnings


def test_rerun_after_load_needs_the_defined_model_again(flagged_results, tmp_path):
    loaded = st.Results.load(flagged_results.save(tmp_path / "run"))
    with pytest.raises(st.ResultsError, match="defined model"):
        loaded.rerun_flagged()
    assert loaded.rerun_flagged(model=scripted_model()).flagged() == []


def test_rerun_keeps_persona_text_only_for_flagged_twins(flagged_results, results):
    assert set(flagged_results.run["rerun_personas"]) == {a.twin_id for a in flagged_results.flagged()}
    assert results.run["rerun_personas"] == {}
