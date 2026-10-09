# Running a plan

`run` asks every twin in a plan its survey and returns `Results`; `arun` does the
same in async code. This page covers what happens in between: where the key comes
from, how static and staged runs ask, what happens to answers that cannot be
used, the image check, the usage and progress callbacks, and `surveytwin.batch`,
the OpenAI Batch path. A plan comes from `st.plan(survey, config)` (see
[Configuring and planning a run](configure-and-plan.md)); nothing before `run`
calls a model.

The examples on this page run without a key or a network: a small Python
function stands in for the model. It reads the questions from the prompt and
picks the first option of each, or 5 where there are no options. A real model
reads the whole prompt instead. Later examples continue from this one.

```python
import json
import surveytwin as st

def questions_in(request: st.ModelRequest) -> list[dict]:
    """The questions one call asks, read from the prompt (for stand-in models only)."""
    listed = request.user.split("Questions JSON:\n")[-1].split("\n\nRespond using JSON schema")[0]
    return json.loads(listed)

def first_option(request: st.ModelRequest) -> st.Reply:
    """A stand-in for a real model: each question gets its first option, or 5."""
    answers = []
    for question in questions_in(request):
        value = (question.get("options") or ["5"])[0]
        answers.append({"question_id": question["id"], "answer_value": value, "answer_label": value, "answer_raw": value})
    return st.Reply(text=json.dumps({"answers": answers}), input_tokens=len(request.user) // 4, output_tokens=40)

survey = st.read_qsf("examples/example_survey.qsf")
config = st.configure(
    bank="examples/example-bank",
    sample_size=3,
    seed=7,
    model=st.Model.from_function(first_option, name="first-option"),
    accept_missing_images=True,  # this model takes no images: run without them
)
plan = st.plan(survey, config)
print(plan)
```

```
Plan: staged survey, 3 twins, seed 7, model first-option
  asked stage by stage: each stage's questions depend on the twin's earlier answers
  twins: ex-08, ex-05, ex-04
  loop: Loop&Merge (BL_KS_FB_12) asks 3 rows of 1 question (1 answer a row), up to 3 answers per twin
```

## `run`

```python
surveytwin.run(plan: Plan, **kwargs: Any) -> Results
```

Asks every twin in the plan its survey and returns the answers as `Results`. It
runs [`arun`](#arun) to the end with `asyncio.run`, and passes every keyword
argument on to it unchanged. It returns when every twin is done.

**Parameters**

- `plan` (`Plan`): the plan to run, from `st.plan(...)` or `plan.head(n)`.
- `**kwargs`: the keyword arguments of `arun`, passed on as given: `api_key`,
  `max_concurrency`, `keep_prompts`, `on_usage`, `on_progress` and
  `image_checker`. They are described under [`arun`](#arun). Any other name
  raises `TypeError`.

**Returns** `Results`: every answer, each twin's status and path through the
survey, one usage record per model call, the warnings and the provenance (see
[Results](results.md)).

**Raises**

- `RuntimeError`: `run` was called where an event loop is already running, as in
  async code. Use `await st.arun(plan)` there. If a notebook gives you this error,
  use `await st.arun(plan)` in the cell.
- Everything `arun` raises.

A model call that fails does not raise: see [Calls that fail](#calls-that-fail).

**Example**

```python
results = st.run(plan)
print(results)
print(results.warnings[0])
```

```
Results(3 twins, 293 answers, 0 flagged, mode=staged)
3 survey image(s) were left out because the model takes no image input. A question that shows one of them was not asked and is recorded as skipped; an image in descriptive text was left out, and the questions after it were asked without it.
```

(The example survey's three images are in descriptive text, so every question
is still asked.)

## `arun`

```python
surveytwin.arun(
    plan: Plan,
    *,
    api_key: str | None = None,
    max_concurrency: int = 8,
    keep_prompts: bool = False,
    on_usage: Callable[[UsageRecord], None] | None = None,
    on_progress: Callable[[int, int], None] | None = None,
    image_checker: Callable[[str], dict[str, str]] | None = None,
) -> Results
```

`run` for async code: an async function, so you `await` it. Before any model
call it checks that a preset model has a key, then checks the survey's images.
Then it asks every twin, in one call each or stage by stage (see
[What a run does](#what-a-run-does)), and builds the `Results`.

**Parameters**

- `plan` (`Plan`): the plan to run, from `st.plan(...)` or `plan.head(n)`.
- `api_key` (`str | None`, default `None`): the key for a preset model's
  provider: an OpenAI key for `provider="openai"`, an OpenRouter key for
  `provider="openrouter"`. `None` reads `OPENAI_API_KEY` or `OPENROUTER_API_KEY`
  from the environment. Models you define, and function models, do not use it.
  It is never stored. See [Where the key comes from](#where-the-key-comes-from).
- `max_concurrency` (`int`, default `8`): how many model calls may run at the
  same time. In a staged run it counts stage calls across all twins. A value
  below 1 counts as 1. Twins are taken in groups of 100, so no more than 100
  calls run at once, whatever the setting.
- `keep_prompts` (`bool`, default `False`): keep the exact request of every
  survey call in `results.prompts` (`results.save()` writes them to
  `prompts.jsonl`). Each is a dict with `twin_id`, `stage` (counted from 1 in a
  staged run, `None` in a static run), `kind` and `max_tokens`, and then either
  `system` and `user` (`kind="chat"`, a call without images) or `input_items`
  (`kind="multimodal"`: the system and user items, with text and image parts in
  order). A call that is asked again is kept again. These are the requests as
  the library builds them, before a defined model's layout reshapes them; the
  retry of a cut-off reply is not kept separately. With `configure(batch=True)`,
  each batch line is kept too (`kind="batch"`, with its `messages`).
- `on_usage` (`Callable[[UsageRecord], None] | None`, default `None`): called
  with a [`UsageRecord`](#usagerecord) after every model call, as it finishes:
  failed calls and retries included, and batch lines when `configure(batch=True)`.
  The same records are kept, as dicts, in `results.usage`. An error raised inside
  `on_usage` is ignored, and the record is still kept.
- `on_progress` (`Callable[[int, int], None] | None`, default `None`): called as
  `on_progress(done, total)` each time a twin has finished (see
  [Progress](#progress)). An error raised inside it stops the run.
- `image_checker` (`Callable[[str], dict[str, str]] | None`, default `None`):
  replaces the built-in check of image links (see [Images](#images)). It gets one
  image link and returns a dict whose `"status"` is `"supported"` (the image can
  be sent), or `"inaccessible"` or `"auth_required"` (it cannot); a `"detail"`
  text is optional. A checker that raises an error, returns something other than
  a dict, or returns an unknown status marks that image inaccessible. It is not
  called when the model takes no image input. `None` loads each link over the
  network.

**Returns** `Results`: as for `run`.

**Raises**

- `ConfigError`: the model is a preset and there is no key for its provider.
  This is checked first, before the images.
- `ImagesRefused`: some survey images cannot be sent and the settings do not
  have `accept_missing_images=True`. Nothing has been sent yet.
- Any error raised inside `on_progress`.

**Example**

```python
import asyncio

async def main() -> st.Results:
    return await st.arun(plan, max_concurrency=2, keep_prompts=True)

results = asyncio.run(main())
first = results.prompts[0]
print(len(results.prompts), first["twin_id"], first["stage"], first["kind"], first["max_tokens"])
```

```
6 ex-08 1 chat 12300
```

Six calls: each of the three twins answered in two stages.

## Where the key comes from

**A preset model** (`model="gpt-5.6-luna-medium"`, or `st.Model.preset(...)`)
calls OpenAI or OpenRouter with a key. The run takes:

1. the `api_key` argument, if given;
2. otherwise `OPENAI_API_KEY` for `provider="openai"`, or `OPENROUTER_API_KEY` for
   `provider="openrouter"`, from the environment.

Without a key, `run` stops with `ConfigError` before anything else is done. With
an OpenAI preset, `OPENROUTER_API_KEY`, when set in the environment, is also a
fallback: a call that fails at OpenAI is made once more through OpenRouter, with
the same images.

```python
import os

preset_plan = st.plan(survey, st.configure(
    bank="examples/example-bank", sample_size=3, seed=7,
    model="gpt-5.6-luna-medium", accept_missing_images=True,
))
results = st.run(preset_plan)  # needs OPENAI_API_KEY in the environment
results = st.run(preset_plan, api_key=os.environ["MY_OPENAI_KEY"])  # or a key you pass yourself
```

**A model you define** (`Model.http`, `Model.openai_compatible`) carries its own
keys: `env("NAME")` placeholders in its headers or body, or `api_key_env=`. Each
is read from the environment when a call is made; `api_key` is not used. Before
anything is sent, the run checks that every one of these variables is set
(`Model.missing_env`), and stops with `ConfigError` naming the ones that are not.
Run `model.check()` once after defining a model, to see that its replies can be
read (see [Models](models.md)).

**A function model** makes its own calls; the library needs no key for it.

**Keys are never saved**: not in `Results`, not in a saved run folder, and not in
the model's recorded settings. The settings show every header value as
`<hidden>`, each `env(...)` placeholder by its name, and the address without its
query string. A key written into a model's `body` as plain text, not as
`env(...)`, would be recorded as written.

## What a run does

The plan has already decided how each twin is asked: `plan.mode` is `"static"`
or `"staged"`. Why a survey runs staged is in `survey.check().staging_reasons`
(see [Reading and checking a survey](survey.md)).

### Static runs

Nothing a twin is shown depends on its answers. The plan has built each twin's
own copy of the survey beforehand, with its randomizer arms, question and option
orders, random numbers and embedded data. The run sends each twin its whole copy
in one model call, and turns the reply into one answer per question.

### Staged runs

What a twin is shown depends on its answers: a branch, display logic or skip
logic that reads an answer, text that pipes in an earlier answer, or an end of
the survey partway through the flow. (`st.plan(..., mode="staged")` also runs a
static survey this way.)

Each twin goes through the survey in stages. At each stage the library works
out, from the survey flow and the twin's answers so far, which questions come
next. It asks them in one call, records the answers (setting embedded data,
taking branches and skips) and goes on until the survey ends. Every call after
the first starts with the twin's earlier questions and answers, so that the twin
stays consistent. A twin's stages are asked one after another; different twins
are asked side by side.

- A twin that needs more stages than `configure(max_stages=...)` allows (25 by
  default) is stopped there: `results.twins` marks it `incomplete`, and the rest
  of its survey is not asked.
- Each stage sends its own images, plus those of questions answered earlier in
  the same block; `configure(carry_stage_images=True)` sends every earlier
  stage's images again.
- Stages are counted from 1 in usage records and kept prompts.
  `plan.preview_prompt()` shows the first stage as `stage=0`.

### Calls that fail

A model call can fail (an error from the provider, a reply cut off twice, a
reply that is not JSON), or come back without usable answers (no list of
answers, or none for the questions asked). Either way the run asks the same call
again, with the same prompt, up to 5 attempts in all, waiting 1, 2, 3 and then 4
seconds between them. An error that would come back again is not retried: an
HTTP error in the 400s, other than 408 and 429, such as a wrong key or an unknown
model.

If no attempt gives usable answers, the questions of that call are recorded as
`skipped`, and `results.warnings` says so:
`<twin>: no usable answers after 5 attempt(s); every question recorded as skipped (last error: ...)`.
(A broken reply to a call with images, or a broken batch line, still gives back
the complete answers it holds.) A call that answers some of its questions is not
asked again: the questions it left out are recorded as `skipped`.

If the provider refuses one of the survey's images during the run, the image is
left out and the call made again: the question that shows it is not asked and
is recorded as `skipped`, and the other questions are asked as before, with the
other images. When the provider's error does not say which image it refused, no
image is sent, and every question that shows one is skipped. A warning names
the twin, the image and the questions. This holds with the OpenRouter fallback
too, when OpenAI refused the image and the OpenRouter call then failed.

None of this stops the run: `run` returns `Results` even when every call failed.
Look at `results.warnings` and `results.flagged()`.

### With the Batch API

With `configure(batch=True)` (static surveys and OpenAI preset models only),
`run` sends every twin's call as one OpenAI batch, waits for it (it checks every
15 seconds, for up to 24 hours) and reads the answers. A twin whose batch line
is missing, cut off, empty or not JSON is asked again in real time. If the batch
ends without completing, or cannot be sent, every twin is asked in real time
instead. `results.warnings` says which happened, and `results.provenance["batch"]`
records the batch. To leave a batch running and collect it later, use
[`surveytwin.batch`](#surveytwinbatch).

## Answers that cannot be used

The library never makes up an answer. When a reply gives nothing usable for a
question, the answer is left blank in the data and flagged with a status
(`Answer.status`; `answer_status` in the files):

| Status | Meaning |
|---|---|
| `skipped` | The twin was asked the question but gave nothing usable: the reply left it out, left a required text box empty, or every attempt of the call failed. |
| `out_of_range` | The twin answered, but the answer matches none of the options; or a number question got text with no number in it, or a number the question does not allow (outside its range, off its step, or with more decimal places than it allows). The words it gave are kept in `Answer.raw`; `value` and `label` stay blank. Nothing is moved to a nearby allowed answer: repairs happen after the run (`rerun_flagged`). |

Older data may hold `needs_validation`; `Answer.status` reports it as `skipped`.

Some answers look odd but are not flagged:

- An explicit refusal, such as "Prefer not to say", to an optional question is
  kept as a blank answer, as when a respondent leaves a question unanswered. An
  option with that text is matched first.
- A number the question allows is written in the question's format: 7.0 for a
  whole-number question is 7.
- Fewer choices than a question's minimum are kept as given; `results.warnings`
  says so, and the answer's `adjustments` record it.
- A multi-select option the twin did not pick is blank: it means "not selected".

`results.flagged()` lists the flagged answers that have no repair yet, and
`results.rerun_flagged()` asks those twins again for just those questions. In
`results.twins`, a twin all of whose answers are `skipped` is marked `failed`.
See [Results](results.md).

**Example**

A stand-in that answers "purple" to everything gives answers that match no
option:

```python
def purple(request: st.ModelRequest) -> str:
    """A stand-in that answers "purple" to every question."""
    return json.dumps({"answers": [{"question_id": q["id"], "answer_label": "purple"} for q in questions_in(request)]})

purple_config = st.configure(
    bank="examples/example-bank",
    sample_size=3,
    seed=7,
    model=st.Model.from_function(purple, name="purple"),
    accept_missing_images=True,
)
results = st.run(st.plan(survey, purple_config))
print(results)
for answer in results.flagged()[:2]:
    print(answer.twin_id, answer.question_id, answer.status, repr(answer.label), repr(answer.raw))
```

```
Results(3 twins, 290 answers, 150 flagged, mode=staged)
ex-08 QID56 out_of_range '' 'purple'
ex-08 QID4 out_of_range '' 'purple'
```

## Images

Before the first model call, the run checks every image the survey would show a
twin; images in dropped blocks are not checked. An image cannot be sent when:

- it is in a survey built in Python, and its image reference did not resolve
  (`survey.unresolved_media`);
- the model takes no image input (`images=False`, the default for function
  models): then every image counts, and the links are not loaded;
- its link fails the check: the image cannot be loaded (an error status, a
  timeout, or a reply that is not an image), or it needs a login (a 401 or 403
  reply, or a page that asks you to log in).

The built-in check loads each `http` or `https` link, at most 200 of them, 8 at a
time, waiting at most 3 seconds for each. `image_checker` replaces it. Images
embedded in the survey as `data:` addresses, and links past the 200th, are sent
without a check. `survey.check(network=True)` runs the same check ahead of time
and reports a status for each image (see
[Reading and checking a survey](survey.md)).

If any image cannot be sent, the run stops with `ImagesRefused` before any model
call, and lists up to 10 of them. With `configure(accept_missing_images=True)`,
those images are left out instead, and every model call goes without them. A
question that shows one of them (in its text, its answer choices, or its matrix
rows or columns) is not asked and is recorded as `skipped`: a twin never answers
a question without its picture. An image in descriptive text is only left out,
and the questions after it are asked without it. `results.warnings` says how
many images were left out, names them, and says what happened;
`results.provenance["images_left_out"]` lists them.

`st.batch.submit`, `st.batch.collect` and `plan.to_batch_file` check the images
the same way.

**Example**

A model that takes images, and a checker that calls every link unreachable
without loading it:

```python
sees_images = st.Model.from_function(first_option, name="first-option", images=True)

def unreachable(url: str) -> dict[str, str]:
    """An image checker that calls every link unreachable, without loading it."""
    return {"status": "inaccessible", "detail": "not loaded in this example"}

strict = st.configure(bank="examples/example-bank", sample_size=3, seed=7, model=sees_images)
try:
    st.run(st.plan(survey, strict), image_checker=unreachable)
except st.ImagesRefused as error:
    print(error)

lenient = st.configure(bank="examples/example-bank", sample_size=3, seed=7, model=sees_images, accept_missing_images=True)
results = st.run(st.plan(survey, lenient), image_checker=unreachable)
print(results.warnings[0])
print(results.provenance["images_left_out"])
```

```
3 survey image(s) cannot be loaded (unreachable or behind a Qualtrics login):
  inaccessible: https://example.qualtrics.com/CP/Graphic.php?IM=IM_6rRJhUflGhFyOiI
  inaccessible: https://example.qualtrics.com/CP/Graphic.php?IM=IM_kzK2j2nnpKnan0w
  inaccessible: https://example.qualtrics.com/CP/Graphic.php?IM=IM_vO2pLnPnoykG9oP
Fix the links, drop those blocks, or configure(accept_missing_images=True) to run without them (a question that shows one is then skipped).
3 survey image(s) could not be loaded (unreachable link or Qualtrics login required): QID37 question image 1, QID52 question image 1, QID68 question image 1. A question that shows one of them was not asked and is recorded as skipped; an image in descriptive text was left out, and the questions after it were asked without it.
['https://example.qualtrics.com/CP/Graphic.php?IM=IM_6rRJhUflGhFyOiI', 'https://example.qualtrics.com/CP/Graphic.php?IM=IM_kzK2j2nnpKnan0w', 'https://example.qualtrics.com/CP/Graphic.php?IM=IM_vO2pLnPnoykG9oP']
```

## `UsageRecord`

One model call, as passed to `on_usage`. `results.usage` keeps the same records
as dicts. The class is `surveytwin.providers.usage.UsageRecord`; import it from
there for type hints. A record (a dataclass).

It holds token counts as the provider reported them. The library has no prices
and computes no cost.

### Attributes / properties

| Name | Type | Meaning |
|---|---|---|
| `provider` | `str` | Who answered. For a preset, the provider actually called: `"openai"` or `"openrouter"` (an OpenRouter fallback shows `"openrouter"`). For a defined model, its kind: `"http"`, `"openai-compatible"` or `"function"`. |
| `endpoint` | `str` | For a preset, `"responses"` or `"chat_completions"`. For a defined model, its request layout, or `"function"`. |
| `model` | `str` | The model's name as you gave it (`Model.name`). |
| `resolved_model` | `str` | The model name sent to the API: for a preset, the API model (`gpt-5.4` for `gpt-5.4-medium`; `openai/gpt-5.4` through OpenRouter). For a defined model, its name. |
| `status` | `str` | `"completed"`, or `"failed"` for a call that ended in an error. (A preset call that gets no reply at all, such as on a network error, makes no record.) |
| `input_tokens` | `int` | Input tokens, as reported; `0` when the reply gave none. |
| `cached_input_tokens` | `int` | The part of the input the provider read from its cache. Presets only; `0` for defined models. |
| `output_tokens` | `int` | Output tokens, as reported; `0` when the reply gave none. |
| `reasoning_tokens` | `int` | The part of the output spent on reasoning, when the provider reports it. Presets only; `0` for defined models. |
| `total_tokens` | `int` | The provider's total, or input plus output. |
| `latency_ms` | `int` | How long the call took, in milliseconds; `0` for a batch line. |
| `request_id` | `str` | The provider's ID for the request, when it gave one. |
| `twin_id` | `str` | The twin the call was for. |
| `stage` | `int \| None` | The stage, counted from 1, in a staged run; `None` in a static run. |
| `purpose` | `str` | `"answer"` for a run's calls; `"rerun"` for `results.rerun_flagged()`; `"survey_draft.generate"` and `"survey_draft.refine"` for the builder's drafting calls. |
| `batch` | `bool` | `True` for a line read from an OpenAI batch. |
| `error` | `str` | For a failed call, the error message (its first 500 characters). |
| `extra` | `dict[str, Any]` | For a defined model, `usage_known` (`False` when the reply gave no token counts, so the counts above are 0) and `stop_reason` (as read from the reply). Empty for presets. |

### `UsageRecord.to_dict`

```python
UsageRecord.to_dict() -> dict[str, Any]
```

The record as a plain dict, one key per field. This is what `results.usage`
holds.

**Returns** `dict[str, Any]`.

**Example**

```python
records = []
results = st.run(plan, on_usage=records.append)
print(sorted((record.twin_id, record.stage) for record in records))
record = records[0]
print(record.provider, record.endpoint, record.model, record.status, record.output_tokens, record.extra)
print(records[0].to_dict() == results.usage[0])
print(results.usage_totals)
```

```
[('ex-04', 1), ('ex-04', 2), ('ex-05', 1), ('ex-05', 2), ('ex-08', 1), ('ex-08', 2)]
function function first-option completed 40 {'usage_known': True, 'stop_reason': None}
True
{'calls': 6, 'input_tokens': 40069, 'output_tokens': 240, 'reasoning_tokens': 0, 'total_tokens': 40309}
```

## Progress

`on_progress(done, total)` is called each time a twin has finished all its
calls. `done` is how many twins have finished so far (1, 2, 3, and so on) and
`total` is the number of twins in the plan. Twins finish in any order; in a
staged run a twin finishes after its last stage. With `configure(batch=True)`,
it is first called once the batch is done. The function runs inside the run, so
keep it quick; an error raised inside it stops the run.

**Example**

```python
results = st.run(plan, on_progress=lambda done, total: print(f"{done} of {total} twins done"))
```

```
1 of 3 twins done
2 of 3 twins done
3 of 3 twins done
```

## `surveytwin.batch`

The OpenAI Batch API path for static surveys, without keeping Python running
while OpenAI works through the batch: submit now, check later, and collect when
it is done.

```python
static_survey = survey.drop_blocks([
    "BL_KS_MC_1", "BL_KS_TG_10", "BL_KS_RO_8", "BL_KS_FB_13",
    "BL_KS_FB_14", "BL_5mvGQoBt7GcmAFo", "BL_8jFpZfwsRRTsiOi",
])
batch_config = st.configure(bank="examples/example-bank", sample_size=3, seed=7, model="gpt-5.6-luna-medium")
batch_plan = st.plan(static_survey, batch_config)
print(batch_plan.mode)
```

```
static
```

```python
batch_id = st.batch.submit(batch_plan)           # needs OPENAI_API_KEY
print(st.batch.status(batch_id))                 # "validating", then "in_progress", ...
results = st.batch.collect(batch_id, batch_plan)  # once the status is "completed"
```

- **Which models**: OpenAI preset models only: a preset name, or any OpenAI
  model name, fine-tuned ones included, with `provider="openai"`. Not OpenRouter
  presets, models you define, or function models.
- **Which surveys**: static plans only. A staged survey cannot be batched, since
  each stage needs the answers to the one before.
- **Which key**: the `api_key` argument, or `OPENAI_API_KEY`.
- **The batch**: one request per twin that needs a model call, to
  `/v1/chat/completions`, with the twin's whole survey; images go in as image
  parts. Its ID in the batch (`custom_id`) is the twin's position in
  `plan.twins` (`"0"`, `"1"`, ...). A GPT-5 model gets the preset's reasoning
  effort and the same extra output room for reasoning as a real-time call; a
  line cut off by the limit is asked again in real time when collecting.
  `plan.to_batch_file(path)` writes the same lines to a file without sending
  them.

These functions do not need `configure(batch=True)`; that setting makes `run`
submit a batch and wait for it (see [With the Batch API](#with-the-batch-api)).
Like `run`, each function runs its own event loop. In async code, call it in a
thread, for example `await asyncio.to_thread(st.batch.status, batch_id)`.

### `batch.submit`

```python
surveytwin.batch.submit(plan: Any, *, api_key: str | None = None, image_checker: Any = None) -> str
```

Checks the plan and its images, uploads one request per twin as a file, and
creates the batch (24-hour completion window). Returns the batch ID. Keep it,
and keep what made the plan, to collect the batch later.

**Parameters**

- `plan` (`Any`): the `Plan` to submit: a static plan whose model is an OpenAI
  preset model.
- `api_key` (`str | None`, default `None`): the OpenAI key. `None` reads
  `OPENAI_API_KEY`.
- `image_checker` (`Any`, default `None`): a function that checks one image
  link, as `image_checker` in [`arun`](#arun). `None` loads each link over the
  network.

**Returns** `str`: the batch ID.

**Raises**

- `BatchNotAvailable`: the plan is staged, or its model is not an OpenAI preset
  model; or no twin in it needs a model call, so there is nothing to submit.
- `ImagesRefused`: some images cannot be sent and the settings do not have
  `accept_missing_images=True` (see [Images](#images)). Nothing is sent.
- `ConfigError`: no OpenAI key was given or set.
- `httpx.HTTPStatusError`: OpenAI refused the upload or the batch, for example
  for a wrong key.

**Example**

The checks run before any key is needed:

```python
try:
    st.batch.submit(plan)
except st.BatchNotAvailable as error:
    print(error)
```

```
Staged surveys cannot be batched: each stage needs the previous answers
```

### `batch.status`

```python
surveytwin.batch.status(batch_id: str, *, api_key: str | None = None) -> str
```

The batch's status as OpenAI reports it, for example `"validating"`,
`"in_progress"`, `"completed"`, `"failed"`, `"expired"` or `"cancelled"`. Collect
the batch once it is `"completed"`.

**Parameters**

- `batch_id` (`str`): the ID `submit` returned.
- `api_key` (`str | None`, default `None`): the OpenAI key. `None` reads
  `OPENAI_API_KEY`.

**Returns** `str`: the status.

**Raises**

- `ConfigError`: no OpenAI key was given or set.
- `httpx.HTTPStatusError`: OpenAI refused the request (a wrong key or an
  unknown batch ID).

**Example**

```python
import time

while st.batch.status(batch_id) not in {"completed", "failed", "expired", "cancelled"}:  # needs OPENAI_API_KEY
    time.sleep(60)
```

### `batch.collect`

```python
surveytwin.batch.collect(
    batch_id: str,
    plan: Any,
    *,
    api_key: str | None = None,
    max_concurrency: int = 8,
    keep_prompts: bool = False,
    image_checker: Any = None,
)
```

Reads a finished batch and builds `Results`, as `run` would. Each twin gets its
batch answer; a twin whose line is missing, cut off, empty or not JSON is asked
again in real time. Pass the same plan you submitted (the same survey, settings
and seed), as lines are matched to twins by their position in the plan.

The results record the batch in `results.provenance["batch"]` (its `batch_id`
and `status`). The token counts of each batch line are in `results.usage`, with
`batch=True`, under the twin they belong to. `results.warnings` holds the plan's
own warnings, and says how many lines were unusable and asked again.

The images are checked again here, as in `submit` (pass the same
`image_checker`). An image that could be sent when the batch was submitted but
not any more does not stop the collection: the batch answers stand, since they
were given with the image, and a warning says so. A twin asked again in real
time sends the image; when the provider refuses it, the question that shows it
is skipped, as in [Calls that fail](#calls-that-fail). With
`accept_missing_images=True`, images that cannot be sent now are left out of
every twin, batch answers included: a question that shows one is recorded as
skipped.

Differences from `run`: there is no `on_usage` or `on_progress`.

**Parameters**

- `batch_id` (`str`): the ID `submit` returned.
- `plan` (`Any`): the `Plan` that was submitted.
- `api_key` (`str | None`, default `None`): the OpenAI key, for reading the batch
  and for any twin asked again. `None` reads `OPENAI_API_KEY`.
- `max_concurrency` (`int`, default `8`): how many twins may be asked again in
  real time at the same time.
- `keep_prompts` (`bool`, default `False`): keep each batch line's request
  (kind `"batch"`, with its `messages` and output limit) and each request made
  again in real time, in `results.prompts`.
- `image_checker` (`Any`, default `None`): checks each image link, as in `submit`;
  `None` loads each link over the network.

**Returns** `Results`: as `run` returns, with `results.run["mode"]` `"static"`.

**Raises**

- `BatchNotAvailable`: the plan is staged or its model is not an OpenAI preset
  model; or the batch is not completed yet (`"Batch <ID> is in_progress, not
  completed"`).
- `ConfigError`: no OpenAI key was given or set.
- `httpx.HTTPStatusError`: OpenAI refused a request.

**Example**

```python
results = st.batch.collect(batch_id, batch_plan)  # needs OPENAI_API_KEY
print(results.provenance["batch"])
results.save("runs/batch-example")
```

See also: [Models](models.md), [Results](results.md),
[Configuring and planning a run](configure-and-plan.md),
[Reading and checking a survey](survey.md).
