# surveytwin

Run a survey on simulated respondents ("digital twins") and get the data back
in the layout of a Qualtrics export.

You give the library a survey, a bank of personas, and a language model. The
survey is one definition in [the standard survey format](docs/reference/survey-format.md):
a JSON document of questions, responses, logic, blocks and flow, and conjoint and
MaxDiff experiments. Write one yourself (`read_survey`), or give a survey
exported from Qualtrics (a `.qsf` file; `read_qsf` reads it into the same format).
Each twin is one persona from the bank: the model reads that person's profile
and answers the survey as they would, in one go, or stage by stage when later
questions depend on earlier answers. You get
back the same files a Qualtrics export has (labels, coded values, a long table),
one text file of answers per twin, what each twin was shown in conjoint and
MaxDiff tasks, and a validation report.

Runs are reproducible: the same survey, personas, seed and model give the same
prompts and the same export files.

> **Status:** version 0.1, pre-release. The default persona bank (Twin-2K-500 in
> the Croissant Twins format) is not published for download yet; see
> [Persona banks](#persona-banks). The repository includes a small example bank
> of invented personas so everything below can be tried now.

## Contents

- [Install](#install)
- [Quick start](#quick-start)
- [How a run fits together](#how-a-run-fits-together)
- [The standard survey format](#the-standard-survey-format)
- [Step by step](#step-by-step)
- [Building a survey in Python](#building-a-survey-in-python)
- [Objects and functions](#objects-and-functions)
- [Models](#models) and [API keys](#api-keys)
- [Persona banks](#persona-banks)
- [Command line](#command-line)
- [Reproducibility](#reproducibility)
- [Full reference](docs/reference/index.md)

## Install

Python 3.11 or later.

```bash
pip install -e .              # from a checkout of this repository
pip install -e ".[pandas]"    # also installs pandas, for Results.to_pandas()
```

## Quick start

```python
import surveytwin as st

survey = st.read_qsf("examples/example_survey.qsf")
# or a survey written in the standard format:
# survey = st.read_survey("examples/survey-format/coffee-habits.json")
config = st.configure(
    bank="examples/example-bank",       # invented personas, for trying things out
    sample_size=3,
    seed=7,
    model="gpt-5.6-luna-medium",        # an OpenAI model; needs OPENAI_API_KEY
    accept_missing_images=True,         # the example survey's image links are placeholders
)
plan = st.plan(survey, config)          # picks the twins and builds each twin's survey
print(plan.preview_prompt())            # the exact prompt the first twin will get

results = st.run(plan)                  # asks every twin
results.save("runs/example")            # every export file, the report, one text file per twin
```

Nothing before `st.run` calls a model or needs a key.

## How a run fits together

Five functions take you from a survey file to results. Each returns an object
you can print and inspect before going on.

```
read_survey("study.json")  ->  Survey  the survey: blocks, questions, flow, what is supported
read_qsf("study.qsf")  ->  Survey      the same, from a Qualtrics export
bank(...)              ->  Bank        the personas: representations, filters, sampling
configure(...)         ->  RunConfig   every setting of the run, checked
plan(survey, config)   ->  Plan        the sampled twins and each twin's survey; prompts to preview
run(plan)              ->  Results     every answer, the export files, the report, reruns
```

A survey can be written in [the standard format](#the-standard-survey-format),
read from a QSF, or built question by question in Python: see [Building a
survey in Python](#building-a-survey-in-python).

Objects are records of what you asked for. To change something, make a new
one: `survey.drop_blocks(...)` and `survey.add_question(...)` return a new
survey, and to change a setting you
call `configure(...)` again and then `plan(...)` again. Editing an object's
attributes by hand is not supported; a plan keeps the settings it was built
with.

## The standard survey format

A survey in the standard format is one JSON document. Questions say what they
show (`content`) and what they ask for (`response`: a choice, text, a number, a
grid, a form, a ranking, an allocation, columns side by side); blocks group
questions and can repeat; the flow shows blocks, sets variables, branches on
conditions, draws random arms and ends the survey. Nothing in it is
Qualtrics-specific, and it is checked field by field before anything runs: an
error names the path of the field (`$.questions[2].response.options[1].label`).

```python
survey = st.read_survey("examples/survey-format/coffee-habits.json")
print(survey, survey.kind)
```

```
Survey('Coffee habits', 14 questions, 7 blocks) standard
```

A picture is named by its `source`: a web link, or the path of a picture file on
your computer, absolute or relative to the folder of the JSON file
(`{"type": "image", "source": "images/ad.png"}` next to `study.json`).
`read_survey` reads each file and stops on one that is missing or not a picture;
a run sends the picture itself to the model, and a saved run keeps a copy.

Every QSF is read into this format too (`survey.standard`); on all the test and
study QSFs of the library it runs exactly as the QSF reader ran it before.
`surveytwin convert study.qsf -o study.json` writes it out, to read, edit and
run again with `read_survey`.

Experiments are blocks of their own. A **conjoint** block gives attributes,
levels and the rules for combining them (which levels may appear together,
which combinations never, how often each level is drawn), and the engine draws
each twin's tasks, each shown as a table of the alternatives; a **MaxDiff** block
gives a pool of items, and each task asks for the best and the worst of a few of
them, which must differ (a pair that names one item twice is flagged, for a
rerun to ask again). Each twin's
tasks depend only on the run's seed, the twin and the block, so they never
change between previews, stages and reruns; all tasks of a block are asked in
one model call by default, each with its own answer (or one task per call, when
the block says so); and the run records what every twin was shown:

```python
survey = st.read_survey("examples/survey-format/coffee-conjoint.json")
print(survey, survey.questions["coffee_t1"].type)
```

```
Survey('Choosing a coffee shop', 8 questions, 2 blocks) MC
```

`results.exposures` (and the files `conjoint_exposures.csv` and
`maxdiff_exposures.csv`) list every alternative or item shown to every twin,
with its levels and whether it was chosen. The whole format, with examples of
every part, is in [docs/reference/survey-format.md](docs/reference/survey-format.md).

## Step by step

The output shown below comes from the files in `examples/`: a test survey that
uses most Qualtrics features (its image links are placeholders, so a run of it
needs `accept_missing_images=True`), and the example bank.

### Step 1. Read the survey

`read_qsf` reads a `.qsf` file (or its bytes, or the parsed JSON) and returns a
`Survey`. `survey.check()` tells you how the survey will run and what the
library does with each feature it uses. It makes no network calls unless you
pass `network=True`, which also tests every image link.

```python
survey = st.read_qsf("examples/example_survey.qsf")
print(survey)
print(survey.check())
```

```
Survey('Untitled project', 36 questions, 12 blocks)

Execution mode: staged
Staged because: branch or display logic reads an answer; display logic on questions; skip logic; piping of an earlier answer (${q://...})
Design cells: 3
[Full] execution.staged : QSF uses answer-dependent flow or question piping; simulation will run in staged passes.
[Partial] piped_text.question : Survey question piped text (${q://...}) will be resolved from prior staged answers.
[Partial] question_option.display_logic : Question-based branch/display logic will be evaluated after prior answers exist.
[Partial] question_option.skip_logic : Question skip logic will be evaluated after the skip-origin answer exists.
[Partial] flow.randomizer : Survey-flow randomizers will choose deterministic per-persona arms, balance Evenly Present elements when configured, and store arm metadata.
[Full] question_option.choice_randomization : 7 question(s) use choice randomization; each simulated respondent gets a deterministic per-persona presentation order.
[Partial] block_option.loop_and_merge : Loop & Merge asks every row: Loop&Merge (BL_KS_FB_12) asks 3 row(s) of 1 question(s) (1 answer(s) a row), up to 3 answers per twin.
Images: 3 found, links not tested (use network=True)
OK
```

- **Execution mode.** A *static* survey is answered in one model call per twin.
  A *staged* survey is answered in stages, because something later (a branch,
  display or skip logic, piped text) depends on an earlier answer; each stage
  sees the twin's earlier answers. A block is not one call: when a question pipes
  the answer to an earlier question of its own block, the questions before it
  are asked first and it waits for that answer, and nothing unresolved is shown.
  A later stage shows only the questions it asks (with the block's descriptive
  text as first shown); the questions answered before are listed with their
  answers, and their images are sent again. Descriptive text shown after the
  twin's last answer (an article before the outcome question, say) opens the
  next stage; text shown before an earlier answer, such as a consent page, is
  not repeated. An answer the data holds no answer for (skipped, out of range)
  counts as unanswered in piped text and logic.
- **Logic a twin cannot meet.** A condition on the device, the location, a quota
  or panel data is ignored: the rest of the logic decides, a question gated only
  on it is shown, and a screen-out on it never ends a survey.
- **Display logic on single options** (and matrix statements, scale points) is
  applied: an option shows only when its condition holds, and a question left
  with none is not shown. When the condition reads an answer, the survey is
  staged. Rows of a side-by-side question are the exception, named by the check.
- **Loop & Merge** asks every row; the plan says how many.
- **Findings.** Each line names a Qualtrics feature and how fully it is
  supported: `Full`, `Partial`, `Detected` (seen but not simulated) or
  `Not supported`.
- **Design cells.** How many different versions of the survey the randomizers
  can produce.
- **OK** means nothing blocks a run. Otherwise the check says what does, such as
  an image link that needs a Qualtrics login, or a survey with nothing for a twin
  to answer.

**Embedded data and random numbers.** The survey keeps each rule as Qualtrics
wrote it: `offer = ${rand://int/10:99}` in the flow, `Your offer is
${e://Field/offer}` in a question. Each twin's values are worked out when its
survey is prepared, in flow order, the same way in static and staged runs (a twin
draws the same random numbers in both), and kept with the run, never in the
survey. `survey.embedded_fields` lists every field: where it is set (in the flow,
from a panel or the survey link, by the survey's JavaScript, or never) and what
reads it. The check says, for each field that is read but never set, why it will
be blank; pass `survey.check(embedded_data={...})` the values you will supply to
the run, and a key the survey never uses is named. A random-number token that
cannot be drawn is listed as not supported.

### Step 2. Look at the blocks and drop what twins should not answer

Consent forms, attention checks and screen-outs are usually not meant for
twins. `preview_blocks()` lists every block in flow order with what it holds and
what else in the survey reads its answers.

```python
print(survey.preview_blocks())
```

```
12 blocks, in flow order:
  BL_KS_MC_1          'MC_Sigle'                11 to answer  MC 11
      read by the rest of the survey: branch at FL FL_14 reads QID56
      read by the rest of the survey: piping at SQ QID48 reads QID56
      ...
  BL_KS_TE_4          'TextEntry'               3 to answer   TE 3
  BL_KS_TG_10         'GraphicEntry'            1 to answer   DB 2, TE 1        2 images
  BL_KS_SBS_9         'SBS Block'               1 to answer   SBS 1
  BL_KS_RO_8          'Rank Order Block'        2 to answer   DB 1, RO 1, TE 1  1 image
  BL_KS_MX_6          'Matrix'                  6 to answer   Matrix 6
  BL_KS_SL_7          'Slider support fixture'  3 to answer   Slider 3
  BL_KS_FB_12         'Loop&Merge'              1 to answer   MC 1
  BL_KS_FB_13         'QOptions'                2 to answer   MC 2              screen-out
  BL_KS_FB_14         'ResponseDependent1'      1 to answer   TE 1
  BL_5mvGQoBt7GcmAFo  'ResponseDependent2'      1 to answer   TE 1
  BL_8jFpZfwsRRTsiOi  'ResponseDependent3'      1 to answer   TE 1
```

The columns are the block ID, its name, how many questions a twin answers, the
question types (`MC` multiple choice, `TE` text entry, `DB` descriptive text,
`RO` rank order, `SBS` side by side), and flags: images, and `screen-out` for a
block that can end the survey early.

`drop_blocks` returns a new survey without those blocks. A dropped question no
longer exists: no twin is asked it, and no CSV file, text file or report lists it.
A drop that would leave nothing to answer is refused (`DraftError`,
`survey.no_answerable`), as building such a survey is.
The drop report says what was dropped, and which logic in the rest of the survey
still reads a dropped question:

```python
trimmed = survey.drop_blocks(["BL_KS_MC_1"])
print(trimmed.drop_report)
```

```
Dropped blocks: BL_KS_MC_1
Dropped questions (11): QID1, QID10, QID11, QID12, QID2, QID4, QID56, QID6, QID62, QID7, QID9
Logic that reads a dropped question (it counts as never shown, with no answer):
  branch at FL FL_14 reads QID56
  piping at SQ QID48 reads QID56
  ...
```

That logic treats the dropped question the way Qualtrics treats a question a
respondent never saw: it has no answer. Here the branch "if QID56 is Cat" is
never taken, and text that pipes QID56 shows nothing; "is not selected" would be
true, and a number comparison is false (as it is for an answer left blank). The
same holds for any question a twin never sees (hidden by display logic, or in a
branch not taken): logic that reads it is decided at once, and the rest of the
survey is still asked. One exception: a branch whose
question conditions all read dropped questions and that runs no block (it ends
the survey or sets embedded data) is removed with the dropped blocks, whatever
else it tests (`drop_report.pruned_branches`), so dropping a consent block does
not screen out every twin; an "or" with an embedded field goes with it. To
change what twins see, drop the blocks behind those branches too. Display logic
on single choices follows the same rule: a choice shown only when a dropped
question was answered a certain way is hidden (`drop_report.choice_logic` lists
them). `preview_blocks()` marks
dropped blocks `DROPPED`, and `check()` lists the logic that reads them.

Questions can also be left out when reading:
`st.read_qsf(path, exclude_questions=["QID7"])`, by internal ID or by the export
tag of one question; anything else is refused, so a typo never leaves a question
in. They get the same treatment:
logic that reads one sees a question never shown, a skip rule that jumped to one
goes on to the next question kept, and a screen-out that reads only excluded
questions is removed (`drop_report` lists all three). `exclude_blocks=[...]` in
`read_qsf` is the same as `drop_blocks`.

### Step 3. Choose the personas

A bank is a set of personas in the Croissant Twins format. Each persona has
attributes you can filter on (age, region, ...) and one or more
*representations*: different texts describing the same person, such as a short
summary or demographics only. The default bank is Twin-2K-500
(`st.bank()`); here we use the example bank.

```python
bank = st.bank("examples/example-bank")
print(bank)
print(bank.representations())
print(bank.filter_options())
```

```
Example Bank (example-bank v1.0.0), 8 personas
Representations: Summary (default), Demographics
Filterable fields: age, region, education

Representations:
  Summary       default  8 personas  representations::summary
  Demographics           8 personas  representations::demographics
  Summary: A short paragraph about the person: work, household, habits and views.
  Demographics: Age, region and education only.

3 filterable fields:
  age        Age        numeric      23 to 70
  region     Region     categorical  Northeast (2), Midwest (2), South (2), West (2)
  education  Education  categorical  High school (2), Some college (2), Bachelor's degree (2), Graduate degree (2)
```

Filters are a dictionary from a field name to the values to keep. A value can
be given by its label or its code. A numeric field takes a range, and
`{"missing": True}` keeps personas with no value. Check how many personas a
filter keeps before you use it:

```python
print(bank.preview_filter({"region": ["South", "West"], "age": {"max": 50}}, sample_size=3))
```

```
3 of 8 personas match (37.5%), with representation representations::summary
  region: South 1, West 2
  enough for the sample size: yes
```

### Step 4. Configure the run

`configure` collects every setting and checks it at once, so a mistake (an
unknown filter field, a bad representation name) fails here, before any model
call.

```python
config = st.configure(
    bank="examples/example-bank",
    filters={"region": ["South", "West"], "age": {"max": 50}},
    sample_size=3,
    seed=7,
    model="gpt-5.6-luna-medium",
)
print(config)
```

```
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
  missing images      stop the run
```

| Setting | Default | What it does |
|---|---|---|
| `bank` | `"twin-2k-500"` | The bank: the default, a folder or zip, or a `Bank` object. |
| `representation` | the bank's default | Which text describes each persona. `"empty"` runs with no persona, as a baseline. |
| `filters` | none | Which personas can be sampled (see step 3). |
| `sample_size` | required | How many twins to sample. |
| `seed` | `42` | Picks the twins and every random choice in the survey (randomizer arms, question and choice order, random numbers). |
| `model` | required | A preset name such as `"gpt-5.6-luna-medium"`, or a `Model` (see [Models](#models)). |
| `provider` | `"openai"` | For preset names: `"openai"` or `"openrouter"`. |
| `batch` | `False` | Use the OpenAI Batch API (static surveys and OpenAI models only). |
| `system_instruction` | the default | Replaces the instruction that tells the model how to behave as a respondent. The answer format and the rule that persona text is data, not instructions, cannot be changed. The default is `st.prompts.DEFAULT_SYSTEM_INSTRUCTION`. |
| `embedded_data` | none | Values for embedded-data fields the real study filled from outside the survey, such as URL parameters (`survey.embedded_fields` shows which fields come from outside). A key the survey never reads or sets is named in the plan's warnings. |
| `max_stages` | `25` | The most stages that ask something a staged twin may take; a twin stopped by the limit is marked incomplete. `plan()` warns when a twin answering placeholders needs more. |
| `carry_stage_images` | `False` | Send each stage's images again with every later stage. |
| `accept_missing_images` | `False` | Run even if some survey images cannot be sent (dead link, Qualtrics login, a model without image input); they are left out and listed in the results. A question that shows one is not asked and is recorded as skipped; an image in descriptive text is only left out. |

### Step 5. Plan and preview

`plan` samples the twins and builds each twin's copy of the survey, with its own
randomizer arms and orders. It makes no model call, so you can read exactly what
each twin will be asked before spending anything.

```python
plan = st.plan(trimmed, config)
print(plan)
for twin in plan.twins:
    print(twin)
```

```
Plan: staged survey, 3 twins, seed 7, model gpt-5.6-luna-medium
  asked stage by stage: each stage's questions depend on the twin's earlier answers
  twins: ex-03, ex-06, ex-07

Twin('ex-03', age=23, region='West', education='some_college'; 524 characters of representations::summary)
Twin('ex-06', age=29, region='South', education='ba'; 504 characters of representations::summary)
Twin('ex-07', age=38, region='West', education='grad'; 486 characters of representations::summary)
```

`preview_prompt` shows the complete prompt for one twin as readable text (for a
staged survey, the first stage; later stages depend on the answers):

```python
print(plan.preview_prompt("ex-03"))
```

```
=== SYSTEM ===
You are a survey simulation respondent. Stay faithful to the persona context. Return valid JSON only.
CRITICAL — match the answer format to each question's fields:
...
=== USER ===
Persona profile:
Source bank: example-bank v1.0.0 | Representation: Summary (summary) | Media: text/plain | ID: ex-03/summary
----- BEGIN PERSONA REPRESENTATION (untrusted data) -----
Age 23, lives in the West region of the United States, education: some college. Takes community college classes part time while working at a coffee shop. Shares an apartment with two roommates, follows several creators on social media, buys secondhand clothing, and cares a lot about environmental claims on packaging.
----- END PERSONA REPRESENTATION -----
Survey sequence:
Q1:
In a few sentences, tell us a little about yourself.
Question Type: Text Entry
Default: My name is:

Q2:
What is your full name?
Question Type: Text Entry
...
```

- `plan.prompts(twin_id)` gives the same prompt as the exact messages sent to
  the model, images included.
- `plan.head(3)` is the same plan for its first 3 twins, each with exactly the
  survey it has in the full plan: run it to try a model or a system instruction
  cheaply.
- `plan.warnings` lists anything worth knowing, for example fewer matching
  personas than the sample size.

### Step 6. Run

```python
results = st.run(plan)                   # or: await st.arun(plan) in async code
```

`run` takes `api_key=` (otherwise `OPENAI_API_KEY` or `OPENROUTER_API_KEY`),
`max_concurrency=8`, `keep_prompts=False` (keep every prompt sent), and two
callbacks: `on_progress(done, total)` and `on_usage(record)` for each model
call. Before any call it checks that every survey image can be sent (see
`accept_missing_images`).

### Step 7. Read and save the results

```python
print(results)                           # one line: twins, answers, flagged answers, mode
for answer in results.answers[:3]:
    print(answer.twin_id, answer.question_id, answer.label)
results.flagged()                        # answers that were skipped or out of range
results.usage_totals                     # calls and the token counts the providers reported
results.summary_stats()                  # per-question distributions
results.to_pandas()                      # one row per answer (needs pandas)
```

Each `Answer` has the twin and question, the question text and type, the
answer's `label` and `value` (the option chosen, or the text or number given),
its `code` (the chosen option's code in the values file), `raw` (what the model
wrote, which differs from the label only when it matched no option), and a
`status`: `None` for a usable answer, `"skipped"`, `"out_of_range"`, or
`"not_shown"` when display logic hid the question.

The export files:

```python
results.to_qualtrics_csv("labels.csv")                  # Qualtrics layout, answer labels (rerun repairs included)
results.to_qualtrics_csv("values.csv", values=True)     # coded values (each answer's code, from the options the twin saw)
results.to_qualtrics_csv("repaired.csv", repaired=True) # the labels file again, under its own name
results.to_long_csv("long.csv")                         # one row per twin and answer (each option or matrix row its own)
results.to_wide_csv("wide.csv")
results.to_text("text/")                                # questions/<twin>.txt and answers/<twin>.txt
results.report("report.html")                           # the validation report (format="json" too)

results.save("runs/example")                            # all of the above, plus results.json
again = st.Results.load("runs/example")                 # checks every file against its checksum
```

**Reruns.** `results.rerun_counts()` says how many twins and answers are
flagged; `results.rerun_flagged()` asks those twins again, only for the flagged
questions, showing the rest of the survey with their other answers filled in, and
the options and descriptive text as each twin saw them (with its own piped and
random values). An answer given to text, options or visibility that came from
an answer repaired since ("Why do you like ?" after "Grape" was repaired to
Banana) is marked, not re-asked: `answer.shaped_by_repaired` and the
`shaped_by_repaired` column of `long.csv` name the repaired answers.

**The survey is saved with the run.** `results.save(...)` also writes
`survey.json`, the whole survey definition (its rules and expressions, drops and
edits, never a twin's values), covered by the checksums; `Results.load(...).survey`
gives it back, to check or plan again (it refuses a file that was changed, or that
belongs to another run). Each twin's embedded-data values, in the order they were
set, are in `results.audit[i]["assignments"]`. A run made with `plan.head(n)`
records the plan's full size in `results.run["population"]`: plan that many twins
and take the same head to ask the same twins again.
A repair is stored beside the original answer, never over it, and the repaired
CSV uses it.

## Building a survey in Python

Start an empty survey with `new_survey`, or take one you read (`read_survey`,
`read_qsf`), and change it with the survey's edit methods. Each returns a new
survey and writes [the standard format](#the-standard-survey-format) directly:

```python
survey = st.new_survey("Coffee habits")
survey = survey.add_question("single_choice", "Do you drink coffee?", options=["Yes", "No"], id="drinks", required=True)
survey = survey.add_question("number", "How many cups on a typical day?", id="cups", min=0, max=12,
                             show_if={"op": "selected", "question": "drinks", "option": "1"})
survey = survey.add_question("grid", "How important is each when you buy coffee?", id="importance",
                             rows=["Price", "Taste"], scale="agreement_5")
survey = survey.group_into_block(["cups", "importance"], "habits", title="Habits")
print(survey)
print(survey.standard["flow"])
```

```
Survey('Coffee habits', 3 questions, 2 blocks)
[{'type': 'show_block', 'block': 'main'}, {'type': 'show_block', 'block': 'habits'}]
```

- **Questions:** `add_question(type, text, ...)`, where `type` is
  `instruction`, `single_choice`, `multiple_choice`, `text`, `number`, `grid`,
  `number_grid`, `text_grid`, `sum_grid`, `form`, `ranking`, `allocation` or
  `side_by_side`; then `update_question`, `move_question`, `swap_questions` and
  `remove_question`. A question goes into `block=` at `position=`, or
  `after=`/`before=` another question.
- **Blocks:** `add_block`, `remove_block`, and `group_into_block`, which gathers
  existing questions into a new block.
- **Settings:** any field of the format is a keyword: `required=True`,
  `max_selected=2`, `randomization={"order": "shuffle"}`, `show_if={...}`,
  `skip=[...]`, `image="images/ad.png"`. Options, rows and items are labels, or
  objects in the format.
- **Checks:** every edit is checked as `read_survey` checks a file
  (`SurveyFormatError`, with the path of each field). An edit that would leave
  logic reading a question that is gone, or asked later, raises
  `UnsupportedEdit`. Only what an edit names changes: the rest of the survey,
  a QSF's included, stays as it was.

`survey.standard` is the result in the standard format; save it as JSON and
`read_survey` reads it again. Every method, type and setting is in [Editing a
survey](docs/reference/survey.md#editing-a-survey).

### Drafts from a web form

`surveytwin.builder` makes a survey from a draft, a JSON shape a web form can
send: a title and a list of questions of seven simple types, in one block.

```python
from surveytwin import builder

survey = builder.build_survey({
    "title": "Product evaluation",
    "questions": [
        {"id": "Q1", "type": "grid", "text": "How would you rate this product?",
         "rows": ["Quality", "Appearance"], "options": ["Poor", "Fair", "Good"], "required": True},
        {"id": "Q2", "type": "open_text", "text": "What would you improve?", "max_chars": 500},
    ],
})
survey = builder.add_questions(survey, [
    {"id": "Q3", "type": "single_choice", "text": "Would you recommend it?", "options": ["Yes", "No"]},
], after_question_id="Q2")
```

A mistake in a draft raises `DraftError`, whose `issues` name the question and
field of each problem, so a web form can show them where they belong.
`to_draft` gives the survey back as a draft for the form. The full guide is
[docs/builder.md](docs/builder.md).

## Objects and functions

Everything below is available as `st.<name>` after `import surveytwin as st`.
The tables are a summary; the [full reference](docs/reference/index.md) gives
every parameter, return value and error, with an example for each.

### Functions

| Function | Returns | What it does |
|---|---|---|
| `read_survey(source, *, folder=None)` | `Survey` | Reads a survey written in the standard format (a `.json` path, bytes, or parsed JSON), checked field by field. |
| `read_qsf(source, *, exclude_blocks=None, exclude_questions=None)` | `Survey` | Reads a `.qsf` path, bytes, or parsed JSON. `exclude_blocks` drops blocks (as `drop_blocks`); `exclude_questions` leaves questions out, as if they did not exist (see Step 2). Refused when nothing would be left to answer. |
| `new_survey(title="", *, id=None, description=None, language=None, folder=None)` | `Survey` | An empty survey in the standard format, to fill with `add_question`. |
| `bank(name_or_path="twin-2k-500")` | `Bank` | Opens the default bank or a Croissant Twins folder or zip. |
| `configure(*, sample_size, model, ...)` | `RunConfig` | Checks and collects the run settings (table in step 4). |
| `plan(survey, config, *, mode="auto")` | `Plan` | Samples twins and builds their surveys. `mode="static"` refuses a survey that needs stages; `"staged"` forces stages. |
| `run(plan, *, api_key=None, max_concurrency=8, keep_prompts=False, on_usage=None, on_progress=None, image_checker=None)` | `Results` | Asks every twin. |
| `arun(plan, ...)` | `Results` | `run` for async code. |
| `batch.submit(plan)`, `batch.status(batch_id)`, `batch.collect(batch_id, plan)` | batch id, status, `Results` | The OpenAI Batch API without waiting (static surveys). |
| `Results.load(folder, *, verify=True)` | `Results` | Reopens a saved run. |
| `env("NAME")` | | A key read from an environment variable when a call is made; never stored. |

### The survey

| Object | You get it from | Holds | Methods |
|---|---|---|---|
| `Survey` | `read_survey`, `read_qsf`, `new_survey`, the edit methods, `survey.drop_blocks`, `builder.build_survey` and its edit functions, `Survey.from_dict`, `results.survey` | `name`, `id`, `language`, `kind` (`standard`, `qsf`, `builder` or `template`), `standard` (the definition), `questions` (a dict of `Question` by ID), `flow`, `exclude_blocks`, `drop_report`, `embedded_fields`, `edits`, `source`, `checksum`, `unresolved_media` | `add_question(type, text, ...)`, `update_question(question_id, ...)`, `move_question(question_id, ...)`, `swap_questions(first, second)`, `remove_question(question_id)`, `add_block(block_id, ...)`, `remove_block(block_id)`, `group_into_block(question_ids, block_id, ...)`, `check(network=False, embedded_data=None)`, `preview_blocks()`, `drop_blocks(block_ids)`, `to_dict()` |
| `EmbeddedField` | `survey.embedded_fields` | `name`, `source` (set in the flow, from a panel or the survey link, set by JavaScript, never set), `set_in`, `from_outside`, `set_by_javascript`, `read_by`, `variable_type` | |
| `Question` | `survey.questions[qid]` | `id`, `export_tag`, `type`, `selector`, `text`, `options`, `rows`, `columns`, `display_logic`, `skip_logic`, `validation`, `randomization`, `recode_values`, `media`, `kind` (`answerable`, `descriptive`, `unsupported`, or `timing`, `meta info`, `captcha`) | |
| `SurveyCheck` | `survey.check()` | `mode`, `staging_reasons`, `findings`, `unsupported_questions`, `images`, `design_cells`, `unset_embedded_fields`, `reads_dropped`, `ok` | |
| `Finding` | `check.findings` | `feature`, `level`, `source`, `message` | |
| `BlockPreview` | `survey.preview_blocks()` | `block_id`, `name`, `question_counts`, `answerable`, `images`, `screenout`, `dropped`, `referenced_by`, `findings` | |
| `DropReport` | `survey.drop_report` | `dropped_blocks`, `dropped_questions`, `references` (logic that reads a dropped question), `pruned_branches` | |

### The draft builder (`from surveytwin import builder`)

| Function | Returns | What it does |
|---|---|---|
| `build_survey(draft, *, block_id=None, block_name=None, image_resolver=None, policy=None)` | `Survey` | A survey from a draft (a `SurveyDraft` or its JSON), in block `BL_BUILDER` unless the draft or `block_id` names another; a whole survey in the standard format is read as `read_survey` reads it. |
| `add_questions(survey, questions, *, block_id=None, after_question_id=None, before_question_id=None, image_resolver=None, policy=None)` | `Survey` | Inserts draft questions into a block. |
| `update_question(survey, question_id, changes=None, **fields)` | `Survey` | Changes one question's fields. |
| `remove_questions(survey, question_ids)` | `Survey` | Removes questions; refused while logic reads them. |
| `reorder_questions(survey, order=None, *, swap=None, move=None, block_id=None)` | `Survey` | A new order, a swap of two questions or runs of questions, or a move up or down. |
| `to_draft(survey)` | `SurveyDraft` | A built survey as it is now, back as a draft for a web form; each question keeps its internal ID (`qid`), and the draft keeps the survey's next internal number, block and metadata. |
| `apply_draft(survey, draft, *, image_resolver=None, policy=None)` | `Survey` | What a web form sends back, applied to the survey it came from; each question keeps its internal ID even when the form dropped `qid`. |
| `validate_draft(draft, *, policy=None)` | list of `DraftIssue` | Every problem, placed by question and field. |
| `planned_qids(draft)` | list of text | The internal ID each question will get when the draft is built. |
| `assign_ids(draft)` | `SurveyDraft` | Fills blank question IDs. |
| `default_image_resolver(reference)` | text or None | Image URLs, data URIs and local image files. |

| Object | Holds | Methods |
|---|---|---|
| `SurveyDraft` | `title`, `questions`, `notes` (remarks attached to the draft) | `from_dict(payload)`, `to_dict()`, `copy()` |
| `DraftQuestion` | the 18 draft fields (`id`, `type`, `text`, `required`, `options`, `rows`, ...) | `from_dict(payload)`, `to_dict()` |
| `DraftIssue` | `severity`, `message`, `code`, `question_id`, `question_index`, `field`, `item_index` | `to_dict()` |
| `BuilderPolicy` | `max_questions`, `require_title`, `warn_on_demographics`; `WEBSITE_POLICY` holds a 40-question cap, a required title and a demographic warning | |

### The personas

| Object | You get it from | Holds | Methods |
|---|---|---|---|
| `Bank` | `bank(...)` | `id`, `name`, `version`, `size`, `fingerprint`, `default_representation`, `description`, `warnings` | `representations()`, `filter_options()`, `preview_filter(filters, representation=None, sample_size=None)`, `sample(...)`, `twin(persona_id, representation)` |
| `Representation` | `bank.representations()` | `id`, `label`, `kind`, `description`, `is_default`, `coverage` | |
| `FilterOptions` | `bank.filter_options()` | `fields`: each with `name`, `label`, `type`, `values` or `min`/`max` | `names()` |
| `AudiencePreview` | `bank.preview_filter(...)` | `matching`, `total`, `share`, `by_value`, `covers_sample_size` | |
| `Twin` | `plan.twins`, `bank.twin(...)` | `id`, `attributes`, `representation`, `text` (the profile as the model sees it), `source` | |

### The run

| Object | You get it from | Holds | Methods |
|---|---|---|---|
| `RunConfig` | `configure(...)` | every setting in the table in step 4 | `to_record()` (the settings as saved with results) |
| `Plan` | `plan(...)` | `survey`, `config`, `mode`, `twins`, `warnings`, `population`, `loops` (each Loop & Merge block's rows and questions per row) | `preview_prompt(twin_id=None)`, `prompts(twin_id=None)`, `head(n)`, `to_batch_file(path)` |
| `PromptPreview` | `plan.preview_prompt()` | `twin_id`, `text`, `messages`, `images`, `input_tokens` (rough: characters / 4), `max_output_tokens`, `kind` | |

### Models

| Object | You get it from | Holds | Methods |
|---|---|---|---|
| `Model` | `Model.preset`, `Model.http`, `Model.openai_compatible`, `Model.from_function` | `name`, `kind`, `layout`, `images`, `settings` (what is saved with results; never keys) | `check()` |
| `ModelRequest` | passed to a function model | `system`, `user`, `content` (text and images in order), `images`, `max_output_tokens`, `json_mode`, `temperature` | |
| `Reply` | returned by a function model | `text`, `input_tokens`, `output_tokens`, `cut_off` | |
| `ModelCheck` | `model.check()` | `ok`, `text_found`, `usage_found`, `stop_reason_found`, `latency_s`, `response` | |

### The results

| Object | You get it from | Holds | Methods |
|---|---|---|---|
| `Results` | `run(...)`, `Results.load(...)` | `answers`, `twins` (status and usage of each), `audit` (each twin's path through the survey and its embedded-data values in order), `usage` (one record per model call), `warnings`, `provenance`, `survey` | `flagged()`, `usage_totals`, `summary_stats()`, `to_pandas()`, `to_qualtrics_csv(...)`, `to_long_csv(...)`, `to_wide_csv(...)`, `to_text(...)`, `report(...)`, `save(...)`, `rerun_counts()`, `rerun_flagged(...)` |
| `Answer` | `results.answers`, `results.flagged()` | `twin_id`, `question_id`, `question_text`, `question_type`, `raw`, `label`, `value`, `code` (the chosen option's code in the values file), `status`, `repair`, `shaped_by_repaired`, `adjustments`, `embedded_data` | |

### Errors

Every error the library raises on purpose is a `st.SurveytwinError`, with one
of these types:

| Error | When |
|---|---|
| `QSFReadError` | The file is not a readable QSF. |
| `SurveyError` | An unknown block ID, or nothing left to answer. |
| `BankError` | A bank cannot be read or fails the Croissant Twins checks; the default bank is not available. |
| `FilterError` | A filter names an unknown field or value. |
| `PersonaError` | No persona matches, or a persona is blank. |
| `ConfigError` | An invalid setting or model definition, or no key for a preset model. |
| `StaticModeRefused` | `mode="static"` for a survey that needs stages. |
| `BatchNotAvailable` | Batch for a staged survey or a non-OpenAI model. |
| `ContextBudgetError` | A persona is too large for one prompt; nothing is cut to fit. |
| `ImagesRefused` | Survey images cannot be sent and `accept_missing_images` is off. |
| `ProviderError` | A model check found no usable reply. During a run, a failed call is recorded as skipped answers instead. |
| `ResultsError` | A saved run folder is missing files or fails its checksums, or a rerun has no key. |
| `DraftError` | A survey draft or a builder edit has errors; `issues` places each one. |
| `UnsupportedEdit` | A builder edit would lose survey content or break the survey's logic; nothing changed. |

## Models

Every model is a `Model` object that knows how to call its API. A run sends each
model the same request (`ModelRequest`: system text, user text, the content in
order with its pictures, the output limit, JSON mode) and reads the same reply
(`Reply`: the text, the token counts, whether the reply was cut off, the
provider's request ID). The reply text holds the answers in one fixed format,
`{"answers": [{"question_id", "answer_value", "answer_label", "answer_raw"}]}`,
which the prompt asks for and the library reads.

**Built-in models** already map their API to that request and reply:

```python
gpt = st.Model.openai("gpt-5.4", reasoning_effort="low")       # OPENAI_API_KEY
routed = st.Model.openrouter("gpt-5.4-low")                    # OPENROUTER_API_KEY
claude = st.Model.anthropic("claude-opus-5-5", effort="low")   # ANTHROPIC_API_KEY
gemini = st.Model.gemini("gemini-2.5-pro")                     # GEMINI_API_KEY
deepseek = st.Model.deepseek("deepseek-chat")                  # DEEPSEEK_API_KEY

config = st.configure(..., model=claude)
config = st.configure(..., model="gpt-5.6-luna-medium")        # a preset name: the same as Model.openai(name)
```

OpenAI models are called this way: the Responses API for GPT-5
models (with a reasoning allowance) and for any call with images, chat
completions in JSON mode otherwise. A preset name such as `gpt-5.4-low` fixes the
API model and its reasoning effort. A failed OpenAI call goes to OpenRouter only
with `Model.openai(..., fallback_to_openrouter=True)`, and the run then says how
many calls did. Current Claude models think before answering and take no
temperature, so none is sent.

**Models of your own** send the same content to any endpoint in one of four
request layouts (`openai-chat`, `openai-responses`, `anthropic`, `gemini`), or
are a Python function:

```python
gateway = st.Model.http(
    "my-gateway",
    "https://gateway.example/v1/chat/completions",
    layout="openai-chat",
    headers={"Authorization": st.env("GATEWAY_KEY")},
    body={"model": "some-model"},
)
local = st.Model.openai_compatible("llama", "http://localhost:8000/v1", api_key_env="LOCAL_KEY")
mine = st.Model.from_function(lambda request: my_model(request.system, request.user), name="mine")

print(claude.check())                    # one tiny call: were the reply, token counts and stop reason found?
print(claude.ask("Say hello.").text)     # ask any model directly, outside a run
```

`body` fields are added to the layout's request (`None` removes a field). For an
unusual reply, `text_key`, `usage_keys` and `stop_key` (with `cut_off_values`)
point at the right fields by dotted path, such as `"content.0.text"`. A reply
cut off by the output limit is asked again once with twice the room. A function
model receives a `ModelRequest` and returns a string or a `Reply`.

### API keys

- **Set the key in your environment** before you run, in the variable the model
  reads: for example `export OPENAI_API_KEY=...` in the terminal, or
  `os.environ["OPENAI_API_KEY"] = ...` in Python. Each built-in model has its
  own variable (above); `api_key_env="MY_VARIABLE"` names another.
- **Or pass it to the run:** `st.run(plan, api_key=...)` (and `rerun_flagged`,
  `model.check`, `model.ask`) replaces the model's own key for that call.
- **A model of your own** puts `st.env("NAME")` where its API wants the key
  (`headers={"x-api-key": st.env("MY_KEY")}`); the variable is read when each
  call is made.
- Keys are never written into results, saved runs or logs: settings show the
  variable's name only, and a request field named like a secret (`api_key`,
  `token`, ...) shows as `<hidden>`. The library does not read `.env` files;
  load one yourself (for example with `python-dotenv`) if you keep keys there.
- A run stops before its first call when a model's key variable is not set.

The library has no prices and computes no cost. Each call's token counts, as the
provider reports them, are kept in `results.usage`.

## Persona banks

Personas come only from banks in the **Croissant Twins** format: a folder or zip
whose `croissant.json` names the Croissant Twins 0.1 profile in `conformsTo`.
A bank is checked when opened, read into a local cache (`~/.cache/surveytwin`,
or `SURVEYTWIN_CACHE_DIR`), and opened again instantly while its files are
unchanged.

**The default bank** is Twin-2K-500 in the Croissant Twins format
(`twin-2k-500-ct`). Until it is published for download, point the library at a
copy:

```bash
export SURVEYTWIN_TWIN2K_BANK=/path/to/twin-2k-500-ct      # a folder or zip
export SURVEYTWIN_TWIN2K_HF_REPO=owner/name                # or a Hugging Face dataset repository holding it
```

**The example bank** in `examples/example-bank` has eight invented personas for
trying the library. They are not real people and not a sample of anything.

## Command line

Installing the package also installs the `surveytwin` command. It uses the same
functions, with no extra dependencies.

```bash
surveytwin check study.qsf
surveytwin prompts study.qsf --bank examples/example-bank --twins 3 --seed 7
surveytwin prompts study.qsf --bank examples/example-bank --twins 500 --show 3   # the first 3 of a 500-twin run
surveytwin model-check gpt-5.6-luna-medium
surveytwin run study.qsf --twins 500 --seed 7 --model gpt-5.6-luna-medium --out runs/study
surveytwin rerun runs/study
surveytwin export runs/study --bundle
surveytwin report runs/study
surveytwin batch submit study.qsf --twins 500 --model gpt-5.6-luna-medium --out runs/study
surveytwin batch status --out runs/study
surveytwin batch collect --out runs/study
```

Wherever a command takes a `.qsf` file, it also takes a survey draft saved as
`.json` (`title` and `questions`).

Commands that need personas take `--bank`, `--representation` and
`--filter FIELD=VALUE[,VALUE]` (also `FIELD=LOW..HIGH` and `FIELD=:missing`).
`--drop BLOCK_ID` drops a block (repeatable). `check`, `prompts`, `run` and `batch`
take `--embedded-data FIELD=VALUE` (repeatable) for fields the study filled from outside.

`--model` takes a preset name, or `FILE.py:NAME` for a model defined in a Python
file (`NAME` can also be a function that takes no arguments and returns the
model):

```python
# mymodels.py
import surveytwin as st

claude = st.Model.http(
    "claude-sonnet",
    "https://api.anthropic.com/v1/messages",
    layout="anthropic",
    headers={"x-api-key": st.env("ANTHROPIC_API_KEY"), "anthropic-version": "2023-06-01"},
    body={"model": "claude-sonnet-5"},
)
```

```bash
surveytwin model-check mymodels.py:claude
surveytwin run study.qsf --twins 50 --model mymodels.py:claude --out runs/claude
surveytwin rerun runs/claude --model mymodels.py:claude
```

The file is run as Python, like any script you run yourself. The results record
where the model came from (the file name, the name in it, and a fingerprint of
the file). A rerun of such a run needs `--model` again. The batch commands take
OpenAI preset names only.

## Reproducibility

The seed picks the twins and every random choice in the survey, so the same
survey, bank, seed and settings give the same twins and the same prompts, on any
machine. A twin's random numbers are the same in static and staged runs. Model replies can still vary from call to call. `results.provenance`
records the library and prompt versions, the survey's checksum, the bank's
fingerprint, the model settings, the seed, and any images that were left out.

## License

Apache License 2.0. See [LICENSE](LICENSE).
