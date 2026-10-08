"""Fixes of problems found while writing the reference (docs/reference/)."""

from __future__ import annotations

import surveytwin as st
from fakes import accept_all_images
from flow_kit import block, mc, qsf, te, when


def _no_usable_answers(monkeypatch):
    from surveytwin.answers import executor

    monkeypatch.setattr(executor, "MAX_CALL_RETRIES", 1)
    monkeypatch.setattr(executor, "RETRY_BACKOFF_BASE_S", 0.0)
    return st.Model.from_function(lambda request: "I would rather not say.", name="unusable")


def _with_multi_select(staged: bool):
    many = mc("QID1", "Which do you use?", ["Phone", "Laptop", "Tablet"])
    many["Selector"] = "MAVR"
    questions = {"QID1": many, "QID2": te("QID2", "Why?")}
    if staged:  # a pipe of the first answer makes the second wait for it
        questions["QID2"] = te("QID2", "Why ${q://QID1/ChoiceGroup/SelectedChoices}?")
        return qsf({"BL_A": ("A", ["QID1"]), "BL_B": ("B", ["QID2"])}, questions, [block("BL_A"), block("BL_B")])
    return qsf({"BL_A": ("A", ["QID1", "QID2"])}, questions, [block("BL_A")])


# ---- 1. a twin the model gave no usable answer is failed ---------------------------------------------


def test_a_twin_with_no_usable_answer_is_failed_even_with_a_multi_select(mini_bank, monkeypatch):
    model = _no_usable_answers(monkeypatch)
    for staged in (False, True):
        config = st.configure(bank=mini_bank, sample_size=1, seed=3, model=model)
        results = st.run(st.plan(st.read_qsf(_with_multi_select(staged)), config), image_checker=accept_all_images)
        twin = results.twins[0]
        assert twin["status"] == "failed", (staged, twin)  # was "complete": the blank options looked like answers
        assert "no usable answers" in twin["reason"]


def test_a_twin_that_answered_is_complete(mini_bank):
    from flow_kit import answering

    for staged in (False, True):
        config = st.configure(bank=mini_bank, sample_size=1, seed=3, model=answering(lambda q: None))
        results = st.run(st.plan(st.read_qsf(_with_multi_select(staged)), config), image_checker=accept_all_images)
        assert results.twins[0]["status"] == "complete"


# ---- 2. rebuilding the report or the export files keeps the run folder loadable ----------------------


def _saved_run(mini_bank, folder, *, n=2, seed=3, keep_prompts=False):
    from flow_kit import answering

    data = qsf({"BL_A": ("A", ["QID1", "QID2"])}, {"QID1": mc("QID1", "Fruit?", ["Apple", "Pear"]), "QID2": te("QID2", "Why?")},
               [block("BL_A")])
    config = st.configure(bank=mini_bank, sample_size=n, seed=seed, model=answering(lambda q: None))
    results = st.run(st.plan(st.read_qsf(data), config), image_checker=accept_all_images, keep_prompts=keep_prompts)
    return results.save(folder)


def test_rebuilding_the_report_or_the_exports_keeps_the_folder_loadable(mini_bank, tmp_path):
    import zipfile

    from surveytwin.cli import main

    folder = _saved_run(mini_bank, tmp_path / "run")
    (folder / "report.html").write_text("an older report", encoding="utf-8")  # as rebuilt by another version
    main(["report", str(folder)])
    st.Results.load(folder)  # refused before: "report.html does not match its checksum"
    main(["export", str(folder), "--bundle"])
    st.Results.load(folder)
    assert "report.html" in zipfile.ZipFile(folder / "export_bundle.zip").namelist()  # the bundle left the report out


def test_a_hand_edited_file_is_still_caught_after_a_rebuild(mini_bank, tmp_path):
    import pytest

    from surveytwin.cli import main

    folder = _saved_run(mini_bank, tmp_path / "run")
    (folder / "audit.jsonl").write_text("changed by hand\n", encoding="utf-8")
    main(["report", str(folder)])
    with pytest.raises(st.ResultsError, match="audit.jsonl"):
        st.Results.load(folder)


def test_the_report_is_the_same_in_every_process(tmp_path):
    import json
    import os
    import subprocess
    import sys
    from pathlib import Path

    # a three-field form whose answers repeat across twins, with tied counts
    src = Path(st.__file__).resolve().parents[1]  # the library this test imported
    script = f"""
import sys, json
sys.path[:0] = [{str(Path(__file__).parent)!r}, {str(src)!r}]
import surveytwin as st
from fakes import accept_all_images
from flow_kit import block, qsf
form = {{"QuestionID": "QID1", "DataExportTag": "QID1", "QuestionText": "Name three brands", "QuestionType": "TE", "Selector": "FORM",
        "Configuration": {{"QuestionDescriptionOption": "UseText"}}, "Validation": {{"Settings": {{"ForceResponse": "OFF", "Type": "None"}}}},
        "Choices": {{"1": {{"Display": "First"}}, "2": {{"Display": "Second"}}, "3": {{"Display": "Third"}}}}, "ChoiceOrder": [1, 2, 3]}}
data = qsf({{"BL_A": ("A", ["QID1"])}}, {{"QID1": form}}, [block("BL_A")])
brands = [["Acme", "Zeta", "Kilo"], ["Zeta", "Acme", "Lima"], ["Kilo", "Lima", "Acme"], ["Lima", "Zeta", "Kilo"]]
calls = iter(range(100))
def reply(request):
    text = request.user
    start = text.rindex("Questions JSON:\\n") + len("Questions JSON:\\n")
    qs = json.loads(text[start: text.index("\\n\\nRespond using JSON schema", start)])
    names = brands[next(calls) % 4]
    answers = [{{"question_id": q["id"], "answer_value": names[i], "answer_label": names[i], "answer_raw": names[i]}} for i, q in enumerate(qs)]
    return st.Reply(text=json.dumps({{"answers": answers}}), input_tokens=1, output_tokens=1)
import conftest
bank = conftest.write_mini_bank(__import__("pathlib").Path(sys.argv[1]))
config = st.configure(bank=bank, sample_size=4, seed=3, model=st.Model.from_function(reply, name="brands"))
results = st.run(st.plan(st.read_qsf(data), config), image_checker=accept_all_images, max_concurrency=1)
print(json.dumps(results.report()["distributions"], sort_keys=True))
"""
    outputs = []
    for seed in ("1", "4", "5"):  # hash seeds under which the old order differed
        env = dict(os.environ, PYTHONHASHSEED=seed)
        done = subprocess.run([sys.executable, "-c", script, str(tmp_path / f"bank{seed}")], capture_output=True, text=True, env=env, check=True)
        outputs.append(done.stdout.strip().splitlines()[-1])
    assert "Acme" in outputs[0]
    assert outputs[0] == outputs[1] == outputs[2]  # tied counts came out in a different order per process


# ---- 3. saving into a used folder leaves one run in it ----------------------------------------------


def test_saving_into_a_used_folder_leaves_only_this_run(mini_bank, tmp_path):
    folder = tmp_path / "run"
    _saved_run(mini_bank, folder, n=3, seed=3, keep_prompts=True)
    (folder / "notes.txt").write_text("mine", encoding="utf-8")
    first = {p.name for p in (folder / "answers").glob("*.txt")}
    _saved_run(mini_bank, folder, n=1, seed=9, keep_prompts=False)
    loaded = st.Results.load(folder)
    assert loaded.prompts == []  # the first run's prompts were read as this run's
    assert not (folder / "prompts.jsonl").exists()
    twins = {t["id"] for t in loaded.twins}
    assert {p.stem for p in (folder / "answers").glob("*.txt")} == twins != {Path_stem(n) for n in first}
    import json
    assert (folder / "notes.txt").read_text(encoding="utf-8") == "mine"  # not the library's: kept
    assert "notes.txt" not in json.loads((folder / "checksums.json").read_text(encoding="utf-8"))


def Path_stem(name: str) -> str:
    return name.rsplit(".", 1)[0]


# ---- 4. a defined model's key variables are checked before any call ----------------------------------


def test_a_defined_models_missing_key_stops_the_run_before_any_call(mini_bank, monkeypatch):
    import pytest

    monkeypatch.delenv("SURVEYTWIN_TEST_UNSET_KEY", raising=False)
    model = st.Model.http("claude", "http://127.0.0.1:9/v1/messages", layout="anthropic",
                          headers={"x-api-key": st.env("SURVEYTWIN_TEST_UNSET_KEY")}, body={"model": "x"})
    assert model.missing_env() == ["SURVEYTWIN_TEST_UNSET_KEY"]
    data = qsf({"BL_A": ("A", ["QID1"])}, {"QID1": te("QID1", "Why?")}, [block("BL_A")])
    config = st.configure(bank=mini_bank, sample_size=1, seed=3, model=model)
    sent = []
    import httpx
    monkeypatch.setattr(httpx.AsyncClient, "send", lambda *a, **k: sent.append(a) or (_ for _ in ()).throw(AssertionError("no call")))
    with pytest.raises(st.ConfigError, match="SURVEYTWIN_TEST_UNSET_KEY"):
        st.run(st.plan(st.read_qsf(data), config), image_checker=accept_all_images)
    assert sent == []  # every call used to be made, fail and be tried again; all answers skipped
    monkeypatch.setenv("SURVEYTWIN_TEST_UNSET_KEY", "set")
    assert model.missing_env() == []


# ---- 5. summary statistics count a repaired answer as answered ---------------------------------------


def test_summary_statistics_count_repaired_answers(mini_bank, tmp_path):
    from flow_kit import answering

    data = qsf({"BL_A": ("A", ["QID1"])}, {"QID1": mc("QID1", "Fruit?", ["Apple", "Pear"])}, [block("BL_A")])
    config = st.configure(bank=mini_bank, sample_size=3, seed=3, model=answering(lambda q: "Grape"))
    results = st.run(st.plan(st.read_qsf(data), config), image_checker=accept_all_images)
    before = next(m["metric"] for m in results.summary_stats() if m["metric_key"] == "QID1")
    assert before["n_out_of_range"] == 3
    results.save(tmp_path / "run")
    fixed = st.Results.load(tmp_path / "run").rerun_flagged(model=answering(lambda q: "Pear"))
    after = next(m["metric"] for m in fixed.summary_stats() if m["metric_key"] == "QID1")
    assert after["n_out_of_range"] == 0 and after["n"] == 3  # still counted out of range and low quality
    assert not after.get("low_quality")


# ---- 6. calls through OpenRouter keep the GPT-5 reasoning setting and the images ---------------------


def _openrouter_requests(monkeypatch):
    from surveytwin.providers.openai_client import OpenAIClient

    sent: list[dict] = []

    async def fake_post(self, *, url, api_key, payload, **_kwargs):
        sent.append({"url": url, "payload": payload})
        return {"choices": [{"message": {"content": '{"answers": []}'}, "finish_reason": "stop"}], "usage": {}}

    monkeypatch.setattr(OpenAIClient, "_post", fake_post)
    return OpenAIClient(openrouter_api_key="test-key"), sent


def test_a_gpt5_model_through_openrouter_gets_its_reasoning_setting(monkeypatch):
    import asyncio

    client, sent = _openrouter_requests(monkeypatch)
    asyncio.run(client.chat_text("gpt-5.4-high", "system", "user", max_tokens=1000))
    payload = sent[-1]["payload"]
    assert payload["model"] == "openai/gpt-5.4"
    assert payload["reasoning"] == {"effort": "high"}  # was left out, and a temperature sent instead
    assert "temperature" not in payload
    assert payload["max_tokens"] > 1000  # room for the reasoning too


def test_images_go_through_openrouter(monkeypatch):
    import asyncio

    client, sent = _openrouter_requests(monkeypatch)
    items = [
        {"role": "system", "content": [{"type": "input_text", "text": "Answer as the twin."}]},
        {"role": "user", "content": [{"type": "input_text", "text": "Look at the ad."},
                                     {"type": "input_image", "image_url": "https://example.org/ad.png"},
                                     {"type": "input_text", "text": "Do you like it?"}]},
    ]
    asyncio.run(client.responses_input_text(model="gpt-4.1", input_items=items, max_tokens=500))
    messages = sent[-1]["payload"]["messages"]
    assert messages[0] == {"role": "system", "content": "Answer as the twin."}
    parts = messages[1]["content"]
    assert {"type": "image_url", "image_url": {"url": "https://example.org/ad.png"}} in parts
    assert "[Image attachment provided by the user.]" not in str(messages)  # the image was replaced by this line


def test_a_picture_openai_cannot_download_skips_its_question_even_through_openrouter(monkeypatch):
    """OpenAI rejects the whole call when it cannot download one image. With an
    OpenRouter key the client retries through OpenRouter, now with the image,
    which fails too. The question that shows the image must then be recorded
    as skipped (never answered without it) and the others asked again, with a
    note: the OpenRouter error alone does not say the image was the problem,
    and its 400 would otherwise end the call as permanent."""
    import asyncio
    import json

    import httpx
    from surveytwin.answers import executor
    from surveytwin.providers.caller import PresetCaller
    from surveytwin.providers.models import Model
    from surveytwin.providers.openai_client import OpenAIClient

    monkeypatch.setattr(executor, "RETRY_BACKOFF_BASE_S", 0.0)
    answer = json.dumps({"answers": [
        {"question_id": "Q1", "answer_value": "b", "answer_label": "b", "answer_raw": "b"},
        {"question_id": "Q2", "answer_value": "Yes", "answer_label": "Yes", "answer_raw": "Yes"},
    ]})
    calls: list[str] = []

    def sends_an_image(payload):
        items = payload.get("input") if isinstance(payload.get("input"), list) else payload.get("messages") or []
        return any(isinstance(part, dict) and part.get("type") in {"input_image", "image_url"}
                   for item in items if isinstance(item, dict) and isinstance(item.get("content"), list)
                   for part in item["content"])

    class Providers:
        is_closed = False

        async def post(self, url, *, headers, json, timeout):
            request = httpx.Request("POST", url)
            provider = "openai" if "api.openai.com" in url else "openrouter"
            image = sends_an_image(json)
            calls.append(provider + (" with the image" if image else ""))
            if image and provider == "openai":
                return httpx.Response(400, request=request, json={"error": {
                    "message": "Error while downloading https://dead.example.com/shoe.png.", "code": "invalid_image_url"}})
            if image:
                return httpx.Response(400, request=request, json={"error": {"message": "Provider returned error", "code": 400}})
            if url.endswith("/responses"):
                return httpx.Response(200, request=request, json={"output_text": answer})
            return httpx.Response(200, request=request, json={"choices": [{"message": {"content": answer}, "finish_reason": "stop"}]})

    client = OpenAIClient(openai_api_key="test-key", openrouter_api_key="test-key")
    client._client = Providers()
    notes: list[str] = []
    answers = asyncio.run(executor._llm_answer_questions(
        PresetCaller(Model.preset("gpt-4.1"), client),
        "Persona text",
        [{"id": "Q1", "text": "Which one shows a shoe?", "type": "single_choice", "options": ["a", "b"],
          "question_media": [{"kind": "image", "src": "https://dead.example.com/shoe.png"}]},
         {"id": "Q2", "text": "Do you run?", "type": "single_choice", "options": ["Yes", "No"]}],
        notes=notes,
    ))
    assert calls == ["openai with the image", "openrouter with the image", "openai"]  # stopped after two calls
    assert answers[0]["answer_status"] == executor.ANSWER_STATUS_SKIPPED  # not answered without its picture
    assert answers[1]["answer_label"] == "Yes" and "answer_status" not in answers[1]
    assert notes == ["the model could not load https://dead.example.com/shoe.png; Q1 was not asked and recorded as skipped"]


# ---- 7. collecting a batch: prompts, warnings, images, output room -----------------------------------


def _finished_batch(plan, monkeypatch, *, lines):
    import json

    reply = json.dumps({"answers": [{"question_id": "QID1", "answer_value": "Pear", "answer_label": "Pear", "answer_raw": "Pear"}]})

    class FinishedBatch:
        on_usage = None

        async def poll_chat_batch_once(self, batch_id):
            return {"status": "completed"}

        async def collect_chat_batch_results(self, batch_id, **kwargs):
            return {str(index): reply for index in range(lines)}

        async def chat_json(self, **kwargs):  # a twin asked again in real time
            return json.loads(reply)

        async def responses_input_text(self, **kwargs):
            return reply

        async def close(self):
            pass

    monkeypatch.setattr(st.batch, "_client", lambda api_key: FinishedBatch())


def _batch_plan(mini_bank, *, accept=False):
    data = qsf({"BL_A": ("A", ["QID1"])}, {"QID1": mc("QID1", "Fruit?", ["Apple", "Pear"])}, [block("BL_A")])
    data["SurveyElements"][-1]["Payload"]["QuestionText"] = 'Fruit? <img src="https://example.org/fruit.png">'
    return st.plan(st.read_qsf(data), st.configure(bank=mini_bank, sample_size=2, seed=5, model="gpt-4o-mini",
                                                   accept_missing_images=accept, filters=None))


def test_collect_keeps_prompts_warns_and_tells_what_was_asked_again(mini_bank, monkeypatch):
    plan = _batch_plan(mini_bank)
    _finished_batch(plan, monkeypatch, lines=1)  # the second twin's line is missing
    results = st.batch.collect("batch_1", plan, keep_prompts=True, image_checker=lambda url: {"status": "ok"})
    kinds = [p["kind"] for p in results.prompts]
    assert kinds.count("batch") == 2  # keep_prompts had no effect
    assert any("1 batch answer(s) were unusable" in w for w in results.warnings)
    assert all(w in results.warnings for w in plan.warnings)


def test_an_image_that_broke_after_submitting_does_not_stop_the_collection(mini_bank, monkeypatch):
    plan = _batch_plan(mini_bank)
    _finished_batch(plan, monkeypatch, lines=2)
    results = st.batch.collect("batch_1", plan, image_checker=lambda url: {"status": "unreachable"})  # raised ImagesRefused
    assert any("could no longer be sent" in w for w in results.warnings)
    assert [a.label for a in results.answers] == ["Pear", "Pear"]


def test_a_gpt5_batch_line_has_room_for_reasoning():
    from surveytwin.providers.openai_client import OpenAIClient

    body = OpenAIClient().build_chat_request_body(model="gpt-5.4-high", messages=[], max_tokens=1000)
    assert body["max_completion_tokens"] > 1000  # was the answer budget only


# ---- 9. nothing private in what a user reads ---------------------------------------------------------


def test_the_user_documents_and_messages_name_no_website_or_internal_notes():
    import ast
    import json
    import re
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    pattern = re.compile(r"website(?!_policy)|open question|pr #\d", re.IGNORECASE)
    documents = [root / "README.md", root / "docs" / "builder.md", *sorted((root / "docs" / "reference").glob("*.md"))]
    found = [f"{d.name}: {m.group(0)}" for d in documents for m in pattern.finditer(d.read_text(encoding="utf-8"))]
    table = json.loads((root / "src" / "surveytwin" / "data" / "support_table.json").read_text(encoding="utf-8"))
    found += [f"support_table source: {m.group(0)}" for m in pattern.finditer(table["source"])]
    # the strings of the code, docstrings aside (they are for the people working on it)
    for path in sorted((root / "src" / "surveytwin").rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        docstrings = {
            id(node.body[0].value)
            for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Module))
            and node.body and isinstance(node.body[0], ast.Expr) and isinstance(node.body[0].value, ast.Constant)
        }
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docstrings:
                found += [f"{path.name}:{node.lineno}: {m.group(0)}" for m in pattern.finditer(node.value) if node.value != "WEBSITE_POLICY"]
    assert not found, found
