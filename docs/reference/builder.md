# The survey builder (`surveytwin.builder`)

`surveytwin.builder` makes a `Survey` from a draft (a title and a list of
questions) and edits surveys question by question; a built survey takes the
place of `read_qsf` at the start of a run and goes through the same check, plan,
run and exports. This page lists every name in the module. For a walk-through,
see [Building surveys without Qualtrics](../builder.md).

```python
from surveytwin import builder
```

| Name | What it does |
|---|---|
| [`build_survey`](#build_survey) | A survey from a draft. |
| [`to_draft`](#to_draft) | A built survey back as a draft, for a form to load. |
| [`add_questions`](#add_questions) | Inserts questions into a block. |
| [`update_question`](#update_question) | Changes one question's fields. |
| [`remove_questions`](#remove_questions) | Removes questions. |
| [`reorder_questions`](#reorder_questions) | A new order, a swap, or a move up or down. |
| [`apply_draft`](#apply_draft) | Applies a whole edited draft to the survey it came from. |
| [`validate_draft`](#validate_draft) | Every problem with a draft. |
| [`assign_ids`](#assign_ids) | Fills blank question IDs. |
| [`planned_qids`](#planned_qids) | The internal ID each question will get. |
| [`default_image_resolver`](#default_image_resolver) | Accepts image URLs, data URIs and local image files. |
| [`SurveyDraft`](#surveydraft), [`DraftQuestion`](#draftquestion), [`DraftIssue`](#draftissue), [`BuilderPolicy`](#builderpolicy) | The records. |
| [`DEFAULT_POLICY`](#default_policy), [`WEBSITE_POLICY`](#website_policy), [`SCALE_PRESETS`](#scale_presets), [`QUESTION_TYPES`](#question_types), [`ANSWERABLE_TYPES`](#answerable_types), [`DEFAULT_BLOCK_ID`](#default_block_id) | The constants. |

## Drafts, surveys and question IDs

**A draft is only an input.** A draft is a `SurveyDraft`, or the same content as
JSON: `{"title": ..., "questions": [...]}`, each question with the fields of
[`DraftQuestion`](#draftquestion). Every function that takes a draft accepts
either form and never changes it. `build_survey` turns a draft into a `Survey`,
the object every other step runs on. The edit functions take a `Survey` and
return a new one; the survey passed in, and any plan made from it, stay as they
were. The edit functions also work on surveys read from a QSF (see
[`update_question`](#update_question) for what may change there).

**Every question has two IDs.**

- The **export tag** is the draft's `id`, such as `Q1`. It names the question's
  columns in the Qualtrics-layout CSV files (`Q1`, or `Q3_1`, `Q3_2`, ... for a
  question with several answers). You choose it, and you can rename it.
- The **internal ID** has the form `QID<n>`. The survey gives it when the
  question is built or added, as a Qualtrics export does. Answers, `long.csv`,
  `wide.csv`, the import-ID row of the Qualtrics-layout files and piped text
  (`${q://QID1/ChoiceGroup/SelectedChoices}`) use it, and `survey.questions` is
  keyed by it. A draft carries it in `qid`, which `to_draft` fills. Because of
  this, an export tag of the form `QID<n>` is not allowed.

**Internal IDs are kept through every edit and never reused.** A question keeps
its internal ID whatever happens to its position, export tag, text or type:
through `add_questions`, `update_question`, `remove_questions` of other
questions, `reorder_questions`, `apply_draft`, and a round trip through
`to_draft` and `build_survey`. No edit renumbers anything. The
internal ID of a question that was removed, or that is in a dropped block, is
never given to a new question; a draft from `to_draft` carries `next_qid` so
that this also holds after a rebuild. So piped text always reads the question it
was written for. Export tags work differently: once no question has a tag, a new
question with a blank `id` can get it.

```python
from surveytwin import builder

survey = builder.build_survey({
    "title": "Snacks",
    "questions": [
        {"id": "Q1", "type": "single_choice", "text": "Which snack do you like best?",
         "options": ["Chips", "Fruit", "Nuts"]},
        {"id": "Q2", "type": "open_text",
         "text": "Why do you like ${q://QID1/ChoiceGroup/SelectedChoices}?"},
        {"id": "Q3", "type": "number", "text": "How many snacks do you eat a day?",
         "min_value": 0, "max_value": 10},
    ],
})
print({qid: q.export_tag for qid, q in survey.questions.items()})
# {'QID1': 'Q1', 'QID2': 'Q2', 'QID3': 'Q3'}

survey = builder.remove_questions(survey, "Q3")
survey = builder.add_questions(survey, {"type": "scale", "text": "How healthy are your snacks?",
                                        "options": ["Not at all", "Somewhat", "Very"]})
survey = builder.reorder_questions(survey, move=("Q3", "up"))
print({qid: q.export_tag for qid, q in survey.questions.items()})
# {'QID1': 'Q1', 'QID4': 'Q3', 'QID2': 'Q2'}   (the tag Q3 is free again; QID3 never is)
```

**Naming a question in an edit.** The edit functions take a question's export
tag (`"Q3"`) or its internal ID (`"QID3"`). An internal ID names its own
question, even when another question's export tag is the same text. A name that
could mean two questions (a QSF can repeat a tag) is refused with
`id.ambiguous`, unless only one of them exists (a question in a dropped block
does not). An unknown name is refused with `id.unknown`, and close matches are
suggested. A question left out with `read_qsf(..., exclude_questions=...)` does
not exist, so naming it also gives `id.unknown`. A question in a dropped block
cannot be edited or removed (`SurveyError`); edit the survey before dropping
blocks.

**Edits are recorded.** Each edit adds one entry to `survey.edits` (its `op` is
the function's name, with the IDs it touched), and results save the list in
`provenance["survey_edits"]`. Warnings from an edit, such as a policy's
demographic warning, are in the new survey's `edit_warnings`, a list of
`DraftIssue`. They are not part of the survey's definition and are not saved.

**Images.** A question names its image with `image_id`. The builder turns that
reference into an image URL or a `data:image/...` URI when the question is
built, added, or given a new `image_id`, by calling an image resolver. An
`ImageResolver` is any function that takes the reference (a `str`) and returns
the URL or URI, or `None` when it has no such image. Without one,
[`default_image_resolver`](#default_image_resolver) is used. When a reference
does not resolve, or the resolver raises, the question still keeps it, as a
missing image: `survey.unresolved_media` lists it, `survey.check()` is not OK,
and a run stops with `ImagesRefused` unless `accept_missing_images=True`. An edit
that does not change `image_id` keeps the image as it is and never asks the
resolver again.

## What the builder refuses

A refusal raises before anything changes. `DraftError` and `UnsupportedEdit`
are available as `surveytwin.DraftError` and `surveytwin.UnsupportedEdit`; both
are kinds of `SurveyError`.

| Error | Raised when | For example |
|---|---|---|
| `DraftError` | The draft or the edit breaks a rule. Its `issues` attribute is a list of [`DraftIssue`](#draftissue), each with a `code`, the question and the field, so a web form can show each problem where it belongs. `issues` also holds any warnings found at the same time; the error message counts only the errors. | a choice question with one option (`options.too_few`), a repeated or reserved ID (`id.duplicate`, `id.reserved`), a CSV column name that would appear twice (`id.column_clash`), piped text that reads a later or missing question (`text.piping_later`, `text.piping_unknown`), an edit that leaves nothing to answer (`survey.no_answerable`), a policy's question cap (`survey.too_many`), an unknown question name (`id.unknown`) |
| `UnsupportedEdit` | The edit would lose survey content or break the survey's logic. | removing a question that logic, piped text or embedded data still reads; an order that makes logic read a later question or makes skip logic jump backwards; on a question read from a QSF, a type change or a field the builder cannot change without losing Qualtrics content; `to_draft` or `apply_draft` on a survey that a draft cannot hold |
| `SurveyError` | The call itself cannot work. | an unknown block ID; no `block_id` when the survey has several blocks; a question in a dropped block; `reorder_questions` given two ways to reorder, or an order that leaves out a question |

Every code is listed under [`DraftIssue`](#codes).

## `build_survey`

```python
surveytwin.builder.build_survey(
    draft: SurveyDraft | dict[str, Any],
    *,
    block_id: str | None = None,
    block_name: str | None = None,
    image_resolver: ImageResolver | None = None,
    policy: BuilderPolicy | None = None,
) -> Survey
```

Turns a draft into a `Survey` with one block that holds the questions in order.
It makes no model call and writes no file.

[`DraftQuestion`](#draftquestion) shows how each question type becomes a survey
question. Blank question IDs get the lowest free `Q<n>`, as
[`assign_ids`](#assign_ids) fills them. A question with a `qid` keeps that
internal ID, and the others get new ones, as [`planned_qids`](#planned_qids)
lists them. Warnings do not stop the build; see them with
[`validate_draft`](#validate_draft).

**Parameters**

- `draft` (`SurveyDraft | dict[str, Any]`): the draft, or its JSON payload. It is
  never changed.
- `block_id` (`str | None`, default `None`): the block's ID. `None` takes the
  draft's own `block_id` (a draft from `to_draft` has one), else `BL_BUILDER`
  ([`DEFAULT_BLOCK_ID`](#default_block_id)). Surrounding spaces are removed.
- `block_name` (`str | None`, default `None`): the block's name. `None` takes the
  draft's `block_name`, else the title (`Survey` when the title is blank).
- `image_resolver` (`ImageResolver | None`, default `None`): turns each
  `image_id` into an image URL or `data:image/...` URI (see Images above). `None`
  uses `default_image_resolver`.
- `policy` (`BuilderPolicy | None`, default `None`): product rules to add, such
  as [`WEBSITE_POLICY`](#website_policy). `None` adds none.

**Returns** `Survey`: a survey whose `kind` is `"builder"`, named after the
draft's title (`Untitled survey` when the title is blank). Each question's
export tag is its `id`. Its internal IDs are `QID1`, `QID2`, ... in order,
unless the draft carries `qid` or `next_qid`. A draft from `to_draft` also gives
back the survey's metadata.

**Raises**

- `DraftError`: the draft has an error, including a payload of the wrong shape
  (`draft.invalid`, `question.invalid`), or two CSV columns would get the same
  name.
- `SurveyError`: `block_id` is blank.

**Example**

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
print(survey.kind, survey.blocks[0]["block_id"], survey.blocks[0]["question_ids"])
# builder BL_BUILDER ['QID1', 'QID2']

try:
    builder.build_survey({"title": "Bad", "questions": [
        {"id": "Q1", "type": "single_choice", "text": "Pick one", "options": ["Yes"]},
    ]})
except st.DraftError as exc:
    for issue in exc.issues:
        print(issue.code, issue.question_id, issue.field)
# options.too_few Q1 options
```

## `to_draft`

```python
surveytwin.builder.to_draft(survey: Survey) -> SurveyDraft
```

Gives a built survey back as a draft, as it is now (after any edits), for a web
form to load again. Building that draft again gives the same survey: the same
internal IDs, block and metadata, so piped text reads the same questions.

**Parameters**

- `survey` (`Survey`): a survey the builder made, edited or not.

**Returns** `SurveyDraft`: the title and the questions in order, each with its
internal ID in `qid`. It also carries `next_qid` (one past every internal ID
number the survey has given, so a question added in the form never takes the ID
of one removed before), `block_id`, `block_name` and `metadata`. Dropped blocks
are left out.

**Raises**

- `UnsupportedEdit`: a draft cannot hold this survey without losing something.
  A draft is one plain block of questions, so this covers: questions read from a
  QSF; more than one block, or a block shown more than once; any other flow
  element (branches, randomizers, embedded data); a block that loops (Loop &
  Merge) or shows its questions in random order; survey parts besides the flow;
  a question changed outside the builder (it no longer matches its draft); and a
  draft that would not build back. The message names the cause.
- `SurveyError`: every block of the survey is dropped.

**Example**

```python
from surveytwin import builder

survey = builder.build_survey({"title": "Snacks", "questions": [
    {"id": "Q1", "type": "single_choice", "text": "Which snack do you like best?",
     "options": ["Chips", "Fruit", "Nuts"]},
    {"id": "Q2", "type": "open_text", "text": "Why?"},
]})
survey = builder.remove_questions(survey, "Q2")
draft = builder.to_draft(survey)
print([(q.id, q.qid) for q in draft.questions], draft.next_qid)
# [('Q1', 'QID1')] 3
payload = draft.to_dict()   # JSON for the form, with qid, next_qid, block_id and block_name
```

## `add_questions`

```python
surveytwin.builder.add_questions(
    survey: Survey,
    questions: Sequence[dict[str, Any] | DraftQuestion] | dict[str, Any] | DraftQuestion,
    *,
    block_id: str | None = None,
    after_question_id: str | None = None,
    before_question_id: str | None = None,
    image_resolver: ImageResolver | None = None,
    policy: BuilderPolicy | None = None,
) -> Survey
```

Returns a new survey with draft questions inserted into one block, in the order
given. It works on built surveys and on surveys read from a QSF; every other
question, block and flow element stays as it was.

**Parameters**

- `survey` (`Survey`): the survey to add to. It does not change.
- `questions` (a list of draft questions, or a single one): each is a `dict` of
  draft fields or a `DraftQuestion`. A given `id` is kept. A blank `id` gets the
  next `Q<n>` above the highest in use, skipping one whose column names would
  clash. A question may not carry a `qid`: new questions always get this
  survey's next internal IDs, never one used before, not even by a removed
  question.
- `block_id` (`str | None`, default `None`): the block to add to. It can be left
  out when the survey has one block, or when an anchor question names the block;
  given together with an anchor, it must be the anchor's block.
- `after_question_id` (`str | None`, default `None`): insert right after this
  question (export tag or internal ID).
- `before_question_id` (`str | None`, default `None`): insert right before this
  question. Give at most one of the two anchors; with neither, the questions go
  at the end of the block.
- `image_resolver` (`ImageResolver | None`, default `None`): resolves the new
  questions' `image_id` (see Images above). `None` uses `default_image_resolver`.
- `policy` (`BuilderPolicy | None`, default `None`): product rules for the new
  questions. The question cap counts the questions that exist after the
  addition. The policy's warnings are in the result's `edit_warnings`.

**Returns** `Survey`: the new survey. Its `edits` ends with an `add_questions`
entry: the block, the position, and each new question's tag and internal ID.

**Raises**

- `DraftError`: no questions (`add.empty`); an item that is not an object
  (`question.invalid`); a question with a `qid` (`qid.not_allowed`); any rule a
  question breaks, as in `validate_draft`, with IDs checked against the whole
  survey; the policy's cap (`survey.too_many`); text, options or rows that pipe a
  question that is missing, dropped or later (`text.piping_unknown`,
  `text.piping_dropped`, `text.piping_later`); a CSV column name that would
  appear twice (`id.column_clash`); an unknown or ambiguous anchor.
- `SurveyError`: both anchors given; `block_id` is not the anchor's block;
  several blocks and no `block_id`; an unknown block ID; a dropped block.

**Example**

```python
from surveytwin import builder

survey = builder.build_survey({"title": "Snacks", "questions": [
    {"id": "Q1", "type": "single_choice", "text": "Which snack do you like best?",
     "options": ["Chips", "Fruit", "Nuts"]},
    {"id": "Q2", "type": "open_text", "text": "Why?"},
]})
survey = builder.add_questions(survey, [
    {"type": "number", "text": "How many snacks do you eat a day?", "min_value": 0, "max_value": 10},
], after_question_id="Q1")
print([(qid, q.export_tag) for qid, q in survey.questions.items()])
# [('QID1', 'Q1'), ('QID3', 'Q3'), ('QID2', 'Q2')]
```

## `update_question`

```python
surveytwin.builder.update_question(
    survey: Survey,
    question_id: str,
    changes: dict[str, Any] | None = None,
    *,
    image_resolver: ImageResolver | None = None,
    policy: BuilderPolicy | None = None,
    **fields: Any,
) -> Survey
```

Returns a new survey with one question's fields changed. The question keeps its
internal ID and its place.

**Parameters**

- `survey` (`Survey`): the survey. It does not change.
- `question_id` (`str`): the question, by export tag or internal ID.
- `changes` (`dict[str, Any] | None`, default `None`): the new values, by draft
  field name: `id`, `type`, `text`, `required`, `options`, `rows`,
  `min_selections`, `max_selections`, `min_chars`, `max_chars`, `min_value`,
  `max_value`, `step`, `decimals`, `image_id`, `image_name`, `image_bytes`,
  `rationale`. `id` renames the export tag. A `qid` is accepted only when it is
  the question's own, so a web form can send a whole question back.
- `fields` (keyword arguments, `**fields`): the same changes as keyword
  arguments, for example `text="..."`. They are merged with `changes`; a keyword
  argument wins.
- `image_resolver` (`ImageResolver | None`, default `None`): used only when
  `image_id` changes to a different image. `None` uses `default_image_resolver`.
- `policy` (`BuilderPolicy | None`, default `None`): product rules. Its warnings
  about the changed fields are in the result's `edit_warnings`.

**What may change.** A question the builder made takes any field, a new type
included; the whole question must then pass the draft rules. New text, options
or rows may pipe another question (`${q://QID2/...}`, or `${q://1_QID2/...}` in
a loop) only if that question exists and comes earlier in the survey. Piping is
checked only in the fields you change, and only for pipes they did not have
before: piping already in a question stays as it is.

A question read from a QSF takes only what maps onto it without losing anything.
Every other question, block and setting stays exactly as read.

| Question read from the QSF | Fields it takes |
|---|---|
| any | `text`, `id` (not for side-by-side questions), `image_id` and `image_name` (when it shows at most one image); `image_bytes` is accepted and not used |
| multiple choice | also `required` and `options`; with several answers, also `min_selections` and `max_selections` |
| Likert matrix, one or several answers per row | also `required`, `rows` and `options` (the scale points) |
| text entry: one line, several lines, or essay | also `required`, `min_chars` and `max_chars` (its validation type follows the limits) |
| horizontal slider (`HSLIDER`) with one statement | also `required`, `min_value`, `max_value`, `step` and `decimals` |

On a question read from a QSF, `UnsupportedEdit` is raised for:

- a type change (remove the question and add a new one instead), a `rationale`,
  or any field not in the table;
- new options or rows on a question with recode values, its own choice IDs,
  write-in options, option images, default answers, a custom choice order, or
  display logic on its options;
- a different number of options when logic reads the question, since logic
  refers to options by position (relabeling them in place is allowed; logic in a
  dropped block does not count, nor does a skip rule that only jumps to the
  question);
- length limits on a text question that checks the answer's format (a number,
  an email address, ...);
- a slider range that would leave a default answer outside it;
- renaming a side-by-side question (its sub-questions name its columns).

These are `DraftError`s instead: fewer options than a multi-select's selection
limits or than a random subset shows, and a slider step larger than its range.
A snap-to-grid slider's step follows a new range unless you give `step`.

**Returns** `Survey`: the new survey, with an `update_question` entry in `edits`
(the fields changed, and `renamed_from` after a rename). When the only change is
an `image_id` equal to the question's current one, the survey passed in is
returned as it is.

**Raises**

- `DraftError`: no changes (`update.empty`); an unknown field (`field.unknown`);
  a `qid` that is not the question's own (`qid.fixed`); a blank `id`
  (`id.missing`); a value or a question that breaks a draft rule; new piping
  that reads a missing, dropped or later question, or the question itself; the
  last answerable question turned into an instruction (`survey.no_answerable`);
  a CSV column name that would appear twice; an unknown or ambiguous
  `question_id`.
- `UnsupportedEdit`: on a question read from a QSF, a change listed above; on a
  builder question, a question changed outside the builder (it no longer
  matches its draft).
- `SurveyError`: the question is in a dropped block.

**Example**

```python
from surveytwin import builder

survey = builder.build_survey({"title": "Snacks", "questions": [
    {"id": "Q1", "type": "single_choice", "text": "Which snack do you like best?",
     "options": ["Chips", "Fruit", "Nuts"]},
]})
survey = builder.update_question(survey, "Q1", text="Which snack do you like most?", required=True)
survey = builder.update_question(survey, "Q1", {"options": ["Chips", "Fruit", "Nuts", "Cookies"]})
survey = builder.update_question(survey, "QID1", id="favorite_snack")   # renames the export tag
question = survey.questions["QID1"]
print(question.export_tag, question.text, question.options)
# favorite_snack Which snack do you like most? ['Chips', 'Fruit', 'Nuts', 'Cookies']
```

## `remove_questions`

```python
surveytwin.builder.remove_questions(survey: Survey, question_ids: str | Sequence[str]) -> Survey
```

Returns a new survey without these questions. The other questions keep their
IDs. A block left empty disappears from the flow.

**Parameters**

- `survey` (`Survey`): the survey. It does not change.
- `question_ids` (`str | Sequence[str]`): one question or a list, each by export
  tag or internal ID.

**Returns** `Survey`: the new survey, with a `remove_questions` entry in `edits`
(each removed question's tag and internal ID, and any block that disappeared).
The removed internal IDs are never given to a new question.

**Raises**

- `UnsupportedEdit`: other parts of the survey still read a removed question: a
  branch, display logic (also on options or scale points), skip logic that
  jumps to it, piped text, embedded data, or a Loop & Merge field. The message
  lists every place; change or remove those first. Logic inside a dropped block
  does not count.
- `DraftError`: an empty list (`remove.empty`); nothing would be left to answer
  (`survey.no_answerable`); an unknown or ambiguous name; a builder question
  whose draft pipes a removed question in a field its type does not show.
- `SurveyError`: a question in a dropped block.

**Example**

```python
import surveytwin as st
from surveytwin import builder

survey = builder.build_survey({"title": "Snacks", "questions": [
    {"id": "Q1", "type": "single_choice", "text": "Which snack do you like best?",
     "options": ["Chips", "Fruit", "Nuts"]},
    {"id": "Q2", "type": "open_text", "text": "Why do you like ${q://QID1/ChoiceGroup/SelectedChoices}?"},
    {"id": "Q3", "type": "number", "text": "How many snacks do you eat a day?", "min_value": 0, "max_value": 10},
]})
survey = builder.remove_questions(survey, "Q3")
try:
    builder.remove_questions(survey, "Q1")   # Q2 pipes its answer
except st.UnsupportedEdit as exc:
    print(exc)
# Other parts of the survey still read the question(s) to remove; change or remove them first:
#   piping at SQ QID2 reads Q1 (QID1)
```

## `reorder_questions`

```python
surveytwin.builder.reorder_questions(
    survey: Survey,
    order: Sequence[str] | None = None,
    *,
    swap: tuple[str | Sequence[str], str | Sequence[str]] | None = None,
    move: tuple[str, str | int] | None = None,
    block_id: str | None = None,
) -> Survey
```

Returns a new survey with the questions of one block in a new order. Questions
stay in their block and keep both IDs. Give exactly one of `order`, `swap` and
`move`.

**Parameters**

- `survey` (`Survey`): the survey. It does not change.
- `order` (`Sequence[str] | None`, default `None`): every question of the block,
  in the new order.
- `swap` (`tuple[str | Sequence[str], str | Sequence[str]] | None`, default
  `None`): `(a, b)`, two questions that trade places. Each side can also be a
  list of consecutive questions, which moves as a run and keeps its own order.
- `move` (`tuple[str, str | int] | None`, default `None`): `(question, "up")` or
  `(question, "down")` moves one question one place; `-1` and `1` work too.
- `block_id` (`str | None`, default `None`): a check. When given, the questions
  you name must be in this block; the questions themselves say which block is
  reordered.

**Returns** `Survey`: the new survey, with a `reorder_questions` entry in
`edits` that gives the new order by export tag (`order`) and by internal ID
(`order_qids`, plus `swap_qids` or `move_qid`).

**Raises**

- `UnsupportedEdit`: the order would make a question's display logic (also on
  its options) or piped text read a question that now comes after it, or make
  skip logic jump backwards. The message names each case.
- `SurveyError`: not exactly one of `order`, `swap` and `move`; an order that
  repeats or leaves out a question of the block; questions from more than one
  block (remove a question and add it to move it to another block); `block_id`
  is not their block; swap sides that are not runs of consecutive questions, or
  that overlap; a move direction other than up or down, or a move past the first
  or last place; a dropped block.
- `DraftError`: an unknown or ambiguous name; a builder question whose draft
  would then pipe a later question in a field its type does not show.

**Example**

```python
import surveytwin as st
from surveytwin import builder

survey = builder.build_survey({"title": "Snacks", "questions": [
    {"id": "Q1", "type": "single_choice", "text": "Which snack do you like best?",
     "options": ["Chips", "Fruit", "Nuts"]},
    {"id": "Q2", "type": "open_text", "text": "Why do you like ${q://QID1/ChoiceGroup/SelectedChoices}?"},
    {"id": "Q3", "type": "number", "text": "How many snacks do you eat a day?", "min_value": 0, "max_value": 10},
]})
survey = builder.reorder_questions(survey, move=("Q3", "up"))     # Q1, Q3, Q2
survey = builder.reorder_questions(survey, ["Q3", "Q1", "Q2"])    # Q3, Q1, Q2
try:
    builder.reorder_questions(survey, swap=("Q1", "Q2"))
except st.UnsupportedEdit as exc:
    print(exc)
# This order would break the survey's logic:
#   Q2 (QID2) reads Q1 (QID1), which would come after it
```

## `apply_draft`

```python
surveytwin.builder.apply_draft(
    survey: Survey,
    draft: Any,
    *,
    image_resolver: ImageResolver | None = None,
    policy: BuilderPolicy | None = None,
) -> Survey
```

Applies an edited draft, such as what a web form sends back, to the survey it
came from. Use it rather than `build_survey` to save a form's edits: every
question keeps its internal ID, even when the form does not send `qid`.

Each draft question is matched to the survey question it came from:

1. A form that keeps `qid` (on any question) says which is which. A question
   with a `qid` is that question; a question without one is new, even if it has
   a deleted question's export tag.
2. A form that sends no `qid` at all is matched by content: the same type,
   text, options and rows (when exactly one question fits), then the same export
   tag and type. A question matched only by its tag, whose text changed while
   other questions pipe its answer, gets the warning `apply.piped_changed`.
3. The remaining draft questions are new and get internal IDs that no question
   of this survey ever had. Survey questions the draft leaves out are removed,
   and their internal IDs are never given out again.

A changed `id` renames the question's export tag. The survey keeps its block,
metadata and edit history; `next_qid`, `block_id`, `block_name` and `metadata`
in the draft are ignored. Every rule `build_survey` applies holds for the whole
survey.

**Parameters**

- `survey` (`Survey`): a survey the builder made (one `to_draft` accepts). It
  does not change.
- `draft` (`Any`): the edited draft, as a `SurveyDraft` or its JSON payload.
- `image_resolver` (`ImageResolver | None`, default `None`): resolves the
  `image_id` of new questions and of questions whose `image_id` changed. `None`
  uses `default_image_resolver`.
- `policy` (`BuilderPolicy | None`, default `None`): product rules. Its warnings
  about the whole survey and about the questions added or changed are in the
  result's `edit_warnings`.

**Returns** `Survey`: the new survey, with one `apply_draft` entry in `edits`:
what was added, removed and changed, a new title if any, and the new order by
export tag (`order`) and internal ID (`order_qids`). When the draft changes
nothing, the survey passed in is returned as it is.

**Raises**

- `UnsupportedEdit`: the survey has no draft form (see [`to_draft`](#to_draft)),
  for example a survey read from a QSF.
- `DraftError`: a payload of the wrong shape; a `qid` of a question this survey
  no longer has (`qid.retired`) or never gave (`qid.unknown`); any rule
  `build_survey` applies; a CSV column name that would appear twice.

**Example**

```python
from surveytwin import builder

survey = builder.build_survey({"title": "Snacks", "questions": [
    {"id": "Q1", "type": "single_choice", "text": "Which snack do you like best?",
     "options": ["Chips", "Fruit", "Nuts"]},
    {"id": "Q2", "type": "open_text", "text": "Why do you like ${q://QID1/ChoiceGroup/SelectedChoices}?"},
    {"id": "Q3", "type": "number", "text": "How many snacks do you eat a day?", "min_value": 0, "max_value": 10},
]})
form = builder.to_draft(survey).to_dict()     # what the form loads, qid included
form["questions"][0]["text"] = "Which snack do you like most?"
del form["questions"][2]                       # the user deletes Q3 ...
form["questions"].append({"type": "open_text", "text": "Anything else?"})   # ... and adds a question
edited = builder.apply_draft(survey, form)
print([(qid, q.export_tag) for qid, q in edited.questions.items()])
# [('QID1', 'Q1'), ('QID2', 'Q2'), ('QID4', 'Q3')]
print(edited.edits[-1]["removed"], edited.edits[-1]["added"])
# [{'id': 'Q3', 'qid': 'QID3'}] [{'id': 'Q3', 'qid': 'QID4'}]
```

## `validate_draft`

```python
surveytwin.builder.validate_draft(
    draft: SurveyDraft | dict[str, Any],
    *,
    policy: BuilderPolicy | None = None,
) -> list[DraftIssue]
```

Lists every problem `build_survey` would raise for this draft, and its warnings,
without building anything. Use it to show a web form all of its problems at
once.

Blank IDs are filled first, as `build_survey` fills them, so a problem is
reported under the ID the question will get. A payload of the wrong shape (not
an object, or `questions` not a list) gives one `draft.invalid` issue, and a
question that is not an object gives `question.invalid`; nothing else is checked
then. The checks are:

- the survey: no questions; no answerable question; with a policy, a blank
  title or too many questions;
- each question: a value of the wrong kind; an unknown type; blank text (an
  instruction with an image may have none); the rules of its type (see
  [`DraftQuestion`](#question-types)); unknown fields (a warning); with a
  policy, a question that asks for demographics (a warning);
- IDs: a repeated ID; an ID of the form `QID<n>`, `TWIN_ID`, or one starting
  with `demo_` (column names every export has); an ID equal to another
  question's column name, or a column name equal to another question's ID;
- internal IDs: a `qid` not of the form `QID<n>`, a repeated `qid`, or a `qid`
  the survey never gave (at or above `next_qid`); a `next_qid`, `block_id`,
  `block_name` or `metadata` of the wrong kind;
- piping: text, options or rows that pipe a question not in the draft, a later
  question, or the question itself, by the internal IDs the draft will be built
  with; and the warning `text.piping_by_position` for a draft that has
  `next_qid` but no `qid` and pipes an answer, because its questions are then
  numbered anew.

**Parameters**

- `draft` (`SurveyDraft | dict[str, Any]`): the draft, or its JSON payload. It
  is never changed.
- `policy` (`BuilderPolicy | None`, default `None`): product rules to add, such
  as [`WEBSITE_POLICY`](#website_policy). `None` adds none.

**Returns** `list[DraftIssue]`: every issue, empty when the draft can be built
and has no warnings. The issues come in the order of the checks (the whole
draft, then each question in order, then IDs and piping), not sorted by
severity: keep those with `severity == "error"` to see what blocks a build.

**Example**

```python
from surveytwin import builder

draft = {"title": "", "questions": [
    {"type": "single_choice", "text": "Which snack do you like best?", "options": ["Chips", "chips"]},
    {"type": "open_text", "text": "What is your age?", "color": "red"},
]}
for issue in builder.validate_draft(draft, policy=builder.WEBSITE_POLICY):
    print(issue.severity, issue.code, issue.question_id, issue.field, issue.item_index)
# error title.required  title -1
# error options.duplicate Q1 options 1
# warning field.unknown Q2 color -1
# warning text.demographic Q2 text -1
```

## `assign_ids`

```python
surveytwin.builder.assign_ids(
    draft: SurveyDraft | dict[str, Any],
    *,
    taken: Iterable[str] = (),
) -> SurveyDraft
```

Returns a copy of the draft with every blank question ID filled with the lowest
free `Q<n>`. `build_survey` and `validate_draft` do this first.

IDs already given are kept as they are (with surrounding spaces removed),
repeated ones included; `validate_draft` reports those. A blank ID never takes
an ID that a later question already has, nor one whose own column names
(`Q<n>_1`, ... for a multi-select, a grid or a number) would repeat an ID.

**Parameters**

- `draft` (`SurveyDraft | dict[str, Any]`): the draft, or its JSON payload. It
  is not changed.
- `taken` (`Iterable[str]`, default `()`): IDs used elsewhere, which a blank
  question must not get.

**Returns** `SurveyDraft`: the copy, with every blank ID filled.

**Raises**

- `TypeError`: `draft` is not a draft (not a dict, `questions` not a list, or a
  question that is not an object).

**Example**

```python
from surveytwin import builder

draft = builder.assign_ids({"title": "Snacks", "questions": [
    {"type": "open_text", "text": "What do you snack on?"},
    {"id": "Q1", "type": "open_text", "text": "When?"},
    {"type": "open_text", "text": "Why?"},
]}, taken=["Q2"])
print([q.id for q in draft.questions])
# ['Q3', 'Q1', 'Q4']
```

## `planned_qids`

```python
surveytwin.builder.planned_qids(draft: SurveyDraft | dict[str, Any]) -> list[str]
```

Lists the internal ID each question will get when the draft is built, in
question order.

- A question with a `qid` keeps it.
- A new question gets the next number above every internal ID the draft has or
  pipes, and at least `next_qid`. So it never takes an ID that another
  question's piped text reads, or one the survey gave a question before.
- A draft with neither `qid` nor `next_qid` numbers its questions by position:
  `QID1`, `QID2`, ...

**Parameters**

- `draft` (`SurveyDraft | dict[str, Any]`): the draft, or its JSON payload. It
  is not changed.

**Returns** `list[str]`: one internal ID per question.

**Raises**

- `TypeError`: `draft` is not a draft (not a dict, `questions` not a list, or a
  question that is not an object).

**Example**

```python
from surveytwin import builder

print(builder.planned_qids({"title": "Snacks", "questions": [
    {"type": "open_text", "text": "What do you snack on?"},
    {"type": "open_text", "text": "Why?"},
]}))
# ['QID1', 'QID2']
print(builder.planned_qids({"title": "Snacks", "next_qid": 5, "questions": [
    {"qid": "QID1", "type": "open_text", "text": "What do you snack on?"},
    {"type": "open_text", "text": "Why?"},
]}))
# ['QID1', 'QID5']
```

## `default_image_resolver`

```python
surveytwin.builder.default_image_resolver(reference: str) -> str | None
```

The image resolver the builder uses when you give none. It returns:

- an `https://` or `http://` URL, as it is;
- a `data:image/...` URI, as it is;
- for the path of a PNG, JPEG, WEBP or GIF file on this computer (by its
  extension; `~` is expanded), the file's content as a `data:` URI;
- `None` for anything else, such as an application's own image ID or a file
  that does not exist.

It does not fetch URLs; `survey.check(network=True)` tests links.

**Parameters**

- `reference` (`str`): an `image_id`. Surrounding spaces are removed.

**Returns** `str | None`: the image source, or `None` when the reference does
not resolve.

**Example**

```python
from surveytwin import builder

print(builder.default_image_resolver("https://example.com/ad.png"))   # https://example.com/ad.png
print(builder.default_image_resolver("img_123"))                      # None
```

## `SurveyDraft`

A draft: a title and an ordered list of questions. It is a record (a dataclass)
whose fields you can change. You get one from `SurveyDraft.from_dict(payload)`,
`to_draft` or `assign_ids`,
or make one with keyword arguments:
`builder.SurveyDraft(title="...", questions=[...])`. `print(draft)` lists its
questions and notes.

### Attributes

| Name | Type | Default | Meaning |
|---|---|---|---|
| `title` | `str` | `'Untitled survey'` | The survey's name. A blank title builds as `Untitled survey`; a policy can require one. |
| `questions` | `list[DraftQuestion]` | `[]` | The questions, in order. |
| `next_qid` | `Any` | `None` | One past every internal ID number the survey has given, removed and dropped questions included; a new question's internal ID is at least `QID<next_qid>`. Set by `to_draft`. When set, a whole number above 0. |
| `block_id` | `Any` | `''` | The block's ID, carried by `to_draft`. When set, text. |
| `block_name` | `Any` | `''` | The block's name, carried by `to_draft`. When set, text. |
| `metadata` | `Any` | `{}` | The survey's own metadata (such as its Qualtrics survey ID and language), carried by `to_draft`. When set, an object. |
| `notes` | `list[DraftIssue]` | `[]` | Remarks a caller attaches to the draft. Not part of the payload. |
| `unknown_fields` | `dict[str, Any]` | `{}` | Keys of the payload that are not draft fields. `validate_draft` warns about each; they are never used and `to_dict` does not write them. |

### `SurveyDraft.from_dict`

```python
SurveyDraft.from_dict(data: dict[str, Any] | SurveyDraft) -> SurveyDraft
```

Reads a draft payload, or copies a draft (a class method). Values are read
leniently, as [`DraftQuestion.from_dict`](#draftquestionfrom_dict) reads them,
and problems are left for `validate_draft` to report. A missing title is
`Untitled survey`; a `null` title is blank.

**Parameters**

- `data` (`dict[str, Any] | SurveyDraft`): the payload,
  `{"title": ..., "questions": [...]}`, optionally with `next_qid`, `block_id`,
  `block_name` and `metadata`; or a `SurveyDraft`, which is copied in full.

**Returns** `SurveyDraft`: a new draft.

**Raises**

- `TypeError`: `data` is not a dict, its `questions` is not a list, or a
  question is not a dict.

### `SurveyDraft.to_dict`

```python
SurveyDraft.to_dict() -> dict[str, Any]
```

Returns the draft as JSON: `title` and `questions` (each as
`DraftQuestion.to_dict` gives it), plus `next_qid`, `block_id`, `block_name` and
`metadata` when they are set. `notes` and `unknown_fields` are left out.

### `SurveyDraft.copy`

```python
SurveyDraft.copy() -> SurveyDraft
```

Returns a full copy of the draft; changing the copy never changes the original.

**Example**

```python
from surveytwin import builder

draft = builder.SurveyDraft.from_dict({"title": "Snacks", "questions": [
    {"type": "single_choice", "text": "Which snack do you like best?", "options": ["Chips", "Fruit"]},
]})
draft.questions.append(builder.DraftQuestion(type="open_text", text="Why?"))
print(draft.to_dict()["questions"][1]["type"])   # open_text
print(builder.build_survey(draft))                # Survey('Snacks', 2 questions, 1 blocks)
```

## `DraftQuestion`

One draft question. It is a record (a dataclass) whose fields you can change.
`DraftQuestion.from_dict(payload)` reads one from JSON, and
`builder.DraftQuestion(type="open_text", text="Why?")` makes one; `type` is the
only field without a default.

### Question types

| `type` | The twin is shown | The twin answers with | Fields it uses | Rules |
|---|---|---|---|---|
| `single_choice` | the text and the `options` | one option | `options`, `required` | at least 2 options; no option repeated (ignoring case) |
| `scale` | the text and labeled scale points (`options`, in order) | one point | `options`, `required`; [`SCALE_PRESETS`](#scale_presets) has ready-made labels | as `single_choice` |
| `multi_select` | the text and the `options` | any options, from `min_selections` to `max_selections` of them | `options`, `min_selections`, `max_selections`, `required` | at least 2 options, none repeated; the minimum not above the maximum; neither above the number of options |
| `grid` | the text, statements (`rows`) and one shared scale (`options`) | one scale point for each statement | `rows`, `options`, `required` | at least 1 statement and 2 scale points; none repeated |
| `open_text` | the text | text from `min_chars` to `max_chars` characters long | `min_chars`, `max_chars`, `required` | the minimum not above the maximum |
| `number` | the text | one number from `min_value` to `max_value`, in steps of `step`, with at most `decimals` decimal places | `min_value`, `max_value`, `step`, `decimals`, `required` | the minimum below the maximum (unset, they are 0 and 100); `step` above 0; `decimals` 0 to 6 |
| `instruction` | the text, an image, or both | nothing | `text`, `image_id`, `image_name` | text may be blank only when there is an image |

Every type also takes `id`, `text`, `image_id`, `image_name`, `image_bytes`,
`rationale` and `qid`. A field a type does not use is kept and not used, but it
must still hold a value of the right kind. A `min_selections`,
`max_selections`, `min_chars` or `max_chars` of 0 means no limit. Blank options
and rows are dropped, and text is trimmed.

Each type becomes the survey question a Qualtrics export would have, so a built
survey and a QSF with the same questions give the same prompts and files:

| `type` | Survey question | Columns in the Qualtrics-layout CSV files, for export tag `Q1` |
|---|---|---|
| `single_choice`, `scale` | multiple choice with one answer (`MC`, `SAVR`) | `Q1` |
| `multi_select` | multiple choice with several answers (`MC`, `MAVR`); the prompt asks yes or no for each option | `Q1_1`, `Q1_2`, ... one per option |
| `grid` | one Likert matrix question, one answer per row (`Matrix`, `Likert`) | `Q1_1`, `Q1_2`, ... one per statement |
| `open_text` | text entry with several lines (`TE`, `ML`) | `Q1` |
| `number` | a slider with one unlabeled statement (`Slider`, `HSLIDER`) | `Q1_1` |
| `instruction` | descriptive text (`DB`); an image with no text reads "[Image stimulus attached]" | none |

`required` becomes Qualtrics' force response (`ON` or `OFF`), and the prompt
says "Response required." for such a question. Each question records its
validation type as a Qualtrics export does.

### Attributes

| Name | Type | Default | Meaning |
|---|---|---|---|
| `type` | `str` | none: required | One of [`QUESTION_TYPES`](#question_types). |
| `text` | `str` | `''` | The question text shown to the twin. It can pipe an earlier question's answer by internal ID, such as `${q://QID1/ChoiceGroup/SelectedChoices}`. |
| `id` | `str` | `''` | The export tag, which names the CSV columns. A blank `id` gets the lowest free `Q<n>`. It must be unique, must not have the form `QID<n>`, be `TWIN_ID` or start with `demo_`, and must not equal another question's column name. |
| `required` | `bool` | `False` | Whether the twin must answer (force response). Not used by `instruction`. |
| `options` | `list[str]` | `[]` | The choices (`single_choice`, `multi_select`), the scale points (`scale`), or the shared scale of a `grid`. |
| `rows` | `list[str]` | `[]` | The statements of a `grid`. |
| `min_selections` | `int \| None` | `None` | `multi_select`: the fewest options to select. `None` or 0 means no minimum. |
| `max_selections` | `int \| None` | `None` | `multi_select`: the most options to select. `None` or 0 means no maximum. |
| `min_chars` | `int \| None` | `None` | `open_text`: the shortest answer, in characters. `None` or 0 means no minimum. |
| `max_chars` | `int \| None` | `None` | `open_text`: the longest answer, in characters. `None` or 0 means no maximum. |
| `min_value` | `float \| None` | `None` | `number`: the lowest value. `None` means 0. |
| `max_value` | `float \| None` | `None` | `number`: the highest value. `None` means 100. |
| `step` | `float \| None` | `None` | `number`: the step between allowed values, above 0. |
| `decimals` | `int \| None` | `None` | `number`: the number of decimal places, 0 to 6. |
| `image_id` | `str` | `''` | A reference to the question's image, turned into an image by the image resolver (see Images above). |
| `image_name` | `str` | `''` | The image's file name, for the person editing the draft. It stays in the draft and is never sent to a twin. |
| `image_bytes` | `int` | `0` | The image's size, for the application's own records. The library does not use it; it must be a whole number of 0 or more. |
| `rationale` | `str` | `''` | An authoring note, such as why the question is asked. It never reaches a twin. |
| `qid` | `str` | `''` | The internal ID (`QID<n>`) the question already has in a survey. `to_draft` fills it; a new question leaves it blank. |
| `unknown_fields` | `dict[str, Any]` | `{}` | Keys of the payload that are not draft fields. `validate_draft` warns about each; they are never used and `to_dict` does not write them. |

`from_dict` reads values leniently: numbers given as text (`"3"`), whole numbers
written as `3.0`, numbers in `options` and `rows` (made text), `null` for a text
field (made blank) or a list (made empty), and `required` written as `true` or
`false`, `"t"`/`"f"`, `"yes"`/`"no"`, `"y"`/`"n"`, `"on"`/`"off"`, `"1"`/`"0"` or
`1`/`0`. `"inf"` and `"nan"` are not numbers. Anything else is kept as given, so
that `validate_draft` can name it (`field.invalid`). Type names are not
translated: `"likert"` in a draft is an unknown type.

### `DraftQuestion.from_dict`

```python
DraftQuestion.from_dict(data: dict[str, Any] | DraftQuestion) -> DraftQuestion
```

Reads one question from a payload, or copies a question (a class method). It
never raises for a bad value; `validate_draft` reports it with its field.

**Parameters**

- `data` (`dict[str, Any] | DraftQuestion`): the question's fields, or a
  `DraftQuestion`, which is copied in full.

**Returns** `DraftQuestion`: a new question.

**Raises**

- `TypeError`: `data` is not a dict.

### `DraftQuestion.to_dict`

```python
DraftQuestion.to_dict() -> dict[str, Any]
```

Returns the question as JSON: the 18 draft fields in a fixed order (`id`,
`type`, `text`, `required`, `options`, `rows`, `min_selections`,
`max_selections`, `min_chars`, `max_chars`, `min_value`, `max_value`, `step`,
`decimals`, `image_id`, `image_name`, `image_bytes`, `rationale`), plus `qid`
when it is set.

**Example**

```python
from surveytwin import builder

question = builder.DraftQuestion.from_dict({
    "type": "multi_select", "text": "Which snacks do you buy?",
    "options": ["Chips", "Fruit", "Nuts"], "max_selections": "2", "required": "yes",
})
print(question.max_selections, question.required)   # 2 True
print(builder.validate_draft({"title": "Snacks", "questions": [question.to_dict()]}))   # []
```

## `DraftIssue`

One problem with a draft or an edit, placed for a web form. It is a record (a
dataclass). You get these from `validate_draft`, from `DraftError.issues`, from
a survey's `edit_warnings`, and from a draft's `notes`. `str(issue)` reads like
`Q2 options[2]: Options must be unique.`

### Attributes

| Name | Type | Default | Meaning |
|---|---|---|---|
| `severity` | `str` | none: required | `"error"` (it blocks a build or an edit), `"warning"`, or `"info"` (only in a draft's `notes`). |
| `message` | `str` | none: required | What is wrong, in plain words. |
| `code` | `str` | none: required | A stable name for the rule (see [Codes](#codes)). |
| `question_id` | `str` | `''` | The question's export tag (after blank IDs are filled); blank for the whole survey. |
| `question_index` | `int` | `-1` | The question's position, counting from 0 (for `add_questions`, its position in the list added); -1 for the whole survey. |
| `field` | `str` | `''` | The draft field at fault; blank for the whole question. |
| `item_index` | `int` | `-1` | The entry of `options` or `rows` at fault, counting from 0, when one entry is; otherwise -1. |

### `DraftIssue.to_dict`

```python
DraftIssue.to_dict() -> dict[str, Any]
```

Returns the seven fields as a dict, ready to send as JSON.

**Example**

```python
from surveytwin import builder

issue = builder.validate_draft({"title": "Snacks", "questions": [
    {"id": "Q1", "type": "single_choice", "text": "Pick one", "options": ["Chips", "Fruit", "chips"]},
]})[0]
print(issue)              # Q1 options[2]: Options must be unique.
print(issue.to_dict())
# {'question_id': 'Q1', 'question_index': 0, 'severity': 'error', 'message': 'Options must be unique.',
#  'field': 'options', 'item_index': 2, 'code': 'options.duplicate'}
```

### Codes

Errors unless marked. "Policy" marks a rule that applies only with a
[`BuilderPolicy`](#builderpolicy).

| Code | Meaning |
|---|---|
| `draft.invalid` | The payload is not an object, or its `questions` is not a list. |
| `question.invalid` | A question is not an object. |
| `field.unknown` | A key that is not a draft field: a warning in a draft (it is ignored), an error in `update_question`. |
| `field.invalid` | A value of the wrong kind (text, a list of text, a whole number, a number, true or false), a `qid` not of the form `QID<n>`, or a `next_qid`, `block_id`, `block_name` or `metadata` of the wrong kind. |
| `type.unknown` | Not one of the seven types. |
| `title.required` | A blank title (policy). |
| `survey.empty` | The draft has no questions. |
| `survey.too_many` | More questions than the policy's cap (policy). |
| `survey.no_answerable` | Nothing a twin can answer: in a draft, or left by an edit, a drop or an exclusion. |
| `text.empty` | Blank question text (an instruction with an image may have none). |
| `text.demographic` | Warning: the question asks for something a persona already carries (policy). |
| `options.too_few` | A choice question with fewer than 2 options, a grid with fewer than 2 scale points, or (on a QSF question) fewer options than a random subset shows. |
| `options.duplicate` | An option or scale point repeated, ignoring case; `item_index` points at the repeat. |
| `rows.too_few` | A grid with no statement. |
| `rows.duplicate` | A grid statement repeated. |
| `min_selections.above_max` | A minimum selection count above the maximum. |
| `min_selections.above_options` | A minimum selection count above the number of options. |
| `max_selections.above_options` | A maximum selection count above the number of options. |
| `min_chars.above_max` | A minimum length above the maximum. |
| `min_value.not_below_max` | A minimum value not below the maximum (unset bounds count as 0 and 100). |
| `step.above_range` | A slider step larger than its range (on a QSF question). |
| `id.duplicate` | An ID another question already has. |
| `id.reserved` | An ID of the form `QID<n>`, or `TWIN_ID`, or one starting with `demo_`. |
| `id.column_clash` | An ID equal to another question's CSV column name, a column name equal to another question's ID, or an edit that would make a column name appear twice. |
| `id.missing` | A blank question name in an edit, or a blank `id` given to `update_question`. |
| `id.unknown` | An edit names a question the survey does not have. |
| `id.ambiguous` | An edit names a question by a tag that several questions have. |
| `qid.duplicate` | Two questions with the same `qid`. |
| `qid.unknown` | A `qid` the survey never gave (at or above `next_qid`). |
| `qid.retired` | `apply_draft`: a `qid` of a question the survey no longer has (removed, or in a dropped block). |
| `qid.not_allowed` | `add_questions`: a new question that carries a `qid`. |
| `qid.fixed` | `update_question`: a `qid` that is not the question's own. |
| `text.piping_self` | Piped text that reads the question's own answer. |
| `text.piping_unknown` | Piped text that reads a question not in the draft or survey. |
| `text.piping_later` | Piped text that reads a question that comes later. |
| `text.piping_dropped` | Piped text that reads a question in a dropped block. |
| `text.piping_by_position` | Warning: a draft with `next_qid` but no `qid` pipes an answer, so its questions are numbered anew and a pipe may read another question. Send each `qid` back, or use `apply_draft`. |
| `add.empty`, `update.empty`, `remove.empty` | An edit given nothing to add, change or remove. |
| `apply.piped_changed` | Warning: `apply_draft` matched a question by its tag only, its text changed, and other questions pipe its answer. |

## `BuilderPolicy`

Product rules an application may add on top of the rules that make a survey
wrong. It is an immutable record (a frozen dataclass). The library applies none
by default ([`DEFAULT_POLICY`](#default_policy)). Pass one as `policy=` to
`build_survey`, `validate_draft`, `add_questions`, `update_question` or
`apply_draft`; make one with keyword arguments, or use
[`WEBSITE_POLICY`](#website_policy).

### Attributes

| Name | Type | Default | Meaning |
|---|---|---|---|
| `max_questions` | `int \| None` | `None` | The most questions a survey may have; `None` means no cap. Going over is an error (`survey.too_many`). `add_questions` counts the questions that exist after the addition. |
| `require_title` | `bool` | `False` | A blank title is an error (`title.required`). |
| `warn_on_demographics` | `bool` | `False` | Warn (`text.demographic`) when question text asks for something a persona already carries, by phrases such as "your age", "how old are you", "your gender", "your income", "highest level of education", "marital status", "political party" or "zip code". Phrases are matched as whole words, so "your agency" is not flagged. Edits warn only about text they change. |

**Example**

```python
from surveytwin import builder

policy = builder.BuilderPolicy(max_questions=2, require_title=True)
issues = builder.validate_draft({"title": "", "questions": [
    {"type": "open_text", "text": "What do you snack on?"},
    {"type": "open_text", "text": "When?"},
    {"type": "open_text", "text": "Why?"},
]}, policy=policy)
print([issue.code for issue in issues])   # ['title.required', 'survey.too_many']
```

## `DEFAULT_POLICY`

```python
surveytwin.builder.DEFAULT_POLICY = BuilderPolicy(max_questions=None, require_title=False, warn_on_demographics=False)
```

The policy used when `policy=None`: no question cap, no title rule and no
demographic warning.

## `WEBSITE_POLICY`

```python
surveytwin.builder.WEBSITE_POLICY = BuilderPolicy(max_questions=40, require_title=True, warn_on_demographics=True)
```

A policy with a 40-question cap, a required title and a demographic warning.
Nothing applies it unless you pass it.

## `SCALE_PRESETS`

```python
surveytwin.builder.SCALE_PRESETS: dict[str, list[str]]
```

Four ready-made label sets for `scale` questions (or a grid's scale points),
from the most negative label to the most positive:

| Key | Labels |
|---|---|
| `agreement_5` | Strongly disagree, Somewhat disagree, Neither agree nor disagree, Somewhat agree, Strongly agree |
| `agreement_7` | Strongly disagree, Disagree, Somewhat disagree, Neither agree nor disagree, Somewhat agree, Agree, Strongly agree |
| `satisfaction_5` | Very dissatisfied, Somewhat dissatisfied, Neither satisfied nor dissatisfied, Somewhat satisfied, Very satisfied |
| `likelihood_5` | Very unlikely, Somewhat unlikely, Neither likely nor unlikely, Somewhat likely, Very likely |

## `QUESTION_TYPES`

```python
surveytwin.builder.QUESTION_TYPES = ('single_choice', 'multi_select', 'scale', 'grid', 'open_text', 'number', 'instruction')
```

The seven question types, as a tuple (see [Question types](#question-types)).

## `ANSWERABLE_TYPES`

```python
surveytwin.builder.ANSWERABLE_TYPES: frozenset[str]
```

The types a twin answers: every type in `QUESTION_TYPES` except `instruction`.
A draft needs at least one question of these types.

## `DEFAULT_BLOCK_ID`

```python
surveytwin.builder.DEFAULT_BLOCK_ID = 'BL_BUILDER'
```

The block ID `build_survey` uses when neither its `block_id` argument nor the
draft names one.

