# The standard survey format

A survey in the standard format is one JSON document that says everything a run
needs: what each question shows, what it asks for, and how the survey moves from
question to question. You can write one by hand or in code (`read_survey` reads
it), or get one from a Qualtrics export (`read_qsf` reads every QSF into this
format, and `surveytwin convert` writes it out). Either way, the survey that runs
is prepared from this form: the prompts a twin reads come from these fields.

This page describes version `0.1` of the format. The machine-readable
definition is the JSON Schema in
[`survey-format.schema.json`](survey-format.schema.json) (also printed by
`surveytwin schema`); the example
[`examples/survey-format/coffee-habits.json`](../../examples/survey-format/coffee-habits.json)
uses most of what is described here.

## A small survey

```json
{
  "schema_version": "0.1",
  "id": "pets",
  "title": "Pets",
  "questions": [
    {
      "id": "has_pet",
      "content": [{"type": "text", "text": "Do you have a pet?"}],
      "response": {
        "type": "choice",
        "mode": "single",
        "required": true,
        "options": [{"id": "yes", "label": "Yes"}, {"id": "no", "label": "No"}]
      }
    },
    {
      "id": "pet_name",
      "show_if": {"op": "selected", "question": "has_pet", "option": "yes"},
      "content": [{"type": "text", "text": "What is your pet's name?"}],
      "response": {"type": "text", "max_length": 40}
    }
  ],
  "blocks": [{"id": "main", "kind": "survey", "questions": ["has_pet", "pet_name"]}],
  "flow": [{"type": "show_block", "block": "main"}]
}
```

```python
import surveytwin as st

survey = st.read_survey("pets.json")
print(survey, survey.kind)
```

```
Survey('Pets', 2 questions, 1 block) standard
```

## How a survey is checked

Reading a survey checks it in two passes, and nothing runs until both pass:

1. **Shape.** Every object has the fields this page lists, of the right type;
   required fields are present; a field this page does not list is an error
   (a key starting with `x-` is allowed anywhere, kept and never used); and
   `schema_version` must be `"0.1"`.
2. **Meaning.** IDs are unique where answers are recorded by them; every
   reference resolves (a block in the flow, a question in a block, an option in a
   condition, a skip target later in the same block, a piped answer); limits are
   possible (`min_selected` is not more than `max_selected`); the rows of a
   repeated block are listed in the order they run; and no question offers two
   options with the same label, since the model answers with the label.

Each problem names the field it is about, as a path:
`$.questions[2].response.options[1].label`. Problems are errors, which stop the
survey (`SurveyFormatError` lists them all in `findings`), or warnings, which do
not (`Survey.read_warnings`): a variable that is read but never set, a block the
flow never shows, a question in no block.

| Code | Level | Meaning |
|---|---|---|
| `schema` | error | A field's shape: a missing or unknown field, a wrong type, a value not allowed. |
| `id.duplicate` | error | Two questions, blocks, flow steps, variables, options, rows, columns or items share an ID. |
| `label.duplicate` | error | Two options (or grid columns, or items) of one question read the same, ignoring case. A survey read from a QSF may have them; the engine tells them apart there. |
| `reference.block` | error | The flow shows a block the survey does not define. |
| `reference.question` | error | A block lists a question the survey does not define, or a question is in two blocks. |
| `reference.condition` | error | A condition reads a question the survey does not define, or an option that question does not have. |
| `reference.skip` | error | A skip goes to a question that is not later in the same block. |
| `reference.pipe` | error | Piped text reads the answer of a question the survey does not define. |
| `reference.default` | error | A preset answer names an option or item the question does not have, or two items share a preset rank. |
| `reference.randomization` | error | A randomization names an option the question does not have (a warning for a survey read from a QSF). |
| `response.limits` | error | A limit that cannot be met: a minimum above its maximum, more selections than options, a total of 0. |
| `response.mode` | error | A grid without what its mode needs (`row_response`, or `columns` and `cell_response`). |
| `repeat.order` | error | The rows of a repeated block are not listed in the order they run (numbers in numeric order). |
| `repeat.take` | error | `take` on a repeat whose rows are not shuffled. |
| `skip.condition` | error | A skip rule that does not test whether an option was selected. |
| `branch.else` | error | An `else` that cannot be decided as written (see [Branches](#branch)). |
| `randomize.take` | error | A randomizer that takes more steps than it holds. |
| `question.kind` | error | A question marked as never asked (`kind`) that also has a response. |
| `flow.empty` | error | Questions but no flow, so nothing would be asked. |
| `variable.unset` | warning | A variable is read but never set or declared; it reads as empty. |
| `block.unused` | warning | A block the flow never shows. |
| `question.unused` | warning | A question in no block, so never asked. |

## The survey

| Field | Type | Meaning |
|---|---|---|
| `schema_version` | `"0.1"` | Required. The version of this format the survey is written in. |
| `id` | text | Required. The survey's ID (no spaces). |
| `title` | text | The survey's name. |
| `description` | text | A description, for people. |
| `language` | text | The language code (`"EN"`). |
| `variables` | list of [variables](#variables) | The variables the survey uses, declared. |
| `questions` | list of [questions](#questions) | Every question and instruction, each once. |
| `blocks` | list of [blocks](#blocks) | Groups of questions shown together. |
| `flow` | list of [flow steps](#flow) | The order blocks are shown in, with conditions, random arms and variables. |
| `source` | any | Where an imported survey came from (see [A survey read from a QSF](#a-survey-read-from-a-qsf)). A written survey leaves it out. |

## Questions

A question shows content and, unless it is an instruction, asks for a
response.

| Field | Type | Meaning |
|---|---|---|
| `id` | text | Required. The question's ID; answers are recorded by it. |
| `export_name` | text | The column name in exports (Qualtrics' "data export tag"). |
| `content` | list of [content](#content) | Required. What the question shows, in order. |
| `response` | a [response](#responses) | What the question asks for. Leave it out for an instruction (shown, never answered). |
| `kind` | `"timing"`, `"meta_info"`, `"captcha"`, `"unsupported"` | A question read from a QSF that is never asked (a timer, browser information, a captcha, a type the engine does not simulate). It has no response. |
| `show_if` | a [condition](#conditions) | The question is shown only when this holds. |
| `skip` | list of [skip rules](#skip-rules) | After it is answered, jump ahead when a rule holds. |
| `presentation` | [presentation](#presentation) | How the response is shown. |
| `source` | object | What a QSF said about the question that has no field here (see [A survey read from a QSF](#a-survey-read-from-a-qsf)). |

### Content

| `type` | Fields | Shown as |
|---|---|---|
| `text` | `text` ([text](#text-and-piped-values), required); `format`: `"plain"` (default) or `"html"` | The text. Plain text keeps its line breaks. HTML loses its markup and keeps its lines as a browser shows them: a `<br>` ends a line, a `<div>` or a list item is a line of its own, a paragraph, a list or a table is set apart by a blank line, and the line breaks of the HTML source itself are spaces. A list item starts with "- " (or its number, in a numbered list). Which table cells are headings comes from the markup: a heading row (in `<thead>`, or all `<th>`) with one row of values reads as "Heading: value. Heading: value", and so does a two-column table with `<th>` labels down its first column; a wider table with labels down its first column gives one "Label: values" line per row; two rows with no heading cells stay two rows; any other table is a Markdown table. |
| `image` | `source` (required: a web link, a `data:` address, or the path of a picture file: absolute, or relative to the folder of the survey's JSON file); `alt`; `title` | The image, sent to the model where it stands among the question's content (see [Pictures](#pictures)). |
| `table` | `columns` (each `id`, `label`); `rows` (each `id`, `label`, `cells` by column ID); `caption`; `corner` (the label above the row labels) | A table, with every cell under its column, as Markdown. A table is something the question shows; to ask one answer per row, use a [grid](#matrix). |

Several content items are shown in order, separated by a blank line. A question
whose content is only images reads "[Image stimulus attached]".

### Pictures

A picture's `source`, in image content or in any `media` list (options, rows,
columns, MaxDiff items, conjoint levels), is one of:

- a web link (`https://...`), which the model's provider fetches;
- a `data:image/...` address, the picture itself written into the JSON;
- the path of a picture file on this computer: an absolute path, or a path
  relative to the folder of the survey's JSON file
  (`"source": "images/ad_high_price.png"` next to `study.json`). For a survey
  given to `read_survey` as data rather than a file, `folder=` says where
  relative paths start.

`read_survey` reads every file when it reads the survey. A missing or unreadable
file, a file that is not a PNG, JPEG, WEBP or GIF picture (its type is read from
its content), one larger than 20 MB, or one that is damaged or cut off is an
error naming the field: the library checks each picture's structure to its end
(a PNG's chunks, their checksums and its complete image data; a JPEG's segments
up to its end marker; a GIF's blocks; a WEBP's chunks), without a picture
library. A `data:` address in the JSON is checked the same way, and must hold
base64 data of the type it names. The survey keeps each path
as written; a run sends the picture inside its request, because a model cannot
open a file on this computer, and a saved run keeps one copy of each picture.
Where the files live and how they are organized is up to you or your
application.

### Text and piped values

Wherever this page says *text*, a value is either plain text or an object with
`parts`, each `{"text": "..."}` or `{"ref": ...}`. A reference is replaced by its
value when the question is shown:

```json
{"parts": [
  {"text": "You said you drink "},
  {"ref": {"scope": "answer", "question": "cups", "field": "number_entry"}},
  {"text": " cups a day."}
]}
```

| `scope` | Fields | The value |
|---|---|---|
| `variable` | `variable` | A [variable](#variables)'s value (empty when never set). |
| `answer` | `question`, `field`, `item` (for fields that name an option) | Part of an earlier answer; see the fields below. A question that pipes an answer is asked after it. |
| `loop` | `field`, or `value`: `"current_index"` / `"total"` | In a [repeated block](#repeat): a value of the current row, the row's position (1, 2, ...), or the number of rows. |
| `random` | `kind` (`"int"` or `"float"`), `min`, `max` | A random number, drawn once per twin and place. |
| `source` | `expression` | A piped expression from the source platform with no form here, kept as written. |

Answer fields: `selected_choices`, `selected_choices_recode`,
`selected_choices_text_entry`, `unselected_choices`, `displayed_choices`,
`undisplayed_choices`, `all_choices`, `selected_count`, `question_text`, and,
for one option (`item`), `choice_label`, `choice_recode`, `text_entry` and
`number_entry` (the text or number typed for that option, or the number given
for a numeric question).

## Responses

Every response has a `type` and may have `required`: `true` (an answer is
needed), `false` (the default) or `"request"` (an answer is asked for, but the
twin may go on without one).

### `choice`

One option, or several.

| Field | Type | Meaning |
|---|---|---|
| `mode` | `"single"` or `"multiple"` | Required. |
| `options` | list of [options](#options) | Required. |
| `min_selected`, `max_selected` | whole numbers | (`multiple`) How many options must be selected, at least and at most. |
| `default` | list of option IDs | Options selected before the twin answers (one for `single`). |
| `randomization` | [randomization](#randomization) | How the options are ordered for each twin. |

### Options

| Field | Type | Meaning |
|---|---|---|
| `id` | text | Required. Answers record this ID (or `code`), never the position. |
| `label` | text | Required. What the model reads and answers with. An option with an image and no label reads "[Image option 2]"; a `choice` option with neither reads its position ("3"). |
| `code` | text or number | The value exported for the option (a recode). The ID when absent. |
| `media` | list of images (`source`, `alt`, `title`) | Images shown with the option. |
| `write_in` | true/false | The option has a box for the twin's own words, answered as its own question (`<question>_<option>_TEXT`). |
| `show_if` | a [condition](#conditions) | The option is shown only when this holds. |

The same fields describe a grid's columns, a ranking's items and an
allocation's items.

### `text`

Free text.

| Field | Type | Meaning |
|---|---|---|
| `min_length`, `max_length` | whole numbers | The number of characters, at least and at most. |
| `format` | `"number"`, `"email"`, `"phone"`, `"date"`, `"zip"`, `"us_state"`, `"text_only"` | What the text must be. With `"number"`, the question is asked as a number. |
| `number` | `min`, `max`, `decimals` | The limits of a `"number"` format. |
| `default` | text | Text already in the box. |

A text question written in this format is asked as text whatever its wording
(a question read from a QSF that asks "how many ..." in a one-line box is asked
as a number, as before).

### `number`

One number, typed or on a slider.

| Field | Type | Meaning |
|---|---|---|
| `min`, `max` | numbers | The range (0 to 100 when absent). |
| `step` | number | The answer moves in steps of this size. |
| `decimals` | whole number | Decimal places. |
| `not_applicable` | `{"label": text}` | An answer that is not a number, such as "Not applicable". |

### `matrix`

A grid of statements (rows).

| Field | Type | Meaning |
|---|---|---|
| `mode` | `"per_row"` or `"per_cell"` | Required. One answer per row, or one per row and column. |
| `rows` | list of rows | Required. Each `id`, `label`, `media`, `show_if`, and `default` (a number for a number row, option IDs for a choice row). A bipolar row also has `left_label` and `right_label`. |
| `row_response` | a `choice` or a `number` | (`per_row`) What every row asks for: a choice of the scale's options, or a number (a slider for each statement). |
| `columns` | list of [options](#options) | (`per_cell`) The columns. |
| `cell_response` | a `text` or a `number` | (`per_cell`) What every cell asks for. |
| `constraints` | `[{"type": "sum_equals", "axis": "row", "total": 100}]` | (`per_cell` with number cells) Each row's cells add up to the total. |
| `randomization` | [randomization](#randomization) | How the rows are ordered. |

### `form`

Several labelled text boxes.

| Field | Type | Meaning |
|---|---|---|
| `fields` | list | Required. Each `id`, `label`, `media`, `show_if`, `default` (text already in the box). |
| `field_response` | a `text` response | The checks every box gets (length, format). |

### `ranking`

A strict order of items, 1 the highest.

| Field | Type | Meaning |
|---|---|---|
| `items` | list of [options](#options) | Required. |
| `min_ranked`, `max_ranked` | whole numbers | How many items must be ranked. |
| `default` | list of `{"item": ID, "rank": n}` | Ranks set before the twin answers. |
| `randomization` | [randomization](#randomization) | How the items are ordered. |

### `allocation`

An amount split across items (Qualtrics' constant sum).

| Field | Type | Meaning |
|---|---|---|
| `items` | list of [options](#options) | Required. |
| `total` | number | What the amounts add up to; 100 when absent. |
| `min`, `max` | numbers | The least and most one item may get. |
| `default` | object | Preset amounts by item ID. |
| `randomization` | [randomization](#randomization) | How the items are ordered. |

### `group`

Several grids side by side (Qualtrics' side by side), with `layout`
`"side_by_side"`. `fields` lists the columns: each has an `id`, a `label`, an
`export_name`, `media`, and a `response`, which is a [`matrix`](#matrix) with its
own statements and answer (a choice scale, or text). `randomization` orders each
column's options.

### Randomization

| `order` | Fields | Meaning |
|---|---|---|
| `shuffle` | `take` | A new random order for each twin; with `take`, only that many, drawn at random. |
| `random_reverse` | `consistent` | Reversed for a random half of twins; with `consistent`, the same half for every such question. |
| `positions` | `positions`, `shuffled`, `subset`, `subset_take`, `hidden` | One entry per shown position: an option ID pinned there, or `{"draw": "shuffled"}` / `{"draw": "subset"}` filled from those lists. `hidden` options are never shown. |
| `source` | `qualtrics` | A setting from a QSF with no form here, kept as written and reported; it is not run. |

### Presentation

`presentation.control` says how a response is shown. It never changes what is
asked, but the model is told some of it:

| Response | Controls |
|---|---|
| `choice` | `list` (default), `horizontal`, `columns`, `dropdown`, `select_box` |
| `text` | `single_line`, `multi_line` (default), `essay`, `password` |
| `number`, number rows | `slider` (default), `bar`, `stars`, `number_scale` |
| `matrix` with choice rows | `bipolar` (each row runs from its `left_label` to its `right_label`) |

## Conditions

A condition is a tree:

| `op` | Fields | Holds when |
|---|---|---|
| `selected`, `not_selected` | `question`, `option`, `loop` (`"all"` or `"any"` row of a repeated block) | The option was (not) selected. For a grid, `option` names a scale point, selected when any statement got it. |
| `displayed`, `not_displayed` | `question` | The question was (not) shown. |
| `eq`, `ne`, `lt`, `lte`, `gt`, `gte`, `contains`, `not_contains`, `matches`, `empty`, `not_empty` | `left` (a [reference](#text-and-piped-values): a variable, part of an answer), `right` (a value or text), `ignore_case` | The comparison holds. |
| `and`, `or` | `args` (two or more conditions) | All of them hold, or any. |
| `not` | `arg` | The condition does not hold. |
| `true`, `false` | | Always, or never. |
| `source` | `qualtrics` | A condition from a QSF with no form here (a device, quota or location test), kept as written. |

Any condition can carry a `description` for people. Conditions combined in a QSF
are read left to right with no precedence (`a or b and c` is `(a or b) and c`),
and the tree read from it says exactly that.

## Skip rules

| Field | Type | Meaning |
|---|---|---|
| `when` | a condition | Required: `selected` or `not_selected` of an option of this question. |
| `to` | text | Required: a later question of the same block, `"end_of_block"` or `"end_of_survey"`. |
| `id`, `description` | text | For people. |

## Blocks

A block of `kind` `"survey"` shows its questions together. A block of `kind`
`"conjoint"` or `"maxdiff"` is an experiment whose tasks are drawn for each twin
(see [Conjoint blocks](#conjoint-blocks) and [MaxDiff blocks](#maxdiff-blocks)).

| Field | Type | Meaning |
|---|---|---|
| `id` | text | Required. |
| `kind` | `"survey"` | Required. |
| `title` | text | The block's name. |
| `questions` | list of question IDs | Required. The block's questions, in order. |
| `randomization` | `{"order": "shuffle"}` | The block's questions in a random order for each twin. (`"source"` keeps a QSF setting with no form here.) |
| `repeat` | [repeat](#repeat) | The block runs once per row. |

### Repeat

A repeated block (Qualtrics' loop and merge) runs once per row. Its questions
are asked once per row, as `1_<question>`, `2_<question>`, ..., and `loop`
references read the row's values.

| Field | Type | Meaning |
|---|---|---|
| `rows` | list | Required. Each row has an `id` and `values` (text by field name). List them in the order they run: numbers in numeric order, otherwise alphabetically. |
| `order` | `"fixed"` or `"shuffle"` | `"shuffle"` runs the rows in a random order for each twin. |
| `take` | whole number | (`shuffle`) Run only this many rows, drawn at random. |

## Flow

The flow is a list of steps, run in order. Every step except `show_block` has an
`id`.

| `type` | Fields | What it does |
|---|---|---|
| `show_block` | `block` | Shows a block. A block shown twice is asked twice. |
| `set_variables` | `assignments` | Sets [variables](#variables): each `{"variable": name, "value": text}` (the text may pipe values), or `{"variable": name, "from_outside": true}` for a value supplied with the run. |
| `branch` | `when`, `then`, `else` | Runs `then` when the condition holds, and `else` (optional) when it does not. |
| `randomize` | `take`, `children`, `evenly` | Runs `take` of its steps, drawn at random for each twin. `evenly` is `true` when absent: each step is drawn about equally often across twins. With `"evenly": false`, each twin's steps are drawn on their own, so the counts vary. A survey read from a QSF keeps the randomizer's own setting (Qualtrics' "Evenly Present Elements"). |
| `group` | `children`, `title` | Runs its steps in order. |
| `end` | | Ends the survey. |

### Branch

`else` is decided after `then` has run, as a second branch on the opposite
condition. So a branch with `else` cannot set, in its `then` steps, a variable
its condition reads, and its condition cannot hold a `source` condition (which
has no opposite); write two branches instead.

## Conjoint blocks

A conjoint block describes products by their attributes and levels, and the
rules for combining them; the engine draws each twin's tasks from them. A task
shows a few alternatives and asks which one the twin would choose. Example:
[`examples/survey-format/coffee-conjoint.json`](../../examples/survey-format/coffee-conjoint.json).

| Field | Type | Meaning |
|---|---|---|
| `id`, `kind` (`"conjoint"`), `title` | | As for any block. |
| `attributes` | list | Required. Each has an `id`, a `name`, `levels`, an optional `type` (`"categorical"`, `"numeric"` or `"price"`) with `currency` or `unit`. A level has an `id`, a `label` (what the twin reads), an optional `value` (a number or price kept with the data), `weight` and `media` (images shown with the task). |
| `alternatives` | object | Required. `{"mode": "generic", "count": 3, "label": "Option {letter}"}`: alternatives labelled by position ("Option A", "Option B", ...; `{number}` gives 1, 2, ...). Or `{"mode": "fixed", "items": [...]}`: named alternatives, each with an `id`, a `label` and `fixed_levels` (attribute ID to the level it always has), such as one alternative per brand. |
| `constraints` | object | `applicability`: tables of which levels of two attributes may appear together (`row_attribute`, `row_levels`, `column_attribute`, `column_levels`, and `allowed`, a 0/1 table with one row per row level; every level of both attributes listed once). `forbidden_profiles`: combinations that never appear in one profile, each a map of attribute ID to level ID. |
| `design` | object | Required. How tasks are made; see below. |
| `presentation` | object | `instruction` (the question above the alternatives); `layout`: `"table"` (the default: a column per alternative and a row per attribute) or `"list"` (each alternative with a line per attribute); `attribute_order` and `alternative_order`: `{"mode": "fixed"}` (as listed) or `{"mode": "shuffle", "scope": "participant" or "task", "fixed_positions": {"brand": 1}}`. With `"participant"` (the default scope), one order is drawn for the twin and used in every task; with `"task"`, a new one each task. Every alternative of a task shows its attributes in the same order. |
| `response` | object | `{"type": "choice", "required": true, "outside_option": {"id": "none", "label": "None of these", "code": 0}}`: the twin picks one alternative, or the outside option when there is one. |
| `administration` | object | `{"unit": "block"}` (the default): all tasks of the block in one model call, each task with its own answer; drawing the tasks needs no earlier answer, so the block never makes a survey staged. `{"unit": "task"}`: each task in a model call of its own, after the earlier tasks and answers (the survey then runs staged). |

**A generated design** (`"mode": "generated"`) takes `tasks_per_participant`,
`generator` (`"random_valid_profiles"`, the only one), `duplicates`
(`"allow"`, `"within_task"`, the default, or `"within_participant"`) and
`max_attempts` (1000 when absent). For each alternative, every attribute it does
not fix gets a level drawn at random by the levels' weights (1 when absent; 0
never draws). A profile that breaks a constraint is drawn again in full, and a
task with two identical profiles (or, with `within_participant`, a profile the
twin already saw) is drawn again. Because rejected profiles are drawn again, the
weights set the draw, not how often each level ends up shown; the exposure file
shows what was shown. A design that cannot be drawn is refused when the survey
is read (a fixed alternative whose levels no constraint allows, fewer valid
profiles than distinct ones needed); a draw that still fails `max_attempts` times
stops the run with the reason, and no rule is loosened.

**A supplied design** (`"mode": "supplied"`) lists `versions`, each with `id`
and `tasks`; a task has an `id` and `alternatives`, each `{"id": ...,
"levels": {attribute ID: level ID}}` (for fixed alternatives, the alternative's
ID). Each twin gets one version: at random, or `"assignment": "by_index"` for
the twin's position in the sample. Supplied profiles are checked against the
attributes, fixed levels and constraints.

**What the twin reads.** Each task is a single-choice question,
`<block>_t<n>` (`coffee_t3`): the instruction, then a table with a column per
alternative, in the order shown, and a row per attribute, in the order drawn:

```
Which of these coffee shops would you go to?

| | Dunkin | McDonald's | Starbucks |
|---|---|---|---|
| Brand | Dunkin | McDonald's | Starbucks |
| Seating | Seating only | Seating and Wi-Fi | Seating only |
| Price of a medium latte | 4.50 dollars | 4.50 dollars | 4.50 dollars |
| Promotion | No promotion | 20% off | 20% off |
```

A `|` in a label is written `\|`, and a label's lines are joined with "; ", so
each stays in its cell. With `"layout": "list"`, each alternative is its label
followed by a line per attribute (`- Brand: Dunkin`). A level's picture is sent
in its cell (after its line in a list), labelled by the alternative and the
level ("Dunkin, Brand: Dunkin"); a picture in two cells is sent twice. The task's
record (`Results.audit`) keeps the layout and the attribute order, so the row
each attribute was in is known.

The options are the alternatives' labels and the outside option. The answer
records the chosen option's ID, which names the alternative; the run records
the profile behind every option of every task (`Results.exposures`).

**Drawing.** A twin's tasks depend only on the run's seed, the twin and the
block: the same twin gets the same tasks in a static or a staged run, in a
preview, at every stage and in a rerun. Nothing is drawn again.

## MaxDiff blocks

A MaxDiff block shows a few items at a time from a pool and asks for the best
and the worst of them. Example:
[`examples/survey-format/feature-maxdiff.json`](../../examples/survey-format/feature-maxdiff.json).

| Field | Type | Meaning |
|---|---|---|
| `id`, `kind` (`"maxdiff"`), `title` | | As for any block. |
| `items` | list | Required, at least two. Each has an `id`, a `label`, an optional `code` (exported for it) and `media`. |
| `design` | object | Required; see below. |
| `presentation` | object | `instruction`, and `item_order`: `{"mode": "fixed"}` (the pool's order), `{"mode": "shuffle", "scope": "task"}` (a new order in each task) or `{"mode": "shuffle", "scope": "participant"}` (one random ranking of the pool for each twin, each task's items in that ranking), with `fixed_positions` (an item shown at that position whenever its task includes it). |
| `response` | object | Required. `{"type": "best_worst", "required": true, "best_label": "Most important", "worst_label": "Least important"}`. |
| `anchor` | object | A follow-up after each task about the same items: `id`, `content` and a single-choice `response` (such as "All / Some / None of these are important to me"). |
| `administration` | object | `{"unit": "block"}` (the default) or `{"unit": "task"}`, as for conjoint. |

**A generated design** takes `tasks_per_participant`, `items_per_task`,
`duplicate_tasks` (`"forbid"`, the default: no subset twice for a twin, in any
order; or `"allow"`), `max_attempts`, and a `generator`:

- `"uniform_subsets"`: each task draws `items_per_task` different items, every
  subset equally likely. No balance is promised.
- `"balanced_subsets"`: each task takes the items the twin has seen least, ties
  broken at random, preferring items seen least together with those already
  taken. Within a twin, how often items appear differs by at most one; pair
  balance is the best this rule finds, not a proven optimum.

**A supplied design** lists `versions`, each with `id` and `tasks` (each a list of
item IDs), assigned as for conjoint.

**What the twin reads.** Each task is the instruction with the items, one per
line (`features_t2`, descriptive text), and two single choices of the same items:
best (`features_t2_best`) and worst (`features_t2_worst`), asked together, with
the rule that they are two different items. The rule is checked after the
answers come back, never by asking again during the run: a pair that names one
item twice has both answers recorded as skipped (the item given is kept in
`raw`), never corrected, and a warning says how many such pairs there are.
`Results.rerun_flagged` asks them again with the run's other flagged answers,
the rule included; a repair that names the item its pair's other answer names
is not stored, and the answer stays flagged. The follow-up, when there is one,
is `features_t2_anchor`.

## Current limitations

What the format can say but the engine does not do yet, and what is left for
later on purpose:

- **Conjoint responses other than a choice.** A conjoint task asks the twin to
  choose one alternative (or the outside option). Rating each profile,
  prices calculated from the levels shown (conditional pricing), and a purchase
  question about the chosen profile (dual choice) are the next milestone; a
  block cannot ask for them yet.
- **Other conjoint designs.** Partial profiles, holdout tasks, adaptive
  designs, and balance objectives for conjoint (beyond rejecting invalid and
  duplicate profiles) are not supported.
- **MaxDiff balance.** `balanced_subsets` keeps each twin's item counts within
  one of each other; pair balance is the best a simple rule finds, not an
  optimum, and balance across twins is not a goal.
- **Qualtrics conjoint and MaxDiff projects.** A QSF does not hold their
  designs, so they cannot be read; they will be, once there are source files
  that hold the design and its settings. Write such a study as a conjoint or
  MaxDiff block instead.
- **JavaScript.** Question JavaScript in a QSF is not run (it is reported by the
  check); custom scripts are left for a later version.

## Variables

A variable holds text: set it with `set_variables`, read it with piped text or
conditions. Declaring it in `variables` is optional (`id`, `type`, `description`,
`from_outside`) but documents it, and a variable that is read but never set or
declared is reported.

## A survey read from a QSF

`read_qsf` reads every QSF into this format, and the survey runs from it. On
every test and study QSF of the library, the form compiles back to exactly what
the QSF reader gave before; `scripts/format_roundtrip.py` checks that, and
`scripts/format_engine_check.py` checks that the fields on this page alone (with
everything under `source` removed) give the same prompts and the same answers.

A QSF has settings with no field here; they are kept under `source`, so the
survey still exports as before:

- the survey's `source.metadata` (Qualtrics' metadata) and `source.web_services`;
- a question's `source.settings` (its settings as read), `source.recode_values`,
  `source.randomization` and the defaults and per-option logic as read. A
  setting is written back as read for as long as it still means what the
  question's fields say; once a field changes, the setting is written anew;
- a block's `source.type` and `source.options` (its lock, its visibility).

A question's text is kept as the QSF wrote it, HTML included (`"format":
"html"`). It reaches the model as HTML text written in this format does (see
[Content](#content)): its markup removed, its paragraphs, line breaks and list
items kept, and a table with every row and cell. The template form of the survey
(`Survey.template`, and `question_logic.json` in the exports) has the same text.

| Qualtrics | Standard form |
|---|---|
| Multiple choice (all single and multiple selectors) | `choice` |
| Text entry (single line, multi line, essay, password) | `text` |
| Text entry form | `form` |
| Matrix (Likert, bipolar, profile, ...) | `matrix`, `per_row` with a `choice` |
| Matrix with the MaxDiff selector | `matrix`, `per_row` with a `choice`: each item is rated on the best/worst columns, and the rule of one best and one different worst is not enforced (a MaxDiff block enforces it) |
| Matrix text entry | `matrix`, `per_cell` with `text` cells |
| Matrix constant sum | `matrix`, `per_cell` with `number` cells and a sum |
| Slider, bar, stars, number scale | `matrix`, `per_row` with a `number` (one statement: a lone number) |
| Constant sum | `allocation` |
| Rank order | `ranking` |
| Side by side | `group` |
| Descriptive text, graphic | an instruction (no response) |
| Timing, meta info, captcha | `kind` `timing`, `meta_info`, `captcha` |
| Net promoter, heat map, signature, file upload, ... | `kind` `unsupported` (never asked) |
| Display logic, skip logic, option display logic | `show_if`, `skip`, the option's `show_if` |
| Embedded data, branch, randomizer, group, end of survey | `set_variables`, `branch`, `randomize`, `group`, `end` |
| Loop and merge (static) | a block's `repeat` |
| Question randomization in a block | a block's `randomization` |

Not read into this format: Qualtrics' conjoint and MaxDiff projects (a QSF does
not hold their designs; write them as conjoint and MaxDiff blocks), JavaScript, loops over a question's answers (only the
loop's own table is run), quotas, authenticators and web services (kept under
`source`), and the Qualtrics display settings that never reach the model.

## Writing a survey in code

A survey is plain JSON, so any language can write one. In Python:

```python
import surveytwin as st

survey = st.read_survey({
    "schema_version": "0.1",
    "id": "quick",
    "questions": [{
        "id": "q1",
        "content": [{"type": "text", "text": "How old is your car, in years?"}],
        "response": {"type": "number", "min": 0, "max": 40, "step": 1},
    }],
    "blocks": [{"id": "b", "kind": "survey", "questions": ["q1"]}],
    "flow": [{"type": "show_block", "block": "b"}],
})
print(survey.questions["q1"].type)
```

```
Slider
```

`Survey.standard` gives any survey in this format: one read from a QSF, one made
with the builder, or one after `drop_blocks`, as it now runs.
