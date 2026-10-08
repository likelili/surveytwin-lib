"""Planning and running, with a scripted model (no network)."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

import surveytwin as st
from conftest import TESTS, qsf
from fakes import accept_all_images, answer_questions, scripted_model


def configure(bank, **kwargs):
    kwargs.setdefault("sample_size", 3)
    kwargs.setdefault("seed", 5)
    kwargs.setdefault("model", scripted_model())
    return st.configure(bank=bank, **kwargs)


# ---- configure ----------------------------------------------------------------------


@pytest.mark.parametrize("kwargs", [{"sample_size": 0}, {"sample_size": "many"}, {"seed": "x"}])
def test_configure_rejects_bad_values(mini_bank, kwargs):
    with pytest.raises(st.ConfigError):
        configure(mini_bank, **kwargs)


def test_configure_rejects_unknown_filter_before_any_call(mini_bank):
    with pytest.raises(st.FilterError):
        configure(mini_bank, filters={"income": ["high"]})


def test_config_record_has_no_persona_text_or_key(mini_bank, monkeypatch):
    monkeypatch.setenv("K", "sk-hidden")
    model = st.Model.http("m", "https://x.example/v1/chat/completions", layout="openai-chat", headers={"Authorization": st.env("K")})
    record = configure(mini_bank, model=model, system_instruction="Answer briefly.").to_record()
    text = json.dumps(record)
    assert "sk-hidden" not in text and "summary text" not in text
    assert record["system_instruction"] == "Answer briefly."
    assert record["system_instruction_sha256"]


# ---- plan ----------------------------------------------------------------------------


def test_static_plan_and_prompt_preview(mini_bank):
    plan = st.plan(st.read_qsf(qsf("TEST_MC.qsf")), configure(mini_bank))
    assert plan.mode == "static" and len(plan.twins) == 3
    preview = plan.preview_prompt()
    assert preview.kind == "chat"
    assert "BEGIN PERSONA REPRESENTATION (untrusted data)" in preview.text
    assert "Respond using JSON schema" in preview.text
    assert preview.input_tokens > 0
    messages = plan.prompts(plan.twins[1].id)
    assert messages[0]["role"] == "system" and messages[1]["role"] == "user"
    with pytest.raises(st.SurveyError):
        plan.preview_prompt("nobody")


def image_survey() -> st.Survey:
    """TEST_TextGraphics with its text block turned into a text-entry question,
    so the survey has an image and something to answer."""
    data = json.loads(qsf("TEST_TextGraphics.qsf").read_text(encoding="utf-8"))
    for element in data["SurveyElements"]:
        payload = element.get("Payload") if element.get("Element") == "SQ" else None
        if payload and payload.get("QuestionID") == "QID19":
            payload.update({"QuestionType": "TE", "Selector": "SL", "SubSelector": "TX", "QuestionText": "What do you think of the shoe?"})
    return st.read_qsf(data)


def test_survey_with_nothing_to_answer(mini_bank):
    with pytest.raises(st.SurveyError, match="Nothing for a twin to answer"):
        st.plan(st.read_qsf(qsf("TEST_TextGraphics.qsf")), configure(mini_bank))


def test_image_survey_uses_multimodal_input(mini_bank):
    plan = st.plan(image_survey(), configure(mini_bank))
    preview = plan.preview_prompt()
    assert preview.kind == "multimodal" and preview.images


def test_staged_plan_previews_the_first_stage_only(mini_bank):
    plan = st.plan(st.read_qsf(qsf("TEST_FlowBehavior.qsf")), configure(mini_bank))
    assert plan.mode == "staged"
    assert plan.preview_prompt().text
    with pytest.raises(st.SurveyError):
        plan.preview_prompt(stage=1)


def test_static_mode_refused_for_a_staged_survey(mini_bank):
    with pytest.raises(st.StaticModeRefused):
        st.plan(st.read_qsf(qsf("TEST_FlowBehavior.qsf")), configure(mini_bank), mode="static")


def test_batch_not_available_for_a_staged_survey(mini_bank):
    with pytest.raises(st.BatchNotAvailable):
        st.plan(st.read_qsf(qsf("TEST_FlowBehavior.qsf")), configure(mini_bank, model="gpt-4o-mini", batch=True))


def test_no_matching_twins(mini_bank):
    config = configure(mini_bank, filters={"age": {"min": 90}})
    with pytest.raises(st.PersonaError):
        st.plan(st.read_qsf(qsf("TEST_MC.qsf")), config)


def test_fewer_matching_twins_than_asked_is_a_warning(mini_bank):
    plan = st.plan(st.read_qsf(qsf("TEST_MC.qsf")), configure(mini_bank, filters={"region": ["West"]}, sample_size=3))
    assert len(plan.twins) == 2
    assert any("Only 2 twins" in w for w in plan.warnings)


def test_blank_persona_is_refused(mini_bank):
    reps = mini_bank / "bank" / "representations.jsonl"
    rows = [json.loads(line) for line in reps.read_text(encoding="utf-8").splitlines()]
    for row in rows:
        if row["persona_id"] == "p1" and row["is_default"]:
            row["text"] = "   "
    reps.write_text("\n".join(json.dumps(r) for r in rows), encoding="utf-8")
    with pytest.raises((st.PersonaError, st.BankError)):
        st.plan(st.read_qsf(qsf("TEST_MC.qsf")), configure(mini_bank, sample_size=4))


def test_oversized_persona_is_refused_not_cut(mini_bank):
    reps = mini_bank / "bank" / "representations.jsonl"
    rows = [json.loads(line) for line in reps.read_text(encoding="utf-8").splitlines()]
    rows[0]["text"] = "word " * 100_000
    reps.write_text("\n".join(json.dumps(r) for r in rows), encoding="utf-8")
    with pytest.raises(st.ContextBudgetError):
        st.plan(st.read_qsf(qsf("TEST_MC.qsf")), configure(mini_bank))


def test_empty_representation_has_no_persona(mini_bank):
    plan = st.plan(st.read_qsf(qsf("TEST_MC.qsf")), configure(mini_bank, representation="empty"))
    assert "summary text" not in plan.preview_prompt().text


def test_system_instruction_replaces_the_behavioral_instruction(mini_bank):
    survey = st.read_qsf(qsf("TEST_MC.qsf"))
    default = st.plan(survey, configure(mini_bank)).preview_prompt().messages[0]["content"]
    custom = st.plan(survey, configure(mini_bank, system_instruction="Answer like a busy parent.")).preview_prompt().messages[0]["content"]
    assert "Answer like a busy parent." in custom
    assert "Answer like a busy parent." not in default
    # the output format and the persona-safety instruction are not editable
    assert "untrusted" in custom.lower() or "persona representation" in custom.lower()


def test_head(mini_bank):
    plan = st.plan(st.read_qsf(qsf("TEST_MC.qsf")), configure(mini_bank, sample_size=4))
    small = plan.head(2)
    assert [t.id for t in small.twins] == [t.id for t in plan.twins[:2]]
    assert small.config.sample_size == 2 and len(plan.twins) == 4


def test_batch_file_has_one_line_per_twin(mini_bank, tmp_path):
    plan = st.plan(st.read_qsf(qsf("TEST_MC.qsf")), configure(mini_bank, model="gpt-4o-mini", batch=True))
    path = plan.to_batch_file(tmp_path / "batch.jsonl")
    lines = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    assert len(lines) == 3
    assert {line["url"] for line in lines} <= {"/v1/chat/completions", "/v1/responses"}
    assert len({line["custom_id"] for line in lines}) == 3


# ---- determinism --------------------------------------------------------------------------


def _prompts(bank, seed):
    plan = st.plan(st.read_qsf(qsf("kitchen_sink_supported.qsf")), configure(bank, seed=seed, sample_size=4))
    return [t.id for t in plan.twins], [plan.preview_prompt(t.id).text for t in plan.twins]


def test_same_seed_same_twins_and_prompts(mini_bank):
    assert _prompts(mini_bank, 11) == _prompts(mini_bank, 11)


def test_seed_changes_the_draws(mini_bank):
    first = _prompts(mini_bank, 11)
    others = [_prompts(mini_bank, s) for s in (12, 13, 14)]
    assert any(o != first for o in others)


_CHILD = """
import json, sys
sys.path.insert(0, {tests!r})
import surveytwin as st
from fakes import accept_all_images, scripted_model
survey = st.read_qsf({qsf!r})
config = st.configure(bank={bank!r}, sample_size=4, seed=9, model=scripted_model())
plan = st.plan(survey, config)
results = st.run(plan, image_checker=accept_all_images, keep_prompts=True, max_concurrency=1)
print(json.dumps({{"prompts": [plan.preview_prompt(t.id).text for t in plan.twins],
                   "every_prompt": sorted(json.dumps(p, sort_keys=True) for p in results.prompts),
                   "rows": [[r["persona_id"], r["question_id"], r["answer_value"]] for r in results.rows]}}))
"""


def test_identical_across_processes_and_hash_seeds(mini_bank):
    outputs = []
    for hash_seed in ("1", "12345"):
        code = _CHILD.format(tests=str(TESTS), qsf=str(qsf("kitchen_sink_supported.qsf")), bank=str(mini_bank))
        env = {**os.environ, "PYTHONHASHSEED": hash_seed}
        done = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=env, check=True)
        outputs.append(done.stdout)
    assert outputs[0] == outputs[1]
    assert len(json.loads(outputs[0])["every_prompt"]) > 4  # later stages are compared too


# ---- run -------------------------------------------------------------------------------------


def test_static_run(mini_bank):
    plan = st.plan(st.read_qsf(qsf("TEST_MC.qsf")), configure(mini_bank))
    seen = []
    results = st.run(plan, on_progress=lambda done, total: seen.append((done, total)))
    assert results.run["mode"] == "static"
    assert {a.twin_id for a in results.answers} == {t.id for t in plan.twins}
    assert results.usage_totals["calls"] == 3
    assert results.usage_totals["input_tokens"] > 0
    assert seen[-1] == (3, 3)
    provenance = results.provenance
    assert provenance["run_seed"] == 5
    assert provenance["execution_mode"] == "static"
    assert provenance["survey_checksum_sha256"] == plan.survey.checksum
    assert provenance["library_version"] == st.__version__


def test_staged_run_calls_once_per_stage(mini_bank):
    plan = st.plan(st.read_qsf(qsf("kitchen_sink_supported.qsf")), configure(mini_bank, sample_size=2))
    results = st.run(plan, image_checker=accept_all_images)
    assert results.run["mode"] == "staged"
    stages = sum(a["stage_count"] for a in results.audit)
    assert results.usage_totals["calls"] == stages
    assert all(a["stage_count"] >= 2 for a in results.audit)


def test_keep_prompts_records_every_call(mini_bank):
    plan = st.plan(st.read_qsf(qsf("TEST_MC.qsf")), configure(mini_bank))
    results = st.run(plan, keep_prompts=True)
    assert len(results.prompts) == 3


def test_preset_run_needs_a_key(mini_bank):
    plan = st.plan(st.read_qsf(qsf("TEST_MC.qsf")), configure(mini_bank, model="gpt-4o-mini"))
    with pytest.raises(st.ConfigError, match="OPENAI_API_KEY"):
        st.run(plan)


def test_images_refused_for_a_text_only_model(mini_bank):
    text_only = scripted_model()
    text_only.images = False
    survey = image_survey()
    with pytest.raises(st.ImagesRefused):
        st.run(st.plan(survey, configure(mini_bank, model=text_only)), image_checker=accept_all_images)
    results = st.run(st.plan(survey, configure(mini_bank, model=text_only, accept_missing_images=True)), image_checker=accept_all_images)
    assert any("left out" in w for w in results.warnings)


def test_unreachable_images_refused_unless_accepted(mini_bank):
    survey = image_survey()
    login = lambda url: {"status": "auth_required", "detail": "HTTP 403"}  # noqa: E731
    with pytest.raises(st.ImagesRefused, match="cannot be loaded"):
        st.run(st.plan(survey, configure(mini_bank)), image_checker=login)
    results = st.run(st.plan(survey, configure(mini_bank, accept_missing_images=True)), image_checker=login)
    assert any("could not be loaded" in w for w in results.warnings)


def test_function_model_may_be_async(mini_bank):
    async def answer(request):
        return json.dumps({"answers": answer_questions(request.user)})

    model = st.Model.from_function(answer, name="async-fn")
    results = st.run(st.plan(st.read_qsf(qsf("TEST_MC.qsf")), configure(mini_bank, model=model)))
    assert results.answers


def test_run_inside_an_event_loop_points_to_arun(mini_bank):
    import asyncio

    plan = st.plan(st.read_qsf(qsf("TEST_MC.qsf")), configure(mini_bank))

    async def inside():
        with pytest.raises(RuntimeError, match="arun"):
            st.run(plan)
        return await st.arun(plan)

    assert asyncio.run(inside()).answers


def test_head_gives_each_twin_the_same_survey_as_the_full_plan(mini_bank):
    # kitchen_sink has an evenly presented randomizer, which balances over the
    # whole sample; head(2) must not re-balance over 2 twins.
    full = st.plan(st.read_qsf(qsf("kitchen_sink_supported.qsf")), configure(mini_bank, sample_size=4, seed=3))
    head = full.head(2)
    assert head.population == full.population == 4
    for twin in head.twins:
        assert head.preview_prompt(twin.id).text == full.preview_prompt(twin.id).text
    rows = lambda r: [(x["persona_id"], x["question_id"], x["answer_value"]) for x in r.rows]  # noqa: E731
    full_rows = rows(st.run(full, image_checker=accept_all_images))
    head_ids = {t.id for t in head.twins}
    assert [r for r in full_rows if r[0] in head_ids] == rows(st.run(head, image_checker=accept_all_images))


def test_images_left_out_are_listed(mini_bank):
    survey = image_survey()
    login = lambda url: {"status": "auth_required", "detail": "HTTP 403"}  # noqa: E731
    results = st.run(st.plan(survey, configure(mini_bank, accept_missing_images=True)), image_checker=login)
    assert results.provenance["images_left_out"] and all(u.startswith("https://") for u in results.provenance["images_left_out"])
    clean = st.run(st.plan(survey, configure(mini_bank)), image_checker=accept_all_images)
    assert clean.provenance["images_left_out"] == []


def test_batch_file_and_submit_check_images_first(mini_bank, tmp_path):
    survey = image_survey()
    login = lambda url: {"status": "auth_required", "detail": "HTTP 403"}  # noqa: E731
    plan = st.plan(survey, configure(mini_bank, model="gpt-4o-mini", batch=True))
    with pytest.raises(st.ImagesRefused):
        plan.to_batch_file(tmp_path / "b.jsonl", image_checker=login)
    with pytest.raises(st.ImagesRefused):  # refused before any key or upload is needed
        st.batch.submit(plan, image_checker=login)
    accepted = st.plan(survey, configure(mini_bank, model="gpt-4o-mini", batch=True, accept_missing_images=True))
    text = accepted.to_batch_file(tmp_path / "b.jsonl", image_checker=login).read_text(encoding="utf-8")
    urls = [i["url"] for i in survey.check().images]
    assert urls and text and not any(u in text for u in urls)  # the image was left out of the lines


def test_settings_and_plan_print_readably(mini_bank):
    config = configure(mini_bank, filters={"region": ["West"]}, sample_size=2)
    text = str(config)
    assert text.startswith("Run settings:") and "region = West" in text and "missing images      stop the run" in text
    plan = st.plan(st.read_qsf(qsf("TEST_MC.qsf")), config)
    assert str(plan).startswith("Plan: static survey, 2 twins, seed 5")
    assert "the first 1 of a 2-twin plan" in str(plan.head(1))
