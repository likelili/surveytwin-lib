# Errors

Every error the library raises on purpose is a `SurveytwinError`, so one
`except` clause catches them all. Each subclass names a kind of problem, and each
message names the survey element, twin or file involved. Most of them stop you
before any model call: reading the survey, opening the bank, `configure` and
`plan` check everything they can. The command line prints such an error as
`error: <message>` and exits with status 2.

## How the classes relate

```
Exception
└── SurveytwinError
    ├── QSFReadError
    ├── SurveyError
    │   ├── DraftError
    │   ├── SurveyFormatError
    │   └── UnsupportedEdit
    ├── BankError
    ├── PersonaError
    ├── FilterError
    ├── ConfigError
    ├── StaticModeRefused
    ├── BatchNotAvailable
    ├── ContextBudgetError
    ├── ProviderError
    ├── ImagesRefused
    └── ResultsError
```

`DraftError`, `SurveyFormatError` and `UnsupportedEdit` are kinds of `SurveyError`, so
`except st.SurveyError` catches them too. When you catch a class and its parent,
put the class first.

## Catching errors

```python
import surveytwin as st


def read_survey(path, blocks_to_drop):
    try:
        return st.read_qsf(path, exclude_blocks=blocks_to_drop)
    except st.DraftError as exc:          # a kind of SurveyError, so it comes first
        for issue in exc.issues:
            print("cannot drop these blocks:", issue.code)
    except st.SurveytwinError as exc:     # anything else the library refuses
        print(f"{type(exc).__name__}: {exc}")
    return None


read_survey("examples/missing.qsf", [])
read_survey("examples/example_survey.qsf", ["BL_CONSENT"])
survey = st.read_qsf("examples/example_survey.qsf")
read_survey("examples/example_survey.qsf", [block["block_id"] for block in survey.blocks])
```

```
QSFReadError: examples/missing.qsf: no such file
SurveyError: Unknown block ID(s): BL_CONSENT. Known: BL_5mvGQoBt7GcmAFo, BL_8jFpZfwsRRTsiOi, BL_KS_FB_12, BL_KS_FB_13, BL_KS_FB_14, BL_KS_MC_1, BL_KS_MX_6, BL_KS_RO_8, BL_KS_SBS_9, BL_KS_SL_7, BL_KS_TE_4, BL_KS_TG_10
cannot drop these blocks: survey.no_answerable
```

## `SurveytwinError`

**Parent:** `Exception`.

The base class of every error on this page. Catch it to handle anything the
library refuses. The library raises it directly only on the command line: for
example, when no survey file is given, or when `surveytwin batch status` or
`surveytwin batch collect` finds no batch in the folder.

## `QSFReadError`

**Parent:** `SurveytwinError`.

The survey file cannot be read as a QSF. Raised by:

- `read_qsf`: there is no such file; the file is not JSON (or not UTF-8); its
  top level is not a JSON object; it has no `SurveyElements`, so it is not a
  Qualtrics export; it has no survey flow (`FL` element); or the reader fails on
  its content (the message gives the reason).
- The command line, for a survey draft (`.json`) that cannot be read.

## `SurveyError`

**Parent:** `SurveytwinError`. Its subclasses are `DraftError`,
`SurveyFormatError` and `UnsupportedEdit`.

A problem with the survey, or with how a function is asked to use it. Raised by:

- `read_qsf`: a name in `exclude_questions` matches no question, or is the
  export tag of more than one question; a block in `exclude_blocks` does not
  exist.
- `Survey.drop_blocks`: the survey has no block with that ID. The message lists
  the IDs it has.
- `Survey(...)`: the template is not a parsed survey template (or is a QSF).
- `Survey.from_dict`: the data is not a saved survey, its QSF cannot be read, or
  it no longer matches its checksum (it was changed after saving).
- `plan`: `mode` is not `"auto"`, `"static"` or `"staged"`; or the survey leaves
  nothing for a twin to answer.
- `Plan.prompts` and `Plan.preview_prompt`: the twin is not in the plan, or a
  stage other than 0 is asked for in a staged survey (later stages depend on the
  answers).
- The builder: `build_survey` with a blank `block_id`; `to_draft` when every
  block is dropped; `add_questions` with an unknown block, without `block_id` on
  a survey with several blocks, with both `after_question_id` and
  `before_question_id`, or with an anchor question in another block; an edit to
  a question in a dropped block; `reorder_questions` without exactly one of
  `order`, `swap` and `move`, or with an order, swap or move that does not fit
  one block (questions missing or repeated, overlapping runs, a move past the
  first or last place).

## `SurveyFormatError`

**Parent:** `SurveyError`.

A survey in [the standard format](survey-format.md) has errors; raised by
`read_survey`, and by the [edit methods](survey.md#editing-a-survey) (`Survey.add_question`
and the others) when an edit would leave an error in the definition, name a
picture file that cannot be read, or give two CSV columns the same name (code
`export.column_clash`); only the errors the edit makes are listed. `findings` lists every problem, warnings included, each with
`path` (the field: `$.questions[2].response.options[1].label`), `message`, `code`
(listed in [How a survey is checked](survey-format.md#how-a-survey-is-checked))
and `level` (`"error"` or `"warning"`). The message shows the first five errors.

```python
import surveytwin as st

try:
    st.read_survey({"schema_version": "0.1", "id": "s", "questions": [], "blocks": [],
                    "flow": [{"type": "show_block", "block": "missing"}]})
except st.SurveyFormatError as error:
    for finding in error.findings:
        print(finding.level, finding.code, finding.path)
```

```
error reference.block $.flow[0].block
```

## `DraftError`

**Parent:** `SurveyError`.

A survey draft or a builder edit has errors. `issues` lists every one, each tied
to a question and a field, so a web form can show it where it belongs.

```python
surveytwin.DraftError(issues: list, message: str | None = None)
```

**Parameters**

- `issues` (`list`): the problems, as `builder.DraftIssue` records. Kept as the
  `issues` attribute.
- `message` (`str | None`, default `None`): the error's text. `None` builds it
  from the issues whose severity is `error`: how many there are, then the first
  five, as in `1 problem: Q1 options: Choice questions need at least 2 options.`

**Attributes**

- `issues` (`list[DraftIssue]`): every issue found. Each has `severity`,
  `message`, `code` (a stable name for the rule), `question_id` and
  `question_index` (which question; blank and -1 for the whole survey), `field`
  and `item_index`. The list can also hold warnings, which do not count as
  errors.

Raised by:

- `builder.build_survey`: any error in the draft (see `builder.validate_draft`),
  including a payload of the wrong shape (codes `draft.invalid` and
  `question.invalid`) and a CSV column name that would appear twice
  (`id.column_clash`).
- `builder.add_questions`, `builder.update_question`,
  `builder.remove_questions` and `builder.apply_draft`: the same rules for what
  they add or change; a question name that matches nothing (`id.unknown`), more
  than one question (`id.ambiguous`) or a question excluded when the survey was
  read (`id.excluded`); nothing given to add, change or remove; an unknown field
  (`field.unknown`); piped text that reads a question that is missing, dropped,
  later in the survey, or the question itself; an internal ID (`qid`) that is
  changed (`qid.fixed`), carried over from another survey (`qid.not_allowed`),
  retired (`qid.retired`) or never given by the survey (`qid.unknown`).
- `Survey.drop_blocks`, `read_qsf`, and the builder's edits: the change would
  leave nothing for a twin to answer (`survey.no_answerable`).

**Example**

```python
import surveytwin as st
from surveytwin import builder

try:
    builder.build_survey({"title": "Pets", "questions": [
        {"id": "Q1", "type": "single_choice", "text": "Cat or dog?", "options": ["Cat"]},
        {"id": "Q2", "type": "open_text", "text": "", "hint": "short"},
    ]})
except st.DraftError as exc:
    print(exc)
    for issue in exc.issues:
        print(issue.severity, issue.code, issue.question_id, issue.field)
```

```
2 problems: Q1 options: Choice questions need at least 2 options.; Q2 text: Question text is empty.
error options.too_few Q1 options
warning field.unknown Q2 hint
error text.empty Q2 text
```

## `UnsupportedEdit`

**Parent:** `SurveyError`.

A builder edit that would lose survey content or break the survey's logic.
Nothing was changed. Raised by:

- The [edit methods](survey.md#editing-a-survey) of `Survey`: an edit that would
  leave a condition, a skip rule, piped text, a value set in the flow or a
  repeat's rows reading a question that is gone (`remove_question`,
  `remove_block`) or asked only after it (`add_question`, `move_question`,
  `swap_questions`, `group_into_block`); `update_question` giving a question
  that other parts read a new kind of answer, or a new type to a question with
  skip rules when `skip` is not given; `group_into_block` taking a question out
  of a block that repeats. The message lists each place.

- `builder.to_draft`, and `builder.apply_draft`, which starts from it: the
  survey has no draft form without losing something, such as questions read
  from a QSF, more than one block, a block shown twice, branches, randomizers,
  embedded data or other flow elements, a loop, randomized question order, or a
  question changed outside the builder.
- `builder.update_question` on a question read from a QSF: a type change, a
  rationale, or another field the builder cannot change there; renaming a
  side-by-side question; options or rows on a question with recode values, its
  own choice IDs, write-in options, option images, default answers or a custom
  randomization order; a new number of options while logic reads the question;
  length limits on a text question that checks its answer's format; a slider
  range that would leave a default answer outside it; the image of a question
  that shows more than one.
- `builder.update_question` on a question the builder made that was changed
  outside the builder, so it no longer matches its draft.
- `builder.remove_questions`: logic elsewhere still reads a question to remove.
  The message names every place.
- `builder.reorder_questions`: the new order would make display logic or piped
  text read a later question, or skip logic jump backwards.

[The builder guide](../builder.md#editing-a-survey) lists what an edit may
change on an imported survey.

## `BankError`

**Parent:** `SurveytwinError`.

A persona bank cannot be read, or fails the Croissant Twins checks. Raised by:

- `bank`: no such file or folder; neither a folder nor a `.zip` file; no
  `croissant.json` (not a Croissant Twins bank), or more than one; a manifest
  that cannot be read or does not name the Croissant Twins 0.1 profile; failed
  Croissant Twins checks (each listed with its code); a zip entry with an unsafe
  path; a bank that cannot be read into the local cache; and, for the default
  bank, no copy available (set `SURVEYTWIN_TWIN2K_BANK` to a local copy, or pass
  a folder to `bank`).
- `configure`, `Bank.resolve_representation` and `Bank.preview_filter`: the
  bank has no representation by that name, has several and no default, or has
  none.

## `PersonaError`

**Parent:** `SurveytwinError`.

A twin cannot be used. Raised by:

- `plan`: no persona matches the filters and the representation; or a sampled
  twin's persona text is blank for the chosen representation, so it would run
  with no persona. The message names those twins.

## `FilterError`

**Parent:** `SurveytwinError`.

A filter names an unknown field or value. Raised by:

- `configure`, `Bank.preview_filter`, `Bank.sample` and
  `Bank.normalize_filters`: the filters are not a dict; a field the bank cannot
  filter on (the message suggests close names); a range on a field that is not
  numeric; a dict filter with keys other than `min`, `max` or `missing`; a value
  the field does not have, by label or by code.
- The command line, for a `--filter` that is not `FIELD=VALUE[,VALUE]`, or a
  range without numbers.

## `ConfigError`

**Parent:** `SurveytwinError`.

A run setting or a model definition is not valid, or a key is missing. Raised
by:

- `configure`: `sample_size` is not a whole number of at least 1; `seed` is not
  a whole number of 0 or more; `max_stages` is below 1; `model` is neither a
  preset name nor a `Model`; `provider` is not `"openai"` or `"openrouter"` for
  a preset name; `batch=True` without an OpenAI preset model.
- `Model.preset`, `Model.http`, `Model.openai_compatible` and
  `Model.from_function`: an unknown provider or request layout, an address that
  is not `http` or `https`, `stop_key` without `cut_off_values`, or a function
  that cannot be called.
- `run` and `arun`: a preset model and no key (no `api_key=`, and no
  `OPENAI_API_KEY`, or `OPENROUTER_API_KEY` for `provider="openrouter"`); a
  defined model whose `env(...)` variables are not all set. `Results.rerun_flagged`
  makes the same check for a defined model.
- `batch.submit`, `batch.status` and `batch.collect`: no OpenAI key.
- The command line: a `--model FILE.py:NAME` that cannot be loaded or does not
  give a model; `--provider` with a model from a file; a
  `--system-instruction` file that cannot be read.

A defined model reads its `env(...)` variables when it is called; `run`,
`arun` and `Results.rerun_flagged` check them first and stop before any call. In
`Model.check` an unset variable is reported as a `ProviderError` (see
[Where the key comes from](running.md#where-the-key-comes-from)).

## `StaticModeRefused`

**Parent:** `SurveytwinError`.

Static mode was asked for a survey that needs stages. Raised by:

- `plan(survey, config, mode="static")` (and `--mode static` on the command
  line) when later content in the survey depends on answers. Use
  `mode="auto"` or `mode="staged"`.

## `BatchNotAvailable`

**Parent:** `SurveytwinError`.

The Batch API was asked for where it cannot work. Raised by:

- `plan`: `configure(batch=True)` and a staged survey (each stage needs the
  answers to the one before).
- `Plan.to_batch_file`, `batch.submit` and `batch.collect`: the plan is staged,
  or its model is not an OpenAI preset model.
- `batch.collect`: the batch has not completed yet. The message gives its
  status.

## `ContextBudgetError`

**Parent:** `SurveytwinError`.

A prompt would be too long for the model; nothing is cut to fit. Raised by:

- `plan`: the longest persona text of the chosen representation is too large
  for one prompt (more than about 100,000 tokens, counted as characters
  divided by 4). The message names smaller representations to choose instead.

## `ProviderError`

**Parent:** `SurveytwinError`.

A model check failed. Raised by:

- `Model.check` and `Model.acheck`: the test call failed, for example a network
  error, an error reply from the endpoint, an unset `env(...)` variable, or a
  function model that raised. The message starts with
  `Model check failed for <name>:`.

During a run a failed model call does not raise. After its retries, the answers
it should have given are recorded as skipped, a twin with no usable answer is
marked `failed`, and `results.warnings` gives the error; `rerun_flagged` can ask
again (see [Calls that fail](running.md#calls-that-fail)).

## `ImagesRefused`

**Parent:** `SurveytwinError`.

The survey shows images that cannot be sent. Raised by `run`, `arun`,
`batch.submit`, `batch.collect` and `Plan.to_batch_file`, before any model call,
unless you pass `configure(accept_missing_images=True)`:

- a builder image reference that the image resolver returned nothing for;
- the survey shows images and the model takes no image input;
- an image link that cannot be loaded or needs a Qualtrics login. These
  functions test the links themselves.

With `accept_missing_images=True`, those images are left out and listed in the
results. A question that shows one is not asked and is recorded as skipped; an
image in descriptive text is only left out, and the questions after it are
asked.

## `ResultsError`

**Parent:** `SurveytwinError`.

A saved run cannot be used as asked. Raised by:

- `Results.load`: the folder has no `results.json` (it is not a saved run); a
  file listed in `checksums.json` is missing or does not match its checksum
  (with `verify=True`, the default); `results.json` is not a results file.
- `Results.survey`: the run was saved without its survey (`survey.json`), or
  that file cannot be read or belongs to another run.
- `Results.to_pandas`: pandas is not installed.
- `Results.to_qualtrics_csv`: `values=True` and `repaired=True` together; or,
  like `to_long_csv` and `to_wide_csv`, the file cannot be made for this run.
- `Results.report`: `format` is not `"html"` or `"json"`, or the report could
  not be built (see `results.warnings`).
- `Results.rerun_flagged`: the run used a defined model and none is given
  (a saved run cannot restore one), or a preset model has no key.

## Other exceptions

A few mistakes surface as Python's own exceptions instead:

- `run` called where an event loop is already running (inside async code)
  raises `RuntimeError`; use `await surveytwin.arun(plan)` there. Functions that
  start their own event loop (`Model.check`, `batch.submit`, `batch.status`,
  `batch.collect`, `Results.rerun_flagged`) raise `RuntimeError` in the same
  place; for a model check, use `await model.acheck()`.
- `builder.SurveyDraft.from_dict` and `builder.DraftQuestion.from_dict` raise
  `TypeError` for a payload that is not a dict. `build_survey` reports the
  same mistake as a `DraftError`.

See also: [Reading and checking a survey](survey.md),
[The survey builder](builder.md), [Persona banks](personas.md),
[Configuring and planning a run](configure-and-plan.md),
[Running a plan](running.md), [Results and export files](results.md).
