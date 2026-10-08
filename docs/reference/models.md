# Models

A run asks every twin through a model. This page covers the `Model` class (named
presets, models you define for any HTTP address, and Python functions), `env`
for keys, and the records of a model call: `ModelRequest`, `Reply` and
`ModelCheck`. You choose the model in `configure(model=...)`, with a preset name
or a `Model`; it is first called by `run` (see [Running a plan](running.md)).
Each example on this page continues from the ones before it.

## `Model`

A model a run can call. Build one with a class method, not with `Model(...)`
directly: `Model(...)` takes the fields below as arguments, but only the class
methods check the definition.

| Class method | `kind` | What it calls |
|---|---|---|
| [`Model.preset`](#modelpreset) | `"preset"` | OpenAI or OpenRouter, by preset or model name |
| [`Model.http`](#modelhttp) | `"http"` | any HTTP address, in one of four request layouts |
| [`Model.openai_compatible`](#modelopenai_compatible) | `"openai-compatible"` | a server that accepts OpenAI's chat format |
| [`Model.from_function`](#modelfrom_function) | `"function"` | a Python function of yours |

A preset name given to `configure(model="gpt-5.6-luna-medium", provider=...)` is
the same as `Model.preset(name, provider=provider)`. A `Model` is a record (a
dataclass) of its definition; to change a model, build a new one.

### Attributes / properties

| Name | Type | Meaning |
|---|---|---|
| `name` | `str` | The model's name: a preset name, or the name you gave. Recorded with results. |
| `kind` | `str` | `"preset"`, `"http"`, `"openai-compatible"` or `"function"`. |
| `layout` | `str \| None` | The request layout of an HTTP model: `"openai-chat"`, `"openai-responses"`, `"anthropic"` or `"gemini"`. `None` for presets and function models. |
| `images` | `bool` | Whether the model takes image input. `True` for presets; `True` for HTTP models unless you say otherwise; `False` for function models unless you say otherwise. |
| `url` | `str \| None` | The address an HTTP model posts to; `None` for presets and function models. |
| `headers` | `dict[str, Any]` | The HTTP headers sent with each call, such as the key. A value may be an `env(...)` placeholder. Empty for presets and function models. |
| `body` | `dict[str, Any]` | Fields added to the layout's request body (see [`Model.http`](#modelhttp)). Empty for presets and function models. |
| `text_key` | `str \| Callable[[Any], str] \| None` | Where an HTTP model's reply text is, when not where the layout looks. |
| `usage_keys` | `dict[str, str] \| None` | Where an HTTP model's token counts are, when not where the layout looks. |
| `stop_key` | `str \| None` | Where an HTTP model's stop reason is, when not where the layout looks. |
| `cut_off_values` | `list[str] \| None` | The stop reasons, read at `stop_key`, that mean the reply was cut off. |
| `function` | `Callable[[ModelRequest], Any] \| None` | A function model's function; `None` for other kinds. |
| `provider` | `str` | For a preset, `"openai"` or `"openrouter"`. Other kinds keep the default, `"openai"`, and do not use it. |
| `timeout_s` | `float` | How many seconds an HTTP model waits for each call (default 180). Presets have their own: 240 seconds for the Responses API, 180 for chat completions. |
| `source` | `str` | Where the model was defined, when the command line read it from a file: `"FILE.py:NAME (sha256 <first 12 digits>)"`, the file's name without its folder. Empty otherwise. |
| `settings` | `dict[str, Any]` | Property: what a run records about the model, never a key. See [`Model.settings`](#modelsettings). |

### `Model.preset`

```python
Model.preset(name: str, *, provider: str = 'openai') -> Model
```

A model called through OpenAI or OpenRouter, by name. A named preset fixes the
API model and its reasoning effort, so that runs stay comparable. Any other name
is sent to the API as written: any OpenAI model works, fine-tuned ones included,
and with `provider="openrouter"` any OpenRouter model. A name is not checked
until the first call; `model.check()` tells you whether it works.

| Preset | API model | Reasoning effort |
|---|---|---|
| `gpt-5.6-luna-low` | `gpt-5.6-luna` | low |
| `gpt-5.6-luna-medium` | `gpt-5.6-luna` | medium |
| `gpt-5.6-luna` | `gpt-5.6-luna` | not set |
| `gpt-5.4-low` | `gpt-5.4` | low |
| `gpt-5.4-medium` | `gpt-5.4` | medium |
| `gpt-5.4-high` | `gpt-5.4` | high |
| `gpt-5.4` | `gpt-5.4` | not set |
| `gpt-5.3`, `gpt-5.3-chat-latest`, `gpt-5.3-instant` | `gpt-5.3-chat-latest` | not set |
| `gpt-5.2-low` | `gpt-5.2` | low |
| `gpt-5.2-medium` | `gpt-5.2` | medium |
| `gpt-5.2-high` | `gpt-5.2` | high |
| `gpt-5.2` | `gpt-5.2` | not set |
| `gpt-5.1-low` | `gpt-5.1` | low |
| `gpt-5.1-medium` | `gpt-5.1` | medium |
| `gpt-5.1-high` | `gpt-5.1` | high |
| `gpt-5.1` | `gpt-5.1` | not set |
| `gpt-5-mini-low` | `gpt-5-mini` | low |
| `gpt-5-mini-medium` | `gpt-5-mini` | medium |
| `gpt-5-mini-high` | `gpt-5-mini` | high |
| `gpt-5-mini` | `gpt-5-mini` | not set |
| `gpt-4o-mini` | `gpt-4o-mini` | not set |

How a preset is called:

- **Through OpenAI** (`provider="openai"`; the key from `api_key` or
  `OPENAI_API_KEY`):
  - A call without images to a GPT-5 model (an API model whose name starts with
    `gpt-5`) goes to the Responses API, with the preset's reasoning effort. Its
    output limit gets extra room for reasoning: 2,000 tokens for low effort,
    8,000 for medium or for no effort set, 16,000 for high, and none for the
    `chat` models (`gpt-5.3`).
  - A call without images to any other model goes to chat completions, in JSON
    mode, at temperature 0.3.
  - A call with images goes to the Responses API, whatever the model, with the
    images.
  - A Responses API reply that is cut off by the output limit, or empty, is asked
    again once, with twice the output limit and the reasoning effort lowered to
    low. A chat completions reply is not asked again.
  - When `OPENROUTER_API_KEY` is set in the environment, a call that fails at
    OpenAI is made once more through OpenRouter, as below.
- **Through OpenRouter** (`provider="openrouter"`; the key from `api_key` or
  `OPENROUTER_API_KEY`): every call goes to OpenRouter's chat completions (in
  JSON mode for calls without images), under the OpenRouter name: `openai/` and
  the API model for a name that starts with `gpt-` (such as `openai/gpt-5.4`),
  and any other name as written. A GPT-5 model gets the preset's reasoning
  effort and, as through OpenAI, extra output room for its reasoning; other
  models get temperature 0.3. Images go with the call, as image parts of the
  chat message. A cut-off reply is not asked again.

**Parameters**

- `name` (`str`): a preset name from the table, or any model name the provider
  knows.
- `provider` (`str`, default `'openai'`): `"openai"` or `"openrouter"`, in any
  case.

**Returns** `Model`: with `kind="preset"` and `images=True`.

**Raises**

- `ConfigError`: `provider` is neither `"openai"` nor `"openrouter"`.

**Example**

```python
import surveytwin as st

model = st.Model.preset("gpt-5.4-high")
print(model.kind, model.images)
print(model.settings)
```

```
preset True
{'name': 'gpt-5.4-high', 'kind': 'preset', 'provider': 'openai', 'api_model': 'gpt-5.4', 'reasoning_effort': 'high'}
```

### `Model.http`

```python
Model.http(
    name: str,
    url: str,
    *,
    headers: dict[str, Any] | None = None,
    layout: str,
    body: dict[str, Any] | None = None,
    text_key: str | Callable[[Any], str] | None = None,
    usage_keys: dict[str, str] | None = None,
    stop_key: str | None = None,
    cut_off_values: list[str] | None = None,
    images: bool = True,
    timeout_s: float = 180.0,
) -> Model
```

A model at any HTTP address. Each call posts the request in one of four request
layouts, and reads the reply text, the token counts and the stop reason from
where that API puts them. When a gateway gives the reply another shape,
`text_key`, `usage_keys` and `stop_key` say where to look instead.

**Parameters**

- `name` (`str`): your name for the model. It is recorded with results and not
  sent: put the API's model name in `body`.
- `url` (`str`): the address each call is posted to, starting with `http://` or
  `https://`. Its query string (such as `?key=...`) is not recorded.
- `headers` (`dict[str, Any] | None`, default `None`): the HTTP headers of every
  call, such as the key. A value may be `env("NAME")`, read when each call is
  made. `Content-Type: application/json` is added. Header values are never
  recorded.
- `layout` (`str`, required): `"openai-chat"`, `"openai-responses"`,
  `"anthropic"` or `"gemini"`; see [The four request layouts](#the-four-request-layouts).
- `body` (`dict[str, Any] | None`, default `None`): fields added to the layout's
  request body, such as `{"model": "..."}`; see
  [Adding to the request body](#adding-to-the-request-body).
- `text_key` (`str | Callable[[Any], str] | None`, default `None`): where the
  reply text is, if not where the layout looks: a dotted path such as
  `"content.0.text"`, or a function that takes the reply (parsed from JSON) and
  returns the text.
- `usage_keys` (`dict[str, str] | None`, default `None`): where the token counts
  are, as `{"input_tokens": path, "output_tokens": path}`. When given, both
  counts are read from these paths only: a count it leaves out, or a path that
  finds nothing, is unknown.
- `stop_key` (`str | None`, default `None`): the dotted path to the reply's stop
  reason, if not where the layout looks. It needs `cut_off_values`.
- `cut_off_values` (`list[str] | None`, default `None`): the stop reasons, read at
  `stop_key` and compared as text, that mean the output limit cut the reply off.
  Used only with `stop_key`.
- `images` (`bool`, default `True`): whether the model takes image input. With
  `False`, a survey with images runs only with
  `configure(accept_missing_images=True)`, and the images are left out: a
  question that shows one is not asked and is recorded as skipped.
- `timeout_s` (`float`, default `180.0`): how many seconds to wait for each call.

**Returns** `Model`: with `kind="http"`.

**Raises**

- `ConfigError`: `layout` is not one of the four; `url` does not start with
  `http://` or `https://`; `stop_key` is given without `cut_off_values`.

**Example**

Definitions only: nothing is called, and no key is read, until a run or a check.

```python
claude = st.Model.http(
    "claude-sonnet",
    "https://api.anthropic.com/v1/messages",
    layout="anthropic",
    headers={"x-api-key": st.env("ANTHROPIC_API_KEY"), "anthropic-version": "2023-06-01"},
    body={"model": "claude-sonnet-5"},
)
gemini = st.Model.http(
    "gemini",
    "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-pro:generateContent",
    layout="gemini",
    headers={"x-goog-api-key": st.env("GEMINI_API_KEY")},
)
print(claude.settings)
print(gemini.settings["url"])
```

```
{'name': 'claude-sonnet', 'kind': 'http', 'layout': 'anthropic', 'url': 'https://api.anthropic.com/v1/messages', 'headers': {'x-api-key': '<hidden>', 'anthropic-version': '<hidden>'}, 'body': {'model': 'claude-sonnet-5'}, 'images': True}
https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-pro:generateContent
```

```python
print(claude.check())  # needs ANTHROPIC_API_KEY: one small call
```

#### The four request layouts

What each layout sends, built from the call's [`ModelRequest`](#modelrequest),
and where it reads the reply. No layout sends a model name: give it in `body`
(a Gemini address names the model itself).

**`openai-chat`**: OpenAI's chat completions, and the many servers that copy it.

- Sends `messages` (a system message with `system`, then a user message: the
  `user` text, or, for a call with images, a list of `text` and `image_url` parts
  in order), `max_tokens`, `temperature`, and in JSON mode
  `response_format: {"type": "json_object"}`.
- Reads the text from `choices.0.message.content`, the token counts from
  `usage.prompt_tokens` and `usage.completion_tokens`, and the stop reason from
  `choices.0.finish_reason`. `"length"` means cut off.

**`openai-responses`**: OpenAI's Responses API.

- Sends `input` (a system item, then a user item of `input_text` and
  `input_image` parts in order), `max_output_tokens`, and in JSON mode
  `text: {"format": {"type": "json_object"}}`. It sends no temperature.
- Reads the text of every `message` item in `output`, the token counts from
  `usage.input_tokens` and `usage.output_tokens`, and the stop reason from
  `status`. `"incomplete"`, with `incomplete_details.reason` `"max_output_tokens"`,
  means cut off.

**`anthropic`**: Anthropic's Messages API.

- Sends `system`, `messages` (one user message of `text` and `image` parts in
  order: an image by its URL, or as base64 data for a `data:` address),
  `max_tokens` and `temperature`. It has no JSON mode; the prompt asks for JSON.
- Reads the `text` blocks of `content`, the token counts from
  `usage.input_tokens` and `usage.output_tokens`, and the stop reason from
  `stop_reason`. `"max_tokens"` means cut off.

**`gemini`**: Google's Gemini `generateContent`.

- Sends `systemInstruction`, `contents` (one user turn of `text` and
  `inlineData` parts in order: the library downloads each image and sends it
  inline), and `generationConfig` with `maxOutputTokens`, `temperature` and, in
  JSON mode, `responseMimeType: "application/json"`.
- Reads the `text` parts of `candidates.0.content`, leaving out thought parts;
  the input tokens from `usageMetadata.promptTokenCount`; the output tokens from
  `candidatesTokenCount` plus `thoughtsTokenCount`; and the stop reason from
  `candidates.0.finishReason`. `"MAX_TOKENS"` means cut off.

A reply with an HTTP error status, or one that is not JSON, fails the call.

#### Adding to the request body

`body` is merged into the layout's request body:

- a field you give replaces the layout's field of that name;
- a dict is merged into the layout's dict key by key;
- `None` removes the layout's field: `{"temperature": None}` for a model that
  does not accept one.

Values may be `env("NAME")` placeholders, at any depth. A field you set wins on
every call, the retry with twice the room included: `body={"max_tokens": 8192}`
caps the output of an `anthropic` or `openai-chat` model, whatever limit the run
asks for (see `ModelRequest.max_output_tokens`). The body is recorded with the
results, each `env(...)` shown by its name; a key written as plain text would be
recorded as written.

#### Replies in another shape

A dotted path follows keys and list positions through the reply, parsed from
JSON: `"choices.0.message.content"`. A path that finds nothing gives no value:
empty text, an unknown token count, or no stop reason. An example with
`text_key`, `usage_keys` and `stop_key` is under [`Model.acall`](#modelacall).

### `Model.openai_compatible`

```python
Model.openai_compatible(
    name: str,
    base_url: str,
    *,
    api_key_env: str | None = None,
    model: str | None = None,
    body: dict[str, Any] | None = None,
    images: bool = True,
) -> Model
```

A model behind any server that accepts OpenAI's chat completions format: model
servers on your own machine, and many hosted services. It is `Model.http` with
the `openai-chat` layout, `<base_url>/chat/completions` as its address, the key
sent as `Authorization: Bearer <key>`, and the model name in the body.

**Parameters**

- `name` (`str`): your name for the model; also the model name sent, unless
  `model` is given.
- `base_url` (`str`): the server's API address, such as `http://localhost:8000/v1`,
  starting with `http://` or `https://`. A `/` at the end is dropped, and
  `/chat/completions` is added.
- `api_key_env` (`str | None`, default `None`): the environment variable that
  holds the key. It is read when each call is made and sent as
  `Authorization: Bearer <key>`. `None` sends no key.
- `model` (`str | None`, default `None`): the model name the server expects.
  `None` sends `name`.
- `body` (`dict[str, Any] | None`, default `None`): more request fields, as for
  `Model.http`. A `"model"` here wins over `model`.
- `images` (`bool`, default `True`): whether the model takes image input.

**Returns** `Model`: with `kind="openai-compatible"` and `layout="openai-chat"`.

**Raises**

- `ConfigError`: `base_url` does not start with `http://` or `https://`.

**Example**

```python
local = st.Model.openai_compatible("llama", "http://localhost:8000/v1", api_key_env="LOCAL_KEY")
print(local.url)
print(local.settings)
```

```
http://localhost:8000/v1/chat/completions
{'name': 'llama', 'kind': 'openai-compatible', 'layout': 'openai-chat', 'url': 'http://localhost:8000/v1/chat/completions', 'headers': {'Authorization': '<hidden>'}, 'body': {'model': 'llama'}, 'images': True}
```

### `Model.from_function`

```python
Model.from_function(fn: Callable[[ModelRequest], Any], *, name: str, images: bool = False) -> Model
```

A model that is a Python function of yours: it gets each call's `ModelRequest`
and returns the reply. Use it for a model's own client library, a model on your
machine, or a stand-in for tests.

**Parameters**

- `fn` (`Callable[[ModelRequest], Any]`): takes a `ModelRequest` and returns the
  reply text (`str`) or a `Reply`. It may be an async function. Return a `Reply`
  to report token counts or a cut-off reply; with a plain string the token counts
  are unknown. An error it raises counts as a failed call.
- `name` (`str`): the model's name, recorded with results.
- `images` (`bool`, default `False`): whether the function takes images. With
  `True`, a call with images gives them in `request.content` and
  `request.images`, as links or `data:` addresses.

**Returns** `Model`: with `kind="function"`.

**Raises**

- `ConfigError`: `fn` cannot be called.

**Example**

```python
import json

def my_model(request: st.ModelRequest) -> st.Reply:
    """Call your own model here, with request.system and request.user."""
    text = json.dumps({"answers": []})
    return st.Reply(text=text, input_tokens=len(request.user) // 4, output_tokens=5)

mine = st.Model.from_function(my_model, name="my-model")
print(mine.kind, mine.images, mine.settings)
```

```
function False {'name': 'my-model', 'kind': 'function', 'images': False}
```

### `Model.missing_env`

```python
Model.missing_env() -> list[str]
```

The environment variables this model's `env(...)` placeholders (in its headers
or body, including `api_key_env=`) read that are not set now, each once, in the
order they appear. Empty when every one is set, and for presets and function
models, which have none. `run`, `arun` and `Results.rerun_flagged` call it
before any request and stop with `ConfigError` when it is not empty.

**Returns** `list[str]`: the names of the unset variables.

**Example**

```python
import os

os.environ.pop("EXAMPLE_KEY", None)
model = st.Model.openai_compatible("local", "http://localhost:8000/v1", api_key_env="EXAMPLE_KEY")
print(model.missing_env())
```

```
['EXAMPLE_KEY']
```

### `Model.check`

```python
Model.check(*, api_key: str | None = None) -> ModelCheck
```

Makes one small call to see whether the library can read the model's reply: its
text, its token counts and its stop reason. Run it after defining a model, and
before a run: a run checks that the model's keys are set, but does not stop when
its calls fail; it records the answers as skipped.

- An HTTP or function model gets one `ModelRequest` that asks for
  `{"ok": true}`, with `max_output_tokens=64` and JSON mode, and no retry.
- A preset makes one short JSON call the way a run does, without the OpenRouter
  fallback.

It runs its own event loop; in async code, use [`acheck`](#modelacheck).

**Parameters**

- `api_key` (`str | None`, default `None`): for a preset, the key for its
  provider. `None` reads `OPENAI_API_KEY` or `OPENROUTER_API_KEY`. Other models
  do not use it.

**Returns** `ModelCheck`: what the check found.

**Raises**

- `ProviderError`: the call failed: an error status or a reply that is not JSON
  from the address, a missing key or environment variable, or an error raised by
  the function.
- `RuntimeError`: an event loop is already running.

**Example**

```python
check = mine.check()
print(check.ok, check.text_found, check.usage_found, check.stop_reason_found, check.response)
```

```
True True True True {"answers": []}
```

```python
print(st.Model.preset("gpt-5.6-luna-medium").check())  # needs OPENAI_API_KEY: one small call
```

### `Model.acheck`

```python
Model.acheck(*, api_key: str | None = None) -> ModelCheck
```

`check` for async code: an async function, so you `await` it. It makes the same
call.

**Parameters**

- `api_key` (`str | None`, default `None`): as for `check`.

**Returns** `ModelCheck`.

**Raises**

- `ProviderError`: as for `check`.

**Example**

```python
import asyncio

async def main() -> st.ModelCheck:
    return await mine.acheck()

print(asyncio.run(main()).ok)
```

```
True
```

### `Model.acall`

```python
Model.acall(request: ModelRequest, *, client: httpx.AsyncClient) -> tuple[Reply, dict[str, Any]]
```

Calls an HTTP or function model once, and returns its reply as the library reads
it. A run makes each of its calls through it. Call it yourself to see how your
address's reply is read. It does not retry; the retry of a cut-off reply happens
in the run (see [A reply cut off by the output limit](#a-reply-cut-off-by-the-output-limit)).

**Parameters**

- `request` (`ModelRequest`): what to ask.
- `client` (`httpx.AsyncClient`): the HTTP client to post with, and, for the
  `gemini` layout, to download images. A function model does not use it.

**Returns** `tuple[Reply, dict[str, Any]]`: the reply, and a short summary:
`latency_ms`, `status` (`"completed"`) and, for an HTTP model, `stop_reason` (as
read, or `None`).

**Raises**

- `ConfigError`: the model is a preset, which this method does not call; or an
  `env(...)` variable is not set.
- `RuntimeError`: the address answered with an HTTP error status
  (`"<name> API error <status>: <reply>"`), or with a reply that is not JSON.
- Any error the function of a function model raises.

**Example**

A gateway whose reply is in its own shape, and a stand-in for it, so that the
example runs without a network:

```python
import asyncio
import httpx

gateway_model = st.Model.http(
    "my-gateway",
    "https://gateway.example/generate",
    layout="openai-chat",
    text_key="result.0.text",
    usage_keys={"input_tokens": "meta.in", "output_tokens": "meta.out"},
    stop_key="result.0.why",
    cut_off_values=["limit"],
)
request = st.ModelRequest(
    system="You are a survey respondent.",
    user="How old are you?",
    content=[{"type": "text", "text": "How old are you?"}],
    images=[],
    max_output_tokens=300,
    json_mode=True,
)

def gateway(http_request: httpx.Request) -> httpx.Response:
    """Stands in for the gateway, so that no request leaves the machine."""
    return httpx.Response(200, json={"result": [{"text": '{"age": 34}', "why": "done"}], "meta": {"in": 31, "out": 7}})

async def ask() -> tuple[st.Reply, dict]:
    async with httpx.AsyncClient(transport=httpx.MockTransport(gateway)) as client:
        return await gateway_model.acall(request, client=client)

reply, summary = asyncio.run(ask())
print(reply.text, reply.input_tokens, reply.output_tokens, reply.cut_off)
print(summary["stop_reason"])
```

```
{"age": 34} 31 7 False
done
```

### `Model.settings`

```python
Model.settings -> dict[str, Any]
```

Property. What a run records about the model, in the run settings and in
`results.provenance["model"]`. It never holds a key.

- For a preset: `name`, `kind`, `provider`, `api_model` (the API model name) and
  `reasoning_effort`.
- For other kinds: `name` and `kind`; then, when they are set, `layout`, `url`
  without its query string, `headers` with every value shown as `<hidden>`,
  `body` with each `env(...)` shown as `<env NAME>`, `text_key` (a function shows
  as `<function>`), `usage_keys`, `stop_key` and `cut_off_values`; and `images`.
- `defined_in`, the `source`, when it is set.

A value written into `body` as plain text is recorded as written, so give keys
with `env(...)`.

**Example**

See [`Model.http`](#modelhttp) and [`Model.openai_compatible`](#modelopenai_compatible).

## `env`

```python
surveytwin.env(name: str)
```

A placeholder for the value of an environment variable, read each time a model
call is made. Use it for keys in `Model.http` headers and body: the value never
appears in a model's settings, in results or in saved files, which show the
variable's name only. It reads like a function: `st.env("ANTHROPIC_API_KEY")`.

**Parameters**

- `name` (`str`): the variable's name.

### Attributes / properties

| Name | Type | Meaning |
|---|---|---|
| `name` | `str` | The variable's name. `repr()` shows it as `env('NAME')`. |

### `env.resolve`

```python
env.resolve() -> str
```

The variable's value now, with spaces at either end removed. The library calls
it at each call; call it yourself to test your setup.

**Returns** `str`: the value.

**Raises**

- `ConfigError`: the variable is not set, or is blank
  (`"Environment variable NAME is not set"`). In a run, this makes every call of
  that model fail (see [Running a plan](running.md#where-the-key-comes-from)).

**Example**

```python
import os

os.environ["EXAMPLE_TOKEN"] = "not-a-real-key"
token = st.env("EXAMPLE_TOKEN")
print(token, token.name)
print(token.resolve())
```

```
env('EXAMPLE_TOKEN') EXAMPLE_TOKEN
not-a-real-key
```

## `ModelRequest`

```python
surveytwin.ModelRequest(
    system: str,
    user: str,
    content: list[dict[str, str]],
    images: list[str],
    max_output_tokens: int,
    json_mode: bool,
    temperature: float = 0.3,
)
```

What a model you define is asked in one call: the record a function model gets,
and that `Model.http` turns into its layout's request. A record (a dataclass).
The library builds it; you build one only to try a model, as under
[`Model.acall`](#modelacall).

### Attributes / properties

| Name | Type | Meaning |
|---|---|---|
| `system` | `str` | The system message; in a survey call, how to answer as the persona, and the answer format. |
| `user` | `str` | The user message as plain text; in a survey call, the persona's profile, the survey text and the questions. For a call with images, its text parts joined by blank lines. |
| `content` | `list[dict[str, str]]` | The user message as parts, in order: `{"type": "text", "text": ...}` and `{"type": "image", "url": ...}`, where an image may be a link or a `data:` address. A call without images has one text part, equal to `user`. |
| `images` | `list[str]` | The images in `content`, in order; empty for a call without images. |
| `max_output_tokens` | `int` | The output limit of this call. A survey call asks for 900 tokens plus 120 for each question it asks, at least 8,000 and at most 32,000; a call of `results.rerun_flagged()` for 8,000; a model check for 64. The retry after a cut-off reply asks for twice as much. |
| `json_mode` | `bool` | `True` when the layout should ask the API for a JSON object: calls without images. `False` for calls with images, whose prompt still asks for JSON. |
| `temperature` | `float` | Default `0.3`. Every survey call uses 0.3; the builder's drafting calls use 0. The `openai-responses` layout does not send it. |

**Example**

The request a model check sends:

```python
seen = []

def spy(request: st.ModelRequest) -> str:
    seen.append(request)
    return '{"ok": true}'

st.Model.from_function(spy, name="spy").check()
request = seen[0]
print(request.max_output_tokens, request.json_mode, request.temperature, request.images)
print(request.content)
```

```
64 True 0.3 []
[{'type': 'text', 'text': 'Return exactly this JSON object: {"ok": true}'}]
```

## `Reply`

```python
surveytwin.Reply(
    text: str,
    input_tokens: int | None = None,
    output_tokens: int | None = None,
    cut_off: bool = False,
    request_id: str = '',
    raw: Any = None,
)
```

One reply of a model you define. A function model may return one instead of a
plain string, to report token counts or a cut-off reply; `Model.acall` returns
one for an HTTP model. A record (a dataclass).

### Attributes / properties

| Name | Type | Meaning |
|---|---|---|
| `text` | `str` | The reply text. An empty text counts as no reply: the call is asked again once with twice the room. |
| `input_tokens` | `int \| None` | Input tokens, as the provider counted them; `None` when unknown. |
| `output_tokens` | `int \| None` | Output tokens; `None` when unknown. |
| `cut_off` | `bool` | `True` when the output limit stopped the reply. The call is asked again once with twice the room. |
| `request_id` | `str` | The provider's ID for the request, kept in the usage record. An HTTP model takes it from the `request-id` or `x-request-id` header, or else the reply's `id`. |
| `raw` | `Any` | The whole reply as received: for an HTTP model, parsed from JSON. `ModelCheck.response` shows it. |

A count left at `None` is recorded as 0 in the usage record, with
`extra["usage_known"]` set to `False` (see
[`UsageRecord`](running.md#usagerecord)).

**Example**

See [`Model.from_function`](#modelfrom_function) and
[A reply cut off by the output limit](#a-reply-cut-off-by-the-output-limit).

## `ModelCheck`

```python
surveytwin.ModelCheck(
    ok: bool,
    text_found: bool,
    usage_found: bool,
    stop_reason_found: bool,
    latency_s: float,
    response: Any = None,
    error: str = '',
)
```

What `Model.check()` and `Model.acheck()` found. A record (a dataclass).

### Attributes / properties

| Name | Type | Meaning |
|---|---|---|
| `ok` | `bool` | Reply text was found (the same as `text_found`). It does not say that the text is valid JSON. |
| `text_found` | `bool` | The reply had text where the model looks for it. |
| `usage_found` | `bool` | Both token counts were found. For a preset: the provider reported tokens. |
| `stop_reason_found` | `bool` | A stop reason was found, so that a cut-off reply can be told apart. Always `True` for presets and function models, which are not checked for one. |
| `latency_s` | `float` | How many seconds the check took. |
| `response` | `Any` | What came back: an HTTP model's reply (parsed from JSON), a preset's reply text, or a function model's `Reply.raw`, else its text. |
| `error` | `str` | Always empty: a check that fails raises `ProviderError` instead. |

**Example**

See [`Model.check`](#modelcheck).

## A reply cut off by the output limit

Every call has an output limit (`ModelRequest.max_output_tokens`). When the reply
of a model you define is cut off by it (as the layout, `stop_key` or
`Reply.cut_off` says), or has no text, the call is asked again once with twice
the limit:

- if the second reply has no text, the call fails;
- if it is still cut off, a call without images fails, since its JSON is
  incomplete; a call with images uses the cut-off text.

A failed call is one failed attempt: the run asks the twin's whole call again,
up to 5 attempts (see [Calls that fail](running.md#calls-that-fail)). Each
reply, the retry's included, makes its own usage record. Presets retry the same
way on the Responses API (see [`Model.preset`](#modelpreset)).

**Example**

A function model whose first reply is cut off:

```python
survey = st.read_qsf("examples/example_survey.qsf")
static_survey = survey.drop_blocks([
    "BL_KS_MC_1", "BL_KS_TG_10", "BL_KS_RO_8", "BL_KS_FB_13",
    "BL_KS_FB_14", "BL_5mvGQoBt7GcmAFo", "BL_8jFpZfwsRRTsiOi",
])
limits = []

def cut_off_once(request: st.ModelRequest) -> st.Reply:
    limits.append(request.max_output_tokens)
    if len(limits) == 1:
        return st.Reply(text='{"answers": [{"question_id": "QI', cut_off=True)
    return st.Reply(text='{"answers": [{"question_id": "QID13", "answer_value": "Hello"}]}')

config = st.configure(
    bank="examples/example-bank", sample_size=1, seed=7,
    model=st.Model.from_function(cut_off_once, name="cut-off-once"),
)
results = st.run(st.plan(static_survey, config))
print(limits)
print([(u["status"], u["extra"]["usage_known"]) for u in results.usage])
```

```
[8580, 17160]
[('completed', False), ('completed', False)]
```

## Token counts, not costs

The library has no prices and computes no cost. It keeps each call's token
counts as the provider reported them: input and output tokens for every model,
and for presets also cached input tokens and reasoning tokens. They reach
`on_usage` and `results.usage` as usage records, and `results.usage_totals`
adds them up (see [Running a plan](running.md#usagerecord) and
[Results](results.md)). For a model you define, a count the reply did not give is
recorded as 0, with `extra["usage_known"]` set to `False`.

See also: [Running a plan](running.md), [Results](results.md),
[Configuring and planning a run](configure-and-plan.md).
