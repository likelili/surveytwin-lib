# Reading and checking a survey

A run starts with a survey. `read_survey` reads a survey written in [the
standard format](survey-format.md); `read_qsf` reads a Qualtrics export (a `.qsf`
file) into that format; [the builder](builder.md) makes the same object from a
draft.
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

## `Survey`

One survey: its definition in the standard format, which planning, prompts and
exports run on, and views of its flow, blocks and questions in Qualtrics' terms.
You get one from `read_survey`, `read_qsf`, from the builder
(`builder.build_survey` and the edit functions), from `Survey.drop_blocks`, from
`Survey.from_dict`, or from a saved run (`results.survey`).

A survey never changes. `drop_blocks` and the builder's edit functions return a
new survey; the survey you started from, and any plan made from it, stay as
they were. The dicts and lists that the properties return are for reading:
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
| `kind` | `str` | Where the survey came from: `"standard"` (`read_survey`), `"qsf"` (`read_qsf`), `"builder"` (the builder) or `"template"` (made with `Survey(...)`). Recorded with results. |
| `standard` | `dict[str, Any]` | The survey in [the standard format](survey-format.md), as it runs: planning, prompts and exports read it. Dropped blocks and excluded questions are left out, edits applied. A survey written in the format is the survey as written, less its dropped blocks; one read from a QSF is as read, or read again after an edit. A copy. |
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
| `checksum` | `str` | The SHA-256 digest (hex) of the QSF file, or the standard-format file, as read. For a built survey, or one changed with the builder's edit functions, the digest of its template. Dropping blocks does not change it. When `read_qsf` was given parsed JSON, the digest is of that JSON written out again, so it differs from the file's. |
| `source` | `dict[str, Any]` | Where the survey came from and what changed since: `kind`, `name` (the `source_name`), `checksum_sha256` and `edits`. |
| `qsf` | `dict[str, Any] \| None` | A copy of the QSF as read, before any drop or edit; `None` for a built survey. |
| `raw_bytes` | `bytes` | The bytes of the QSF file as read (for parsed JSON, that JSON written out again); empty for a built survey. |
| `edits` | `list[dict[str, Any]]` | The builder edits made since the survey was read or built, in order, one dict each, for example `{"op": "update_question", "qid": "QID56", "question": "KS_MC_Q1", "fields": ["text"]}`. Saved with results. |
| `edited` | `bool` | `True` when `edits` is not empty. Dropping blocks is not an edit. |
| `edit_warnings` | `list[DraftIssue]` | Warnings from the builder edit that made this survey, such as a policy's warning about a demographic question. They are not part of the survey's definition: `to_dict` leaves them out. |
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
