# Building surveys without Qualtrics: `surveytwin.builder`

`surveytwin.builder` lets you write a survey in Python, or accept one from a web
form, instead of exporting a QSF from Qualtrics. It has seven question types,
checks every draft before building it, and can draft or refine a survey with a
model.

A **draft** (a title and a list of questions) is only an input. `build_survey`
turns it into a `Survey`, the same object `read_qsf` returns, so a built survey
goes through the same check, preview, plan, run, answer checks and exports as an
imported one. The edit functions take a `Survey` and return a new one, and they
work on imported surveys too.

The builder makes simple surveys: one block, questions in order, no branching,
display logic or randomization. For those, build the survey in Qualtrics and
export the QSF.

```python
import surveytwin as st
from surveytwin import builder

survey = builder.build_survey({
    "title": "Product evaluation",
    "questions": [
        {"id": "Q1", "type": "grid", "text": "How would you rate this product?",
         "rows": ["Quality", "Appearance"], "options": ["Poor", "Fair", "Good"], "required": True},
        {"id": "Q2", "type": "open_text", "text": "What would you improve?", "max_chars": 500},
    ],
})
print(survey.preview_blocks())
print(survey.check())

survey = builder.add_questions(survey, [
    {"id": "Q3", "type": "single_choice", "text": "Would you recommend this product?",
     "options": ["Yes", "No"], "required": True},
], after_question_id="Q2")

plan = st.plan(survey, st.configure(bank="examples/example-bank", sample_size=3, model="gpt-5.6-luna-medium"))
print(plan.preview_prompt())
```

## The draft

A draft is `{"title": ..., "questions": [...]}`, a JSON shape a web form can
send. `builder.SurveyDraft.from_dict(payload)` reads it and `to_dict()` returns
it with every field. Anything that takes a draft accepts either the dict or a
`SurveyDraft`, and never changes it.

| Field | Type | Default | Used by |
|---|---|---|---|
| `id` | text | blank (filled with the lowest free `Q<n>`) | all |
| `type` | one of the seven types below | required | all |
| `text` | text | blank | all |
| `required` | true / false | false | all but `instruction` |
| `options` | list of text | `[]` | `single_choice`, `multi_select`, `scale`; for `grid`, the scale points |
| `rows` | list of text | `[]` | `grid` (the statements) |
| `min_selections`, `max_selections` | whole number | none | `multi_select` |
| `min_chars`, `max_chars` | whole number | none | `open_text` |
| `min_value`, `max_value` | number | 0 and 100 | `number` |
| `step` | number above 0 | none | `number` |
| `decimals` | whole number 0 to 6 | none | `number` |
| `image_id`, `image_name` | text | blank | any type (see [Images](#images)) |
| `image_bytes` | whole number | 0 | not used by the library (a form's size bookkeeping) |
| `rationale` | text | blank | authoring note; never shown to a twin |
| `qid` | `QID<n>` | blank | not a form field: the question's internal ID in a survey, filled by `to_draft` (see IDs below) |

`to_draft` also fills four draft fields that a form does not send, written
by `to_dict` only when set: `next_qid` (one past every internal ID the survey
ever gave, removed and dropped questions included), `block_id` and
`block_name` (the survey's block), and `metadata` (the survey's own, such as its
Qualtrics ID and language). `build_survey` uses them, so building the draft
gives the survey back.

The types are `single_choice`, `multi_select`, `scale` (a labeled single choice;
`builder.SCALE_PRESETS` has four ready label sets), `grid`,
`open_text`, `number` and `instruction` (text or an image shown to the twin,
with nothing to answer). Fields a type does not use are kept and not used, but
must still hold valid values.

Values are read leniently: numbers given as text
(`"3"`), whole numbers written as `3.0`, and `required` written as `true`/`false`,
`"t"`/`"f"`, `"yes"`/`"no"`, `"on"`/`"off"` or `1`/`0`. The library is stricter
on one point: `"inf"` and `"nan"` are not numbers.

## How a draft becomes a survey

| Draft type | Survey question |
|---|---|
| `instruction` | Descriptive text (`DB`). An image with no text reads "[Image stimulus attached]". |
| `single_choice`, `scale` | Multiple choice, one answer (`MC`, `SAVR`). A scale is a single choice with labeled points. |
| `multi_select` | Multiple choice, several answers (`MC`, `MAVR`), with `MinChoices`/`MaxChoices` when set above 0. Each option becomes a yes/no answer in the prompt and a column (`Q3_1`, `Q3_2`, ...). |
| `grid` | One matrix question (`Matrix`, `Likert`, one answer per row). Each row becomes an answer and a column (`Q5_1`, ...). |
| `open_text` | Text entry (`TE`, several lines), with `MinChars`/`TotalChars` when set above 0. |
| `number` | A slider with one unlabeled statement (`Slider`, `HSLIDER`), range `min_value` to `max_value` (0 to 100 when not set), `decimals` and `step` when set. The twin answers with one number; the column is `Q7_1`. |

- `required` becomes Qualtrics' force response (`ON` or `OFF`).
- Each question records its validation type as a Qualtrics export does
  (`MinChoices`, `MaxChoices`, `ChoiceRange`, `MinChar`, `TotalChar`,
  `CharRange`, or `None`).
- Blank options and rows are dropped.
- One block: the draft's `block_id` and `block_name` when it has them, else
  `BL_BUILDER` named after the title; `block_id=` and `block_name=` override both.

**IDs.** Your question ID (`Q1`) becomes the question's **export tag**, which names
its columns in the Qualtrics-layout CSV files (labels, values, repaired). The
question also gets an internal ID (`QID1`, `QID2`, ... in order), as in a
Qualtrics export; answers, `long.csv`, `wide.csv`, the import-ID row and piped
text (`${q://QID1/...}`) use it. Using `Q1` for both would collide with the
`Q1:` labels the prompt gives questions. IDs of the form `QID<n>` are therefore
reserved for `id`.

An internal ID is the question's identity: it never changes, whatever happens
to the question's position, export tag or text. `to_draft` writes it into each
question's `qid`, and `build_survey` keeps a given `qid`, so a survey reordered,
turned into a draft, saved, refined and built again has the same IDs, and piped
text reads the same questions. A question without a `qid` is new: it gets the
next number above every ID the draft has or pipes, and at least the draft's
`next_qid`, so it never takes the ID of a question removed or dropped before
(`builder.planned_qids(draft)` lists them). A draft with neither `qid` nor
`next_qid` is numbered by position, as before; one with `next_qid` but no `qid`
that pipes an answer gets the warning `text.piping_by_position`, because its
pipes may now read other questions. `add_questions` refuses a question that
carries a `qid` (it came from another survey), and `update_question` accepts one
only unchanged, so a web form can send a whole question back.

A web form that cannot keep `qid` (many form models drop fields they do not know) should
send its draft back through `apply_draft(survey, draft)` instead of
`build_survey`: it matches each question to the one it came from and keeps its
ID (see [Editing a survey](#editing-a-survey)).

A built survey and a QSF written by hand to mean the same questions give the
same prompts, answers, CSV files, text files and report (a test checks this).

## Checking a draft

`builder.validate_draft(draft, *, policy=None)` returns every problem as a
`DraftIssue`:

- `severity`: `error` or `warning` (`info` appears only in the `notes` of a
  model-written draft)
- `message`
- `code`: a stable name for the rule
- `question_id` and `question_index`: which question, or blank and -1 for the whole survey
- `field`: which field, blank for the whole question
- `item_index`: which entry of `options` or `rows`, when one entry is at fault

`to_dict()` gives it as JSON for a web form. `build_survey` raises `DraftError`
when there is any error; its `issues` are those of the draft after blank IDs are
filled (so a question without an ID is named by the ID it would get). A payload
of the wrong shape (not an object, `questions` not a list, a question that is
not an object) is an issue too, with code `draft.invalid` or `question.invalid`.

| Rule | Message | Field |
|---|---|---|
| no questions | The survey has no questions. | |
| no answerable question | The survey has no answerable questions. | |
| blank text (an image-only instruction is fine) | Question text is empty. | `text` |
| fewer than 2 options | Choice questions need at least 2 options. | `options` |
| repeated option (ignoring case) | Options must be unique. | `options` |
| grid without rows | Grid questions need at least 1 row statement. | `rows` |
| grid with fewer than 2 points | Grid questions need at least 2 scale points. | `options` |
| min selections above max | Minimum selections cannot exceed the maximum. | `min_selections` |
| max selections above the option count | Maximum selections cannot exceed the number of options. | `max_selections` |
| min characters above max | Minimum characters cannot exceed the maximum. | `min_chars` |
| min value not below max (unset bounds count as 0 and 100) | Minimum value must be below the maximum value. | `min_value` |
| repeated row or scale point | Row statements / Scale points must be unique. | `rows`, `options` |
| min selections above the option count | Minimum selections cannot exceed the number of options. | `min_selections` |
| unknown type, value of the wrong kind | ... | the field |
| repeated ID, `QID<n>` ID, `TWIN_ID` or `demo_...` (export column names), an ID equal to another question's column name, or a question whose column name equals another question's ID | ... | `id` |
| unknown field | Unknown field 'x' is ignored. (warning) | the field |
| `qid` not of the form `QID<n>`, or repeated | ... | `qid` |
| piped text reading a question that is not in the draft, or comes later | The question pipes QID7, which is not in this draft. | `text`, `options`, `rows` |

**Product rules** are not survey errors, so they apply only when you pass
`policy=builder.WEBSITE_POLICY`, or your own `builder.BuilderPolicy`:

- at most 40 questions
- a title is required
- a warning on questions that ask for what a persona already carries ("What is
  your age?"), matched as whole words so "your agency" is not flagged

The library's default policy has none of these.

## Editing a survey

Each function returns a new `Survey`. The survey you pass in, and any plan made
from it, stay exactly as they were. A question can be named by its export tag
(`Q3`) or its internal ID (`QID3`). A name that could mean two questions is
refused, unless only one of them exists (a dropped question does not). Every
edit is listed in `survey.edits` and saved with results
(`provenance["survey_edits"]`).

```python
builder.add_questions(survey, questions, *, block_id=None, after_question_id=None,
                      before_question_id=None, image_resolver=None, policy=None) -> Survey
builder.update_question(survey, question_id, changes=None, *, image_resolver=None,
                        policy=None, **fields) -> Survey
builder.remove_questions(survey, question_ids) -> Survey          # one ID or a list
builder.reorder_questions(survey, order=None, *, swap=None, move=None, block_id=None) -> Survey
builder.to_draft(survey) -> SurveyDraft                            # a built survey as it is now, back as a draft
builder.apply_draft(survey, draft, *, image_resolver=None, policy=None) -> Survey   # a form's edited draft, applied
```

- **`add_questions`** inserts draft questions right after `after_question_id`,
  right before `before_question_id`, or at the end of the block. `block_id` can
  be left out when the survey has one block, or when an anchor question names it.
  Given IDs are kept; blank ones get the next `Q<n>` that no question has, a
  dropped one included. Each new question gets a new internal ID, never one used
  before, even by a removed question. An ID that is not text is an error.
- **`update_question`** changes fields by their draft names, for example
  `update_question(survey, "Q2", text="...", required=True)`. `id=` renames the
  export tag (it cannot be blank); the internal ID stays. The question's image
  stays as it is unless `image_id` changes to a different image, so an edit never
  needs the image resolver again. New text, options or rows may pipe another
  question (`${q://QID2/...}`, or `${q://1_QID2/...}` in a loop) only if that
  question exists (it is not in a dropped block) and comes earlier in the
  survey. Only the fields you change are checked, and only for pipes they did
  not have: piping already in a question stays as it is. An object or a list
  given for a text field is an error.
- **Column names.** After every edit (and every build) the library works out
  the CSV column names with the export's own column plan. An edit that would make
  one appear twice is refused with a `DraftError` naming it. Clashes already
  present in an imported file are left alone.
- **Nothing to answer.** `update_question`, `remove_questions`, `apply_draft`
  and `survey.drop_blocks` refuse an edit that would leave nothing to answer (the
  last answerable question turned into an instruction, removed, or dropped), with
  the error building such a draft gives (`survey.no_answerable`). A survey that
  had nothing to answer before the edit is not refused again.
- **`to_draft`** gives the survey as it is now (dropped blocks are left out),
  with each question's `qid` and the survey's `next_qid`, block and metadata. A
  draft holds one plain block of questions, so it refuses, with
  `UnsupportedEdit` naming the cause, a survey it would flatten or lose: questions
  read from a QSF, more than one block, a block shown more than once, branches,
  randomizers, embedded data or any other flow element, a loop, randomized
  question order, other survey parts (web services), a question changed outside
  the builder (it no longer matches its draft), or a draft that could not be
  built back (for example, piped text reading a question in a dropped block).
- **`apply_draft`** applies a whole draft sent back by a web form to the survey
  it came from. A form that keeps `qid` says which question is which: each
  question keeps the ID it carries, and a question without one is new, even if
  it has a deleted question's export tag (a form may reuse tags after a
  delete). A form that sends no `qid` at all is matched by the same type, text,
  options and rows, then by the same export tag and type; a question matched only
  by its tag whose text changed while others pipe it gets the warning
  `apply.piped_changed`. A matched question keeps its internal ID, and the rest
  are new. Questions the
  draft leaves out are removed, and their IDs are never given out again: a
  `qid` of a removed or dropped question is refused (`qid.retired`), and so is
  one the survey never gave (`qid.unknown`). The survey keeps its block,
  metadata and edit history, with one `apply_draft` entry (added, removed,
  changed, and the new order by internal ID). Every rule of `build_survey`
  applies; a draft that changes nothing returns the survey itself.
- **Every edit keeps the survey's own draft buildable.** Piped text is checked
  in every field a question's draft keeps, including a field its type does not
  show (a later type change would show it); a question cannot pipe itself; the
  question cap counts only questions that exist; a blank ID never gets a tag
  whose columns would clash. A policy's warnings (for example a demographic
  question) come back in the new survey's `edit_warnings`, which is not part of
  the survey's definition. Reorders are logged with internal IDs (`order_qids`,
  `swap_qids`, `move_qid`), since tags can be renamed later or repeat.
- **`remove_questions`** refuses, naming every place, while logic still reads a
  removed question: a branch, display logic, skip logic that jumps to it, piped
  text, or embedded data (logic inside a dropped block does not count; it no
  longer exists). A block left empty disappears. Questions in a dropped block
  cannot be edited or removed.
- **`reorder_questions`** works within one block. Give exactly one of:
  - `order=[...]`: every question of the block, in the new order
  - `swap=(a, b)`: two questions, or two runs of consecutive questions, each given
    as a list, trade places; each run keeps its own order
  - `move=("Q3", "up")`: one question moves one place (`"down"`, `-1` and `+1` work too)

  No ID is renumbered. It refuses an order that would make display logic or piped
  text read a question that now comes later, or make skip logic jump backwards.

**Surveys read from a QSF.** An edit touches only the question it names. Every
other question, block, flow element and setting stays exactly as read.

| Question read from the QSF | Fields `update_question` can change |
|---|---|
| any | `text`, `id` (the export tag; not for side-by-side questions), and `image_id`, `image_name` when it shows at most one question image |
| multiple choice | also `required`, `options`, and for several answers `min_selections`, `max_selections` |
| Likert matrix, one or several answers per row | also `required`, `rows`, `options` (the columns) |
| text entry (one line, several lines, essay) | also `required`, `min_chars`, `max_chars` (its validation type follows the limits) |
| horizontal slider (`HSLIDER`) with one statement | also `required`, `min_value`, `max_value`, `step`, `decimals` |

`UnsupportedEdit` is raised, and nothing changes, for:
- a type change, or renaming a side-by-side question (its sub-questions name its columns)
- options or rows on a question with recode values, its own choice IDs, write-in
  options, option images, default answers or a custom choice-randomization order
- changing how many options a question has when logic reads it (logic refers to
  options by position; relabeling them in place is allowed). Logic inside a
  dropped block does not count, nor does a skip rule that only jumps to the
  question
- length limits on a text question that checks its answer's format (a number,
  an email, ...)
- a slider range that would leave a default answer outside it
- any other field

These are errors (`DraftError`) instead: fewer options than a multi-select's
selection limits or than a random subset shows, and a slider step larger than
its range. A snap-to-grid slider's step is recalculated from a new range unless
you give one.

Add, remove and reorder work on any block of an imported survey. The QSF-only
findings in `check()` (JavaScript, carry-forward, ...) describe the file as read.

## Images

A draft names a question's image with `image_id`. Its `image_name` (the
file's name) stays in the draft and is never sent to a twin: a respondent
does not see a file name, and a name like `high_price.png` would tell the
twin what the picture is meant to show. Pass
`image_resolver=` to turn your references into an image URL or a
`data:image/...` URI (a backend can map its stored image IDs to data URIs).
Without a resolver, `builder.default_image_resolver` accepts:

- an `https://` or `http://` URL
- a `data:image/...` URI
- the path of a PNG, JPEG, WEBP or GIF file on this computer

An embedded image (a `data:` URI) is reported by `check()` as supported and is
sent as it is. A run folder stores each embedded image once, in `run["media"]`
of `results.json`, and refers to it by digest elsewhere.

A reference that does not resolve is not dropped:

- `survey.unresolved_media` lists it.
- `survey.check()` reports it with status `unresolved`, and the check is not OK.
- A run stops with `ImagesRefused` unless `accept_missing_images=True`. The run
  then leaves the image out and lists it in `provenance["images_left_out"]`.

Uploads, storage, size limits and permissions stay with the application.

## Using the builder from a web backend

A backend keeps its own authentication, storage and image uploads, and passes
the form's draft to the library. For example, with FastAPI:

```python
from fastapi import FastAPI
import surveytwin as st
from surveytwin import builder

app = FastAPI()

def resolve_image(image_id: str) -> str | None:
    return my_storage.data_uri(image_id)          # your storage; None when missing

@app.post("/survey-draft/check")
def check_draft(draft: dict):
    issues = builder.validate_draft(draft, policy=builder.WEBSITE_POLICY)
    return {"issues": [i.to_dict() for i in issues]}

@app.post("/survey-draft/preview")
def preview(draft: dict):
    try:
        survey = builder.build_survey(draft, image_resolver=resolve_image, policy=builder.WEBSITE_POLICY)
    except st.DraftError as exc:
        return {"issues": [i.to_dict() for i in exc.issues]}
    config = st.configure(bank=my_bank_path, sample_size=1, model="gpt-5.6-luna-medium")
    return {"prompt": st.plan(survey, config).preview_prompt().text, "check": str(survey.check())}
```

The prompt preview is the library's own `plan.preview_prompt()`, the prompt a
run sends, not a separate preview path.

To save a form's edits to a survey it already has, apply them to that survey, so
every question keeps its internal ID even when the form does not send `qid`:

```python
@app.post("/surveys/{survey_id}/draft")
def save_draft(survey_id: str, draft: dict):
    survey = st.Survey.from_dict(my_storage.load(survey_id))
    try:
        edited = builder.apply_draft(survey, draft, image_resolver=resolve_image, policy=builder.WEBSITE_POLICY)
    except st.DraftError as exc:
        return {"issues": [i.to_dict() for i in exc.issues]}
    my_storage.save(survey_id, edited.to_dict())
    return {"issues": [i.to_dict() for i in edited.edit_warnings]}
```

## The command line

`surveytwin check`, `prompts`, `run` and `batch submit` also take a draft saved as
a `.json` file with `title` and `questions`:

```bash
surveytwin check draft.json
surveytwin run draft.json --twins 50 --model gpt-5.6-luna-medium --out runs/draft
```
