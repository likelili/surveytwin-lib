"""Each built-in model's request and reply, as the providers' own documents give
them (checked 2026-10-09), and what a run records about a model.

- Claude (Messages API): no temperature (current models refuse one); thinking
  counts toward ``max_tokens``; the input is ``input_tokens`` plus the cache's;
  a refusal is no answer.
- Gemini (generateContent): no temperature (Gemini 3 should keep its default);
  thinking counts toward ``maxOutputTokens``; a safety stop or a blocked prompt
  is no answer.
- DeepSeek: ``deepseek-flash`` (pictures) and ``deepseek-v4-pro``; thinking by
  default; cached tokens as ``prompt_cache_hit_tokens``.
- OpenAI: GPT-5 and later and the o-series are reasoning models; chat completions
  take ``max_completion_tokens``.
"""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

import surveytwin as st
from conftest import TESTS
from surveytwin.answers.executor import _is_image_input_error
from surveytwin.providers.caller import make_caller
from surveytwin.providers.openai_client import OpenAIClient
from test_models import Recorder, call, request

BANK = TESTS.parent / "examples" / "example-bank"
CLAUDE_REPLY = {"content": [{"type": "text", "text": '{"answers": []}'}], "usage": {"input_tokens": 10, "output_tokens": 5}, "stop_reason": "end_turn"}


def run_caller(model: st.Model, reply: dict) -> tuple[object, list[dict], list]:
    """One call through the caller a run uses: (its result or the error, the bodies sent, the usage records)."""
    recorded: list = []
    bodies: list[dict] = []

    def handler(req: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(req.content))
        return httpx.Response(200, json=reply)

    async def go():
        caller = make_caller(model, api_key="SYNTHETIC_ONLY", on_usage=recorded.append)
        caller._http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        try:
            return await caller.chat_json(system_prompt="s", user_prompt="Answer in JSON.", temperature=0.3, max_tokens=100)
        except Exception as exc:  # noqa: BLE001 - the error is the result here
            return exc
        finally:
            await caller.close()

    return asyncio.run(go()), bodies, recorded


# ---- what a run records about a model, and making it again ---------------------------------


@pytest.mark.parametrize(
    "model",
    [
        st.Model.anthropic("example-claude", body={"max_tokens": 1234}, timeout_s=17),
        st.Model.anthropic("c", effort="low", temperature=0.5, images=False, url="https://proxy.example/v1/messages", timeout_s=30, thinking_room=500),
        st.Model.gemini("example-gemini", body={"generationConfig": {"maxOutputTokens": 1234}}, timeout_s=17),
        st.Model.gemini("g", temperature=1.0, thinking_level="low", images=False, timeout_s=33, body={"safetySettings": [{"category": "X"}]}),
        st.Model.gemini("g25", body={"generationConfig": {"thinkingConfig": {"thinkingBudget": 1024}}}),
        st.Model.deepseek(body={"max_tokens": 1234}),
        st.Model.deepseek("deepseek-v4-pro", reasoning_effort="max", api_key_env="DS", base_url="https://alt.example", timeout_s=99, body={"user": "u"}),
        st.Model.deepseek(reasoning_effort="none"),
        st.Model.anthropic("with-env", body={"metadata": {"user_id": st.env("STUDY_USER")}}),
    ],
    ids=lambda m: f"{m.builtin}-{m.name}",
)
def test_every_setting_of_a_built_in_model_survives_its_settings(model, monkeypatch):
    restored = st.Model.from_settings(model.settings)
    assert restored is not None and restored.settings == model.settings
    for name in ("timeout_s", "thinking_room", "images", "url", "api_key_env"):
        assert getattr(restored, name) == getattr(model, name), name
    # and the request it sends is the same, token limits included
    monkeypatch.setenv(model.api_key_env, "SYNTHETIC_ONLY")
    monkeypatch.setenv("STUDY_USER", "u-1")
    reply = CLAUDE_REPLY if model.builtin == "anthropic" else (
        {"candidates": [{"content": {"parts": [{"text": "{}"}]}, "finishReason": "STOP"}]} if model.builtin == "gemini"
        else {"choices": [{"message": {"content": "{}"}, "finish_reason": "stop"}]}
    )
    first, again = Recorder(reply), Recorder(reply)
    call(model, request(), first)
    call(restored, request(), again)
    assert again.body == first.body


def test_a_token_limit_is_recorded_as_a_number_and_a_secret_whole():
    sentinel = "SYNTHETIC_AUDIT_VALUE_ONLY"
    model = st.Model.http(
        "probe", "https://example.invalid", layout="openai-chat",
        body={
            "max_tokens": 5, "budget_tokens": 6, "generationConfig": {"maxOutputTokens": 7},
            "credentials": {"value": sentinel, "nested": [sentinel]}, "api_key": sentinel,
            "auth": {"user": sentinel}, "access_token": sentinel, "key": st.env("SOME_KEY"),
        },
    )
    body = model.settings["body"]
    assert (body["max_tokens"], body["budget_tokens"], body["generationConfig"]) == (5, 6, {"maxOutputTokens": 7})
    assert body["credentials"] == {"value": "<hidden>", "nested": ["<hidden>"]}
    assert body["auth"] == {"user": "<hidden>"} and body["access_token"] == body["api_key"] == "<hidden>"
    assert body["key"] == "<env SOME_KEY>"
    record = st.configure(bank=BANK, sample_size=1, model=model).to_record()
    assert sentinel not in json.dumps(model.settings) and sentinel not in json.dumps(record, default=str)


def test_a_built_in_model_whose_body_hides_a_secret_is_not_made_again():
    model = st.Model.anthropic("c", body={"metadata": {"api_key": "plain-text"}})
    assert st.Model.from_settings(model.settings) is None


def test_usage_names_the_provider_and_counts_cached_and_thinking_tokens():
    claude = {
        "content": [{"type": "thinking", "thinking": "", "signature": "s"}, {"type": "text", "text": '{"answers": []}'}],
        "usage": {"input_tokens": 10, "cache_creation_input_tokens": 3, "cache_read_input_tokens": 7, "output_tokens": 50,
                  "output_tokens_details": {"thinking_tokens": 40}},
        "stop_reason": "end_turn",
    }
    gemini = {
        "candidates": [{"content": {"parts": [{"text": '{"answers": []}'}]}, "finishReason": "STOP"}],
        "usageMetadata": {"promptTokenCount": 12, "cachedContentTokenCount": 4, "candidatesTokenCount": 6, "thoughtsTokenCount": 30},
    }
    deepseek = {
        "choices": [{"message": {"content": '{"answers": []}', "reasoning_content": "..."}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 20, "completion_tokens": 30, "prompt_cache_hit_tokens": 5, "prompt_cache_miss_tokens": 15,
                  "completion_tokens_details": {"reasoning_tokens": 25}},
    }
    seen = []
    for model, reply in ((st.Model.anthropic("claude-opus-5-5"), claude), (st.Model.gemini("gemini-3.1-pro-preview"), gemini), (st.Model.deepseek(), deepseek)):
        result, _bodies, records = run_caller(model, reply)
        assert result == {"answers": []}
        r = records[0]
        seen.append((r.provider, r.input_tokens, r.cached_input_tokens, r.output_tokens, r.reasoning_tokens, r.extra["kind"]))
    assert seen == [
        ("anthropic", 20, 7, 50, 40, "http"),  # the input is the uncached tokens and the cache's
        ("gemini", 12, 4, 36, 30, "http"),  # the output is the answer and the thinking
        ("deepseek", 20, 5, 30, 25, "openai-compatible"),
    ]
    _result, _bodies, records = run_caller(st.Model.http("mine", "https://x.example", layout="openai-chat"), deepseek)
    assert records[0].provider == "http"


# ---- Claude ----------------------------------------------------------------------------------


def test_claude_gets_room_to_think_and_no_temperature():
    _result, bodies, _records = run_caller(st.Model.anthropic("claude-opus-5-5"), CLAUDE_REPLY)
    assert bodies[0]["max_tokens"] == 100 + 16000 and "temperature" not in bodies[0]
    assert st.Model.anthropic("claude-opus-5-5", effort="max").thinking_room == 32000
    assert st.Model.anthropic("claude-opus-4-6", thinking_room=0).thinking_room == 0


def test_a_refusal_is_no_answer_and_says_why():
    refusal = {"content": [], "usage": {"input_tokens": 10, "output_tokens": 2}, "stop_reason": "refusal",
               "stop_details": {"type": "refusal", "category": "cyber", "explanation": "Declined."}}
    error, _bodies, records = run_caller(st.Model.anthropic("claude-opus-5-5"), refusal)
    assert isinstance(error, RuntimeError) and "gave no answer: refusal (cyber; Declined.)" in str(error)
    assert [(r.status, r.input_tokens) for r in records] == [("stopped", 10)]  # the call's tokens still count


def test_an_empty_system_text_is_left_out(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "SYNTHETIC_ONLY")
    rec = Recorder(CLAUDE_REPLY)
    req = request()
    req.system = ""
    call(st.Model.anthropic("claude-opus-5-5"), req, rec)
    assert "system" not in rec.body


# ---- Gemini ----------------------------------------------------------------------------------


def test_gemini_sends_no_temperature_and_gets_room_to_think():
    reply = {"candidates": [{"content": {"parts": [{"text": '{"answers": []}'}]}, "finishReason": "STOP"}]}
    _result, bodies, _records = run_caller(st.Model.gemini("gemini-3.1-pro-preview"), reply)
    assert bodies[0]["generationConfig"] == {"maxOutputTokens": 100 + 16000, "responseMimeType": "application/json"}
    model = st.Model.gemini("gemini-3.1-pro-preview", thinking_level="low", temperature=1.0)
    _result, bodies, _records = run_caller(model, reply)
    assert bodies[0]["generationConfig"] == {
        "maxOutputTokens": 100 + 2000, "responseMimeType": "application/json", "temperature": 1.0, "thinkingConfig": {"thinkingLevel": "low"},
    }
    assert st.Model.gemini("gemini-2.5-flash", body={"generationConfig": {"thinkingConfig": {"thinkingBudget": 0}}}).thinking_room == 0


@pytest.mark.parametrize(
    "reply, why",
    [
        ({"candidates": [{"content": {"parts": []}, "finishReason": "SAFETY"}]}, "the reply was stopped (SAFETY)"),
        ({"promptFeedback": {"blockReason": "PROHIBITED_CONTENT"}}, "the prompt was blocked (PROHIBITED_CONTENT)"),
    ],
)
def test_a_gemini_safety_stop_is_no_answer(reply, why):
    error, _bodies, _records = run_caller(st.Model.gemini("gemini-3.1-pro-preview"), reply)
    assert isinstance(error, RuntimeError) and why in str(error)


def test_a_picture_gemini_cannot_have_is_named_as_a_picture_error():
    async def go():
        transport = httpx.MockTransport(lambda req: httpx.Response(404))
        async with httpx.AsyncClient(transport=transport) as client:
            return await st.Model.gemini("g").acall(request(images=["https://example.org/gone.png"]), client=client)

    with pytest.raises(RuntimeError) as caught:
        asyncio.run(go())
    assert _is_image_input_error(caught.value) and "https://example.org/gone.png" in str(caught.value)


# ---- DeepSeek --------------------------------------------------------------------------------


def test_deepseek_defaults_to_its_current_model():
    model = st.Model.deepseek()
    assert (model.name, model.body["model"], model.images, model.thinking_room, model.timeout_s) == ("deepseek-flash", "deepseek-flash", True, 16000, 600.0)
    pro = st.Model.deepseek("deepseek-v4-pro", reasoning_effort="low")
    assert (pro.images, pro.thinking_room, pro.body["reasoning_effort"]) == (False, 2000, "low")
    assert st.Model.deepseek(body={"thinking": {"type": "disabled"}}).thinking_room == 0


@pytest.mark.parametrize("reason", ["content_filter", "insufficient_system_resource"])
def test_a_deepseek_reply_cut_short_is_no_answer(reason):
    reply = {"choices": [{"message": {"content": ""}, "finish_reason": reason}], "usage": {"prompt_tokens": 5, "completion_tokens": 0}}
    error, _bodies, _records = run_caller(st.Model.deepseek(), reply)
    assert isinstance(error, RuntimeError) and reason in str(error)


def test_ask_says_why_there_was_no_answer(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "SYNTHETIC_ONLY")
    original = httpx.AsyncClient

    def mocked(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(lambda req: httpx.Response(200, json={"choices": [{"message": {"content": None, "refusal": "No."}, "finish_reason": "stop"}]}))
        return original(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", mocked)
    with pytest.raises(st.ProviderError, match="gave no answer: refusal: No."):
        st.Model.deepseek().ask("Hello")


# ---- OpenAI ----------------------------------------------------------------------------------


@pytest.mark.parametrize("name, reasoning", [
    ("gpt-5.4", True), ("gpt-6-astra", True), ("gpt-6.1-sol", True), ("openai/gpt-6-astra", True), ("o3", True), ("o4-mini", True),
    ("ft:gpt-5-mini:org::x", True), ("gpt-4.1", False), ("gpt-4o-mini", False), ("ft:gpt-4.1-2025-04-14:org::y", False),
])
def test_openais_reasoning_models_are_recognized(name, reasoning):
    assert OpenAIClient._is_gpt5_model(name) is reasoning


def test_openai_chat_completions_name_the_limit_max_completion_tokens():
    sent = []

    def handler(req: httpx.Request) -> httpx.Response:
        sent.append((str(req.url), json.loads(req.content)))
        if req.url.path.endswith("/responses"):
            return httpx.Response(200, json={"status": "completed", "output": [{"type": "message", "content": [{"type": "output_text", "text": "{}"}]}], "usage": {}})
        return httpx.Response(200, json={"choices": [{"message": {"content": "{}"}, "finish_reason": "stop"}], "usage": {}})

    async def ask(client: OpenAIClient, model: str) -> None:
        client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        try:
            await client.chat_text(model, "s", "u", max_tokens=100, response_format={"type": "json_object"})
        finally:
            await client.close()

    asyncio.run(ask(OpenAIClient(openai_api_key="SYNTHETIC_ONLY"), "gpt-4.1"))
    asyncio.run(ask(OpenAIClient(openai_api_key="SYNTHETIC_ONLY"), "gpt-6-astra"))
    asyncio.run(ask(OpenAIClient(openrouter_api_key="SYNTHETIC_ONLY"), "gpt-4.1"))
    (url1, chat), (url2, responses), (url3, routed) = sent
    assert url1.endswith("/v1/chat/completions") and chat["max_completion_tokens"] == 100 and "max_tokens" not in chat and chat["temperature"] == 0.3
    assert url2.endswith("/v1/responses") and "temperature" not in responses  # a reasoning model
    assert url3.startswith("https://openrouter.ai/") and routed["max_tokens"] == 100  # OpenRouter's name for it
    body = OpenAIClient().build_chat_request_body(model="gpt-4.1", messages=[], max_tokens=100)
    assert body["max_completion_tokens"] == 100 and "max_tokens" not in body
