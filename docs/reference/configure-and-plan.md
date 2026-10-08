# Configuring and planning a run

`configure` collects every setting of a run and checks it; `plan` then samples
the twins and builds each twin's copy of the survey. Both come after reading the
survey and before `run`, and the plan lets you read what a run will send before
anything is sent.

**Nothing before running calls a model or needs a key.** `configure`, `plan` and
everything a `Plan` offers (prompt previews, `head`, the batch file) work without
an API key. A key is read only by `run`, `arun` and the functions of
`surveytwin.batch`, which talk to the model's provider. Two steps can still use
the network without calling a model: opening the default bank from a Hugging
Face repository (see [Persona banks](personas.md)), and `Plan.to_batch_file`,
which tests the survey's image links unless you pass `image_checker`.

## `configure`

```python
surveytwin.configure(
    *,
    bank: str | Path | Bank = 'twin-2k-500',
    representation: str | None = None,
    filters: dict[str, Any] | None = None,
    sample_size: int,
    seed: int = 42,
    model: str | Model,
    provider: str = 'openai',
    batch: bool = False,
    system_instruction: str | None = None,
    embedded_data: dict[str, Any] | None = None,
    carry_stage_images: bool = False,
    max_stages: int = 25,
    accept_missing_images: bool = False,
) -> RunConfig
```

Collects every setting of a run and checks each one now, so a mistake (an unknown
filter field, a bad representation name) fails here, before any model call. Every
argument is a keyword; `sample_size` and `model` are required.

**Parameters**

- `bank` (`str | Path | Bank`, default `'twin-2k-500'`): the bank to sample
  twins from: `'twin-2k-500'` for the default bank, the path of a Croissant Twins
  folder or zip, or a `Bank` you opened with `surveytwin.bank`. A name or path is
  opened as `surveytwin.bank` opens it.
- `representation` (`str | None`, default `None`): the text that describes each
  twin: the ID, kind or label of one of the bank's representations, in any case.
  `None` takes the bank's default (`summary` for Twin-2K-500). `"empty"` runs the
  twins with no persona, as a baseline.
- `filters` (`dict[str, Any] | None`, default `None`): which personas can be
  sampled: a dict from a field name to the values to keep, a
  `{"min": ..., "max": ...}` range, or `{"missing": True}`. `None` keeps every
  persona. Checked against the bank now. See
  [Persona banks](personas.md#filters).
- `sample_size` (`int`, required): how many twins to sample, a whole number of 1
  or more. If fewer personas match, `plan` uses all of them and warns.
- `seed` (`int`, default `42`): a whole number of 0 or more. It picks the twins
  and every random choice in the survey: randomizer arms, question and choice
  orders, and random numbers. The same survey, bank, settings and seed give the
  same twins and the same prompts on any machine.
- `model` (`str | Model`, required): a preset name, such as
  `"gpt-5.6-luna-medium"` (any OpenAI model name works, fine-tuned ones too), or
  a `Model` built with `Model.preset`, `Model.http`, `Model.openai_compatible` or
  `Model.from_function`. Nothing is called and no key is looked for here.
- `provider` (`str`, default `'openai'`): for a preset name only, where it is
  called: `"openai"` or `"openrouter"`, in any case. Ignored when `model` is a
  `Model`.
- `batch` (`bool`, default `False`): send the twins through the OpenAI Batch API.
  `run` then submits one batch, waits for it to finish, and asks in real time any
  twin the batch did not answer. Needs an OpenAI preset model (not OpenRouter),
  and `plan` refuses it for a survey that runs staged. To submit a batch and
  collect it later, use `surveytwin.batch`.
- `system_instruction` (`str | None`, default `None`): replaces the instruction
  that tells the model how to act as a respondent. `None` uses the default,
  `surveytwin.prompts.DEFAULT_SYSTEM_INSTRUCTION`. The answer format rules and
  the line telling the model that the persona text is data, not instructions,
  always follow it and cannot be changed. Spaces at either end are removed, and
  a blank text means the default.
- `embedded_data` (`dict[str, Any] | None`, default `None`): values for
  embedded-data fields that the real study filled from outside the survey, such
  as survey link parameters or a contact list (`survey.embedded_fields` shows
  which fields come from outside). Every twin gets these values before the
  survey flow starts; a field that the flow sets later takes the flow's value.
  Values become text (`None` becomes empty text), and blank names are dropped.
  `plan` warns about a name the survey never reads or sets.
- `carry_stage_images` (`bool`, default `False`): for staged surveys. `True` sends
  every earlier stage's images again with each later stage. By default a stage
  sends its own images, plus those of questions answered earlier in the same
  block.
- `max_stages` (`int`, default `25`): for staged surveys, the most stages that ask
  something a twin may take, 1 or more. A twin stopped by the limit is marked
  incomplete, and the rest of its survey is not asked. `plan` warns when a twin
  giving placeholder answers would need more.
- `accept_missing_images` (`bool`, default `False`): what happens to survey images
  that cannot be sent: a dead link, a link behind a Qualtrics login, a model that
  takes no image input, or an image a built survey could not find. With `False`,
  `run` (and `Plan.to_batch_file`) stop with `ImagesRefused` before any call.
  With `True`, those images are left out and the results list them. A question
  that shows one (in its text, its answer choices, or its matrix rows or
  columns) is not asked and is recorded as skipped, since a twin never answers a
  question without its picture. An image in descriptive text is only left out,
  and the questions after it are asked.

**Returns** `RunConfig`: the checked settings.

**Raises**

- `BankError`: the bank cannot be opened (the default bank too, when no copy is
  set), or the bank has no such representation.
- `FilterError`: a filter names a field or value the bank does not have, or has
  the wrong form.
- `ConfigError`: `sample_size` or `seed` is not a whole number; `sample_size` or
  `max_stages` is below 1; `seed` is below 0; `model` is neither a non-empty name
  nor a `Model`; `provider` is not `"openai"` or `"openrouter"` for a preset
  name; `batch=True` without an OpenAI preset model.

**Example**

```python
import surveytwin as st

config = st.configure(
    bank="examples/example-bank",
    filters={"region": ["South", "West"], "age": {"max": 50}},
    sample_size=3,
    seed=7,
    model="gpt-5.6-luna-medium",      # a preset name: no key is needed until st.run
    accept_missing_images=True,       # the example survey's image links are placeholders
)
print(config)
```

```text
Run settings:
  bank                Example Bank (example-bank v1.0.0)
  representation      Summary
  filters             region = South, West; age = ..50
  twins               3
  seed                7
  model               gpt-5.6-luna-medium (preset, openai)
  batch               no
  system instruction  default
  embedded data       none
  max stages          25
  carry stage images  no
  missing images      skip their questions
```

A model of your own works the same way, and so does a baseline with no persona:

```python
import surveytwin as st

fake = st.Model.from_function(lambda request: '{"answers": []}', name="fake")
config = st.configure(bank="examples/example-bank", representation="empty", sample_size=2, model=fake)
print(config.representation, config.provider, config.seed)
```

```text
empty function 42
```

## `RunConfig`

Every setting of a run, checked. You get one from `configure`. It is a record (a
dataclass). To change a setting, call `configure` again and then `plan` again:
editing a field by hand skips the checks, and a plan keeps the settings it was
built with. Printing it shows a table of the settings.

### Attributes / properties

| Name | Type | Meaning |
|---|---|---|
| `bank` | `Bank` | The opened bank. |
| `representation` | `str` | The representation's ID, such as `representations::summary`, or `"empty"`. |
| `filters` | `dict[str, Any]` | The filters as you gave them; `{}` for none. |
| `sample_size` | `int` | How many twins to sample. |
| `seed` | `int` | The seed. |
| `model` | `Model` | The model. A preset name is turned into `Model.preset(name, provider=provider)`. |
| `provider` | `str` | `"openai"` or `"openrouter"` for a preset model; otherwise the model's kind (`"http"` or `"function"`). |
| `batch` | `bool` | Whether `run` uses the OpenAI Batch API. |
| `system_instruction` | `str \| None` | Your instruction, without spaces at either end; `None` for the default. |
| `embedded_data` | `dict[str, str]` | Values for fields filled from outside the survey, as text. Default `{}`. |
| `carry_stage_images` | `bool` | Whether a staged run sends earlier stages' images again. Default `False`. |
| `max_stages` | `int` | The stage limit for a staged twin. Default `25`. |
| `accept_missing_images` | `bool` | Whether images that cannot be sent are left out, with the questions that show them recorded as skipped (`True`), or stop the run (`False`). Default `False`. |

### `RunConfig.to_record`

```python
RunConfig.to_record() -> dict[str, Any]
```

The settings as they are saved with results: no keys and no persona text.

**Returns** `dict[str, Any]`: `bank` (its `bank_id`, `name`, `version`, `format`
and `fingerprint`), `representation`, `representation_label`, `filters`,
`sample_size`, `seed`, `model` (the model's `settings`: its name and kind, and for
a preset its provider, the model name sent to the provider and the reasoning
effort), `batch`, `system_instruction`, `system_instruction_sha256` (a SHA-256 of
your instruction, or `None`), `embedded_data`, `carry_stage_images`,
`max_stages` and `accept_missing_images`.

**Example**

```python
import surveytwin as st

config = st.configure(bank="examples/example-bank", sample_size=3, seed=7, model="gpt-5.6-luna-medium")
record = config.to_record()
print(record["bank"])
print(record["model"])
```

```text
{'bank_id': 'example-bank', 'name': 'Example Bank', 'version': '1.0.0', 'format': 'Croissant Twins', 'fingerprint': 'f0ec0bd949cb51d4'}
{'name': 'gpt-5.6-luna-medium', 'kind': 'preset', 'provider': 'openai', 'api_model': 'gpt-5.6-luna', 'reasoning_effort': 'medium'}
```

## `plan`

```python
surveytwin.plan(survey: Survey, config: RunConfig, *, mode: str = 'auto') -> Plan
```

Samples the twins and builds each twin's copy of the survey, with its own
randomizer arms, question and choice orders, and random values. It calls no model
and needs no key, so you can read exactly what each twin will be asked before
running.

**Parameters**

- `survey` (`Survey`): the survey, from `read_qsf`, `survey.drop_blocks`, the
  builder, or `results.survey`.
- `config` (`RunConfig`): the settings, from `configure`.
- `mode` (`str`, default `'auto'`): how the survey is asked: `"auto"`,
  `"static"` or `"staged"`, as in the table below. Any other value raises
  `SurveyError`.

| Mode | What it does | What it refuses |
|---|---|---|
| `"auto"` | Static when nothing later in the survey depends on an answer, staged otherwise. | Nothing. |
| `"static"` | One model call per twin; each twin's whole survey is prepared in advance. | A survey whose later content depends on answers (`StaticModeRefused`). |
| `"staged"` | Stage by stage, even for a survey that could be static: each stage sees the twin's earlier answers. More calls. | Nothing. |

`survey.check()` says which mode a survey needs, and why. With `batch=True` in
the settings, a survey that runs staged is refused in any mode
(`BatchNotAvailable`).

**Returns** `Plan`: the sampled twins and each twin's survey. `plan.mode` says
which mode is used, `"static"` or `"staged"`.

`plan.warnings` lists what is worth knowing before running, without stopping:

- fewer personas match than `sample_size` asks for;
- logic, or display logic on choices, reads a dropped question or a question the
  survey does not have (it counts as never shown, with no answer);
- `embedded_data` names a field the survey never reads or sets;
- a random-number token cannot be drawn and reaches the model as written;
- piped text is left unfilled in the static survey copies;
- a staged twin giving placeholder answers would need more stages than
  `max_stages`.

`plan` does not test image links; `run` and `Plan.to_batch_file` do.

**Raises**

- `SurveyError`: `mode` is not one of the three; nothing is left for a twin to
  answer.
- `PersonaError`: no persona matches the filters and representation; a sampled
  twin has a blank text in the representation.
- `StaticModeRefused`: `mode="static"` for a survey that needs stages.
- `BatchNotAvailable`: `batch=True` and the survey runs staged.
- `ContextBudgetError`: the representation's longest text is over about 100,000
  tokens (counted as four characters per token). Nothing is cut to fit; the
  message names smaller representations.

**Example**

```python
import surveytwin as st

survey = st.read_qsf("examples/example_survey.qsf")
config = st.configure(
    bank="examples/example-bank",
    sample_size=3,
    seed=7,
    model="gpt-5.6-luna-medium",
    accept_missing_images=True,
)
plan = st.plan(survey, config)
print(plan)
for twin in plan.twins:
    print(twin)
```

```text
Plan: staged survey, 3 twins, seed 7, model gpt-5.6-luna-medium
  asked stage by stage: each stage's questions depend on the twin's earlier answers
  twins: ex-08, ex-05, ex-04
  loop: Loop&Merge (BL_KS_FB_12) asks 3 rows of 1 question (1 answer a row), up to 3 answers per twin
Twin('ex-08', age=70, region='Northeast', education='some_college'; 503 characters of representations::summary)
Twin('ex-05', age=52, region='Midwest', education='hs'; 520 characters of representations::summary)
Twin('ex-04', age=45, region='Northeast', education='grad'; 514 characters of representations::summary)
```

The example survey needs stages, so `mode="static"` is refused. Without the
blocks whose logic reads answers, and the blocks with images, it can run static:

```python
import surveytwin as st

survey = st.read_qsf("examples/example_survey.qsf")
config = st.configure(bank="examples/example-bank", sample_size=3, seed=7, model="gpt-5.6-luna-medium")
try:
    st.plan(survey, config, mode="static")
except st.StaticModeRefused as error:
    print(error)

static_survey = survey.drop_blocks([
    "BL_KS_MC_1", "BL_KS_TG_10", "BL_KS_RO_8", "BL_KS_FB_13",
    "BL_KS_FB_14", "BL_5mvGQoBt7GcmAFo", "BL_8jFpZfwsRRTsiOi",
])
print(st.plan(static_survey, config).mode)
print(st.plan(static_survey, config, mode="staged").mode)
```

```text
This survey needs staged execution (its later content depends on answers); static mode would be wrong. Use mode='auto' or 'staged'.
static
staged
```

## `Plan`

What a run will do: the sampled twins and each twin's survey. You get one from
`surveytwin.plan`, and a smaller one from `Plan.head`. Treat it as read-only: it
keeps the survey and settings it was built with. Printing it shows the mode, the
twins, the size of each Loop & Merge block and the warnings.

### Attributes / properties

| Name | Type | Meaning |
|---|---|---|
| `survey` | `Survey` | The survey that was planned. |
| `config` | `RunConfig` | The settings it was planned with. In a plan from `head(n)`, a copy whose `sample_size` is the number of twins kept. |
| `mode` | `str` | `"static"` (one model call per twin) or `"staged"` (stage by stage). |
| `twins` | `list[Twin]` | The sampled twins, in sample order. |
| `warnings` | `list[str]` | What is worth knowing before running (listed under `plan`). A run's results repeat them. |
| `population` | `int` | How many twins the survey's randomization is laid out for: the number of twins in the full plan. `head` keeps it, so each kept twin gets the same branches and orders as in the full plan. |
| `prompt_version` | `str` | The version of the prompt texts (`"2"`), saved with results. |
| `loops` | `list[dict[str, Any]]` | Each Loop & Merge block a twin can be shown, as a dict: `block_id`, `name`, `rows` (every row is asked, or the size of a random subset), `questions` (per row) and `answers_per_row`. |
| `twin_surveys` | `list[dict[str, Any]]` | For a static plan, each twin's copy of the survey, in the order of `twins` (described below). Empty for a staged plan, whose surveys unfold as the twins answer. |
| `definition` | `dict[str, Any]` | The survey the plan runs, in the standard format (docs/reference/survey-format.md), without dropped blocks: what every static copy is made from, and what a staged run walks stage by stage. |
| `template` | `dict[str, Any] \| None` | For a staged plan, the same survey in the Qualtrics-like form earlier versions of the library ran, kept so code written for them still works (the run itself reads `definition`). `None` for a static plan. |
| `instrument` | `dict[str, Any]` | Everything the run reads, in the library's internal form. `twin_surveys` and `definition` are the parts meant for reading. |

Each entry of `twin_surveys` is a dict with these keys:

- `survey_pid`: the copy's label: `pid_1`, `pid_2`, and so on.
- `questions`: the questions the twin answers, as the prompt lists them (ID,
  type, text, options and answer rules).
- `prompt_sequence_text`: the survey as the twin reads it, in order.
- `prompt_sequence_media`: the images shown, by question.
- `embedded_data`: the twin's embedded-data values.
- `assignments`: each value set, in order: those supplied with `embedded_data`
  (`"source": "outside"`), then those set by the survey flow (`"source": "flow"`,
  with the flow element's ID).
- `blocks`: the twin's copy as shown, block by block in the order shown. Each
  block is a dict with `block` (the block's ID; each row of a Loop & Merge
  block adds `_` and the row's ID), `title`, `row` (for a Loop & Merge row: its
  ID, its field values and its place; otherwise `None`) and `questions`. Each
  question is a dict whose `question` is the question in the standard format
  (docs/reference/survey-format.md), its text filled in and its options and
  rows in the order shown; its other keys are for the run.
- `elements`: the same blocks and questions in the Qualtrics-like form earlier
  versions of the library gave, written from `blocks` (kept so code written for
  them still works).

```python
import surveytwin as st

survey = st.read_qsf("examples/example_survey.qsf")
static_survey = survey.drop_blocks([
    "BL_KS_MC_1", "BL_KS_TG_10", "BL_KS_RO_8", "BL_KS_FB_13",
    "BL_KS_FB_14", "BL_5mvGQoBt7GcmAFo", "BL_8jFpZfwsRRTsiOi",
])
config = st.configure(bank="examples/example-bank", sample_size=3, seed=7, model="gpt-5.6-luna-medium")
plan = st.plan(static_survey, config)
for twin, copy in zip(plan.twins, plan.twin_surveys):
    print(twin.id, copy["embedded_data"], len(copy["questions"]))
print(plan.loops)
print(plan.template)
```

```text
ex-08 {'pet': 'Dog'} 64
ex-05 {'pet': 'Cat'} 64
ex-04 {'pet': 'None'} 64
[{'block_id': 'BL_KS_FB_12', 'name': 'Loop&Merge', 'rows': 3, 'questions': 1, 'answers_per_row': 1}]
None
```

### `Plan.preview_prompt`

```python
Plan.preview_prompt(twin_id: str | None = None, *, stage: int = 0) -> PromptPreview
```

The complete prompt for one twin, as readable text, with the exact messages
beside it. For a staged plan this is the first stage; later stages depend on the
twin's answers.

**Parameters**

- `twin_id` (`str | None`, default `None`): the ID of a twin in `plan.twins`.
  `None` is the first twin.
- `stage` (`int`, default `0`): the stage to show. Only `0` is known before the
  twin answers, so a staged plan refuses any other. A static plan has one call
  per twin, and `stage` makes no difference to it.

**Returns** `PromptPreview`: the prompt. A twin with nothing to answer in that
call gets a preview of kind `"none"`, with no messages.

**Raises**

- `SurveyError`: the twin is not in this plan; `stage` is not `0` in a staged
  plan.

The images in a preview are not tested. When `accept_missing_images=True`, a run
leaves out the images that cannot be sent.

**Example**

```python
import surveytwin as st

survey = st.read_qsf("examples/example_survey.qsf")
config = st.configure(bank="examples/example-bank", sample_size=3, seed=7,
                      model="gpt-5.6-luna-medium", accept_missing_images=True)
plan = st.plan(survey, config)
preview = plan.preview_prompt("ex-05")
print(preview.kind, len(preview.images), preview.input_tokens, preview.max_output_tokens)
print("\n".join(preview.text.splitlines()[:3]))
```

```text
multimodal 2 10716 12420
=== SYSTEM ===
You are a survey simulation respondent. Stay faithful to the persona context. Return valid JSON only.
CRITICAL — match the answer format to each question's fields:
```

### `Plan.prompts`

```python
Plan.prompts(twin_id: str | None = None, *, stage: int = 0) -> list[dict[str, Any]]
```

The exact content a run sends for one twin, in OpenAI's format. With no images,
it is two chat messages, `system` and `user`, each with text `content`. With
images, it is input items of OpenAI's Responses API, whose `content` is a list of
`input_text` and `input_image` parts in reading order. A model with another
request layout gets the same content in its own layout.

**Parameters**

- `twin_id` (`str | None`, default `None`): the ID of a twin in `plan.twins`.
  `None` is the first twin.
- `stage` (`int`, default `0`): as for `preview_prompt`.

**Returns** `list[dict[str, Any]]`: the messages or input items; empty when the
twin has nothing to answer in that call.

**Raises**

- `SurveyError`: as for `preview_prompt`.

**Example**

```python
import surveytwin as st

survey = st.read_qsf("examples/example_survey.qsf")
config = st.configure(bank="examples/example-bank", sample_size=3, seed=7,
                      model="gpt-5.6-luna-medium", accept_missing_images=True)
plan = st.plan(survey, config)
for item in plan.prompts():                      # the first twin, ex-08
    print(item["role"], [part["type"] for part in item["content"]])
```

```text
system ['input_text']
user ['input_text', 'input_text', 'input_text', 'input_text', 'input_image', 'input_text']
```

### `Plan.head`

```python
Plan.head(n: int) -> Plan
```

The same plan for its first `n` twins, to try a prompt, a model or a system
instruction on a few twins before asking them all. Each kept twin gets exactly
the survey it gets in the full plan (branches, orders and random draws), because
the smaller plan keeps the full plan's `population`. The results of a run of the
smaller plan record that population in `results.run["population"]`.

**Parameters**

- `n` (`int`): how many twins to keep, from the start of `plan.twins`. Below 1
  counts as 1; more than the plan has keeps them all.

**Returns** `Plan`: a new plan with the first `n` twins, the same survey, mode and
warnings, and a copy of the settings whose `sample_size` is the number of twins
kept. The original plan is unchanged.

**Example**

```python
import surveytwin as st

survey = st.read_qsf("examples/example_survey.qsf")
config = st.configure(bank="examples/example-bank", sample_size=8, seed=7,
                      model="gpt-5.6-luna-medium", accept_missing_images=True)
plan = st.plan(survey, config)
trial = plan.head(2)
print(trial)
print(trial.population, trial.config.sample_size)
```

```text
Plan: staged survey, 2 twins, seed 7, model gpt-5.6-luna-medium
  asked stage by stage: each stage's questions depend on the twin's earlier answers
  twins: ex-01, ex-02
  the first 2 of a 8-twin plan (plan.head)
  loop: Loop&Merge (BL_KS_FB_12) asks 3 rows of 1 question (1 answer a row), up to 3 answers per twin
8 2
```

### `Plan.to_batch_file`

```python
Plan.to_batch_file(path: str | Path, *, image_checker: Any = None) -> Path
```

Writes the input file of an OpenAI batch for a static plan, without sending
anything: JSON Lines, one line per twin that needs a model call. Each line is a
`POST` to `/v1/chat/completions` whose `custom_id` is the twin's position in
`plan.twins` (`"0"`, `"1"`, and so on). Use it to submit a batch yourself;
`surveytwin.batch.submit` sends the same lines.

The survey's images are checked first, as `run` checks them: an image that cannot
be sent stops here with `ImagesRefused`, unless the settings have
`accept_missing_images=True`; it is then left out of every line.

**Parameters**

- `path` (`str | Path`): the file to write. Missing folders are created, and an
  existing file is replaced.
- `image_checker` (`Any`, default `None`): a function that tests one image link.
  It takes the link and returns a dict with a `"status"` and a `"detail"`; the
  status `"inaccessible"` or `"auth_required"` means the image cannot be sent,
  and any other (such as `"supported"`) lets it through. `None` tests each link
  over the network. Pass a function to work offline, or in tests.

**Returns** `Path`: the file written.

**Raises**

- `BatchNotAvailable`: the plan is staged, or its model is not an OpenAI preset
  model.
- `ImagesRefused`: some images cannot be sent and `accept_missing_images` is
  `False`.

**Example**

```python
import surveytwin as st

survey = st.read_qsf("examples/example_survey.qsf")
static_survey = survey.drop_blocks([
    "BL_KS_MC_1", "BL_KS_TG_10", "BL_KS_RO_8", "BL_KS_FB_13",
    "BL_KS_FB_14", "BL_5mvGQoBt7GcmAFo", "BL_8jFpZfwsRRTsiOi",
])
config = st.configure(bank="examples/example-bank", sample_size=3, seed=7, model="gpt-5.6-luna-medium")
plan = st.plan(static_survey, config)
path = plan.to_batch_file("runs/example/batch.jsonl")
print(path, len(path.read_text().splitlines()))
```

```text
runs/example/batch.jsonl 3
```

### `Plan.batch_items`

```python
Plan.batch_items(client: Any, blocked_image_urls: list[str] | None = None) -> list[dict[str, Any]]
```

The batch lines of a static plan as Python dicts, before they are written: one
per twin that needs a model call, each `{"custom_id": ..., "body": ...}` with the
request body for `/v1/chat/completions`. `to_batch_file` and
`surveytwin.batch.submit` call it. It checks neither the images nor the model,
and it works for static plans only.

**Parameters**

- `client` (`Any`): builds each request body. Pass the library's OpenAI client,
  `surveytwin.providers.openai_client.OpenAIClient()`; building bodies needs no
  key.
- `blocked_image_urls` (`list[str] | None`, default `None`): image links to leave
  out of every request. `None` leaves every image in.

**Returns** `list[dict[str, Any]]`: one dict per twin that needs a model call.

**Example**

```python
import surveytwin as st
from surveytwin.providers.openai_client import OpenAIClient

survey = st.read_qsf("examples/example_survey.qsf")
static_survey = survey.drop_blocks([
    "BL_KS_MC_1", "BL_KS_TG_10", "BL_KS_RO_8", "BL_KS_FB_13",
    "BL_KS_FB_14", "BL_5mvGQoBt7GcmAFo", "BL_8jFpZfwsRRTsiOi",
])
config = st.configure(bank="examples/example-bank", sample_size=3, seed=7, model="gpt-5.6-luna-medium")
plan = st.plan(static_survey, config)
items = plan.batch_items(OpenAIClient())
print([item["custom_id"] for item in items], sorted(items[0]["body"]))
```

```text
['0', '1', '2'] ['max_completion_tokens', 'messages', 'model', 'reasoning_effort', 'response_format']
```

## `PromptPreview`

One twin's prompt, from `plan.preview_prompt(...)`. A record; `print(preview)`
prints its `text`.

| Name | Type | Meaning |
|---|---|---|
| `twin_id` | `str` | The twin's ID. |
| `stage` | `int` | The stage asked for. |
| `text` | `str` | The whole prompt as readable text: a `=== SYSTEM ===` part and a `=== USER ===` part, with each image shown as `[image: <link>]`. |
| `messages` | `list[dict[str, Any]]` | The exact messages or input items, as `Plan.prompts` returns them. |
| `images` | `list[str]` | The links of the images sent with the prompt. |
| `input_tokens` | `int` | A rough count of input tokens: the characters of `text` divided by four. |
| `max_output_tokens` | `int` | The output limit the call asks for. It grows with the number of questions: 900 plus 120 per question, at least 8,000 and at most 32,000. |
| `kind` | `str` | `"chat"` (text only), `"multimodal"` (with images), or `"none"` (nothing to answer, so no call). |

See also: [Reading and checking a survey](survey.md), [Persona banks](personas.md),
[Results and export files](results.md), [Errors](errors.md).
