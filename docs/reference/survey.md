# Reading and checking a survey

A run starts with a survey. `read_survey` reads a survey written in [the
standard format](survey-format.md); `read_qsf` reads a Qualtrics export (a `.qsf`
file) into that format; `new_survey` starts an empty one; [the draft
builder](builder.md) makes the same object from a web form's draft.
`Survey.add_question` and the other [edit methods](#editing-a-survey) change a
survey's definition: add, change, move and remove questions and blocks.
Before you plan a run, the survey tells you how it will run and what the library
does with each Qualtrics feature it uses (`Survey.check`), lists its blocks
(`Survey.preview_blocks`), and leaves out blocks that twins should not answer
(`Survey.drop_blocks`). `plan` takes the survey next.

## `read_qsf`

```python
surveytwin.read_qsf(
    source: str | Path | bytes | dict[str, Any],
    *,
    exclude_blocks: list[str] | None = None,
    exclude_questions: list[str] | None = None,
) -> Survey
```

Reads a QSF into a `Survey`. Reading never touches the network.

**Parameters**

- `source` (`str | Path | bytes | dict[str, Any]`): the QSF. Give the path of the
  file, its bytes, the parsed JSON, or the JSON as text (a string that starts
  with `{`; any other string is a path). Text is read as UTF-8, with or without a
  byte order mark.
- `exclude_blocks` (`list[str] | None`, default `None`): the IDs of blocks to
  drop, exactly as `Survey.drop_blocks` drops them. `None` drops nothing.
- `exclude_questions` (`list[str] | None`, default `None`): questions to leave
  out, as if the file did not have them. Name each by its internal ID (`"QID51"`)
  or by its export tag (`"KS_RO_Q22"`) when exactly one question has that tag. A
  question the reader cannot read can be left out this way, and the rest of the
  file is read. `None` leaves nothing out.

[Dropped blocks and excluded questions](#dropped-blocks-and-excluded-questions)
says what both do.

**Returns** `Survey`: the survey, with `kind` set to `"qsf"`. The QSF is read
into [the standard format](survey-format.md) (`Survey.standard`), and the
survey runs from what that form compiles to: on every test and study QSF of the
library, exactly what the QSF reader gave before.

**Raises**

- `QSFReadError`: there is no such file, the file is not JSON, its top level is
  not a JSON object, it has no `SurveyElements` or no survey flow (`FL` element),
  or the reader fails on it.
- `SurveyError`: a name in `exclude_questions` matches no question, or is the
  export tag of more than one question; or a block in `exclude_blocks` does not
  exist (the message lists the block IDs the survey has).
- `DraftError`, with the issue code `survey.no_answerable`: what is left out
  would leave nothing for a twin to answer. A survey that had nothing to answer
  to begin with is not refused here; `Survey.check` reports it and `plan`
  refuses it.

**Example**

```python
import surveytwin as st

survey = st.read_qsf("examples/example_survey.qsf")
print(survey)

trimmed = st.read_qsf(
    "examples/example_survey.qsf",
    exclude_blocks=["BL_KS_MC_1"],
    exclude_questions=["KS_RO_Q22"],    # an export tag; "QID51" works too
)
print(trimmed)
print(trimmed.drop_report)
```

```
Survey('Untitled project', 36 questions, 12 blocks)
Survey('Untitled project', 24 questions, 11 blocks)
Dropped blocks: BL_KS_MC_1
Dropped questions (11): QID1, QID10, QID11, QID12, QID2, QID4, QID56, QID6, QID62, QID7, QID9
Excluded when read (1): QID51
Logic that reads a dropped question (it counts as never shown, with no answer):
  branch at FL FL_14 reads QID56
  piping at SQ QID48 reads QID56
  branch at FL FL_29 reads QID56
  piping at SQ QID57 reads QID56
  branch at FL FL_32 reads QID56
  piping at SQ QID58 reads QID56
survey.check() lists the logic that reads an excluded question.
```

## `read_survey`

```python
surveytwin.read_survey(source: str | Path | bytes | dict[str, Any], *, folder: str | Path | None = None) -> Survey
```

Reads a survey written in [the standard format](survey-format.md). The whole
definition is checked first, its shape and then its meaning (references,
limits, unique IDs and labels); nothing runs until it passes. Reading never
touches the network.

A picture's `source` may be the path of a picture file on this computer. Each
file is read now: an absolute path as written, a relative path from the folder
of the JSON file (or from `folder`). The survey keeps the paths as written
(`survey.standard`); a run sends each picture inside its request, and a saved run
keeps one copy of each, so it does not need the files again.

**Parameters**

- `source` (`str | Path | bytes | dict[str, Any]`): the survey. Give the path of
  a `.json` file, its bytes, the parsed JSON, or the JSON as text (a string that
  starts with `{`; any other string is a path). The value is never changed.
- `folder` (`str | Path | None`, default `None`): where a relative picture path
  starts. `None` means the folder of the JSON file when `source` is a path, and
  the current folder otherwise.

**Returns** `Survey`: the survey, with `kind` set to `"standard"`. Warnings (a
variable read but never set, a block the flow never shows) are in
`read_warnings`.

**Raises**

- `SurveyFormatError`: the definition has errors; `findings` lists every one with
  the path of its field. A picture file that is missing, is not a PNG, JPEG,
  WEBP or GIF picture, or is larger than 20 MB is one (code `picture.file`).
- `SurveyError`: the file does not exist or is not JSON, or it is a Qualtrics
  export (read it with `read_qsf`).
- `DraftError` (`survey.no_answerable`): the survey has no question a twin can
  answer.

**Example**

```python
import surveytwin as st

survey = st.read_survey("examples/survey-format/coffee-habits.json")
print(survey, survey.kind)
print([b["block_id"] for b in survey.blocks])
```

```
Survey('Coffee habits', 14 questions, 7 blocks) standard
['screen', 'habits', 'message_a_block', 'message_b_block', 'reaction', 'drinks', 'goodbye']
```

## `new_survey`

```python
surveytwin.new_survey(
    title: str = "",
    *,
    id: str | None = None,
    description: str | None = None,
    language: str | None = None,
    folder: str | Path | None = None,
) -> Survey
```

An empty survey in the standard format, to fill with `Survey.add_question` and
the other [edit methods](#editing-a-survey). It has no question yet, so `plan`
refuses it until one is added.

**Parameters**

- `title` (`str`, default `""`): the survey's title (its `name`).
- `id` (`str | None`, default `None`): the survey's ID in the format; `None`
  makes one from the title (`"Coffee habits"` gives `coffee_habits`), or
  `survey`.
- `description` (`str | None`, default `None`): a description, kept in the
  definition.
- `language` (`str | None`, default `None`): a language code, such as `"EN"`.
- `folder` (`str | Path | None`, default `None`): where a relative picture path
  that an edit names starts; `None` is the current folder.

**Returns** `Survey`, with `kind` `"standard"`.

**Raises** `SurveyFormatError`: the ID has a space in it.

**Example**

```python
import surveytwin as st

survey = st.new_survey("Coffee habits")
print(survey, survey.kind)
print(survey.standard)
```

```
Survey('Coffee habits', 0 questions, 0 blocks) standard
{'schema_version': '0.1', 'id': 'coffee_habits', 'title': 'Coffee habits', 'questions': [], 'blocks': [], 'flow': []}
```

## `Survey`

One survey: its definition in the standard format, which planning, prompts and
exports run on, and views of its flow, blocks and questions in Qualtrics' terms.
You get one from `read_survey`, `read_qsf`, `new_survey`, the edit methods
(`Survey.add_question` and the others), the draft builder
(`builder.build_survey` and its edit functions), `Survey.drop_blocks`,
`Survey.from_dict`, or a saved run (`results.survey`).

A survey never changes. The edit methods, `drop_blocks` and the draft builder's
edit functions return a new survey; the survey you started from, and any plan
made from it, stay as they were. The dicts and lists that the properties return are for reading:
changing them is not a way to edit a survey.

Printing a survey shows its name and how many questions and blocks it has now,
for example `Survey('Untitled project', 36 questions, 12 blocks)`.

### Attributes / properties

Unless a row says otherwise, these describe the survey as it is now: without its
dropped blocks and the questions excluded when it was read.

| Name | Type | Meaning |
|---|---|---|
| `name` | `str` | The survey's name (`SurveyName` in its metadata), or `"Untitled Survey"` when it has none. A built survey takes the draft's title. |
| `id` | `str` | The Qualtrics survey ID (`SV_...`) from the metadata; empty when there is none, as for a built survey. |
| `language` | `str` | The language code in the metadata (`"EN"`), or empty. |
| `metadata` | `dict[str, Any]` | A copy of the template's metadata: the survey's ID, name, description, language and dates, as the QSF gives them. |
| `kind` | `str` | Where the survey came from: `"standard"` (`read_survey`, `new_survey`), `"qsf"` (`read_qsf`), `"builder"` (the draft builder) or `"template"` (made with `Survey(...)`). An edit keeps it. Recorded with results. |
| `standard` | `dict[str, Any]` | The survey in [the standard format](survey-format.md), as it runs: planning, prompts and exports read it. Dropped blocks and excluded questions are left out, edits applied. A survey written in the format is the survey as written, less its dropped blocks; one read from a QSF is as read. The edit methods change it directly; after an edit of the draft builder, it is read again from the template. A copy. |
| `read_warnings` | `list` | The warnings `read_survey` found (each with `path`, `message`, `code` and `level`); empty for other surveys. Not part of the survey's definition. |
| `source_name` | `str` | The file name when the QSF was read from a path (empty when read from bytes or parsed JSON); the draft's title for a built survey. |
| `questions` | `dict[str, Question]` | Every question by internal ID (`"QID56"`), in flow order. A block shown twice in the flow gives its questions once. |
| `blocks` | `list[dict[str, Any]]` | Every block in flow order, once each, as `{"block_id": ..., "name": ..., "question_ids": [...]}`. |
| `flow` | `list[dict[str, Any]]` | A copy of the survey flow, nested as in Qualtrics: blocks with their questions, branches, randomizers, groups, embedded-data elements and end-of-survey elements. |
| `template` | `dict[str, Any]` | The survey in a Qualtrics-like form, `{"Metadata": ..., "Elements": [...]}`: what `questions`, `blocks` and `flow` show, and the form earlier versions of the library ran. For a survey written in the standard format, it is made from `standard`. A branch that reads only dropped or excluded questions and runs no block is removed with them (`DropReport.pruned_branches`). |
| `embedded_fields` | `list[EmbeddedField]` | Every embedded-data field the survey sets, declares or reads, sorted by name: how it gets its value and what reads it. Prints as a table. See `EmbeddedField` below. |
| `exclude_blocks` | `list[str]` | The IDs of the dropped blocks, sorted. |
| `exclude_questions` | `list[str]` | The internal IDs of the questions excluded when the survey was read, sorted. |
| `drop_report` | `DropReport` | What the dropped blocks and excluded questions removed, and what in the rest of the survey still reads a dropped question. |
| `checksum` | `str` | The SHA-256 digest (hex) of the QSF file, or the standard-format file, as read. For a survey changed with the edit methods, the digest of its definition; for one built or changed with the draft builder, of its template. A survey that names picture files covers the pictures too. Dropping blocks does not change it. When `read_qsf` was given parsed JSON, the digest is of that JSON written out again, so it differs from the file's. |
| `source` | `dict[str, Any]` | Where the survey came from and what changed since: `kind`, `name` (the `source_name`), `checksum_sha256` and `edits`. |
| `qsf` | `dict[str, Any] \| None` | A copy of the QSF as read, before any drop or edit; `None` for a built survey. |
| `raw_bytes` | `bytes` | The bytes of the QSF file as read (for parsed JSON, that JSON written out again); empty for a built survey. |
| `edits` | `list[dict[str, Any]]` | The edits made since the survey was read or built, in order, one dict each: an edit method's has `"on": "definition"`, for example `{"op": "update_question", "on": "definition", "question": "cups", "changed": ["max", "text"]}`; a draft builder edit's, for example, `{"op": "update_question", "qid": "QID56", "question": "KS_MC_Q1", "fields": ["text"]}`. Saved with results. |
| `edited` | `bool` | `True` when `edits` is not empty. Dropping blocks is not an edit. |
| `edit_warnings` | `list` | Warnings from the edit that made this survey: for an edit method, the new warnings in the definition (each with `path`, `message`, `code` and `level`, as `read_warnings`); for a draft builder edit, `DraftIssue`s such as a policy's warning about a demographic question. They are not part of the survey's definition: `to_dict` leaves them out. |
| `unresolved_media` | `list[dict[str, Any]]` | Image references in builder questions that the image resolver could not turn into an image, for questions still in the survey. Each has `question_id`, `question` (the export tag), `reference`, `name` and `detail` (why it did not resolve). |

**Example**

```python
import surveytwin as st

survey = st.read_qsf("examples/example_survey.qsf")
print(survey.name, survey.id, survey.language)
print(survey.kind, survey.source_name)
print(len(survey.questions), len(survey.blocks))
print(survey.blocks[1])
```

```
Untitled project SV_82O2df4oFFBLDRY EN
qsf example_survey.qsf
36 12
{'block_id': 'BL_KS_TE_4', 'name': 'TextEntry', 'question_ids': ['QID13', 'QID14', 'QID64']}
```

### Making a `Survey` directly

```python
surveytwin.Survey(
    template: dict[str, Any],
    *,
    kind: str = "template",
    source_name: str = "",
    qsf: dict[str, Any] | None = None,
    raw_bytes: bytes | None = None,
    exclude_blocks: list[str] | None = None,
    exclude_questions: list[str] | None = None,
    unresolved_media: list[dict[str, Any]] | None = None,
    authoring: dict[str, dict[str, Any]] | None = None,
    edits: list[dict[str, Any]] | None = None,
) -> Survey
```

You rarely need this. Use `read_qsf` for a Qualtrics export, the builder for a
draft, and `Survey.from_dict` for a saved survey. The constructor wraps a parsed
template you already have, and checks only that it is one.

**Parameters**

- `template` (`dict[str, Any]`): a parsed survey template,
  `{"Metadata": {...}, "Elements": [...]}`, such as `survey.template`. A QSF
  itself (with `SurveyElements`) is refused.
- `kind` (`str`, default `"template"`): recorded with results. `read_qsf` uses
  `"qsf"`, and the builder `"builder"`. With `"qsf"` and `raw_bytes`, `check`
  also reports what only the QSF file shows (JavaScript, carry-forward choices,
  and so on), and `checksum` is the digest of those bytes.
- `source_name` (`str`, default `""`): the file name or title recorded with
  results.
- `qsf` (`dict[str, Any] | None`, default `None`): the QSF the template came
  from. `check` reads it for display logic on single options and for JavaScript
  that sets embedded data, and an excluded question's skip rules are placed by
  the block order it gives.
- `raw_bytes` (`bytes | None`, default `None`): the bytes of the QSF file.
  `None` leaves `raw_bytes` empty.
- `exclude_blocks` (`list[str] | None`, default `None`): the IDs of blocks to
  drop. Not checked here; `drop_blocks` checks them.
- `exclude_questions` (`list[str] | None`, default `None`): internal IDs of
  questions to leave out, as `read_qsf` leaves them out. Not checked here.
- `unresolved_media` (`list[dict[str, Any]] | None`, default `None`): builder
  image references that did not resolve.
- `authoring` (`dict[str, dict[str, Any]] | None`, default `None`): the
  builder's draft of each question it made, by internal ID.
- `edits` (`list[dict[str, Any]] | None`, default `None`): the edit history.

**Returns** `Survey`.

**Raises**

- `SurveyError`: `template` is not a dict with an `Elements` list, or it is a
  QSF.

### `Survey.check`

```python
Survey.check(
    *,
    network: bool = False,
    url_checker: Any = None,
    embedded_data: dict[str, Any] | None = None,
) -> SurveyCheck
```

Works out how the survey will run and what the library does with each feature
it uses: the execution mode, one finding per feature, the questions it cannot
simulate, the images, the design cells, the embedded-data fields twins will see
blank, and whether anything blocks a run. It calls nothing over the network
unless you ask it to.

Findings that only the QSF file shows (JavaScript, carry-forward choices, custom
validation, web services, and so on) come from the file as read, so after a
builder edit they can still mention content the edit removed.

**Parameters**

- `network` (`bool`, default `False`): `True` tests every image link over the
  network (the first 200 links, 8 at a time). `False` lists the links with the
  status `not_checked`.
- `url_checker` (`Callable[[str], dict[str, str]] | None`, default `None`):
  your own test for an image link. It takes the URL and returns
  `{"status": ..., "detail": ...}`, where the status is `"supported"`,
  `"auth_required"`, `"inaccessible"` or `"skipped"`. When you give one, it is
  used instead of the network, whatever `network` says. Any other status, or an
  exception raised by the function, counts as `"inaccessible"`.
- `embedded_data` (`dict[str, Any] | None`, default `None`): the values you will
  pass to `configure(embedded_data=...)`. Only the keys matter here. A field they
  supply is not reported as blank, and a key the survey never reads or sets is
  reported (level `Detected`), so a misspelled name shows up before the run.

**Returns** `SurveyCheck`: see `SurveyCheck` below. Printing it gives a short
report.

**Example**

```python
import surveytwin as st

survey = st.read_qsf("examples/example_survey.qsf")
check = survey.check(embedded_data={"pet": "Cat", "campaign": "spring"})
print(check.mode, check.staging_reasons)
print(check.design_cells, check.ok)
for finding in check.findings:
    if finding.level in ("Detected", "Not supported"):
        print(f"[{finding.level}] {finding.feature}: {finding.message}")


def my_checker(url):
    # your own test, for example through a proxy; here every link passes
    return {"status": "supported", "detail": "checked by my own proxy"}


print([image["status"] for image in survey.check(url_checker=my_checker).images])
```

```
staged ['branch or display logic reads an answer', 'display logic on questions', 'skip logic', 'piping of an earlier answer (${q://...})']
3 True
[Detected] piped_text.embedded_data: embedded_data supplies 'campaign', but the survey never reads or sets it; check the spelling.
['supported', 'supported', 'supported']
```

### `Survey.preview_blocks`

```python
Survey.preview_blocks() -> list[BlockPreview]
```

Lists every block in flow order, with what it holds and what else in the survey
reads its answers. Use it to decide which blocks to drop. Dropped blocks are
still listed, marked `dropped`; questions excluded when the survey was read are
not counted. A block shown twice in the flow is listed once.

The list prints as a table: the block ID, its name, how many questions take an
answer, the question types, and flags (`DROPPED`, images, `screen-out`,
findings). Under a block, each "read by the rest of the survey" line is logic
elsewhere that reads one of its questions.

**Returns** `list[BlockPreview]`: one `BlockPreview` per block.

**Example**

```python
import surveytwin as st

survey = st.read_qsf("examples/example_survey.qsf")
blocks = survey.preview_blocks()
print(blocks)
print([block.block_id for block in blocks if block.screenout])
```

```
12 blocks, in flow order:
  BL_KS_MC_1          'MC_Sigle'                11 to answer  MC 11
      read by the rest of the survey: branch at FL FL_14 reads QID56
      read by the rest of the survey: piping at SQ QID48 reads QID56
      read by the rest of the survey: branch at FL FL_29 reads QID56
      read by the rest of the survey: piping at SQ QID57 reads QID56
      read by the rest of the survey: branch at FL FL_32 reads QID56
      read by the rest of the survey: piping at SQ QID58 reads QID56
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
['BL_KS_FB_13']
```

### `Survey.drop_blocks`

```python
Survey.drop_blocks(block_ids: list[str]) -> Survey
```

Returns a new survey without these blocks. Their questions no longer exist: no
twin is asked them, no output file lists them, and logic elsewhere that reads
one treats it as a question that was never shown. See
[Dropped blocks and excluded questions](#dropped-blocks-and-excluded-questions).

**Parameters**

- `block_ids` (`list[str]`): the IDs of the blocks to drop, as `preview_blocks()`
  and `blocks` show them. Give a list even for one block: a single string is
  read letter by letter, and refused. Blocks the survey has already dropped stay
  dropped.

**Returns** `Survey`: a new survey. The survey you call it on does not change.

**Raises**

- `SurveyError`: the survey has no block with one of these IDs. The message
  lists the IDs it has.
- `DraftError`, with the issue code `survey.no_answerable`: the drop would leave
  nothing for a twin to answer. A survey that had nothing to answer before is
  not refused.

**Example**

```python
import surveytwin as st

survey = st.read_qsf("examples/example_survey.qsf")
trimmed = survey.drop_blocks(["BL_KS_MC_1"])
print(survey)
print(trimmed)
print(trimmed.exclude_blocks, trimmed.checksum == survey.checksum)
```

```
Survey('Untitled project', 36 questions, 12 blocks)
Survey('Untitled project', 25 questions, 11 blocks)
['BL_KS_MC_1'] True
```

### Editing a survey

Eight methods edit a survey's definition (`survey.standard`) directly: add,
change, move, swap and remove questions; add and remove blocks; gather
questions into a new block. Each returns a new survey; the survey you call it
on, and any plan made from it, do not change. They work the same on a survey
written in the standard format, one read from a QSF, one started with
`new_survey`, and one built from a draft.

```python
import surveytwin as st

survey = st.new_survey("Coffee habits")
survey = survey.add_question("single_choice", "Do you drink coffee?", options=["Yes", "No"], id="drinks", required=True)
survey = survey.add_question("number", "How many cups on a typical day?", id="cups", min=0, max=12,
                             show_if={"op": "selected", "question": "drinks", "option": "1"})
survey = survey.add_question("grid", "How important is each when you buy coffee?", id="importance",
                             rows=["Price", "Taste"], scale=["Not important", "Somewhat important", "Very important"])
print(survey)
print(survey.standard["blocks"])
```

```
Survey('Coffee habits', 3 questions, 1 block)
[{'id': 'main', 'kind': 'survey', 'questions': ['drinks', 'cups', 'importance']}]
```

What every edit does:

- **Only what it names changes.** Every other question, block and flow step
  stays as it was, field for field, and so does everything a QSF reader kept.
  Adding one question to `examples/survey-format/coffee-habits.json` changes two
  places: the new question and its block's list.
- **The result is checked as `read_survey` checks a file.** An edit that leaves
  an error in the definition raises `SurveyFormatError`, with the path of each
  field (`$.questions[2].response.max_selected`). So does an edit that names a
  picture file that cannot be read, or one that would give two CSV columns the
  same name. Only problems the edit makes are refused: a QSF's own oddities do
  not stop later edits. New warnings (a variable never set) are in
  `edit_warnings`.
- **Logic keeps reading earlier questions.** An edit is refused
  (`UnsupportedEdit`) when it would leave a condition, a skip rule, piped text,
  a value set in the flow or a repeat's rows reading a question that is gone,
  or one asked only after it in the flow as written. The message lists each
  one.
- **Dropped blocks are left alone.** An edit that touches a dropped block, or
  adds questions to a conjoint or MaxDiff block (whose tasks are drawn from its
  definition), raises `SurveyError`.
- **Each edit is recorded** in `edits`, for example
  `{"op": "add_question", "on": "definition", "question": "cups", "type": "number", "block": "main", "position": 2}`,
  and saved with results.

Picture files a question names (`image="images/ad.png"`, or a `media` entry of an
option) are read when the edit is made: an absolute path as written, a relative
path from the folder of the survey's JSON file (`read_survey`), the `folder`
given to `new_survey`, or else the current folder. A missing file is refused.

#### Question types

`add_question` (and `update_question`'s `type`) take these types. Each becomes a
question of [the standard format](survey-format.md):

| Type | The question's `response` | Lists it takes |
|---|---|---|
| `instruction` | none: shown, never answered | |
| `single_choice` | `choice`, `mode: "single"` | `options` |
| `multiple_choice` | `choice`, `mode: "multiple"` | `options` |
| `text` | `text` | |
| `number` | `number` | |
| `grid` | `matrix`, `per_row`, a single choice per row | `rows`, `scale` |
| `number_grid` | `matrix`, `per_row`, a number per row | `rows` |
| `text_grid` | `matrix`, `per_cell`, text in each cell | `rows`, `columns` |
| `sum_grid` | `matrix`, `per_cell`, numbers adding to `total` across each row (100 unless given; `axis="column"` adds down each column) | `rows`, `columns` |
| `form` | `form` | `fields` |
| `ranking` | `ranking` | `items` |
| `allocation` | `allocation` (`total` 100 unless given) | `items` |
| `side_by_side` | `group`, `layout: "side_by_side"`: each column a grid of the shared `rows`, on its own scale | `rows`, `columns` (each `{"label": ..., "scale": [...]}`, or a column in the format with its own `response`) |

A list is labels (`["Yes", "No"]`; each gets the ID of its place, `"1"`, `"2"`,
...) or objects in the format (`{"id": "y", "label": "Yes", "code": 1}`), or
both. `options` and `scale` also take the name of a scale in
`builder.SCALE_PRESETS` (`"agreement_5"`).

#### Settings

Any other keyword sets a field of the question or of its response, by its name
in the format:

- the question's own fields: `export_name`, `show_if`, `skip`, `presentation`,
  and `control` (short for `presentation={"control": ...}`), and `content` (the
  whole content list, in place of `text` and `image`);
- the response's fields, such as `required`, `min_selected`, `max_selected`,
  `randomization`, `default`, `min_length`, `max_length`, `format`, `min`,
  `max`, `step`, `decimals`, `not_applicable`, `min_ranked`, `max_ranked` and
  `total`. On a grid or form, a field only the answer of each row, cell or field
  takes goes there: `min` and `max` of a `number_grid` set its `row_response`,
  `max_length` of a `text_grid` its `cell_response`.

A keyword the type does not take is refused, with the list of those it takes.

### `Survey.add_question`

```python
Survey.add_question(
    type: str,
    text: Any = None,
    *,
    id: str | None = None,
    block: str | None = None,
    position: int | None = None,
    after: str | None = None,
    before: str | None = None,
    image: Any = None,
    options: Any = None,
    rows: Any = None,
    columns: Any = None,
    scale: Any = None,
    items: Any = None,
    fields: Any = None,
    **settings: Any,
) -> Survey
```

Returns a new survey with one more question, written into the definition.

**Parameters**

- `type` (`str`): one of the [question types](#question-types).
- `text` (`Any`, default `None`): what the question says: a string, `{"parts":
  [...]}` for text that pipes a value, a `text` or `table` content item, or a
  list of these. An `instruction` may have only a picture.
- `id` (`str | None`, default `None`): the question's ID (text without spaces).
  `None` gives `Q<n>`, one past every `Q<n>` the survey uses as an ID or export
  name, or used before an edit removed it.
- `block` (`str | None`, default `None`): the block to add it to. `None` means
  the block of the question `after` or `before` names, or the survey's only
  block; a survey with no block gets one, `main`, shown at the end of the flow.
  A survey with several blocks needs `block`, `after` or `before`.
- `position` (`int | None`, default `None`): where in the block, 1 for first.
  `None` adds it last.
- `after` (`str | None`, default `None`): a question to put it right after (its
  ID, or its export name).
- `before` (`str | None`, default `None`): a question to put it right before.
  Give one of `position`, `after` and `before`.
- `image` (`Any`, default `None`): a picture shown after the text: a web link, a
  `data:` address, the path of a picture file, `{"source": ..., "alt": ...}`, or
  a list of these.
- `options` (`Any`, default `None`): the options of a choice question.
- `rows` (`Any`, default `None`): the statements of a grid or side-by-side
  question.
- `columns` (`Any`, default `None`): the columns of a `text_grid`, `sum_grid` or
  `side_by_side` question.
- `scale` (`Any`, default `None`): the scale points of a `grid`.
- `items` (`Any`, default `None`): the items of a ranking or allocation.
- `fields` (`Any`, default `None`): the text boxes of a form.
- `**settings`: any other field of the question or its response (see
  [Settings](#settings)).

**Returns** `Survey`.

**Raises**

- `SurveyError`: an unknown type or setting, a list the type needs is missing,
  an ID already taken, a question with neither text nor a picture, or a place
  that cannot be found.
- `SurveyFormatError`: the question leaves an error in the definition (for
  example `max_selected` larger than the number of options, or two options with
  the same label), a picture file cannot be read, or a CSV column name would
  repeat.
- `UnsupportedEdit`: its logic reads a question asked after it.

**Example**

```python
import surveytwin as st

survey = st.read_survey("examples/survey-format/coffee-habits.json")
edited = survey.add_question("text", "What would make you bring your own cup?", block="reaction", after="own_cup")
print(next(b for b in edited.standard["blocks"] if b["id"] == "reaction"))
```

```
{'id': 'reaction', 'kind': 'survey', 'title': 'Reaction', 'questions': ['own_cup', 'Q11', 'price_fair']}
```

The new question is `Q11` because the survey's export names run to `Q10`.

### `Survey.update_question`

```python
Survey.update_question(
    question_id: str,
    *,
    type: Any = unchanged,
    text: Any = unchanged,
    image: Any = unchanged,
    options: Any = unchanged,
    rows: Any = unchanged,
    columns: Any = unchanged,
    scale: Any = unchanged,
    items: Any = unchanged,
    fields: Any = unchanged,
    **settings: Any,
) -> Survey
```

Returns a new survey with one question changed. Only what you give changes; a
parameter left out stays as it is.

**Parameters**

- `question_id` (`str`): the question's ID, or its export name when only one
  question has it.
- `type` (`Any`): a new [question type](#question-types). The new response keeps
  the lists and the `required` setting the new type also takes (a single choice's
  options become a multiple choice's options, an allocation's items a ranking's
  items); give the others. When the answer becomes another kind (a choice
  becomes text, say), the question's presentation goes, and for a question read
  from a QSF, the Qualtrics settings of the old answer go too. Refused when other
  parts of the survey read the question and the new kind would answer them
  differently, and when the question has skip rules but `skip` is not given.
- `text` (`Any`): new text, in place of the text the question shows; its
  pictures stay.
- `image` (`Any`): new pictures, in place of the old; `None` removes them.
- `options`, `rows`, `columns`, `scale`, `items`, `fields` (`Any`): a new list,
  in place of the old one. For a question read from a QSF, Qualtrics' own copy
  of the old list (side-by-side statements) goes with it.
- `**settings`: any setting `add_question` takes. `None` removes the setting
  (`show_if=None`, `required=None`). A question's ID cannot change.

**Returns** `Survey`.

**Raises**

- `SurveyError`: an unknown question, type or setting, nothing to change, or an
  `id` given.
- `SurveyFormatError`: the change leaves an error in the definition, for example
  a condition elsewhere names an option that is gone.
- `UnsupportedEdit`: a new kind of answer for a question other parts read, or a
  new display condition that reads a later question.

**Example**

```python
import surveytwin as st

survey = st.new_survey("Coffee habits")
survey = survey.add_question("number", "How many cups on a typical day?", id="cups", min=0, max=12)
survey = survey.update_question("cups", text="How many cups on a weekday?", max=20)
print(survey.standard["questions"][0])
print(survey.edits[-1])
```

```
{'id': 'cups', 'content': [{'type': 'text', 'text': 'How many cups on a weekday?'}], 'response': {'type': 'number', 'min': 0, 'max': 20}}
{'op': 'update_question', 'on': 'definition', 'question': 'cups', 'changed': ['max', 'text']}
```

### `Survey.move_question`

```python
Survey.move_question(
    question_id: str,
    *,
    block: str | None = None,
    position: int | None = None,
    after: str | None = None,
    before: str | None = None,
) -> Survey
```

Returns a new survey with one question moved, within its block or to another.

**Parameters**

- `question_id` (`str`): the question to move.
- `block` (`str | None`, default `None`): the block to move it to; `None` keeps
  its block (or takes the block of `after` or `before`).
- `position` (`int | None`, default `None`): its place in the block after the
  move, 1 for first; `None` puts it last when `block` is given.
- `after` (`str | None`, default `None`): a question to put it right after.
- `before` (`str | None`, default `None`): a question to put it right before.
  Give at least one of these four, and one of `position`, `after`, `before`.

**Returns** `Survey`.

**Raises**

- `SurveyError`: an unknown question or block, a dropped block, or no place
  given.
- `UnsupportedEdit`: logic or piped text would then read a question asked after
  it.

**Example**

```python
import surveytwin as st

survey = st.new_survey("Coffee habits")
survey = survey.add_question("single_choice", "Do you drink coffee?", options=["Yes", "No"], id="drinks")
survey = survey.add_question("number", "How many cups on a typical day?", id="cups",
                             show_if={"op": "selected", "question": "drinks", "option": "1"})
survey = survey.add_question("grid", "How important is each?", id="importance", rows=["Price", "Taste"], scale="agreement_5")
print(survey.move_question("importance", position=1).standard["blocks"][0]["questions"])
try:
    survey.move_question("drinks", after="cups")
except st.UnsupportedEdit as error:
    print(error)
```

```
['importance', 'drinks', 'cups']
This edit would leave logic or piped text reading a question asked only after it; reorder or change them first:
  the display condition of question 'cups' reads question 'drinks', which would be asked after it
```

### `Survey.swap_questions`

```python
Survey.swap_questions(first: str, second: str) -> Survey
```

Returns a new survey where two questions trade places, in one block or across
two.

**Parameters**

- `first` (`str`): one question.
- `second` (`str`): the other.

**Returns** `Survey`.

**Raises** `SurveyError` (an unknown question, the same question twice, a
dropped block) and `UnsupportedEdit` (logic would then read a later question).

**Example**

```python
print(survey.swap_questions("cups", "importance").standard["blocks"][0]["questions"])
```

```
['drinks', 'importance', 'cups']
```

### `Survey.remove_question`

```python
Survey.remove_question(question_id: str) -> Survey
```

Returns a new survey without the question. It is refused while other parts of
the survey read the question (conditions, skip rules that jump to it, piped
text, values set in the flow): change or remove those first. The question's own
skip rules and conditions go with it.

**Parameters**

- `question_id` (`str`): the question to remove.

**Returns** `Survey`.

**Raises** `SurveyError` (an unknown question, a dropped block) and
`UnsupportedEdit` (other parts read it; the message lists them).

**Example**

```python
try:
    survey.remove_question("drinks")
except st.UnsupportedEdit as error:
    print(error)
print(survey.remove_question("importance").standard["blocks"][0]["questions"])
```

```
remove_question('drinks'): other parts of the survey read this question; change or remove them first:
  the display condition of question 'cups' reads question 'drinks'
['drinks', 'cups']
```

### `Survey.add_block`

```python
Survey.add_block(
    block_id: str,
    *,
    title: str | None = None,
    position: int | None = None,
    after: str | None = None,
    before: str | None = None,
) -> Survey
```

Returns a new survey with an empty block, and a step in the flow that shows it.
Add questions to it with `add_question(..., block=...)` or `move_question`. (A
block with no questions is in `standard`, but not yet in `blocks`.)

**Parameters**

- `block_id` (`str`): the new block's ID (text without spaces, not taken).
- `title` (`str | None`, default `None`): its title.
- `position` (`int | None`, default `None`): its step's place in the top level of
  the flow, 1 for first.
- `after` (`str | None`, default `None`): a block to show it right after, at the
  same level of the flow (inside the same branch or group). Next to an arm of a
  randomizer, it is a new arm.
- `before` (`str | None`, default `None`): a block to show it right before.
  With none of the three, the block is shown last.

**Returns** `Survey`.

**Raises** `SurveyError`: a block ID already taken or not valid, or `after` or
`before` names a block the flow does not show, or shows more than once.

**Example**

```python
edited = survey.add_block("feedback", title="Feedback").add_question("text", "Anything else to tell us?", block="feedback", id="more")
print(edited.standard["flow"])
```

```
[{'type': 'show_block', 'block': 'main'}, {'type': 'show_block', 'block': 'feedback'}]
```

### `Survey.remove_block`

```python
Survey.remove_block(block_id: str) -> Survey
```

Returns a new survey without the block, its questions, and every flow step that
shows it. It is refused while other parts of the survey read its questions.
Branches and groups that showed it stay, with one step fewer.

**Parameters**

- `block_id` (`str`): the block to remove (a conjoint or MaxDiff block too).

**Returns** `Survey`.

**Raises** `SurveyError` (an unknown or dropped block), `UnsupportedEdit`
(other parts read its questions), and `SurveyFormatError` (a randomizer would
then take more arms than it has).

**Example**

```python
print(edited.remove_block("feedback").standard["flow"])
```

```
[{'type': 'show_block', 'block': 'main'}]
```

### `Survey.group_into_block`

```python
Survey.group_into_block(
    question_ids: list[str],
    block_id: str,
    *,
    title: str | None = None,
    after: str | None = None,
    before: str | None = None,
) -> Survey
```

Returns a new survey where existing questions are gathered, in the order you
list them, into a new block. They leave the blocks they were in. The new block
is shown right after the block of the first question listed (at the same level
of the flow), or next to the block `after` or `before` names. A block emptied
this way stays, with no questions; remove it with `remove_block`.

**Parameters**

- `question_ids` (`list[str]`): the questions to gather.
- `block_id` (`str`): the new block's ID.
- `title` (`str | None`, default `None`): its title.
- `after` (`str | None`, default `None`): a block to show the new block right
  after.
- `before` (`str | None`, default `None`): a block to show it right before.

**Returns** `Survey`.

**Raises**

- `SurveyError`: an unknown question, a question named twice, a block ID
  already taken, a dropped block, or the first question's block is an arm of a
  randomizer and neither `after` nor `before` is given (the new block would be
  an arm of its own).
- `UnsupportedEdit`: a question is in a block that repeats (loop and merge),
  where it is asked once per row and in a new block would be asked once; or
  logic would then read a later question.

**Example**

```python
grouped = survey.group_into_block(["cups", "importance"], "habits", title="Habits")
print(grouped.standard["blocks"])
print(grouped.standard["flow"])
```

```
[{'id': 'main', 'kind': 'survey', 'questions': ['drinks']}, {'id': 'habits', 'kind': 'survey', 'title': 'Habits', 'questions': ['cups', 'importance']}]
[{'type': 'show_block', 'block': 'main'}, {'type': 'show_block', 'block': 'habits'}]
```

### `Survey.to_dict`

```python
Survey.to_dict() -> dict[str, Any]
```

The whole survey definition as JSON-ready data, to store and rebuild later with
`Survey.from_dict`. It holds the template as read or built, with every rule and
expression as written (`${e://...}`, `${rand://...}`, logic, embedded-data
declarations) and never a twin's values; the dropped blocks and excluded
questions; the builder's drafts and edits; and the QSF file's bytes. A saved run
keeps it as `survey.json`.

**Returns** `dict[str, Any]` with the keys `format` (`"surveytwin-survey"`),
`format_version` (`1`), `kind`, `source_name`, `checksum_sha256`,
`definition_sha256`, `template`, `exclude_blocks`, `exclude_questions`,
`authoring`, `unresolved_media`, `edits` and `qsf_base64` (the QSF file's bytes in
base64; empty for other surveys). A survey read with `read_survey` also has
`standard` (its definition as read) and `source_base64` (the file's bytes).

**Example**

```python
import surveytwin as st

survey = st.read_qsf("examples/example_survey.qsf")
data = survey.to_dict()
print(data["format"], data["kind"], data["source_name"])
print(sorted(data))
```

```
surveytwin-survey qsf example_survey.qsf
['authoring', 'checksum_sha256', 'definition_sha256', 'edits', 'exclude_blocks', 'exclude_questions', 'format', 'format_version', 'kind', 'qsf_base64', 'source_name', 'template', 'unresolved_media']
```

### `Survey.from_dict`

```python
Survey.from_dict(data: dict[str, Any]) -> Survey
```

A class method. Rebuilds a survey saved with `to_dict`: the same survey, with
the same checksum.

**Parameters**

- `data` (`dict[str, Any]`): what `to_dict()` returned, for example read back
  from a JSON file.

**Returns** `Survey`.

**Raises**

- `SurveyError`: `data` is not a saved survey (its `format` is not
  `"surveytwin-survey"`); its QSF cannot be read; it does not match its
  checksum, or its definition (template, drops, edits) was changed after it was
  saved; or its template is not a parsed template.

**Example**

```python
import json

import surveytwin as st

survey = st.read_qsf("examples/example_survey.qsf").drop_blocks(["BL_KS_MC_1"])
text = json.dumps(survey.to_dict())          # store it anywhere
again = st.Survey.from_dict(json.loads(text))
print(again, again.exclude_blocks)
print(again.checksum == survey.checksum)
```

```
Survey('Untitled project', 25 questions, 11 blocks) ['BL_KS_MC_1']
True
```

## `Question`

A read-only view of one question in the survey's template. Get one from
`survey.questions` by internal ID. Printing it shows the ID, the type and the
selector, for example `Question('QID56', MC/SAVR)`.

```python
surveytwin.Question(raw: dict[str, Any], block_id: str)
```

You do not need to make one yourself.

**Parameters**

- `raw` (`dict[str, Any]`): one question of a survey template.
- `block_id` (`str`): the ID of the block that holds it.

### Attributes / properties

| Name | Type | Meaning |
|---|---|---|
| `raw` | `dict[str, Any]` | The question as the template holds it (not a copy: read it, do not change it). |
| `block_id` | `str` | The ID of the block that holds the question. |
| `id` | `str` | The internal ID (`"QID56"`). |
| `export_tag` | `str` | The export tag (`"KS_MC_Q1"`), which names the question's columns in the Qualtrics-layout CSV files; the internal ID when there is none. |
| `type` | `str` | The QSF question type: `MC`, `TE`, `Matrix`, `Slider`, `CS`, `RO`, `SBS`, `DB` (descriptive text), `Timing`, `Meta`, `Captcha`, or the type of a question the library does not support. |
| `selector` | `str` | The QSF selector, which says which variant of the type it is: `SAVR` (one answer), `MAVR` (several answers), `Likert`, `TB` (text), `ML` (several lines), `HSLIDER`, `DND` (drag and drop), and so on. |
| `kind` | `str` | What the library does with it: `"answerable"` (twins answer it), `"descriptive"` (text or an image, shown as context), `"unsupported"` (a blank column; never shown to a twin), or `"timing"`, `"meta info"` or `"captcha"` (skipped). |
| `text` | `str` | The question text with HTML removed. A question that has only an image reads `"[Image stimulus attached]"`. |
| `html` | `str` | The template's `QuestionTextHTML` when it has one, else the same as `text`. Surveys read from a QSF or made with the builder do not keep the HTML, so this equals `text`. |
| `media` | `list[Any]` | The images shown with the question text, each a dict with `kind` (`"image"`) and `src` (the link or a `data:` URI). |
| `options` | `list[Any]` | The answer options (`Options`): the choices of a multiple-choice question or the items of a rank-order question; for a side-by-side question, its statements. |
| `option_ids` | `list[Any]` | The QSF's own IDs for `options`, in the same order, when the template keeps them; empty otherwise. |
| `rows` | `list[Any]` | The statements of a matrix or constant-sum question (`Rows`), or else of a slider (`Statements`); empty for other types. |
| `columns` | `list[Any]` | The columns of a matrix (`Columns`), such as its scale points; for other types, the same as `options`. |
| `statements` | `list[Any]` | The template's `Statements` (a slider's statements); empty otherwise. |
| `display_logic` | `Any` | The question's display logic as the QSF gives it, or `None`. |
| `skip_logic` | `Any` | The question's skip rules as the QSF gives them (each with a condition and a `SkipToDestination`), or `None`. |
| `validation` | `Any` | The template's `Validation` entry, or `None`. Questions read from a QSF or made with the builder keep their validation rules in `raw["Settings"]` instead (`ForceResponse`, `MinChoices`, `MaxChoices`, `ValidationType`, and so on), so this is normally `None`. |
| `randomization` | `Any` | The question's choice randomization settings, or `None`. |
| `recode_values` | `Any` | The recode values, from option ID to the value the values file writes, or `None`. |
| `defaults` | `Any` | The default answers (`DefaultChoices`, else `DefaultChoiceLabels`), or `None`. |
| `source` | `str` | How findings name the question: `"SQ QID56"`. |

**Example**

```python
import surveytwin as st

survey = st.read_qsf("examples/example_survey.qsf")
question = survey.questions["QID56"]
print(question)
print(question.export_tag, question.type, question.selector, question.kind)
print(question.text, question.options)

matrix = survey.questions["QID17"]
print(matrix.rows)
print(matrix.columns)
```

```
Question('QID56', MC/SAVR)
KS_MC_Q1 MC SAVR answerable
What is your favorite pet? ['Cat', 'Dog', 'Rabbit']
['I enjoy working with data.', 'I make decisions quickly.', 'I like learning new tools.']
['Strongly disagree', '2', '3', '4', 'Strongly agree']
```

## `EmbeddedField`

One embedded-data field, as `Survey.embedded_fields` lists it: where the survey
sets it and what reads it. A record (a dataclass). It lives in
`surveytwin.survey` and is not exported as `st.EmbeddedField`; you get these
objects from `survey.embedded_fields`, which prints them as a table.

### Attributes / properties

| Name | Type | Meaning |
|---|---|---|
| `name` | `str` | The field's name. A field set with `setJSEmbeddedData` carries Qualtrics' `__js_` prefix. |
| `set_in` | `list[str]` | The survey-flow elements that set a value, such as `"FL FL_21"`. |
| `from_outside` | `list[str]` | The survey-flow elements that declare the field without a value: in Qualtrics it comes from a panel, a contact list or the survey link. Give such values with `configure(embedded_data=...)`. |
| `set_by_javascript` | `list[str]` | The questions (`"SQ QID9"`), or the survey header or footer, whose JavaScript sets it. The simulation does not run JavaScript. |
| `read_by` | `list[str]` | What reads it: question text and options that pipe it (`"SQ QID5"`), display logic (`"display logic SQ QID37"`), display logic on single options (`"choice display logic SQ QID3"`), branches (`"branch FL FL_7"`), and other fields' values (`"FL FL_3 total"`). |
| `variable_type` | `str` | The type Qualtrics declares for it, such as `"String"`; empty when none is given. |
| `source` | `str` | How the field gets its value, the first that applies: `"set in the flow"`, `"set by JavaScript"`, `"from a panel or the survey link"`, or `"never set"`. |

A field that something reads but the flow never sets is blank for twins unless
you supply it. `Survey.check` says why for each one (see
`SurveyCheck.unset_embedded_fields`).

**Example**

```python
import surveytwin as st

survey = st.read_qsf("examples/example_survey.qsf")
print(survey.embedded_fields)
for field in survey.embedded_fields:
    print(field.name, "|", field.source, "|", field.read_by)
```

```
1 embedded-data field:
  pet  set in the flow  FL FL_21, FL FL_22, FL FL_35  read by display logic SQ QID37, display logic SQ QID52
pet | set in the flow | ['display logic SQ QID37', 'display logic SQ QID52']
```

## `SurveyCheck`

What `Survey.check` returns: how the survey will run and what the library does
with it. A record (a dataclass). Printing it gives a short report that ends with
`OK`, or with `Needs attention before a run` when something blocks a run.

### Attributes / properties

| Name | Type | Meaning |
|---|---|---|
| `mode` | `str` | `"static"` or `"staged"`: how a run asks each twin (see below). `plan(..., mode="auto")` uses this mode. |
| `staging_reasons` | `list[str]` | Why the survey is staged, in plain words (for example `"skip logic"`); empty for a static survey. |
| `findings` | `list[Finding]` | One `Finding` for each Qualtrics feature the survey uses, and for each place that needs your attention, with how fully the library supports it. |
| `unsupported_questions` | `list[str]` | The internal IDs of questions of a type the library does not simulate. Each gets a blank column and is never shown to a twin. |
| `images` | `list[dict[str, str]]` | Every image the survey shows, one dict each, with `status`, `label`, `url`, `context` (where it appears, such as `"question"`), `question_id`, `prompt_label`, `detail` and `message`. See [Image statuses](#image-statuses). |
| `design_cells` | `int` | How many different versions of the survey the randomizers can produce; `1` without randomizers. |
| `unset_embedded_fields` | `list[str]` | Embedded-data fields that something reads but the flow never sets and `embedded_data` does not supply. Twins see them blank; a finding for each says why. |
| `ok` | `bool` | `True` when nothing blocks a run: no image is unreachable, behind a login or unresolved, and the survey has something for a twin to answer. |
| `reads_dropped` | `list[str]` | Logic that reads a dropped question, as text (`"branch at FL FL_14 reads QID56"`): the same places as `drop_report.references`. |

Without `network=True` or a `url_checker`, image links are not tested, so `ok`
does not promise that they load. `run` tests the links itself before any model
call.

**Example**

```python
import surveytwin as st

print(st.read_qsf("examples/example_survey.qsf").check())
```

```
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

### Execution mode: static or staged

A **static** survey is answered in one model call per twin. Nothing in it
depends on the twin's answers, so everything the twin will see is settled before
it answers: embedded-data values, randomizer arms, random numbers, and the order
of questions and options.

A **staged** survey is answered in stages, because later content depends on
earlier answers: a branch or display logic that reads an answer, skip logic,
piped text that shows an earlier answer (`${q://...}`), or an end-of-survey
element partway through the flow. Each stage is one model call. It asks what can
be settled so far and shows the twin's earlier answers, and the answers decide
what the next stage asks. `staging_reasons` lists what made the survey staged.
Conditions on embedded data and randomizers alone do not make a survey staged.

`plan(..., mode="static")` refuses a staged survey (`StaticModeRefused`), and
`mode="staged"` asks even a static survey in stages.

### Support levels

Each finding has a level that says how fully the library supports the feature.
The levels come from the library's support table:

| Level | Meaning |
|---|---|
| `Full` | Executed as Qualtrics executes it. |
| `Partial` | The common cases are executed; named edge cases are simplified. |
| `Detected` | Found and reported, but not executed. |
| `Not supported` | Found and reported, but not simulated. A question of an unsupported type gets a blank column and is never shown to a twin; for other features, the finding's message says what twins get instead (for example, a random-number token that cannot be drawn reaches the model as written). |
| `Not modeled` | Deliberately ignored. Only page breaks have this level (a block is one sequence in the prompt), and no check reports it. |

A finding's level is usually its feature's level in the table. Where the check
looks at a single case, the level is that case's: for example, an embedded-data
field that is read but never set is `Full` (twins see it blank, as Qualtrics
respondents would), one that comes from a panel or the survey link is `Partial`,
and one that the survey's JavaScript sets is `Not supported`.

The table ships with the library as `surveytwin/data/support_table.json`: each
feature's ID, its level, and what the library does with it.

```python
import json
from importlib import resources

text = resources.files("surveytwin").joinpath("data/support_table.json").read_text(encoding="utf-8")
table = json.loads(text)
print(table["levels"]["Detected"])
for row in table["features"]:
    if row["level"] == "Detected":
        print(row["id"], "-", row["what_the_library_does"])
```

```
found and reported, not executed
question_type.timing_meta_captcha - Skipped; no timings or device data invented
question_option.carry_forward - Reported; only works when the QSF already lists the final choices
question_option.javascript - Reported, never run
flow.web_service - Never called; fields it would set stay unresolved
```

### Image statuses

The `status` of each entry in `SurveyCheck.images`:

| Status | Meaning | Blocks a run |
|---|---|---|
| `not_checked` | Found; the link was not tested (no `network=True` and no `url_checker`). | no |
| `supported` | The link loads (or your `url_checker` said so), or the image is embedded in the survey as a `data:image/...` URI and is sent as it is. | no |
| `auth_required` | The link needs a Qualtrics login. | yes |
| `inaccessible` | The link could not be loaded. | yes |
| `skipped` | Not a link the check can test: not an absolute `http` or `https` address, past the first 200 links, or an image reference in the QSF that does not resolve to a link. | no |
| `unresolved` | A builder image reference that the image resolver returned nothing for. | yes |

An image that blocks a run makes `ok` false, and `run` stops with
`ImagesRefused` unless you pass `configure(accept_missing_images=True)`.

## `Finding`

One line of a check: a Qualtrics feature the survey uses, how fully the library
supports it, and where. `SurveyCheck.findings` and `BlockPreview.findings` hold
them. A record (a dataclass).

### Attributes / properties

| Name | Type | Meaning |
|---|---|---|
| `feature` | `str` | The feature's ID in the support table, such as `"question_option.skip_logic"` or `"flow.randomizer"`. A few IDs are the check's own: `"execution.staged"` (the survey runs in stages), `"logic.missing_question"` (logic reads a question the survey does not have: deleted in Qualtrics, in no block of the flow, or excluded when read), `"survey.no_answerable"` (nothing for a twin to answer) and `"other"`. |
| `level` | `str` | `"Full"`, `"Partial"`, `"Detected"` or `"Not supported"`; see [Support levels](#support-levels). |
| `source` | `str` | Where it applies: a question (`"SQ QID44"`), a survey-flow element (`"FL FL_14"`), the places that read an embedded-data field (separated by commas), or empty for the whole survey. |
| `message` | `str` | What the library does, in a sentence or two. |

**Example**

```python
import surveytwin as st

survey = st.read_qsf("examples/example_survey.qsf", exclude_questions=["QID45"])
for finding in survey.check().findings:
    if finding.source:
        print(f"[{finding.level}] {finding.feature} at {finding.source}: {finding.message}")
```

```
[Full] logic.missing_question at SQ QID44: skip logic jumps to QID45, which was excluded when the survey was read; it goes on to the end of its block, as for a question never shown.
```

## `BlockPreview`

One block as `Survey.preview_blocks` lists it. A record (a dataclass). Printing
one gives its row of the table, then a line for each piece of logic elsewhere
that reads its questions.

### Attributes / properties

| Name | Type | Meaning |
|---|---|---|
| `block_id` | `str` | The block's ID, as `drop_blocks` takes it. |
| `name` | `str` | The block's name in Qualtrics. |
| `flow_path` | `list[str]` | The survey-flow elements that hold the block, outermost first, such as `["Randomizer FL_10"]` (or `"Branch FL_14"`, `"Group ..."`); empty at the top level of the flow. |
| `question_counts` | `dict[str, int]` | How many questions of each type (`Question.type`) the block has, such as `{"DB": 2, "TE": 1}`. |
| `answerable` | `int` | How many of its questions take an answer: not descriptive text, timing, meta info, captcha, or a type the library does not support. |
| `images` | `list[str]` | The links of the images shown with its questions' text. |
| `findings` | `list[Finding]` | Findings for this block: one for each question of a type the library does not support. |
| `screenout` | `bool` | `True` when an answer to one of its questions can end the survey early: skip logic to the end of the survey, or a branch that only ends the survey reads it. |
| `referenced_by` | `list[str]` | Logic elsewhere in the survey that reads one of its questions, as text (`"branch at FL FL_14 reads QID56"`): what would read a dropped question if you dropped this block. |
| `dropped` | `bool` | `True` when this survey has dropped the block. `False` by default. |

**Example**

```python
import surveytwin as st

survey = st.read_qsf("examples/example_survey.qsf")
block = survey.preview_blocks()[2]
print(block.block_id, block.name, block.question_counts, block.answerable)
print(len(block.images), block.screenout, block.dropped)
```

```
BL_KS_TG_10 GraphicEntry {'DB': 2, 'TE': 1} 1
2 False False
```

## Dropped blocks and excluded questions

There are two ways to leave part of a survey out:

- **Drop blocks**, with `survey.drop_blocks([...])` or
  `read_qsf(path, exclude_blocks=[...])`, which does the same. Use it for
  consent forms, attention checks and screen-outs that are not meant for twins.
  `preview_blocks()` still lists a dropped block, marked `DROPPED`.
- **Exclude questions** when reading, with
  `read_qsf(path, exclude_questions=[...])`. Use it for single questions twins
  should not answer, or for a question the reader cannot read.

A dropped or excluded question no longer exists. No twin is asked it, and no
CSV file, text file or report lists it. Logic in the rest of the survey that
reads it treats it the way Qualtrics treats a question a respondent never saw:
it has no answer. A condition that it was answered a certain way is false (in
the example survey, once block `BL_KS_MC_1` is dropped, the three branches that
test which pet was chosen in `QID56` are never taken), a number comparison with
it is false, and piped text that shows its answer shows nothing. The rest of
the survey is still asked.

Three more rules:

- A branch whose question conditions read only dropped or excluded questions,
  and that runs no block (it only ends the survey or sets embedded data), is
  removed with them, whatever else its condition tests. So dropping a consent
  block does not screen out every twin. `DropReport.pruned_branches` lists
  such branches. In a survey written in the standard format, a branch with
  `else` is judged part by part: a part that runs no block goes (the `else`
  part is listed as `"FL <branch ID>_else"`).
- Display logic on single options, matrix statements or scale points that reads
  a dropped question is applied in the same way: an option shown only when the
  dropped question was answered a certain way is hidden.
  `DropReport.choice_logic` lists them.
- A skip rule that jumped to an excluded question goes on to the next question
  of its block that is kept, or to the end of the block, as for a question never
  shown. `DropReport.moved_skips` lists them.

`drop_report` lists the logic that reads a dropped question, and `plan` warns
about it. For an excluded question, `check()` names each piece of logic that
reads it (feature `logic.missing_question`).

## `DropReport`

What `survey.drop_report` holds: what the dropped blocks and the questions
excluded when reading removed, and what in the rest of the survey still reads a
dropped question. A record (a dataclass). It is worked out from the survey as it
is now, so an edit never leaves it out of date. Printing it gives a short report,
or `Nothing dropped.`.

### Attributes / properties

| Name | Type | Meaning |
|---|---|---|
| `dropped_blocks` | `list[str]` | The IDs of the dropped blocks, sorted. |
| `dropped_questions` | `list[str]` | The internal IDs of the questions in the dropped blocks (for a conjoint or MaxDiff block, its tasks), sorted. |
| `pruned_branches` | `list[str]` | The branches removed with them (`"FL FL_7"`): each read only dropped or excluded questions and ran no block. |
| `references` | `list[Dependency]` | Logic outside the dropped blocks that reads a dropped question: branches, display logic, skip rules, piped text, embedded-data values and loop fields. Each counts the dropped question as never shown, with no answer. |
| `choice_logic` | `list[Dependency]` | Display logic on single options, matrix statements or scale points that reads a dropped question, listed apart from `references`. |
| `excluded_questions` | `list[str]` | The internal IDs of the questions excluded when the survey was read, sorted. |
| `moved_skips` | `list[str]` | Skip rules that jumped to an excluded question and now go on to the next question kept, such as `"SQ QID44 to ENDOFBLOCK (was QID45)"`. |

`references` and `choice_logic` cover the dropped blocks. For the excluded
questions, `Survey.check` names the logic that reads them.

**Example**

```python
import surveytwin as st

survey = st.read_qsf("examples/example_survey.qsf")
report = survey.drop_blocks(["BL_KS_MC_1"]).drop_report
print(report.dropped_blocks, len(report.dropped_questions))
for dependency in report.references[:2]:
    print(dependency.kind, dependency.location, dependency.reads)

excluded = st.read_qsf("examples/example_survey.qsf", exclude_questions=["QID45"])
print(excluded.drop_report.moved_skips)
```

```
['BL_KS_MC_1'] 11
branch FL FL_14 QID56
piping SQ QID48 QID56
['SQ QID44 to ENDOFBLOCK (was QID45)']
```

## `Dependency`

One piece of logic that reads a dropped question, as `DropReport.references`
and `DropReport.choice_logic` list it. A record (a dataclass). It lives in
`surveytwin.survey` and is not exported as `st.Dependency`.

### Attributes / properties

| Name | Type | Meaning |
|---|---|---|
| `kind` | `str` | What reads the question: `"branch"`, `"display_logic"`, `"choice_display_logic"`, `"skip_logic"` (a skip rule that jumps to it), `"piping"` (text or options that pipe its answer), `"embedded_data"` (an embedded-data value that pipes it) or `"loop_fields"` (a Loop & Merge block's loop values). |
| `location` | `str` | Where that logic is: a survey-flow element (`"FL FL_14"`), a question (`"SQ QID48"`), a block (`"BL BL_KS_FB_12"`), or one option or scale point (`"SQ QID12 choice 3"`, `"SQ QID12 scale point 2"`). |
| `reads` | `str` | The internal ID of the dropped question it reads. |

See also: [Errors](errors.md), [The survey builder](builder.md),
[Configuring and planning a run](configure-and-plan.md),
[Results and export files](results.md).
