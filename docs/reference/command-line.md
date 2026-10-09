# Command line

Installing the package also installs the `surveytwin` command, which runs the
library's steps from a shell: check a survey, print the prompts, run it, re-ask
flagged answers, rebuild the export files and the report, and use the OpenAI
Batch API. Each command calls the same functions as the Python API, with no
extra dependencies. This page lists every command and option.

```bash
surveytwin check examples/example_survey.qsf
surveytwin prompts examples/example_survey.qsf --bank examples/example-bank --twins 2 --seed 7
surveytwin --help                 # the commands; 'surveytwin COMMAND --help' for one command
```

| Command | What it does | Calls a model | Writes | Python equivalent |
|---|---|---|---|---|
| [`check`](#surveytwin-check) | Reads a survey; prints its blocks and its check. | no | nothing | `read_survey` or `read_qsf`, `survey.preview_blocks()`, `survey.check()` |
| [`convert`](#surveytwin-convert) | Writes a survey in the standard format. | no | the `.json` file | `survey.standard` |
| [`schema`](#surveytwin-schema) | Prints the JSON Schema of the standard format. | no | nothing | `spec.model.json_schema()` |
| [`model-check`](#surveytwin-model-check) | One tiny call: does the model reply, with token counts and a stop reason? | yes | nothing | `model.check()` |
| [`prompts`](#surveytwin-prompts) | Prints the exact first prompt of each planned twin. | no | nothing | `plan(...)`, `plan.preview_prompt(twin_id)` |
| [`run`](#surveytwin-run) | Runs the survey and saves the run folder. | yes | the run folder | `run(plan)`, `results.save(folder)` |
| [`rerun`](#surveytwin-rerun) | Re-asks the flagged answers of a saved run. | yes | the run folder, again | `Results.load(folder).rerun_flagged()` |
| [`export`](#surveytwin-export) | Rebuilds the export files of a saved run. | no | the export files | `results.write_exports(folder)` |
| [`report`](#surveytwin-report) | Rebuilds the validation report of a saved run. | no | `report.html` or `report.json` | `results.report(path)` |
| [`batch`](#surveytwin-batch) `submit`, `status`, `collect` | The OpenAI Batch API, for static surveys. | yes | `batch_state.json`, then the run folder | `batch.submit`, `batch.status`, `batch.collect` |

## The survey argument

`check`, `prompts`, `run` and `batch submit` take the survey as their first
argument (shown as `SURVEY` below):

- a `.qsf` file exported from Qualtrics, read with `read_qsf`;
- or a `.json` file. A file with `schema_version` is a survey in [the standard
  format](survey-format.md), read with `read_survey`; its warnings are printed on
  standard error as `warning: PATH: ...`. A file with `questions` and no
  `SurveyElements` is a survey draft, built with `builder.build_survey` (see [The
  survey builder](builder.md)); its warnings are printed the same way. Any error
  stops the command. Any other `.json` file is read as a QSF.

`--drop BLOCK_ID` drops a block before anything else, as `survey.drop_blocks`
does; repeat it to drop several. An unknown block ID is an error that lists the
known ones.

```bash
cat > product.json <<'EOF'
{"title": "Product evaluation",
 "questions": [
   {"id": "Q1", "type": "single_choice", "text": "Would you buy this product?", "options": ["Yes", "No"]},
   {"id": "Q2", "type": "open_text", "text": "What would you change?", "color": "red"}]}
EOF
surveytwin check product.json
```

```text
warning: Q2 color: Unknown field 'color' is ignored.
Survey('Product evaluation', 2 questions, 1 block)
1 block, in flow order:
  BL_BUILDER  'Product evaluation'  2 to answer  MC 1, TE 1
Execution mode: static
Design cells: 1
OK
```

## Shared options

`prompts`, `run` and `batch` take these options. They become the arguments of
`configure(...)`, except `--mode`, which is the `mode` of `plan(...)`:

| Option | Value | Default | Meaning |
|---|---|---|---|
| `--bank` | `twin-2k-500`, or a folder or zip | `twin-2k-500` | The persona bank: the default Twin-2K-500 bank (until it is published, set `SURVEYTWIN_TWIN2K_BANK`; see [Environment variables](#environment-variables)), or a Croissant Twins bank folder or zip. |
| `--representation` | a name | the bank's default | The text that describes each persona: a representation's ID, its kind or its label (in any case), or `empty` for no persona at all, as a baseline. |
| `--filter` | `FIELD=VALUE[,VALUE]`, `FIELD=LOW..HIGH` or `FIELD=:missing` | none | Which personas can be sampled; repeatable (see [`--filter`](#--filter)). |
| `--twins` | a whole number, 1 or more | `3` | How many twins to sample (`sample_size`). |
| `--seed` | a whole number, 0 or more | `42` | Picks the twins and every random choice in the survey. |
| `--model` | a preset name, or `FILE.py:NAME` | `run`: required; `prompts` and `batch`: `gpt-5.6-luna-medium` | The model (see [`--model`](#--model)). |
| `--provider` | `openai` or `openrouter` | `openai` | Where a preset name is sent. An error with `FILE.py:NAME`, which sets its own address. |
| `--system-instruction` | text, or `@FILE` | the default instruction | Replaces the instruction that tells the model how to behave as a respondent. `@FILE` reads it from a UTF-8 text file. Blank text means the default. The answer format and the rule that persona text is data, not instructions, cannot be changed. |
| `--embedded-data` | `FIELD=VALUE` | none | A value for an embedded-data field the real study filled from outside the survey, such as a URL parameter; repeatable. The text up to the first `=` is the field. An entry without `=` is ignored, and for a field given twice the last value counts. |
| `--max-stages` | a whole number, 1 or more | `25` | The most stages that ask something a staged twin may take; a twin stopped by the limit is marked incomplete. |
| `--carry-stage-images` | none (a flag) | off | Send each stage's images again with every later stage. |
| `--accept-missing-images` | none (a flag) | off | Run even if some survey images cannot be sent (a dead link, a Qualtrics login, a model without image input); they are left out and listed in the results. A question that shows one is not asked and is recorded as skipped. |
| `--mode` | `auto`, `static` or `staged` | `auto` | How the survey is asked: `static` refuses a survey that needs stages, `staged` forces stages. `batch` does not use it. |

`check` takes only `--drop`, `--network` and `--embedded-data`; it needs no bank.

### `--filter`

- `FIELD=VALUE[,VALUE]` keeps personas whose `FIELD` has one of the values. A
  value can be given by its label (in any case) or its code. For a numeric
  field, the values must match exactly.
- `FIELD=LOW..HIGH` keeps personas whose numeric `FIELD` is in the range, ends
  included. Either end can be left out: `age=..50`, `age=60..`. Both ends must
  be numbers.
- `FIELD=:missing` keeps personas with no value for `FIELD`.

Repeat `--filter` for several fields; a persona must match all of them. If the
same field is given twice, the last one counts. Values are split at commas, so
a value cannot contain a comma, and a value that contains `..` is read as a
range. An unknown field or value is an error that suggests close matches.

```bash
surveytwin prompts examples/example_survey.qsf --bank examples/example-bank \
    --twins 3 --filter region=South,West --filter age=..50
```

### `--model`

- A **preset name**, such as `gpt-5.6-luna-medium`, or any OpenAI model name,
  fine-tuned ones included (`ft:gpt-4.1-...:org::id`). See
  [Models](models.md).
- **`FILE.py:NAME`**: a model defined in Python. `NAME` is a `Model` in that
  file, or a function that takes no arguments and returns one. A value is read
  this way only when the part before its last colon ends in `.py`.

The file is run as Python, like any script you run yourself, by every command
given it (`prompts` too). It can import modules from its own folder. Results
record where the model came from: the file name, the name in it, and a
fingerprint of the file. It is an error (exit code 2) when the file does not
exist, `NAME` is not a Python name, the file fails to load, it has no `NAME`
(the message lists the models it defines), calling the function fails, or
`NAME` is not a `Model`.

```python
# mymodels.py
import surveytwin as st

# A model you call over HTTP (its key is read from ANTHROPIC_API_KEY when a call is made).
claude = st.Model.http(
    "claude-sonnet",
    "https://api.anthropic.com/v1/messages",
    layout="anthropic",
    headers={"x-api-key": st.env("ANTHROPIC_API_KEY"), "anthropic-version": "2023-06-01"},
    body={"model": "claude-sonnet-5"},
)

# A model written in Python; this one only replies {"ok": true}, enough for model-check.
echo = st.Model.from_function(
    lambda request: st.Reply(text='{"ok": true}', input_tokens=1, output_tokens=1), name="echo")
```

```bash
surveytwin model-check mymodels.py:echo
# needs ANTHROPIC_API_KEY:
surveytwin run product.json --bank examples/example-bank --twins 8 --model mymodels.py:claude --out runs/claude
```

## `surveytwin check`

```text
surveytwin check SURVEY [--drop BLOCK_ID] [--network] [--embedded-data FIELD=VALUE]
```

Reads a survey and prints what a run would do with it. It makes no model call
and needs no key, and it uses the network only with `--network`.

**Arguments and options**

- `SURVEY`: a `.qsf` file or a survey draft (`.json`); see
  [The survey argument](#the-survey-argument).
- `--drop BLOCK_ID` (repeatable): drop a block before checking.
- `--network`: test every image link over the network, as
  `survey.check(network=True)` does. Without it, the links are counted but not
  tested.
- `--embedded-data FIELD=VALUE` (repeatable): a value you will give the run.
  That field counts as set, so it is not reported as blank, and a field the
  survey never reads or sets is named.

**Prints**

1. the survey on one line: `Survey('NAME', N questions, M blocks)`;
2. every block in flow order, as `survey.preview_blocks()` lists them;
3. with `--drop`, the drop report: the dropped blocks and questions, and the
   logic that still reads them;
4. the check, as `survey.check()` gives it: the execution mode, why it is
   staged, the design cells, each finding with its support level, the images,
   and last `OK` or `Needs attention before a run`.

**Writes** nothing.

**Exit code** 0 when the survey was read and checked, even when the check ends
with `Needs attention before a run`; 2 when it cannot be read (see
[Exit codes](#exit-codes)).

**Example**

```bash
surveytwin check examples/example_survey.qsf
```

```text
Survey('Untitled project', 36 questions, 12 blocks)
12 blocks, in flow order:
  BL_KS_MC_1          'MC_Sigle'                11 to answer  MC 11
      read by the rest of the survey: branch at FL FL_14 reads QID56
      read by the rest of the survey: piping at SQ QID48 reads QID56
      ...
  BL_KS_TE_4          'TextEntry'               3 to answer   TE 3
  BL_KS_TG_10         'GraphicEntry'            1 to answer   DB 2, TE 1        2 images
  ...
Execution mode: staged
Staged because: branch or display logic reads an answer; display logic on questions; skip logic; piping of an earlier answer (${q://...})
Design cells: 3
[Full] execution.staged : QSF uses answer-dependent flow or question piping; simulation will run in staged passes.
[Partial] piped_text.question : Survey question piped text (${q://...}) will be resolved from prior staged answers.
...
Images: 3 found, links not tested (use network=True)
OK
```

## `surveytwin convert`

```bash
surveytwin convert SURVEY [--drop BLOCK_ID ...] [--out FILE]
```

Writes the survey in [the standard format](survey-format.md): a QSF read into
that format, a draft as built, or a standard-format survey as it runs (with
`--drop`, without the dropped blocks). The file reads back with `read_survey`
(or `surveytwin check FILE`) and runs as the original does.

- `--out FILE`, `-o FILE`: the file to write. Without it, the JSON is printed.

```bash
surveytwin convert examples/example_survey.qsf -o example_survey.json
surveytwin check example_survey.json
```

```
Saved 'Untitled project' in the standard format to example_survey.json
Survey('Untitled project', 36 questions, 12 blocks)
...
```

## `surveytwin schema`

```bash
surveytwin schema > survey-format.schema.json
```

Prints the JSON Schema (draft 2020-12) of the standard format, for editors and
tools outside Python. It is the same as `docs/reference/survey-format.schema.json`.

## `surveytwin model-check`

```text
surveytwin model-check MODEL [--provider {openai,openrouter}]
```

Makes one tiny call to a model and says whether the reply text, the token
counts and a stop reason were found where the library looks for them. Use it
before a long run, especially with a model defined in a file.

**Arguments and options**

- `MODEL`: a preset name, or `FILE.py:NAME` (see [`--model`](#--model)).
- `--provider` (`openai` or `openrouter`, default `openai`): where a preset name
  is sent. An error with `FILE.py:NAME`.

**Prints** five lines: `model: NAME (KIND)`, `reply text found: True` or
`False`, `token counts found: ...`, `stop reason found: ...` and `seconds: ...`.
When no reply text was found, a sixth line, `reply: ...`, shows the first 500
characters of the reply.

**Writes** nothing. A preset model needs its key (see
[Environment variables](#environment-variables)); a defined model reads its own.

**Exit code** 0 when reply text was found; 1 when the model replied without
text; 2 when the call failed or the model could not be loaded.

**Example**

With the `mymodels.py` file shown under [`--model`](#--model):

```bash
surveytwin model-check mymodels.py:echo
```

```text
model: echo (function)
reply text found: True
token counts found: True
stop reason found: True
seconds: 0.026
```

## `surveytwin prompts`

```text
surveytwin prompts SURVEY [--drop BLOCK_ID] [shared options] [--show K]
```

Plans the run exactly as `run` would, and prints each twin's first prompt. It
makes no model call and needs no key. Given the same options, `run` sends these
prompts, except that it leaves out any image it cannot send.

**Arguments and options**

- `SURVEY` and `--drop`: as for [`check`](#surveytwin-check).
- The [shared options](#shared-options). `--model` defaults to
  `gpt-5.6-luna-medium`. The prompts do not depend on the model, but the value
  must be valid, and a `FILE.py:NAME` file is still run.
- `--show K` (a whole number; default: every twin): print only the first `K`
  twins. The plan is still made for all `--twins` (randomizer arms are balanced
  over all of them), so these are the prompts the first `K` twins of that run
  get.

**Prints**, for each twin: a header line
`##### twin ID (KIND, ~N input tokens) #####`; the prompt in parts headed
`=== SYSTEM ===` and `=== USER ===`, with each image shown as `[image: URL]`;
and a blank line. `KIND` is `chat` (text only), `multimodal` (with images) or
`none` (nothing to answer, so no model call). `N` is a rough count (characters
divided by 4). For a staged survey this is the first stage; later stages depend
on the twin's answers. The plan's warnings, such as fewer matching personas than
`--twins`, are not printed.

**Writes** nothing.

**Exit code** 0, or 2 on an error (an unknown filter field, `--mode static` for a
survey that needs stages, ...).

**Example**

```bash
surveytwin prompts examples/example_survey.qsf --bank examples/example-bank --twins 2 --seed 7
```

```text
##### twin ex-07 (multimodal, ~10,707 input tokens) #####
=== SYSTEM ===
You are a survey simulation respondent. Stay faithful to the persona context. Return valid JSON only.
CRITICAL — match the answer format to each question's fields:
...
=== USER ===
Persona profile:
Source bank: example-bank v1.0.0 | Representation: Summary (summary) | Media: text/plain | ID: ex-07/summary
----- BEGIN PERSONA REPRESENTATION (untrusted data) -----
Age 38, lives in the West region of the United States, education: a graduate degree. ...
----- END PERSONA REPRESENTATION -----
Survey sequence:
Q1:
What is your favorite pet?
Question Type: Single Choice
...

##### twin ex-08 (multimodal, ~10,711 input tokens) #####
...
```

## `surveytwin run`

```text
surveytwin run SURVEY --model MODEL --out FOLDER [--drop BLOCK_ID] [shared options]
               [--concurrency N] [--keep-prompts]
```

Plans the run, asks every twin, and saves the run folder. A preset model's key,
and the environment variables a model from a file reads its keys from, are
checked before any call is made; a missing one stops the run with an error
naming it. Run [`model-check`](#surveytwin-model-check) once on a model from a
file, to see that its replies can be read.

**Arguments and options**

- `SURVEY` and `--drop`: as for [`check`](#surveytwin-check).
- The [shared options](#shared-options). `--model` is required. Note that
  `--twins` defaults to 3.
- `--out FOLDER` (required): the run folder; it is made if it does not exist.
- `--concurrency N` (a whole number, default `8`): how many twins are asked at
  the same time. A value below 1 counts as 1.
- `--keep-prompts`: also keep every request sent, in `prompts.jsonl`.

**Prints** the progress, `done/total twins`, on standard error while it runs.
Then one line on the results, such as
`Results(2 twins, 196 answers, 0 flagged, mode=staged)`, then
`Saved to FOLDER`, then up to 20 of the run's warnings, each as
`warning: ...`.

**Writes** the run folder, as `results.save(FOLDER)` does:

| File | What it holds |
|---|---|
| `responses_labels.csv`, `responses_values.csv`, `responses_repaired.csv` | The answers in the Qualtrics layout: as labels, as coded values, and as labels with rerun repairs applied. |
| `responses_labels_raw.csv`, `responses_values_raw.csv` | Labels and values without repairs; only once a rerun has repaired an answer. |
| `long.csv`, `wide.csv` | One row per twin and question; one row per twin. |
| `manifest.json` | The export files with their checksums, the columns, row counts, token counts and provenance. |
| `question_logic.json` | The survey as the exports read it. |
| `conjoint_exposures.csv`, `maxdiff_exposures.csv` | What each twin was shown in each conjoint or MaxDiff task, and what it chose; only for surveys with such blocks. |
| `report.html`, `report.json` | The validation report. |
| `questions/TWIN.txt`, `answers/TWIN.txt` | One text file of questions and one of answers per twin. |
| `results.json` | Everything `Results.load` needs to open the run again. |
| `audit.jsonl` | Each twin's path through the survey, one line per twin. |
| `prompts.jsonl` | Every request sent; only with `--keep-prompts`. |
| `survey.json` | The whole survey definition, with its drops and edits. |
| `checksums.json` | A checksum of every file above. |
| `survey.qsf`, `survey_standard.json` or `survey_draft.json` | A copy of the survey file given (a QSF, a survey in the standard format, or a draft). It is written after `checksums.json`, so it is not listed there. |

Saving into a folder that holds an earlier run replaces that run: its files
that the new run does not write (such as an earlier `prompts.jsonl`, or text
files of other twins) are removed. Files the library did not write are left
alone.

**Exit code** 0 when the run finished (a twin may still have failed; the
results say so); 2 on an error, for example a missing key, images that cannot be
sent without `--accept-missing-images`, or a filter no persona matches.

**Example**

```bash
# needs OPENAI_API_KEY
surveytwin run examples/example_survey.qsf --bank examples/example-bank --twins 3 --seed 7 \
    --model gpt-5.6-luna-medium --accept-missing-images --out runs/example
```

## `surveytwin rerun`

```text
surveytwin rerun FOLDER [--model MODEL]
```

Re-asks the flagged answers of a saved run (skipped, or out of range) and saves
the folder again. Only the flagged questions are asked, with the rest of each
twin's survey shown as it saw it, images included. A flagged question whose image
cannot be sent is not asked again (see `Results.rerun_flagged` in
[Results](results.md)).

**Arguments and options**

- `FOLDER`: a run folder. Every file listed in `checksums.json` is checked
  first.
- `--model MODEL` (default: the run's own model): a preset name or
  `FILE.py:NAME`. A run made with a model from a file needs
  `--model FILE.py:NAME` again, since a defined model cannot be restored from
  the folder. A preset name given here is sent to OpenAI; `rerun` has no
  `--provider` option (without `--model`, the run's own provider is used).

**Prints** the counts before the rerun, as
`{'twins': 2, 'flagged_answers': 2}`, and then one line on the updated results,
whose flagged count shows what is left. The rerun's warnings, such as a twin
whose call failed, are not printed; they are saved with the results
(`results.warnings`).

**Writes** the run folder again, as `run` does. Each repair is stored beside the
original answer, never over it: `responses_repaired.csv` applies the repairs,
and `responses_labels_raw.csv` and `responses_values_raw.csv` keep the answers
without them.

**Exit code** 0; 2 on an error: a file that no longer matches its checksum, a
run made with a defined model and no `--model`, or a preset model without its
key (also when nothing is flagged).

**Example**

```bash
surveytwin rerun runs/example                                 # the run's own model; needs its key
surveytwin rerun runs/claude --model mymodels.py:claude       # a run made with a model from a file
```

## `surveytwin export`

```text
surveytwin export FOLDER [--bundle]
```

Rebuilds the export files of a saved run from its `results.json`. It makes no
model call.

**Arguments and options**

- `FOLDER`: a run folder. Its files are not checked against `checksums.json`.
- `--bundle`: also write `export_bundle.zip`, with the three Qualtrics-layout
  CSV files, `long.csv`, the two raw files when there are any, the folder's
  `report.html` when it has one, and the `questions/` and `answers/` text
  files.

**Prints** one line per file written, as `KIND: PATH` (`csv_labels`,
`csv_values`, `csv_repaired`, `wide_csv`, `long_csv`, `manifest`,
`question_logic`, `questions_dir`, `answers_dir`, and `csv_labels_raw`,
`csv_values_raw` and `bundle_zip` when written).

**Writes** those files into `FOLDER`, replacing the saved ones, and updates
their entries in `checksums.json`, so the folder still loads.

**Exit code** 0; 2 when `FOLDER` is not a saved run.

**Example**

```bash
surveytwin export runs/example --bundle
```

## `surveytwin report`

```text
surveytwin report FOLDER [--json]
```

Rebuilds the validation report of a saved run. It makes no model call.

**Arguments and options**

- `FOLDER`: a run folder. Its files are not checked against `checksums.json`.
- `--json`: write `report.json` instead of `report.html`.

**Prints** the path of the file written.

**Writes** `FOLDER/report.html`, or `FOLDER/report.json` with `--json`,
replacing the saved one, and updates its entry in `checksums.json`, so `rerun`
and `Results.load` still accept the folder. For an unchanged run, the rebuilt
report is the same as the saved one, byte for byte.

**Exit code** 0; 2 when `FOLDER` is not a saved run or the report cannot be
built.

**Example**

```bash
surveytwin report runs/example
surveytwin report runs/example --json
```

## `surveytwin batch`

```text
surveytwin batch submit SURVEY --out FOLDER [--drop BLOCK_ID] [shared options]
surveytwin batch status --out FOLDER
surveytwin batch collect --out FOLDER
```

Uses the OpenAI Batch API: submit now, collect later, with nothing left running
in between. It works for static surveys and OpenAI preset models only, and every
action needs `OPENAI_API_KEY`.

- **`submit`** plans the survey as a static survey (`--mode` is not used, and a
  survey that needs stages is refused), checks its images as a run does, uploads
  one request for each twin that has something to answer, and creates the batch.
  It writes
  `FOLDER/batch_state.json`, which holds the batch ID and the arguments of this
  command, and prints the batch ID. `--model` defaults to
  `gpt-5.6-luna-medium`; `--provider openrouter` and a model from a file are
  refused.
- **`status`** prints the batch's status as OpenAI reports it, such as
  `validating`, `in_progress` or `completed`.
- **`collect`** reads `batch_state.json` and makes the plan again from the
  arguments saved by `submit`. Once the batch is `completed`, it saves the run
  folder as `run` does and prints one line on the results. A twin whose batch
  answer is missing or unusable is asked again in real time.

**Arguments and options**

- `submit|status|collect`: the action.
- `--out FOLDER` (required): the run folder that holds `batch_state.json`.
- `SURVEY`, `--drop` and the [shared options](#shared-options): for `submit`
  only. `status` and `collect` use only `--out`: `collect` takes every other
  setting from the saved arguments. Those arguments are read again as given, so
  a relative path (the survey file, a bank folder) is found from the current
  folder: run `collect` from the folder where you ran `submit`. Unlike `run`,
  `collect` does not copy the survey file into the folder.

**Exit code** 0; 2 on an error: no `batch_state.json` in `FOLDER` (`status`,
`collect`), a batch that is not `completed` yet (`collect`), a survey that needs
stages, a model that is not an OpenAI preset, or a missing key.

**Example**

```bash
# needs OPENAI_API_KEY; product.json is the static draft shown under "The survey argument"
surveytwin batch submit product.json --bank examples/example-bank --twins 8 --out runs/batch
surveytwin batch status --out runs/batch
surveytwin batch collect --out runs/batch      # once the status is completed
```

## Environment variables

No command takes a key as an option: keys are read from environment variables,
and they are never saved.

| Variable | Used by | Meaning |
|---|---|---|
| `OPENAI_API_KEY` | `run`, `rerun`, `model-check` and `batch` with a preset model sent to OpenAI | The OpenAI key. |
| `OPENROUTER_API_KEY` | `run`, `rerun` and `model-check` with a preset model sent to OpenRouter (`--provider openrouter`) | The OpenRouter key. When it is set, `run` and `rerun` also use it to try again, through OpenRouter, an OpenAI call that failed, images included. |
| any variable a models file reads with `st.env("NAME")` | commands that call a model from `FILE.py:NAME` | That model's key or setting; read when a call is made, never saved. |
| `SURVEYTWIN_TWIN2K_BANK` | every command with `--bank twin-2k-500` (the default) | The path of a local copy (folder or zip) of the default bank. |
| `SURVEYTWIN_TWIN2K_HF_REPO` | the same, when `SURVEYTWIN_TWIN2K_BANK` is not set | A Hugging Face dataset that holds the default bank; it is downloaded into the cache when first used. |
| `SURVEYTWIN_CACHE_DIR` | every command that opens a bank | Where opened banks are cached; by default `~/.cache/surveytwin`. |

Three more variables tune how answers are requested. They are read once, when
the library is loaded:

| Variable | Default | Meaning |
|---|---|---|
| `SIMULATION_MAX_CALL_RETRIES` | `5` | How many times a twin's call is tried when it fails or returns no usable answer (at least 1). |
| `SIMULATION_RETRY_BACKOFF_S` | `1.0` | Seconds to wait before trying again, times the attempt number. |
| `SIMULATION_MAX_ANSWER_TOKENS` | not set | When set, replaces the output limit the library works out for each answer call. |

## Exit codes

| Code | When |
|---|---|
| 0 | The command finished. For `check`, even when the check ends with `Needs attention before a run`. |
| 1 | `model-check`: the model replied, but no reply text was found. Any unexpected failure that is not a library error also ends with code 1, after a Python traceback. |
| 2 | A library error, printed on standard error as `error: MESSAGE`: a survey or draft that cannot be read, a draft with errors, a drop that leaves nothing to answer, a bank, filter or setting problem, a missing key, images that cannot be sent, a failed model check, and so on. Also a mistake in the command line itself (an unknown command or option, a missing required option, a value of the wrong kind), which prints the usage. |

`--assume`, an option of earlier versions, was removed: given to any command, it
is an error that says why. A batch submitted with it can still be collected; it
is ignored there, with a warning.

See also: [Reading and checking a survey](survey.md),
[The survey builder](builder.md) (drafts that these commands accept as `.json`),
[Persona banks](personas.md), [Configuring and planning a run](configure-and-plan.md),
[Running a plan](running.md), [Results and export files](results.md),
[Models](models.md), [Errors](errors.md).
