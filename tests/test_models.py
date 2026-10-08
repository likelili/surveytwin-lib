"""Models: the four request layouts, keys from the environment, retries, presets."""

from __future__ import annotations

import asyncio
import base64
import json

import httpx
import pytest

import surveytwin as st
from surveytwin.providers.caller import DefinedModelCaller, make_caller
from surveytwin.providers.openai_client import OpenAIClient

PNG = base64.b64encode(b"\x89PNG fake").decode()


def request(images: list[str] | None = None, json_mode: bool = True) -> st.ModelRequest:
    content = [{"type": "text", "text": "Question one?"}]
    for url in images or []:
        content.append({"type": "image", "url": url})
    content.append({"type": "text", "text": "Answer in JSON."})
    return st.ModelRequest(
        system="You are a survey respondent.",
        user="Question one?\n\nAnswer in JSON.",
        content=content,
        images=list(images or []),
        max_output_tokens=300,
        json_mode=json_mode,
    )


class Recorder:
    """An httpx transport that records requests and returns a canned reply."""

    def __init__(self, reply: dict, status: int = 200) -> None:
        self.reply = reply
        self.status = status
        self.requests: list[httpx.Request] = []

    def __call__(self, req: httpx.Request) -> httpx.Response:
        self.requests.append(req)
        if req.method == "GET":
            return httpx.Response(200, content=b"\x89PNG fake", headers={"content-type": "image/png"})
        return httpx.Response(self.status, json=self.reply, headers={"x-request-id": "req_1"})

    @property
    def body(self) -> dict:
        posts = [r for r in self.requests if r.method == "POST"]
        return json.loads(posts[-1].content)


def call(model: st.Model, req: st.ModelRequest, recorder: Recorder):
    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(recorder)) as client:
            return await model.acall(req, client=client)

    return asyncio.run(go())


# ---- the four layouts ---------------------------------------------------------------


def test_openai_chat_layout(monkeypatch):
    monkeypatch.setenv("MY_KEY", "sk-test")
    model = st.Model.http(
        "local", "https://llm.example/v1/chat/completions",
        headers={"Authorization": st.env("MY_KEY")}, layout="openai-chat", body={"model": "m1"},
    )
    rec = Recorder({"choices": [{"message": {"content": '{"answers": []}'}, "finish_reason": "stop"}], "usage": {"prompt_tokens": 11, "completion_tokens": 5}})
    reply, meta = call(model, request(), rec)
    body = rec.body
    assert body["model"] == "m1"
    assert body["messages"][0] == {"role": "system", "content": "You are a survey respondent."}
    assert body["messages"][1]["content"] == "Question one?\n\nAnswer in JSON."
    assert body["response_format"] == {"type": "json_object"}
    assert body["max_tokens"] == 300
    assert rec.requests[0].headers["authorization"] == "sk-test"
    assert (reply.text, reply.input_tokens, reply.output_tokens, reply.cut_off) == ('{"answers": []}', 11, 5, False)
    assert reply.request_id == "req_1"


def test_openai_chat_layout_with_images_and_cut_off():
    model = st.Model.http("local", "https://llm.example/v1/chat/completions", layout="openai-chat")
    rec = Recorder({"choices": [{"message": {"content": "partial"}, "finish_reason": "length"}], "usage": {}})
    reply, _ = call(model, request(images=["https://img.example/a.png"]), rec)
    parts = rec.body["messages"][1]["content"]
    assert [p["type"] for p in parts] == ["text", "image_url", "text"]
    assert parts[1]["image_url"]["url"] == "https://img.example/a.png"
    assert reply.cut_off


def test_openai_responses_layout():
    model = st.Model.http("r", "https://llm.example/v1/responses", layout="openai-responses", body={"model": "m2"})
    rec = Recorder({
        "status": "incomplete", "incomplete_details": {"reason": "max_output_tokens"},
        "output": [{"type": "message", "content": [{"type": "output_text", "text": "hi"}]}],
        "usage": {"input_tokens": 7, "output_tokens": 3},
    })
    reply, _ = call(model, request(images=["https://img.example/a.png"]), rec)
    body = rec.body
    assert body["input"][0]["content"][0] == {"type": "input_text", "text": "You are a survey respondent."}
    assert [p["type"] for p in body["input"][1]["content"]] == ["input_text", "input_image", "input_text"]
    assert body["max_output_tokens"] == 300
    assert body["text"] == {"format": {"type": "json_object"}}
    assert (reply.text, reply.input_tokens, reply.output_tokens, reply.cut_off) == ("hi", 7, 3, True)


def test_anthropic_layout():
    model = st.Model.http(
        "claude", "https://api.anthropic.example/v1/messages", layout="anthropic",
        headers={"x-api-key": "k", "anthropic-version": "2023-06-01"}, body={"model": "c1"},
    )
    rec = Recorder({"content": [{"type": "text", "text": '{"answers": []}'}], "usage": {"input_tokens": 9, "output_tokens": 4}, "stop_reason": "max_tokens"})
    reply, meta = call(model, request(images=["https://img.example/a.png", f"data:image/png;base64,{PNG}"]), rec)
    body = rec.body
    assert body["system"] == "You are a survey respondent."
    content = body["messages"][0]["content"]
    assert content[1] == {"type": "image", "source": {"type": "url", "url": "https://img.example/a.png"}}
    assert content[2]["source"] == {"type": "base64", "media_type": "image/png", "data": PNG}
    assert "response_format" not in body
    assert reply.cut_off and meta["stop_reason"] == "max_tokens"
    assert (reply.input_tokens, reply.output_tokens) == (9, 4)


def test_gemini_layout_downloads_images_and_skips_thoughts():
    model = st.Model.http("gem", "https://gemini.example/v1beta/models/g:generateContent", layout="gemini")
    rec = Recorder({
        "candidates": [{"content": {"parts": [{"text": "thinking", "thought": True}, {"text": "answer"}]}, "finishReason": "STOP"}],
        "usageMetadata": {"promptTokenCount": 20, "candidatesTokenCount": 5, "thoughtsTokenCount": 10},
    })
    reply, _ = call(model, request(images=["https://img.example/a.png"]), rec)
    body = rec.body
    assert body["systemInstruction"] == {"parts": [{"text": "You are a survey respondent."}]}
    parts = body["contents"][0]["parts"]
    assert parts[1]["inlineData"]["mimeType"] == "image/png"
    assert body["generationConfig"]["responseMimeType"] == "application/json"
    assert [r.method for r in rec.requests] == ["GET", "POST"]
    assert (reply.text, reply.input_tokens, reply.output_tokens, reply.cut_off) == ("answer", 20, 15, False)


# ---- overrides and validation -----------------------------------------------------------


def test_body_fields_merge_and_none_removes():
    model = st.Model.http("m", "https://x.example/v1/chat/completions", layout="openai-chat", body={"temperature": None, "top_p": 0.5})
    rec = Recorder({"choices": [{"message": {"content": "{}"}, "finish_reason": "stop"}]})
    call(model, request(), rec)
    assert "temperature" not in rec.body and rec.body["top_p"] == 0.5


def test_key_overrides_for_an_unusual_reply():
    model = st.Model.http(
        "odd", "https://x.example/generate", layout="openai-chat",
        text_key="result.0.text", usage_keys={"input_tokens": "meta.in", "output_tokens": "meta.out"},
        stop_key="result.0.why", cut_off_values=["limit"],
    )
    rec = Recorder({"result": [{"text": "hello", "why": "limit"}], "meta": {"in": 3, "out": 2}})
    reply, _ = call(model, request(), rec)
    assert (reply.text, reply.input_tokens, reply.output_tokens, reply.cut_off) == ("hello", 3, 2, True)
    rec = Recorder({"result": [{"text": "hello", "why": "done"}], "meta": {}})
    reply, _ = call(model, request(), rec)
    assert not reply.cut_off and reply.input_tokens is None


@pytest.mark.parametrize(
    "kwargs, message",
    [
        ({"layout": "cohere"}, "layout must be one of"),
        ({"layout": "openai-chat", "url": "ftp://x"}, "http"),
        ({"layout": "openai-chat", "stop_key": "a.b"}, "cut_off_values"),
    ],
)
def test_defined_model_validation(kwargs, message):
    args = {"url": "https://x.example/v1", **kwargs}
    with pytest.raises(st.ConfigError, match=message):
        st.Model.http("m", **args)


def test_settings_never_hold_keys(monkeypatch):
    monkeypatch.setenv("SECRET_KEY", "sk-very-secret")
    model = st.Model.http(
        "m", "https://x.example/v1/chat/completions?key=abc", layout="openai-chat",
        headers={"Authorization": st.env("SECRET_KEY")}, body={"api_key": st.env("SECRET_KEY")},
    )
    text = json.dumps(model.settings)
    assert "sk-very-secret" not in text and "key=abc" not in text
    assert model.settings["body"]["api_key"] == "<env SECRET_KEY>"
    assert model.settings["headers"] == {"Authorization": "<hidden>"}


def test_missing_environment_variable_fails_at_call_time():
    model = st.Model.http("m", "https://x.example/v1/chat/completions", layout="openai-chat", headers={"Authorization": st.env("NOT_SET_ANYWHERE")})
    with pytest.raises(st.ConfigError, match="NOT_SET_ANYWHERE"):
        call(model, request(), Recorder({}))


def test_openai_compatible_sends_a_bearer_key(monkeypatch):
    monkeypatch.setenv("LOCAL_KEY", "abc")
    model = st.Model.openai_compatible("llama", "http://localhost:8000/v1/", api_key_env="LOCAL_KEY")
    assert model.url == "http://localhost:8000/v1/chat/completions"
    rec = Recorder({"choices": [{"message": {"content": "{}"}, "finish_reason": "stop"}]})
    call(model, request(), rec)
    assert rec.requests[0].headers["authorization"] == "Bearer abc"
    assert rec.body["model"] == "llama"


def test_check_with_a_function_model():
    model = st.Model.from_function(lambda r: st.Reply(text='{"ok": true}', input_tokens=1, output_tokens=1), name="f")
    result = model.check()
    assert result.ok and result.text_found and result.usage_found


def test_check_reports_a_failing_endpoint():
    def broken(req):
        raise RuntimeError("connection refused")

    model = st.Model.from_function(broken, name="broken")
    with pytest.raises(st.ProviderError, match="connection refused"):
        model.check()


# ---- the caller: cut-off retry and usage ---------------------------------------------------


def test_cut_off_reply_is_retried_with_twice_the_room():
    budgets: list[int] = []

    def fn(req: st.ModelRequest) -> st.Reply:
        budgets.append(req.max_output_tokens)
        if len(budgets) == 1:
            return st.Reply(text='{"answers": [', cut_off=True, input_tokens=10, output_tokens=300)
        return st.Reply(text='{"answers": []}', input_tokens=10, output_tokens=20)

    records = []
    model = st.Model.from_function(fn, name="f")
    caller = make_caller(model, api_key=None, on_usage=records.append)
    out = asyncio.run(caller.chat_json(system_prompt="s", user_prompt="u", temperature=0.3, max_tokens=500))
    assert out == {"answers": []}
    assert budgets == [500, 1000]
    assert [(r.input_tokens, r.output_tokens) for r in records] == [(10, 300), (10, 20)]


def test_json_reply_cut_off_twice_is_an_error():
    model = st.Model.from_function(lambda r: st.Reply(text='{"a":', cut_off=True), name="f")
    caller = DefinedModelCaller(model, None)
    with pytest.raises(RuntimeError, match="cut off"):
        asyncio.run(caller.chat_json(system_prompt="s", user_prompt="u", temperature=0.3, max_tokens=100))


def test_unknown_token_counts_are_marked():
    records = []
    model = st.Model.from_function(lambda r: '{"answers": []}', name="f")
    caller = make_caller(model, api_key=None, on_usage=records.append)
    asyncio.run(caller.chat_json(system_prompt="s", user_prompt="u", temperature=0.3, max_tokens=100))
    assert records[0].extra["usage_known"] is False


def test_images_to_a_text_only_model_are_refused():
    model = st.Model.from_function(lambda r: "x", name="f", images=False)
    caller = DefinedModelCaller(model, None)
    items = [{"role": "user", "content": [{"type": "input_text", "text": "q"}, {"type": "input_image", "image_url": "https://img.example/a.png"}]}]
    with pytest.raises(RuntimeError, match="image input"):
        asyncio.run(caller.responses_input_text(input_items=items, max_tokens=100))


# ---- presets send the website's requests -------------------------------------------------------


def preset_call(model_name: str, reply: dict):
    rec = Recorder(reply)
    client = OpenAIClient(openai_api_key="sk-test")
    client._client = httpx.AsyncClient(transport=httpx.MockTransport(rec))

    async def go():
        try:
            return await client.chat_json(model=model_name, system_prompt="sys", user_prompt="user", temperature=0.3, max_tokens=500)
        finally:
            await client.close()

    return asyncio.run(go()), rec


def test_preset_chat_completions_request():
    out, rec = preset_call("gpt-4o-mini", {"choices": [{"message": {"content": '{"answers": []}'}}], "usage": {"prompt_tokens": 3, "completion_tokens": 2}})
    assert out == {"answers": []}
    assert str(rec.requests[0].url) == "https://api.openai.com/v1/chat/completions"
    body = rec.body
    assert body["model"] == "gpt-4o-mini" and body["temperature"] == 0.3
    assert body["response_format"] == {"type": "json_object"}
    assert rec.requests[0].headers["authorization"] == "Bearer sk-test"


def test_preset_gpt5_uses_the_responses_api_with_reasoning():
    out, rec = preset_call(
        "gpt-5.4-medium",
        {"status": "completed", "output": [{"type": "message", "content": [{"type": "output_text", "text": '{"answers": []}'}]}], "usage": {"input_tokens": 3, "output_tokens": 2}},
    )
    assert out == {"answers": []}
    assert str(rec.requests[0].url) == "https://api.openai.com/v1/responses"
    body = rec.body
    assert body["model"] == "gpt-5.4" and body["reasoning"] == {"effort": "medium"}
    assert body["max_output_tokens"] > 500  # the reasoning allowance is added


def test_preset_settings():
    model = st.Model.preset("gpt-5.4-medium")
    assert model.settings["api_model"] == "gpt-5.4"
    assert model.settings["reasoning_effort"] == "medium"
    assert st.Model.preset("any-openai-model").settings["api_model"] == "any-openai-model"
    with pytest.raises(st.ConfigError):
        st.Model.preset("gpt-5.4", provider="azure")


def test_batch_token_counts_are_credited_to_the_twin():
    lines = [
        {"custom_id": "0", "response": {"body": {"choices": [{"message": {"content": '{"answers": []}'}, "finish_reason": "stop"}], "usage": {"prompt_tokens": 5, "completion_tokens": 2}}}},
        {"custom_id": "1", "response": {"body": {"choices": [{"message": {"content": '{"answers": []}'}, "finish_reason": "stop"}], "usage": {"prompt_tokens": 7, "completion_tokens": 3}}}},
    ]

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path.endswith("/batches/batch_1"):
            return httpx.Response(200, json={"id": "batch_1", "status": "completed", "output_file_id": "file_out"})
        if req.url.path.endswith("/files/file_out/content"):
            return httpx.Response(200, text="\n".join(json.dumps(line) for line in lines))
        return httpx.Response(404, json={})

    records = []
    client = OpenAIClient(openai_api_key="sk-test", on_usage=records.append)
    client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

    async def go():
        try:
            return await client.collect_chat_batch_results("batch_1", model="gpt-4o-mini", item_count=2, twin_ids=["p7", "p3"])
        finally:
            await client.close()

    out = asyncio.run(go())
    assert set(out) == {"0", "1"}
    assert [(r.twin_id, r.input_tokens, r.batch) for r in records] == [("p7", 5, True), ("p3", 7, True)]
