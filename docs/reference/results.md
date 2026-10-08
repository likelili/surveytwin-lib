# Results and export files

A run returns a `Results` object: every answer of every twin, each twin's record
and path through the survey, the token counts of every model call, and where the
run came from. This page covers `Results`, the `Answer` records it is made of, and
every file `Results.save` writes to a run folder.

The examples on this page continue from the setup below. It runs the example
survey with no network and no key: the function `stand_in` plays the model and
picks the first option of every question (1 for a number, "No comment" for a
text). The model `careless` also answers "Maybe" to question QID44, which has no
such option, so that the run has flagged answers to show; `careful` is used to
ask them again. With a real model you would pass a preset name instead, such as
`model="gpt-5.6-luna-medium"` (it needs `OPENAI_API_KEY`).

```python
import json
import surveytwin as st

def stand_in(request, unusable=()):
    """A model for trying the library offline: the first option, 1 for a number, a short text."""
    start = request.user.rindex("Questions JSON:\n") + len("Questions JSON:\n")
    questions = json.loads(request.user[start:request.user.index("\n\nRespond using JSON schema", start)])
    answers = []
    for q in questions:
        if q["id"] in unusable:
            value = "Maybe"                  # no such option: recorded as out of range
        elif q.get("options"):
            value = q["options"][0]
        else:
            value = "1" if q.get("type") == "numeric" else "No comment"
        answers.append({"question_id": q["id"], "answer_value": value, "answer_label": value, "answer_raw": value})
    return st.Reply(text=json.dumps({"answers": answers}), input_tokens=len(request.user) // 4, output_tokens=20 * len(answers))

careless = st.Model.from_function(lambda request: stand_in(request, unusable={"QID44"}), name="careless")
careful = st.Model.from_function(stand_in, name="careful")

survey = st.read_qsf("examples/example_survey.qsf")
config = st.configure(
    bank="examples/example-bank",
    sample_size=3,
    seed=7,
    model=careless,
    accept_missing_images=True,  # the example survey's image links are placeholders
)
results = st.run(
    st.plan(survey, config),
    image_checker=lambda url: {"status": "skipped", "detail": "not checked"},  # no network
)
print(results)
```

```
Results(3 twins, 293 answers, 3 flagged, mode=staged)
```

## `Results`

What a run returns. You get one from `st.run`, `st.arun`, `st.batch.collect`,
`Results.load` and `Results.rerun_flagged`; you do not build one yourself.
Printing it gives one line: twins, answers, flagged answers and the execution
mode. Treat it as a record: `rerun_flagged` returns a new `Results` and leaves
the one it was called on unchanged.

```python
surveytwin.Results(
    *,
    rows: list[dict[str, Any]],
    twins: list[dict[str, Any]],
    audit: list[dict[str, Any]],
    usage: list[dict[str, Any]],
    warnings: list[str],
    provenance: dict[str, Any],
    run: dict[str, Any],
    prompts: list[dict[str, Any]] | None = None,
)
```

**Parameters**

Each one becomes the attribute of the same name (see the table below).

- `rows` (`list[dict[str, Any]]`): the answer records.
- `twins` (`list[dict[str, Any]]`): one record per twin.
- `audit` (`list[dict[str, Any]]`): each twin's path through the survey.
- `usage` (`list[dict[str, Any]]`): one record per model call.
- `warnings` (`list[str]`): the run's warnings.
- `provenance` (`dict[str, Any]`): where the run came from.
- `run` (`dict[str, Any]`): what the exports, the report and reruns read.
- `prompts` (`list[dict[str, Any]] | None`, default `None`): the requests kept
  with `keep_prompts=True`; `None` means none were kept.

### Attributes / properties

| Name | Type | Meaning |
|---|---|---|
| `answers` | `list[Answer]` | Property: every answer as an `Answer` record. See [`Results.answers`](#resultsanswers). |
| `usage_totals` | `dict[str, int]` | Property: model calls and token counts, summed over `usage`. See [`Results.usage_totals`](#resultsusage_totals). |
| `survey` | `Survey` | Property: the survey this run asked. See [`Results.survey`](#resultssurvey). |
| `rows` | `list[dict[str, Any]]` | The answer records `answers` is built from, as saved in `results.json`. Each has `persona_id`, `question_id`, `question_text`, `question_type`, `answer_raw`, `answer_value`, `answer_label` and `embedded_data`; and when they apply, `answer_status`, `answer_code`, `not_shown`, `choice_count_violation`, and a repair's `answer_repaired`, `repair_method`, `repaired_by` and `repaired_at`. |
| `twins` | `list[dict[str, Any]]` | One record per twin, in sample order: `id`, `index`, `attributes`, `representation`, `source` (the bank, its version and the persona's representation), `seed`, `status`, `reason` and `usage`. `status` is `"complete"`; `"incomplete"` when the twin was stopped by `max_stages`; or `"failed"` when its call failed, it gave no answers, or every answer is skipped (`reason` says which). `usage` counts the calls and tokens of the run itself, not of later reruns. |
| `audit` | `list[dict[str, Any]]` | Each twin's path through the survey, in sample order. Every record has `persona_id`, `persona_index`, `assignments` (the twin's embedded-data values, in the order they were set, with where each came from) and `shown` (the options and descriptive text the twin was shown, kept for the questions a rerun needs them for); with conjoint or MaxDiff blocks, `experiments` (a record of each task the twin was asked, as drawn for it: see `Results.exposures`). A staged run adds `stage_count`, `question_ids`, `embedded_data`, `randomizer_choices`, `events` (one per stage), `warnings` and `stopped_at_stage_limit`; a static run adds `survey_pid`, `execution_mode`, `status`, `error` and `notes`. |
| `usage` | `list[dict[str, Any]]` | One record per model call, reruns included, with the token counts the provider reported: `provider`, `endpoint`, `model`, `resolved_model`, `status`, `input_tokens`, `cached_input_tokens`, `output_tokens`, `reasoning_tokens`, `total_tokens`, `latency_ms`, `request_id`, `twin_id`, `stage`, `purpose` (`"answer"` or `"rerun"`), `batch`, `error` and `extra`. There are no prices: the library computes no cost. |
| `warnings` | `list[str]` | What is worth knowing about the run, in sentences: the plan's warnings, images left out, multi-select answers with too few options, failed calls, and the notes of each rerun. |
| `provenance` | `dict[str, Any]` | Where the run came from: `library_version`, `prompt_version`, `support_table_version`, `run_seed`, `execution_mode`, `survey_name`, `survey_checksum_sha256`, `survey_source`, `survey_kind`, `survey_edits`, `excluded_blocks`, `bank` (with its `fingerprint`), `representation`, `model` (its settings, never a key), `custom_system_instruction`, `started_at`, `finished_at`, `elapsed_s` and `images_left_out`. A Batch API run adds `batch`; each `rerun_flagged` adds an entry to `reruns`. |
| `run` | `dict[str, Any]` | What the exports, the report and reruns read: `run_id`, `mode`, `config` (the settings, as `RunConfig.to_record()` gives them), `system_prompt` (the system message every twin got), `excluded_blocks`, `analysis` (why the survey runs static or staged), `definition` (the survey as asked, in the standard format, without dropped blocks; `question_logic.json` is written from it), `copies` (each twin's copy of a static run: its blocks of questions as shown, in the standard format), `media` (embedded images, stored once), `batch_phase`, `twin_ids`, `population` (the full plan's size, for a run of `plan.head(n)`) and `rerun_personas` (persona text, kept only for twins with flagged answers). A run saved by an earlier version holds `logic_template` and `participants` (the same in the template shape) instead; it loads, exports and reruns as before. |
| `exposures` | `dict[str, list[dict[str, Any]]]` | Property: what each twin was shown in [conjoint and MaxDiff blocks](survey-format.md#conjoint-blocks), joined with its answers. See [`Results.exposures`](#resultsexposures). |
| `prompts` | `list[dict[str, Any]]` | Every request the run sent, when `st.run`, `st.arun` or `batch.collect` ran with `keep_prompts=True`; empty otherwise. Each has `twin_id`, `stage` and `kind`, then `system`, `user` and `max_tokens` for a text request (`"chat"`), `input_items` and `max_tokens` for a request with images (`"multimodal"`), or `messages` and `max_tokens` for a Batch API line (`"batch"`). The calls of `rerun_flagged` are not kept. |

### `Results.answers`

```python
Results.answers -> list[Answer]
```

Property. Every answer of the run as an `Answer` record, in the order the run
produced them: twin by twin in sample order, and each twin's answers in the
order it was asked. Each access builds new records from `rows`, so changing a
record changes nothing in the results; keep the list in a variable when you use
it more than once. A question the twin never reached (a branch not taken, a
block after a screen-out, a question hidden by display logic) usually has no
answer at all; see `Answer.status` for the exceptions.

**Example**

```python
answer = next(a for a in results.answers if a.question_id == "QID4")
print(answer.twin_id, answer.label, answer.code)
```

```
ex-08 Strongly disagree 10
```

### `Results.exposures`

```python
Results.exposures -> dict[str, list[dict[str, Any]]]
```

A property. What each twin was shown in the survey's [conjoint and MaxDiff
blocks](survey-format.md#conjoint-blocks), joined with its answers, ready for
choice modelling. Saved as `conjoint_exposures.csv` and `maxdiff_exposures.csv`
in the run folder (only when the survey has such blocks).

**Returns** `{"conjoint": [...], "maxdiff": [...]}`; each list is empty when
the survey has no block of that kind.

- `conjoint`: one row per twin, task and option shown, the outside option
  included: `twin_id`, `block`, `task`, `generator` (how the task was drawn, such
  as `"random_valid_profiles/1"`, or `"supplied"`), `version` (of a supplied
  design), `question_id`, `position` (1 is shown first), `option_id` (what the
  answer records), `label`, `alternative` (a fixed alternative's ID), `outside`
  (`"1"` for the outside option), `chosen` (`"1"` or `"0"`; blank when the task
  has no usable answer), and for each attribute its level ID, `<attribute>_label`
  and, when the level has one, `<attribute>_value`.
- `maxdiff`: one row per twin, task and item shown: `twin_id`, `block`, `task`,
  `generator`, `version`, `position`, `item`, `label`, `best` and `worst` (`"1"`,
  `"0"`, or blank when the pair has no usable answer), and `anchor` (the
  follow-up's answer) when the block has one.

A repaired answer counts as repaired.

**Example**

```python
import surveytwin as st

results = st.Results.load("runs/coffee")
rows = results.exposures["conjoint"]
print(rows[0]["task"], rows[0]["label"], rows[0]["price_label"], rows[0]["chosen"])
```

### `Results.flagged`

```python
Results.flagged() -> list[Answer]
```

The answers that came back unusable and have no repair yet: those with status
`"skipped"` or `"out_of_range"`. These are what `rerun_flagged` asks again.

**Returns** `list[Answer]`: in the order of `answers`; empty when every answer is
usable or repaired.

**Example**

```python
for answer in results.flagged():
    print(answer.twin_id, answer.question_id, answer.status, answer.raw)
```

```
ex-08 QID44 out_of_range Maybe
ex-05 QID44 out_of_range Maybe
ex-04 QID44 out_of_range Maybe
```

### `Results.usage_totals`

```python
Results.usage_totals -> dict[str, int]
```

Property. The number of model calls and the token counts the providers
reported, summed over `usage` (reruns included): `calls`, `input_tokens`,
`output_tokens`, `reasoning_tokens` and `total_tokens`. There are no prices. A
model that reports no token counts (a function model that returns plain text,
say) adds calls but no tokens. Each twin's own totals are in `twins[i]["usage"]`.

**Example**

```python
print(results.usage_totals)
```

```
{'calls': 6, 'input_tokens': 40178, 'output_tokens': 5860, 'reasoning_tokens': 0, 'total_tokens': 46038}
```

### `Results.summary_stats`

```python
Results.summary_stats() -> list[dict[str, Any]]
```

A summary of each question's answers: one entry per answer ID (a matrix row or
a multi-select option has its own), sorted by ID. No model call.

**Returns** `list[dict[str, Any]]`: each entry is
`{"metric_key": <answer ID>, "metric": {...}}`. Every metric has:

- `question_id`, `question_text` (with any unfilled `${...}` codes taken out) and
  `question_type`;
- `kind`: `"numeric"` when at least 3 of the usable answers, and at least half of
  them, are numbers; otherwise `"categorical"`;
- `n`: the usable answers counted; `n_skipped` and `n_out_of_range`: the flagged
  ones; `n_cascade`: answers hidden by display logic that read a flagged answer;
  `n_structural`: blanks that are not flagged (an option not selected, a question
  not shown); `n_other`: usable answers not counted in `n` (for a numeric
  question, the answers that are not numbers).

A numeric metric adds `mean`, `median`, `min`, `max` and `std` (the population
standard deviation). A categorical metric adds `distribution`: the 8 most common
labels, each with `count` and `pct` (a share of `n`, from 0 to 1).
`low_quality: True` is added when more than half of the answers the twins were
asked are flagged, or when more than half of a categorical question's answers
are empty words such as "N/A".

A repaired answer (from `rerun_flagged`) counts as answered, with its repaired
label, as in the CSV files and the validation report.

**Example**

```python
stats = {item["metric_key"]: item["metric"] for item in results.summary_stats()}
print(stats["QID4"]["kind"], stats["QID4"]["distribution"])
print(stats["QID44"]["n"], stats["QID44"]["n_out_of_range"], stats["QID44"]["low_quality"])
```

```
categorical [{'label': 'Strongly disagree', 'count': 3, 'pct': 1.0}]
0 3 True
```

### `Results.to_pandas`

```python
Results.to_pandas() -> pandas.DataFrame
```

One row per answer, in the order of `answers`, with one column per `Answer`
field (`twin_id`, `question_id`, ..., `shaped_by_repaired`). Needs pandas:
`pip install -e ".[pandas]"` from a checkout.

**Returns** `pandas.DataFrame`.

**Raises**

- `ResultsError`: pandas is not installed.

**Example**

```python
frame = results.to_pandas()
print(frame.shape)
print(frame.loc[frame.status == "out_of_range", ["twin_id", "question_id", "raw"]])
```

```
(293, 13)
    twin_id question_id    raw
94    ex-08       QID44  Maybe
192   ex-05       QID44  Maybe
290   ex-04       QID44  Maybe
```

### `Results.to_qualtrics_csv`

```python
Results.to_qualtrics_csv(path, *, values=False, repaired=False) -> Path
```

Writes one file in the Qualtrics layout: three header rows, then one row per
twin (see [The Qualtrics layout](#the-qualtrics-layout-responses_csv)). By default
the answers are labels: the text of the chosen option.

**Parameters**

- `path` (`str | Path`): the file to write. Missing folders are made; an existing
  file is replaced.
- `values` (`bool`, default `False`): write coded values instead of labels (the
  content of `responses_values.csv`).
- `repaired` (`bool`, default `False`): write the content of
  `responses_repaired.csv`. It is the same as the labels file: both already show
  each repair in place of the flagged answer.

The files with the answers as first given (`responses_labels_raw.csv`,
`responses_values_raw.csv`) are written only by `write_exports` and `save`.

**Returns** `Path`: `path`.

**Raises**

- `ResultsError`: both `values` and `repaired` are true.

**Example**

```python
results.to_qualtrics_csv("out/labels.csv")
results.to_qualtrics_csv("out/values.csv", values=True)
```

### `Results.to_long_csv`

```python
Results.to_long_csv(path) -> Path
```

Writes `long.csv`: one row per answer, with the answer as first given, its
status and any repair side by side (columns in [long.csv](#longcsv)).

**Parameters**

- `path` (`str | Path`): the file to write. Missing folders are made; an existing
  file is replaced.

**Returns** `Path`: `path`.

**Example**

```python
results.to_long_csv("out/long.csv")
```

### `Results.to_wide_csv`

```python
Results.to_wide_csv(path) -> Path
```

Writes `wide.csv`: one row per twin and one column per answer ID, named by the
survey's internal IDs (columns in [wide.csv](#widecsv)).

**Parameters**

- `path` (`str | Path`): the file to write. Missing folders are made; an existing
  file is replaced.

**Returns** `Path`: `path`.

**Example**

```python
results.to_wide_csv("out/wide.csv")
```

### `Results.to_text`

```python
Results.to_text(folder) -> Path
```

Writes one pair of text files per twin: `questions/<twin_id>.txt` (each question
the twin was asked) and `answers/<twin_id>.txt` (the same, with its answers).
See [The questions/ and answers/ folders](#the-questions-and-answers-folders).

**Parameters**

- `folder` (`str | Path`): the folder that gets the `questions/` and `answers/`
  folders. It is made if missing; files of the same names are replaced.

**Returns** `Path`: `folder`.

**Example**

```python
folder = results.to_text("out/text")
print(sorted(p.name for p in (folder / "answers").iterdir()))
```

```
['ex-04.txt', 'ex-05.txt', 'ex-08.txt']
```

### `Results.report`

```python
Results.report(path=None, *, format="html") -> dict[str, Any]
```

Builds the validation report and returns it. The report is worked out from the
export files alone, with no model call (see
[report.html and report.json](#reporthtml-and-reportjson)).

**Parameters**

- `path` (`str | Path | None`, default `None`): where to write the report.
  Missing folders are made. `None` writes nothing and only returns the report.
- `format` (`str`, default `"html"`): what to write at `path`: `"html"` for the
  page, `"json"` for the data. The returned dictionary is the same either way.

**Returns** `dict[str, Any]`: the content of `report.json`.

**Raises**

- `ResultsError`: `format` is not `"html"` or `"json"`; or the report could not
  be built (the reason is added to `results.warnings`).

**Example**

```python
report = results.report("out/report.html")
print(report["status"], sorted(report))
```

```
ok ['coverage', 'distributions', 'fidelity', 'randomization', 'schema_version', 'setting', 'status', 'validity']
```

### `Results.write_exports`

```python
Results.write_exports(folder, *, report=True, text_files=True, bundle=False) -> dict[str, Path]
```

Writes the export files into `folder`: the five kinds of response CSV,
`long.csv`, `wide.csv`, `manifest.json` and `question_logic.json` (with
`survey_definition.json` too for a survey written in the standard format), and by
default the report and the text files. `save` calls it, then adds the files that let the
run be loaded again; use `write_exports` when you want the exports only.

**Parameters**

- `folder` (`str | Path`): where to write. It is made if missing; files of the
  same names are replaced. When the run has no repairs, raw files left in the
  folder by an earlier export are removed.
- `report` (`bool`, default `True`): also write `report.html` and `report.json`.
  If the report cannot be built, both are left out and the reason is added to
  `results.warnings`.
- `text_files` (`bool`, default `True`): also write the `questions/` and
  `answers/` folders.
- `bundle` (`bool`, default `False`): also write `export_bundle.zip`, holding the
  labels, values and repaired files, the raw files when there are any,
  `long.csv`, `report.html` (when it was built) and the `questions/` and
  `answers/` text files.

**Returns** `dict[str, Path]`: each file written, by kind: `csv_labels`,
`csv_values`, `csv_repaired`, `csv_labels_raw` and `csv_values_raw` (only when
an answer was repaired), `wide_csv`, `long_csv`, `manifest`, `question_logic`,
`report_html` and `report_json` (when the report was built), `questions_dir` and
`answers_dir` (with `text_files`), and `bundle_zip` (with `bundle`).

**Example**

```python
files = results.write_exports("out/export", bundle=True)
print(sorted(files))
```

```
['answers_dir', 'bundle_zip', 'csv_labels', 'csv_repaired', 'csv_values', 'long_csv', 'manifest', 'question_logic', 'questions_dir', 'report_html', 'report_json', 'wide_csv']
```

### `Results.save`

```python
Results.save(folder) -> Path
```

Writes the run folder: every export file (as `write_exports` does, with the
report and the text files), then `results.json`, `audit.jsonl`, `prompts.jsonl`
(when the run kept prompts), `survey.json` and last `checksums.json`. Save every
run: `Results.load` reads the folder back, and the files are the record of what
was asked and answered. [The run folder](#the-run-folder) describes each file.

**Parameters**

- `folder` (`str | Path`): where to write. It is made if missing. A folder that
  holds an earlier save is made to hold this run only: files of the same names
  are replaced, and the earlier save's files that this run does not write (its
  `prompts.jsonl`, the text files of twins this run did not ask, an
  `export_bundle.zip`) are removed. Files the library did not write are left
  alone and are not listed in `checksums.json`.

**Returns** `Path`: `folder`.

**Example**

```python
folder = results.save("runs/example")
print(folder)
```

```
runs/example
```

### `Results.load`

```python
Results.load(folder, *, verify=True) -> Results
```

Class method. Reopens a run folder written by `save`, first checking every file
against `checksums.json`.

**Parameters**

- `folder` (`str | Path`): the run folder.
- `verify` (`bool`, default `True`): check each file listed in `checksums.json`
  against its SHA-256 before loading. A folder without `checksums.json` is loaded
  without a check. `False` skips the check, for a folder whose files you changed
  on purpose.

**Returns** `Results`: everything in `results.json`, the requests in
`prompts.jsonl`, and the survey in `survey.json` (read when first asked for).
The model is not saved: after loading a run made with a defined model (HTTP or
function), pass the model again to `rerun_flagged`.

**Raises**

- `ResultsError`: the folder has no `results.json`; with `verify=True`, a file
  listed in `checksums.json` is missing or does not match its checksum; or
  `results.json` is not a surveytwin results file.

**Example**

```python
results.save("runs/example")
again = st.Results.load("runs/example")
print(again)
```

```
Results(3 twins, 293 answers, 3 flagged, mode=staged)
```

### `Results.to_dict`

```python
Results.to_dict() -> dict[str, Any]
```

The run as one dictionary that JSON can hold: what `save` writes to
`results.json`. It has `format` (`"surveytwin-results"`), `format_version` (`1`),
`rows`, `twins`, `audit`, `usage`, `warnings`, `provenance` and `run`. The kept
requests and the survey are not in it; `save` writes them to `prompts.jsonl` and
`survey.json`. The lists and dictionaries in it are the results' own, not
copies: copy them before you change anything.

**Returns** `dict[str, Any]`.

**Example**

```python
data = results.to_dict()
print(data["format"], data["format_version"], len(data["rows"]))
```

```
surveytwin-results 1 293
```

### `Results.rerun_counts`

```python
Results.rerun_counts() -> dict[str, Any]
```

How much `rerun_flagged` would ask again: `{"twins": ..., "flagged_answers": ...}`,
the twins with at least one flagged answer and the number of flagged answers.
Answers already repaired are not counted. No model call.

**Returns** `dict[str, Any]`: the two counts.

**Example**

```python
print(results.rerun_counts())
```

```
{'twins': 3, 'flagged_answers': 3}
```

### `Results.rerun_flagged`

```python
Results.rerun_flagged(*, api_key=None, model=None, max_concurrency=8, on_usage=None) -> Results
```

Asks each twin with flagged answers again, for those answers only, and returns
new results with the repairs added. Each twin gets one call. It is shown every
question it was asked, with its other answers filled in and the flagged ones
marked `[NEEDS ANSWER]`; the options and descriptive text are those it was shown
(with its own piped and random values). The call sends the images the twin saw
with its questions: those in descriptive text above a question, and each
question's own (in its text, options or rows), each once. The system message is
the run's, with `RERUN_INSTRUCTION` added (see [Prompt texts](prompts.md)). The
answer goes through the same matching as a first answer; one that still does not
fit stays flagged. The best and worst answers of a MaxDiff task are asked with
the rule that they differ, and a repair that names the item its pair's other
answer names is not stored: the answer stays flagged, and a warning says how
many such pairs there are.

A question is never answered without its picture. A flagged question that shows
an image the run left out because it could not be loaded, an image the provider
refuses now, or any image when the model takes no image input
(`images=False`), is not asked again: it stays flagged, and a warning says how
many such questions there are. An image in descriptive text that cannot be sent
is only left out.

Originals never change. A repair is stored beside the answer, in
`Answer.repair`: the labels, values and repaired files show it, and long.csv and
the raw files keep the original. An answer whose text, options or visibility
came from an answer repaired now is marked in `Answer.shaped_by_repaired`, not
asked again, and a warning says how many there are. `provenance["reruns"]` gets
one entry with `at`, `model`, `twins`, `flagged`, `repaired`, `marked`,
`library_version` and `prompt_version` (of the library that asked again). A run
made with another version of the library is asked again with the one that loads
it, and a warning names both versions.

A failed call does not raise: the warnings get "Rerun failed for <twin>: ..."
and the twin's answers stay flagged. A flagged twin whose persona text was not
kept cannot be asked again, and a warning says how many. The method runs its own
event loop, so it cannot be called inside a running one (in async code, for
example): Python raises `RuntimeError` there.

**Parameters**

- `api_key` (`str | None`, default `None`): the key for a preset model. `None`
  reads `OPENAI_API_KEY`, or `OPENROUTER_API_KEY` for an OpenRouter preset.
  Function and HTTP models do not use it.
- `model` (`Model | str | None`, default `None`): the model to ask. A `Model`, or
  the name of an OpenAI preset (for OpenRouter, pass
  `st.Model.preset(name, provider="openrouter")`). `None` asks the run's own
  model. The results `st.run` returned remember it; after `Results.load`, only a
  preset model can be rebuilt from the saved settings, so pass a defined model
  again.
- `max_concurrency` (`int`, default `8`): how many twins are asked at once.
- `on_usage` (callable, default `None`): called with the usage record of each
  call (a `UsageRecord`, as for `st.run`). The records are also added to the new
  results' `usage`.

**Returns** `Results`: new results with the repairs, the original usage plus the
rerun's calls, the warnings plus the rerun's notes, and the new entry in
`provenance["reruns"]`. Twins, audit, run and kept requests are those of the
original run.

**Raises**

- `ResultsError`: the run used a defined model (HTTP or function) that cannot be
  rebuilt, and no `model` was given; or the model is a preset and no key was
  given or set.

**Example**

Here the `careful` model answers QID44 with its first option, "Yes". QID44 has
skip logic that can end the block or the survey, so the twins' later answers to
QID45 and QID48 were given to a survey that the original answer, "Maybe",
decided; they are marked.

```python
fixed = results.rerun_flagged(model=careful)
print(fixed)
for answer in fixed.answers:
    if answer.repair or answer.shaped_by_repaired:
        value = answer.repair["value"] if answer.repair else ""
        print(answer.twin_id, answer.question_id, answer.status, value, answer.shaped_by_repaired)
```

```
Results(3 twins, 293 answers, 0 flagged, mode=staged)
ex-08 QID44 out_of_range Yes []
ex-08 QID45 None  ['QID44']
ex-08 QID48 None  ['QID44']
ex-05 QID44 out_of_range Yes []
ex-05 QID45 None  ['QID44']
ex-05 QID48 None  ['QID44']
ex-04 QID44 out_of_range Yes []
ex-04 QID45 None  ['QID44']
ex-04 QID48 None  ['QID44']
```

With a preset model, a saved run can be asked again later:

```python
again = st.Results.load("runs/example")
fixed = again.rerun_flagged(model="gpt-5.6-luna-medium")  # needs OPENAI_API_KEY: calls OpenAI
fixed.save("runs/example-rerun")
```

### `Results.survey`

```python
Results.survey -> Survey
```

Property. The survey this run asked, exactly as it was: its rules and
expressions, dropped blocks and edits, never a twin's values. Use it to check or
plan the same survey again. After `Results.load` it is read from `survey.json`
when first asked for, and compared with the survey checksum in `provenance`.

**Raises**

- `ResultsError`: the run folder has no `survey.json` (it was saved by an
  earlier version of the library), the file cannot be read, or its checksum is
  not the run's (it was changed, or belongs to another run).

**Example**

```python
results.save("runs/example")
again = st.Results.load("runs/example")
print(st.plan(again.survey, config))
```

```
Plan: staged survey, 3 twins, seed 7, model careless
  asked stage by stage: each stage's questions depend on the twin's earlier answers
  twins: ex-08, ex-05, ex-04
  loop: Loop&Merge (BL_KS_FB_12) asks 3 rows of 1 question (1 answer a row), up to 3 answers per twin
```

## `Answer`

One answer of one twin: one cell of the data. A question with parts gives one
answer per part: each option of a multi-select question, each row of a matrix,
each row of a Loop & Merge block. You get answers from `results.answers` and
`results.flagged()`. It is a record (a dataclass), built from `results.rows` each
time you ask; changing one changes nothing in the results.

```python
surveytwin.Answer(
    twin_id: str,
    question_id: str,
    question_text: str,
    question_type: str,
    value: str,
    label: str,
    raw: str,
    status: str | None,
    adjustments: list[dict[str, Any]] = <factory>,
    repair: dict[str, Any] | None = None,
    embedded_data: dict[str, Any] = <factory>,
    code: str = '',
    shaped_by_repaired: list[str] = <factory>,
)
```

`<factory>` means a new empty list or dictionary for each answer.

### Attributes / properties

| Name | Type | Meaning |
|---|---|---|
| `twin_id` | `str` | The twin's persona ID, such as `"ex-08"`. |
| `question_id` | `str` | The answer's ID: the question's internal ID (`QID56`), with a suffix for a part: `QID1_1` (option, row, statement or item 1), `QID19_1_2` (row 1, column 2), `QID35#1_1` (side by side: sub-question 1, row 1), `QID62_4_TEXT` (the write-in box of option 4), `1_QID43` (Loop & Merge row 1). The Qualtrics-layout files name the same column by the export tag instead. |
| `question_text` | `str` | The question as this twin was shown it, with piped text and random values filled in ("Please briefly explain why you like Cat."). For a part, the text of that part, such as "... Option: Apple. Should this option be selected?". |
| `question_type` | `str` | The kind of answer the twin was asked for: `"single_choice"` (a multi-select option is a single choice of No or Yes), `"matrix"`, `"numeric"` (numbers, sliders, ranks, constant sums) or `"open_text"`. |
| `value` | `str` | The answer as the data holds it: the chosen option's text for a choice or matrix question, `"1"` for a selected multi-select option, the number for a numeric answer, the text for a text answer. Blank when the answer is flagged, not shown, or an option not selected. It is not the coded value: see `code`. |
| `label` | `str` | The chosen option's text, which the labels file shows (for a selected multi-select option, the option's text). For other answers, the same as `value`. |
| `raw` | `str` | The answer as recorded before coding. For a usable answer it is the same as `label`: the answer after matching it to an option, or to a number in range. For an out-of-range answer it keeps the model's own words (`"Maybe"`), so you can see what it said. Blank for a skipped answer, except the best and worst answers of a MaxDiff task that named the same item twice: they are skipped, and `raw` keeps the item given. |
| `status` | `str \| None` | `None` for a usable answer, which can be blank: an option not selected, or an optional question the twin chose to leave blank. `"skipped"`: the twin was asked, but its reply holds no answer to the question (left out, empty, or a blank text where an answer is required); older data's `"needs_validation"` reads as `"skipped"`. `"out_of_range"`: the twin answered, but the answer matches no option or holds no number; its words are in `raw`. `"cascade_hidden"`: in a static run, display logic hid the question, but that logic read a flagged answer, so the hide is not certain. `"not_shown"`: in a static run, the question was in the twin's survey copy and its display logic hid it. `"skipped"` and `"out_of_range"` are the flagged statuses. A repair does not change the status: check `repair`. |
| `adjustments` | `list[dict[str, Any]]` | Rules the answer breaks that were recorded and kept as they were. The only one: a multi-select question answered with fewer options than its minimum. Each option of that question gets `{"rule": "min_choices", "required_min": 2, "selected": 0}`, and the run's warnings say the same. Empty otherwise. |
| `repair` | `dict[str, Any] \| None` | `None`, or the repair a rerun stored beside this answer: `{"value": "Yes", "method": "rerun_llm", "by": "surveytwin", "at": "<UTC time>"}`. `value`, `label`, `raw` and `status` keep the original. |
| `embedded_data` | `dict[str, Any]` | The twin's embedded-data values as they stood at the end of its survey, such as `{"pet": "None"}`; the same on every answer of the twin. |
| `code` | `str` | The chosen option's code in the values file, recorded from the options this twin was shown: the option's choice ID in the QSF, or its recode value when the survey recodes it (`"10"` for "Strongly disagree" in the example). Blank when there is none to record: no usable answer, a question without options, a multi-select option (its column is coded 1), a side-by-side cell, or a label two options share. The values file then looks the label up in the survey's options. A repair has no code of its own. |
| `shaped_by_repaired` | `list[str]` | The IDs of this twin's answers that were repaired since and that decided this question's text, options or whether it was shown. This answer was given to the survey as the original, flagged answer made it. It is marked, not asked again; empty for most answers. |

**Example**

```python
fixed = results.rerun_flagged(model=careful)
repaired = next(a for a in fixed.answers if a.repair)
print(repaired.question_id, repaired.status, repaired.raw, repaired.repair["value"], repaired.repair["method"])
marked = next(a for a in fixed.answers if a.shaped_by_repaired)
print(marked.question_id, marked.question_text, marked.label, marked.shaped_by_repaired)
```

```
QID44 out_of_range Maybe Yes rerun_llm
QID45 Which animal image did you see earlier? Cat ['QID44']
```

## The run folder

`Results.save(folder)` writes the files below. `write_exports` writes the ones
marked "export"; `save` adds the rest. This example saves the results after a
rerun, so that the raw files are there too; the other examples in this section
read the folder it writes.

```python
fixed = results.rerun_flagged(model=careful)
folder = fixed.save("runs/example-fixed")
for path in sorted(folder.iterdir()):
    print(path.name)
```

```
answers
audit.jsonl
checksums.json
long.csv
manifest.json
question_logic.json
questions
report.html
report.json
responses_labels.csv
responses_labels_raw.csv
responses_repaired.csv
responses_values.csv
responses_values_raw.csv
results.json
survey.json
wide.csv
```

| File | Written by | When | What it holds |
|---|---|---|---|
| `responses_labels.csv` | export | always | The Qualtrics layout, answers as labels, repairs applied. |
| `responses_values.csv` | export | always | The Qualtrics layout, answers as coded values, repairs applied. |
| `responses_repaired.csv` | export | always | The same content as `responses_labels.csv`. |
| `responses_labels_raw.csv` | export | an answer was repaired | The labels file with the answers as first given. |
| `responses_values_raw.csv` | export | an answer was repaired | The values file with the answers as first given. |
| `long.csv` | export | always | One row per answer: the answer, its status and any repair. |
| `wide.csv` | export | always | One row per twin, one column per answer ID. |
| `manifest.json` | export | always | The run's settings, the columns, and a checksum of each data file. |
| `question_logic.json` | export | always | The survey's structure and logic, as the exports and the report read it. |
| `survey_definition.json` | export | the survey was written in the standard format | The survey as the run asked it, as written in the standard format. |
| `conjoint_exposures.csv`, `maxdiff_exposures.csv` | export | the survey has conjoint or MaxDiff blocks | What each twin was shown in each task and what it chose (`Results.exposures`). |
| `report.html`, `report.json` | export | the report was built | The validation report. |
| `questions/`, `answers/` | export | always | One text file per twin in each. |
| `results.json` | `save` | always | Everything `Results.load` needs. |
| `audit.jsonl` | `save` | always | Each twin's path through the survey, one line per twin. |
| `prompts.jsonl` | `save` | the run kept prompts | Every request the run sent, one line per call. |
| `survey.json` | `save` | the results know their survey | The survey definition. |
| `checksums.json` | `save` | always | The SHA-256 of every other file above, and of the text files. |

Two more files can appear: `write_exports(..., bundle=True)` adds
`export_bundle.zip`, and the `surveytwin run` command adds a copy of the survey
it read (`survey.qsf`, or `survey_draft.json` for a draft). Neither is listed in
`checksums.json` when it is written, except that `surveytwin export --bundle`
records the bundle it writes.

### The Qualtrics layout (responses_*.csv)

The five `responses_*.csv` files share one layout, the one a Qualtrics export
uses. They are UTF-8 text. Each has three header rows, then one row per twin
that has at least one answer, sorted by twin ID.

1. **Column names.** `TWIN_ID`; then `demo_<attribute>` for each attribute of
   the twins' personas, sorted by name; then one column per embedded-data
   field, in the order first met; then the answer columns, in survey order. An
   answer column is named as Qualtrics names it, by the question's export tag
   (`KS_MC_Q1`): `_<n>` for an option, row, statement or rank item
   (`KS_MC_Q2_1`); `_<row>_<column>` for a cell of a multi-answer matrix;
   `_<option>_TEXT` for a write-in box (`KS_MC_TEOPT_4_TEXT`); `#<sub>_<row>` for
   a side-by-side cell (`KS_SBS_Q19#1_1`); and a `<row>_` prefix for each Loop &
   Merge row (`1_KS_LM_Q31`).
2. **Descriptions.** For an answer column, the question text as written in the
   survey, the same for every twin: piped text stays as its `${...}` code. A
   part adds ` - ` and the option, row or statement text (`Which of these fruits
   do you enjoy eating? Select 2 to 3. - Apple`). Descriptive text shown just
   above a question in its block comes first, followed by ` | `. A Loop & Merge
   row's values are filled in ("How appealing is Coffee during the Morning?").
   Blank for `TWIN_ID`, the `demo_` columns and the embedded-data columns.
3. **Import IDs.** `{"ImportId":"..."}` with the column's internal ID:
   `{"ImportId":"QID56"}`, `{"ImportId":"QID1_1"}`, `{"ImportId":"1_QID43"}`,
   `{"ImportId":"QID13_TEXT"}` for a text box; `{"ImportId":"TWIN_ID"}`,
   `{"ImportId":"demo_age"}` and `{"ImportId":"pet"}` for the other columns.

Every question in the survey has its columns, whether or not a twin saw it;
questions in dropped blocks have none. A cell is blank when the twin was not
shown the question, when its answer is flagged and not repaired, and for a
multi-select option it did not select. Answer IDs the survey's structure does
not name come last, named by internal ID; in the example survey, these are the
cells of the two matrices with several columns (`QID24_1_1`, `QID66_1_1` and so
on).

```python
import csv

with open("runs/example-fixed/responses_labels.csv", encoding="utf-8") as f:
    rows = list(csv.reader(f))
header = rows[0]
for name in ["TWIN_ID", "demo_age", "pet", "KS_MC_Q1", "KS_MC_Q2_1", "1_KS_LM_Q31"]:
    print([row[header.index(name)] for row in rows[:4]])
```

```
['TWIN_ID', '', '{"ImportId":"TWIN_ID"}', 'ex-04']
['demo_age', '', '{"ImportId":"demo_age"}', '45']
['pet', '', '{"ImportId":"pet"}', 'Cat']
['KS_MC_Q1', 'What is your favorite pet?', '{"ImportId":"QID56"}', 'Cat']
['KS_MC_Q2_1', 'Which of these fruits do you enjoy eating? Select 2 to 3. - Apple', '{"ImportId":"QID1_1"}', '']
['1_KS_LM_Q31', 'How appealing is Coffee during the Morning?', '{"ImportId":"1_QID43"}', 'Not appealing']
```

#### How the five files differ

They differ only in what a cell holds:

| File | A usable answer | A flagged answer | A repaired answer |
|---|---|---|---|
| `responses_labels.csv` | the label: the option's text, the number, or the text | blank | the repair |
| `responses_values.csv` | the code: `Answer.code`, or else the code the survey's options give the label (`1` for a selected multi-select option); numbers and text as they are | blank | the repair's code, looked up from its label |
| `responses_repaired.csv` | as the labels file | blank | as the labels file |
| `responses_labels_raw.csv` | as the labels file | blank | blank: the original, flagged answer |
| `responses_values_raw.csv` | as the values file | blank | blank: the original, flagged answer |

So the labels, values and repaired files are the data as it stands now, and the
two raw files are the data as the run first gave it. The raw files are written
only when at least one answer was repaired; before that they would equal the
labels and values files.

```python
import csv

for name in ["responses_labels.csv", "responses_values.csv", "responses_repaired.csv",
             "responses_labels_raw.csv", "responses_values_raw.csv"]:
    with open(f"runs/example-fixed/{name}", encoding="utf-8") as f:
        rows = list(csv.reader(f))
    header = rows[0]
    print(f"{name:25}", [rows[3][header.index(column)] for column in ["KS_MC_Q4", "KS_QO_Q35"]])
```

```
responses_labels.csv      ['Strongly disagree', 'Yes']
responses_values.csv      ['10', '1']
responses_repaired.csv    ['Strongly disagree', 'Yes']
responses_labels_raw.csv  ['Strongly disagree', '']
responses_values_raw.csv  ['10', '']
```

`KS_MC_Q4` is recoded in the survey, so "Strongly disagree" is `10`.
`KS_QO_Q35` is QID44, flagged and then repaired to "Yes".

### long.csv

One row per answer (one per `Answer`), sorted by twin ID and then by answer ID as
text (so `1_QID43` comes before `QID10`, and `QID10` before `QID2`). One header
row. The columns:

| Column | Holds |
|---|---|
| `persona_id` | The twin's ID (`Answer.twin_id`). |
| `question_id` | The answer ID (`Answer.question_id`). |
| `question_text` | The question as the twin was shown it. |
| `question_type` | `single_choice`, `matrix`, `numeric` or `open_text`. |
| `answer_raw` | `Answer.raw`: for an out-of-range answer, the model's own words. |
| `answer_value` | `Answer.value`. |
| `answer_label` | `Answer.label`. |
| `answer_status` | Blank for a usable answer (and for one not shown), or `skipped`, `out_of_range`, `cascade_hidden`, or `needs_validation` in older data. |
| `answer_repaired` | The repair's value, or blank. |
| `shaped_by_repaired` | `Answer.shaped_by_repaired`, joined with commas, or blank. |
| `demo_<attribute>` | The twin's persona attributes, sorted by name. |
| embedded-data fields | The twin's embedded-data values, one column per field. |

long.csv is the one data file that keeps the original answer, its status and its
repair side by side. `Answer.code`, the repair's method and time are not in it;
they are in `results.json`.

```python
import csv

with open("runs/example-fixed/long.csv", encoding="utf-8") as f:
    for row in csv.DictReader(f):
        if row["persona_id"] == "ex-04" and row["question_id"] in ("QID44", "QID45"):
            print({key: row[key] for key in ("question_id", "answer_raw", "answer_status", "answer_repaired", "shaped_by_repaired")})
```

```
{'question_id': 'QID44', 'answer_raw': 'Maybe', 'answer_status': 'out_of_range', 'answer_repaired': 'Yes', 'shaped_by_repaired': ''}
{'question_id': 'QID45', 'answer_raw': 'Cat', 'answer_status': '', 'answer_repaired': '', 'shaped_by_repaired': 'QID44'}
```

### wide.csv

One header row, then one row per twin that has at least one answer, sorted by
twin ID. The columns are
`persona_id`, the `demo_<attribute>` columns, the embedded-data fields, then one
column per answer ID that at least one twin has, named by internal ID
(`QID56`, `QID1_1`, `1_QID43`). Answer columns are ordered by the first number in
the ID, so a loop row such as `2_QID43` sits among the `QID2_` columns. Cells hold
what the labels file holds: labels, with repairs applied and flagged answers
blank. Use it when you want internal IDs rather than the Qualtrics headers;
unlike the Qualtrics layout, it has no columns for questions no twin answered.

### manifest.json

A description of the export, in the format other tools that read these files
expect:

- `export_manifest_version` (`"1.1"`), `generated_at` (when the run finished) and
  `run_id`.
- `config`: `model` (its name), `model_settings`, `persona_type` (the
  representation), `persona_count`, `seed`, `filters`, `persona_bank` (ID, name and
  version), `system_prompt` (the system message every twin got),
  `system_instruction` (yours, or `null` for the default), `use_batch_api`,
  `qsf_block_exclusions` and `qsf_block_names` (the dropped blocks),
  `embedded_data`, `carry_stage_images` and `max_stages`.
- `usage`: the same as `usage_totals`.
- `run_status`: `{"status": "completed", "batch_phase": ..., "error_text": ""}`.
- `simulation_warnings`: the run's warnings, each once.
- `row_counts`: `data_rows` (twins), `questions` (answer IDs),
  `persona_attributes`, `embedded_data_fields` and `summary_metrics`.
- `question_metadata` (each answer ID with its text and type),
  `question_columns`, `persona_attribute_columns` and `embedded_data_columns`:
  the columns of wide.csv.
- `provenance`: as `results.provenance`.
- `files`: each response CSV, long.csv, wide.csv and question_logic.json, with
  its `kind`, `file_name`, `sha256` and `bytes`.
- `qsf_staged_runtime`, for a staged run: twins, answer rows, model calls of all
  stages, and why the survey runs staged.

A few fields are always empty or `"none"`, kept so the format stays the same:
`thread_id`, `message_id`, `research_question`, `source_artifact_ids`,
`source_artifacts`, `report` and `config.report_mode`. The validation report is
not described here; it is in `report.html` and `report.json`.

The manifest describes the run as it was made: after `rerun_flagged`, its
`config` still names the run's model, and the rerun's model is in
`provenance.reruns`.

### question_logic.json

The survey as the exports and the validation report read it: `Metadata` (survey
ID, name and the like) and `Elements`, the flow in order. Blocks hold their
questions with their options, settings and logic; branches, randomizers and
the end of the survey are kept as they are in the flow. Dropped blocks are not
in it. An image embedded in the survey as data appears as a short key
(`media:sha256:...`); the image itself is stored once, in `results.json`.

### survey_definition.json

Only for a survey written in the standard format (`read_survey`): the survey as
the run asked it, in that format (docs/reference/survey-format.md), without its
dropped blocks. `question_logic.json` holds the same survey in the Qualtrics-like
form the report reads; this file shows it as it was written, such as a branch
with `else` as one branch. An image embedded as data appears as a short key, as
in `question_logic.json`. A survey read from a QSF has no such file.

### report.html and report.json

The validation report, worked out from the response files, long.csv,
manifest.json and question_logic.json, with no model call. `report.html` is a
page to read or print; `report.json` holds the same data:

- `schema_version`.
- `setting`: the run's settings and sample.
- `status`: `"ok"`, `"caution"` or `"error"`. It is `"caution"` when 20% or more
  of the questions have problems (flagged or tainted answers, values out of
  range) or 10% or more of the twins gave no usable answer at all, and
  `"error"` at 40% and 30%.
- `fidelity`: how fully each question in the survey is supported, and the run's
  warnings.
- `randomization`: the survey's randomizers, and how evenly the twins were spread
  over their arms.
- `validity`: rows, duplicate rows, skipped, out-of-range and tainted cells, and
  the questions with and without problems.
- `distributions`: each answer column's distribution and flags.
- `coverage`: the spread of the persona attributes in the sample.

The report counts repairs: a repaired answer counts as answered.
`Results.report()` returns the same dictionary as `report.json`. If the report
cannot be built, both files are missing and `results.warnings` says why.

### results.json, audit.jsonl and prompts.jsonl

- `results.json`: what `Results.load` reads, the same as `to_dict()`: `format`
  (`"surveytwin-results"`), `format_version` (`1`), `rows`, `twins`, `audit`,
  `usage`, `warnings`, `provenance` and `run`. It holds persona text only for the
  twins that had flagged answers (`run["rerun_personas"]`), so that a rerun can
  ask them again.
- `audit.jsonl`: `results.audit`, one JSON object per line, one line per twin.
  Each twin's embedded-data values, in the order they were set, are in its
  `assignments`.
- `prompts.jsonl`: `results.prompts`, one JSON object per line, one line per
  model call. Written only when the run kept its requests (`keep_prompts=True`
  in `st.run` or `st.arun`). The calls of `rerun_flagged` are not in it.

### survey.json

The whole survey definition, as `Survey.to_dict()` gives it: its rules and
expressions, dropped blocks and edits, never a twin's values. `checksums.json`
covers it, and `Results.survey` reads it back, refusing a file whose checksum is
not the run's. It is not written when the results do not know their survey (a
run saved by an earlier version of the library); a `survey.json` left in the
folder by another run is then removed.

### checksums.json

A dictionary from each file's path in the folder (such as `"answers/ex-04.txt"`)
to its SHA-256, for every file the library wrote there (the files in the table
above and the text files) except `checksums.json` itself, as the folder was at
the end of `save`. `Results.load` checks every file it lists. A file you add to
the folder is neither listed nor checked. `surveytwin report` and
`surveytwin export` update the entries of the files they rebuild, and keep the
rest, so a file changed by hand is still caught.

### The questions/ and answers/ folders

One text file per twin in each, named by twin ID, with the questions ordered by
the first number in their ID. `questions/<twin_id>.txt` lists what the twin was
asked:

```python
lines = open("runs/example-fixed/questions/ex-04.txt", encoding="utf-8").read().splitlines()
print("\n".join(lines[:8]))
```

```
Participant ID: ex-04
Question Count: 98

1. [1_QID43] How appealing is Coffee during the Morning?
Type: single_choice

2. [QID1_1] Which of these fruits do you enjoy eating? Select 2 to 3. Option: Apple. Should this option be selected?
Type: single_choice
```

`answers/<twin_id>.txt` is the same with `Answer Count` and an `Answer:` line
under each question: `Answer.raw`, or the label or value when it is blank. So an
out-of-range answer shows the model's own words, and a repair is not shown; a
blank answer has no `Answer:` line. When the label or the value differs from
that text, `Answer Label:` and `Answer Value:` lines follow.

```python
lines = open("runs/example-fixed/answers/ex-04.txt", encoding="utf-8").read().splitlines()
print("\n".join(lines[:6]))
```

```
Participant ID: ex-04
Answer Count: 98

1. [1_QID43] How appealing is Coffee during the Morning?
Type: single_choice
Answer: Not appealing
```

See also: [Running a plan](running.md), [Prompt texts](prompts.md),
[Configuring and planning a run](configure-and-plan.md), [Errors](errors.md).
