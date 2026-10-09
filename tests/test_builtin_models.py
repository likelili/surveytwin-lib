"""Built-in models: OpenAI, OpenRouter, Claude, Gemini and DeepSeek, each a model
object that knows how to call its API and where its key is."""

from __future__ import annotations

import asyncio
import base64
import json

import httpx
import pytest

import surveytwin as st
from surveytwin.providers.caller import make_caller
from surveytwin.providers.openai_client import OpenAIClient
from surveytwin.run import _fallback_calls, require_model_env
from test_models import Recorder, call, request

PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==")
GPT5_REPLY = {"status": "completed", "output": [{"type": "message", "content": [{"type": "output_text", "text": '{"answers": []}'}]}], "usage": {"input_tokens": 3, "output_tokens": 2}}


# ---- OpenAI and OpenRouter ---------------------------------------------------------------


def test_an_openai_model_by_name_is_the_preset_and_knows_its_key():
    assert st.Model.preset("gpt-5.4-low") == st.Model.openai("gpt-5.4-low")
    model = st.Model.openai("gpt-5.4-low")
    assert (model.kind, model.provider, model.api_key_env, model.fallback_to_openrouter) == ("preset", "openai", "OPENAI_API_KEY", False)
    assert model.settings["reasoning_effort"] == "low" and model.settings["api_key_env"] == "OPENAI_API_KEY"
    router = st.Model.openrouter("gpt-5.4-low")
    assert (router.provider, router.api_key_env) == ("openrouter", "OPENROUTER_API_KEY")


def test_openrouter_is_a_fallback_only_when_asked_for(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-openai")
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-router")
    plain = make_caller(st.Model.openai("gpt-4o-mini"), api_key=None, on_usage=None)
    assert plain.client.openai_api_key == "sk-openai" and plain.client.openrouter_api_key == ""
    asked = make_caller(st.Model.openai("gpt-4o-mini", fallback_to_openrouter=True), api_key=None, on_usage=None)
    assert asked.client.openrouter_api_key == "sk-router"
    assert st.Model.openai("gpt-4o-mini", fallback_to_openrouter=True).settings["fallback_to_openrouter"] is True


def test_a_run_warns_how_many_calls_went_to_openrouter():
    usage = [{"provider": "openai", "status": "failed"}, {"provider": "openrouter", "status": "completed"}]
    assert _fallback_calls(st.Model.openai("gpt-4o-mini", fallback_to_openrouter=True), usage) == 1
    assert _fallback_calls(st.Model.anthropic("claude-opus-5-5"), usage) == 0


def test_a_reasoning_effort_replaces_the_presets_own(monkeypatch):
    rec = Recorder(GPT5_REPLY)
    caller = make_caller(st.Model.openai("gpt-5.4-medium", reasoning_effort="low"), api_key="sk-test", on_usage=None)
    caller.client._client = httpx.AsyncClient(transport=httpx.MockTransport(rec))

    async def go():
        try:
            return await caller.chat_json(system_prompt="s", user_prompt="u", temperature=0.3, max_tokens=500)
        finally:
            await caller.close()

    assert asyncio.run(go()) == {"answers": []}
    assert rec.body["reasoning"] == {"effort": "low"}
    line = OpenAIClient().build_chat_request_body(model="gpt-5.4-medium", messages=[], reasoning_effort="low")
    assert line["reasoning_effort"] == "low"  # a batch line too


def test_without_a_key_the_run_says_which_variable(monkeypatch):
    monkeypatch.delenv("MY_OPENAI", raising=False)
    model = st.Model.openai("gpt-4o-mini", api_key_env="MY_OPENAI")
    assert model.missing_env() == ["MY_OPENAI"]
    with pytest.raises(st.ConfigError, match="MY_OPENAI"):
        require_model_env(model)
    require_model_env(model, api_key="sk-given")  # a key given stands for it


# ---- Claude, Gemini, DeepSeek ------------------------------------------------------------


def test_claude_through_the_messages_api(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant")
    model = st.Model.anthropic("claude-opus-5-5", effort="low")
    rec = Recorder({
        "content": [{"type": "thinking", "thinking": ""}, {"type": "text", "text": '{"answers": []}'}],
        "usage": {"input_tokens": 12, "output_tokens": 7},
        "stop_reason": "end_turn",
    })
    reply, _meta = call(model, request(), rec)
    sent = rec.requests[0]
    assert str(sent.url) == "https://api.anthropic.com/v1/messages"
    assert sent.headers["x-api-key"] == "sk-ant" and sent.headers["anthropic-version"] == "2023-06-01"
    body = rec.body
    assert body["model"] == "claude-opus-5-5" and body["output_config"] == {"effort": "low"}
    assert body["system"] == "You are a survey respondent." and body["max_tokens"] == 300
    assert "temperature" not in body  # current Claude models refuse one
    assert (reply.text, reply.input_tokens, reply.output_tokens) == ('{"answers": []}', 12, 7)
    assert st.Model.anthropic("claude-3-haiku", temperature=0.2).body["temperature"] == 0.2


def test_gemini_through_generate_content(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "g-key")
    model = st.Model.gemini("gemini-2.5-pro", temperature=0.5)
    rec = Recorder({"candidates": [{"content": {"parts": [{"text": '{"answers": []}'}]}, "finishReason": "STOP"}], "usageMetadata": {"promptTokenCount": 9, "candidatesTokenCount": 4}})
    reply, _meta = call(model, request(), rec)
    sent = rec.requests[0]
    assert str(sent.url) == "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-pro:generateContent"
    assert sent.headers["x-goog-api-key"] == "g-key"
    assert rec.body["generationConfig"]["temperature"] == 0.5
    assert reply.text == '{"answers": []}'


def test_deepseek_through_its_openai_compatible_api(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "ds-key")
    model = st.Model.deepseek()
    assert model.name == "deepseek-chat" and model.images is False
    rec = Recorder({"choices": [{"message": {"content": '{"answers": []}'}, "finish_reason": "stop"}], "usage": {"prompt_tokens": 5, "completion_tokens": 2}})
    reply, _meta = call(model, request(), rec)
    assert str(rec.requests[0].url) == "https://api.deepseek.com/chat/completions"
    assert rec.requests[0].headers["authorization"] == "Bearer ds-key"
    assert rec.body["model"] == "deepseek-chat" and reply.text == '{"answers": []}'


def test_a_key_given_to_the_call_replaces_the_models_own(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    model = st.Model.anthropic("claude-opus-5-5")
    rec = Recorder({"content": [{"type": "text", "text": "ok"}], "usage": {}, "stop_reason": "end_turn"})

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(rec)) as client:
            return await model.acall(request(), client=client, api_key="sk-given")

    asyncio.run(go())
    assert rec.requests[0].headers["x-api-key"] == "sk-given"


# ---- asking a model directly ---------------------------------------------------------------


def test_ask_sends_one_request_with_pictures_from_files(tmp_path):
    picture = tmp_path / "ad.png"
    picture.write_bytes(PNG)
    seen = []

    def reply(req: st.ModelRequest) -> st.Reply:
        seen.append(req)
        return st.Reply(text="a red ad", input_tokens=5, output_tokens=3)

    model = st.Model.from_function(reply, name="f", images=True)
    out = model.ask("What is in this picture?", system="Be brief.", images=[str(picture)])
    assert out.text == "a red ad" and out.input_tokens == 5
    assert seen[0].system == "Be brief." and seen[0].images == ["data:image/png;base64," + base64.b64encode(PNG).decode("ascii")]
    with pytest.raises(st.ConfigError, match="no picture file"):
        model.ask("?", images=[str(tmp_path / "missing.png")])
    with pytest.raises(st.ConfigError, match="takes no image input"):
        st.Model.from_function(reply, name="text-only").ask("?", images=[str(picture)])


def test_ask_an_openai_model(monkeypatch):
    rec = Recorder({"choices": [{"message": {"content": "hello"}, "finish_reason": "stop"}], "usage": {"prompt_tokens": 4, "completion_tokens": 1}})

    def client_with_transport(self, *, api_key=None, on_usage=None):
        client = OpenAIClient(openai_api_key="sk-test", on_usage=on_usage)
        client._client = httpx.AsyncClient(transport=httpx.MockTransport(rec))
        return client

    monkeypatch.setattr(st.Model, "openai_client", client_with_transport)
    out = st.Model.openai("gpt-4o-mini").ask("Say hello.")
    assert (out.text, out.input_tokens, out.output_tokens) == ("hello", 4, 1)
    assert rec.body["messages"][-1]["content"] == "Say hello."


# ---- settings: made again, never a secret ---------------------------------------------------


@pytest.mark.parametrize(
    "model",
    [
        st.Model.openai("gpt-5.4-medium", reasoning_effort="low", fallback_to_openrouter=True),
        st.Model.openrouter("gpt-5.4-low", api_key_env="MY_ROUTER"),
        st.Model.anthropic("claude-opus-5-5", effort="high", api_key_env="MY_CLAUDE"),
        st.Model.anthropic("claude-3-haiku", temperature=0.2, body={"metadata": {"user_id": "study"}}),
        st.Model.gemini("gemini-2.5-pro", temperature=0.7),
        st.Model.deepseek("deepseek-reasoner", body={"top_p": 0.9}),
    ],
)
def test_a_built_in_model_is_made_again_from_its_settings(model):
    again = st.Model.from_settings(json.loads(json.dumps(model.settings)))
    assert again is not None and again.settings == model.settings


def test_a_model_of_your_own_is_not_made_from_settings():
    assert st.Model.from_settings(st.Model.http("x", "https://llm.example/v1", layout="openai-chat").settings) is None
    assert st.Model.from_settings(st.Model.from_function(lambda r: "x", name="f").settings) is None


def test_a_secret_written_in_a_request_body_is_never_recorded():
    model = st.Model.http(
        "x", "https://llm.example/v1", layout="openai-chat",
        body={"model": "m", "api_key": "sk-123", "options": {"auth_token": "abc", "top_p": 0.9}},
    )
    body = model.settings["body"]
    assert body == {"model": "m", "api_key": "<hidden>", "options": {"auth_token": "<hidden>", "top_p": 0.9}}
    assert "sk-123" not in json.dumps(model.settings)
