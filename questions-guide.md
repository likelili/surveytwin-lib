# Question types and configuration

Use this guide to create questions with `Survey.add_question` or write their standard JSON definitions. For running a study, follow the [README](README.md#step-by-step).

## Index

- [Start a survey](#start-a-survey)
- [Instructions](#instructions)
- [Single choice](#single-choice)
- [Multiple choice](#multiple-choice)
- [Text](#text)
- [Numbers and scales](#numbers-and-scales)
- [Choice grids](#choice-grids)
- [Numeric grids](#numeric-grids)
- [Text grids](#text-grids)
- [Sum grids](#sum-grids)
- [Forms](#forms)
- [Rankings](#rankings)
- [Allocations](#allocations)
- [Side-by-side grids](#side-by-side-grids)
- [Option IDs, codes and write-ins](#option-ids-codes-and-write-ins)
- [Pictures and tables](#pictures-and-tables)
- [Display conditions and option order](#display-conditions-and-option-order)
- [Blocks and flow](#blocks-and-flow)
- [Conjoint](#conjoint)
- [MaxDiff](#maxdiff)
- [Save the definition](#save-the-definition)

## Start a survey

```python
import surveytwin as st

survey = st.new_survey("Question examples")
```

The examples below add questions to this survey. Assign each edit's return value to keep it. The first question creates a `main` block; use `block="BLOCK_ID"` when adding to a particular block.

Lists can contain labels or objects with explicit IDs. Labels receive IDs `"1"`, `"2"`, and so on. Use explicit IDs when writing conditions or matching responses to an external dataset.

## Instructions

Use `instruction` for text or pictures that require no answer.

```python
survey = survey.add_question(
    "instruction", "Please answer based on your usual habits.", id="intro",
)
```

In standard JSON, omit `response` from the question.

## Single choice

Use `single_choice` for one option.

```python
survey = survey.add_question(
    "single_choice", "Which drink do you prefer?", id="drink",
    options=[{"id": "coffee", "label": "Coffee"}, {"id": "tea", "label": "Tea"}],
    required=True,
)
```

Standard response: `type="choice"`, `mode="single"`. Set `control="dropdown"`, `"horizontal"` or `"list"` for presentation. Use `default=["coffee"]` for an initial selection.

## Multiple choice

Use `multiple_choice` for several selections.

```python
survey = survey.add_question(
    "multiple_choice", "Where do you buy coffee?", id="places",
    options=["Supermarket", "Café", "Online"], min_selected=1, max_selected=2,
)
```

Standard response: `type="choice"`, `mode="multiple"`. Set `min_selected` and `max_selected` to control selection counts.

## Text

Use `text` for an open answer.

```python
survey = survey.add_question(
    "text", "What do you like about your preferred drink?", id="reason",
    max_length=200, control="multi_line",
)
```

Standard response: `type="text"`. Available settings include `min_length`, `max_length`, `default` and `format`. Use `control="single_line"` for a short box.

## Numbers and scales

Use `number` for a numeric answer or scale.

```python
survey = survey.add_question(
    "number", "How many cups do you drink per day?", id="cups",
    min=0, max=12, step=1, decimals=0,
)
```

Standard response: `type="number"`. Set `min`, `max`, `step` and `decimals`. Numeric presentation controls include `slider`, `bar`, `stars` and `number_scale`.

For a verbal scale, use `single_choice` with a preset:

```python
survey = survey.add_question(
    "single_choice", "I enjoy trying new drinks.", id="agreement",
    options="agreement_5",
)
```

See `st.builder.SCALE_PRESETS` for preset names.

## Choice grids

Use `grid` for one scale choice per row.

```python
survey = survey.add_question(
    "grid", "Rate each feature.", id="importance",
    rows=["Price", "Taste", "Convenience"],
    scale=["Not important", "Somewhat important", "Very important"],
)
```

Standard response: `type="matrix"`, `mode="per_row"`, with a choice `row_response`. Use a preset name such as `scale="agreement_5"` in place of labels.

## Numeric grids

Use `number_grid` for one number per row.

```python
survey = survey.add_question(
    "number_grid", "Rate each drink from 0 to 10.", id="ratings",
    rows=["Coffee", "Tea"], min=0, max=10, step=1,
)
```

Standard response: a per-row matrix with a numeric `row_response`.

## Text grids

Use `text_grid` for a text answer in each row and column.

```python
survey = survey.add_question(
    "text_grid", "Describe each drink.", id="descriptions",
    rows=["Coffee", "Tea"], columns=["What you like", "What you dislike"],
    max_length=100,
)
```

Standard response: `type="matrix"`, `mode="per_cell"`, with a text `cell_response`.

## Sum grids

Use `sum_grid` to allocate a total across cells in each row.

```python
survey = survey.add_question(
    "sum_grid", "Allocate 100 points for each period.", id="spending",
    rows=["Weekdays", "Weekends"], columns=["Coffee", "Tea", "Other"], total=100,
)
```

Standard response: a numeric per-cell matrix with a sum constraint. Rows sum to `total` by default; use `axis="column"` to sum down columns.

## Forms

Use `form` for several labeled text fields.

```python
survey = survey.add_question(
    "form", "Describe your usual café.", id="cafe",
    fields=["Location", "Favorite drink"], max_length=80,
)
```

Standard response: `type="form"`. `field_response` in JSON specifies the text settings shared by the fields.

## Rankings

Use `ranking` to order items. Rank 1 is highest.

```python
survey = survey.add_question(
    "ranking", "Rank these features from most to least important.", id="rank",
    items=["Price", "Taste", "Convenience"],
)
```

Standard response: `type="ranking"`. For a partial ranking, set `min_ranked` and `max_ranked`. A valid ranking uses consecutive ranks without ties.

## Allocations

Use `allocation` to split a total across items.

```python
survey = survey.add_question(
    "allocation", "Split your monthly drink budget.", id="budget",
    items=["Groceries", "Cafés", "Equipment"], total=100, min=0, max=100,
)
```

Standard response: `type="allocation"`. Set the total and optional bounds for each item.

## Side-by-side grids

Use `side_by_side` for shared rows with a separate scale in each column.

```python
survey = survey.add_question(
    "side_by_side", "Rate each drink.", id="comparison", rows=["Coffee", "Tea"],
    columns=[
        {"label": "Liking", "scale": ["Low", "Medium", "High"]},
        {"label": "Frequency", "scale": ["Rarely", "Sometimes", "Often"]},
    ],
)
```

Standard response: `type="group"`, `layout="side_by_side"`. For more detailed column definitions, provide each field's matrix response in standard JSON.

## Option IDs, codes and write-ins

Specify `id` for references, `label` for displayed text and `code` for exported values. Add `write_in=True` to collect text alongside an option.

```python
survey = survey.add_question(
    "single_choice", "Choose a drink.", id="custom_drink",
    options=[
        {"id": "coffee", "label": "Coffee", "code": 10},
        {"id": "other", "label": "Other", "code": 99, "write_in": True},
    ],
)
```

Option media is specified with `media=[{"source": "images/coffee.png"}]`. Options, rows and form fields can also carry a display condition (`show_if`), and rows and form fields an initial value (`default`); use the format reference for each type's fields.

## Pictures and tables

Use `image` for a picture following the question text:

```python
survey = survey.add_question(
    "single_choice", "Would you buy this product?", id="ad_reaction",
    options=["Yes", "No"], image="images/ad.png",
)
```

Provide the file before running this example. Local paths resolve from the survey file's folder, the folder passed to `new_survey`, or the current folder. For an in-memory definition, use `read_survey(definition, folder="study-folder")`.

Use `content` to combine text, pictures and tables:

```python
survey = survey.add_question(
    "single_choice", id="price_reaction", options=["Yes", "No"],
    content=[
        {"type": "table", "corner": "Drink", "columns": [{"id": "price", "label": "Price"}],
         "rows": [{"id": "coffee", "label": "Coffee", "cells": {"price": "$3"}},
                  {"id": "tea", "label": "Tea", "cells": {"price": "$2"}}]},
        {"type": "text", "text": "Are these prices fair?"},
    ],
)
```

## Display conditions and option order

Use `show_if` to show a question after a selected answer:

```python
survey = survey.add_question(
    "text", "Which coffee do you usually buy?", id="coffee_type",
    show_if={"op": "selected", "question": "drink", "option": "coffee"},
)
```

Put conditions on an option or matrix row to control its visibility. Answer-dependent conditions make the survey staged.

Use `randomization={"order": "shuffle"}` to shuffle choices or rows:

```python
survey = survey.update_question("drink", randomization={"order": "shuffle"})
```

For text that uses earlier answers, author a text `parts` list with an answer reference. See [text and piped values](docs/reference/survey-format.md#text-and-piped-values).

## Blocks and flow

Add a block, then place questions in it:

```python
survey = survey.add_block("followup", title="Follow-up")
survey = survey.add_question("text", "Any other comments?", id="comments", block="followup")
```

Use `move_question`, `swap_questions`, `group_into_block` and the other edit methods to organize existing questions. For branching, repeated blocks, variable assignments and randomized arms, edit the standard definition and read it with `read_survey`.

See [blocks and flow](docs/reference/survey-format.md#blocks) and [survey editing](docs/reference/survey.md#editing-a-survey).

## Conjoint

Write a block with `kind="conjoint"`. Define attributes and levels, alternatives and a task design. This example generates three tasks with two alternatives:

```python
conjoint_definition = {
    "schema_version": "0.1", "id": "coffee_choices", "questions": [],
    "blocks": [{
        "id": "coffee", "kind": "conjoint",
        "attributes": [
            {"id": "price", "name": "Price", "levels": [
                {"id": "low", "label": "$2"}, {"id": "high", "label": "$4"}]},
            {"id": "origin", "name": "Origin", "levels": [
                {"id": "local", "label": "Local"}, {"id": "imported", "label": "Imported"}]},
        ],
        "alternatives": {"mode": "generic", "count": 2, "label": "Option {letter}"},
        "design": {"mode": "generated", "generator": "random_valid_profiles",
                   "tasks_per_participant": 3},
        "presentation": {"instruction": "Which coffee would you buy?"},
        "response": {"type": "choice", "required": True},
    }],
    "flow": [{"type": "show_block", "block": "coffee"}],
}
conjoint = st.read_survey(conjoint_definition)
```

Configure the block's main settings:

- `attributes`: levels, optional numeric/price values, weights and pictures.
- `alternatives`: generic alternatives or named alternatives with fixed levels.
- `constraints`: allowed level combinations and forbidden profiles.
- `design`: generated profiles or supplied task versions, task counts and duplicate rules.
- `presentation`: table/list layout and attribute/alternative order.
- `response`: a single choice and an optional outside option.
- `administration`: all tasks together (`unit="block"`, the default) or one task per call (`unit="task"`).

Generated designs sample valid profiles; they do not optimize design efficiency or estimate utilities. For a supplied design, set `design.mode="supplied"` and provide versions with tasks and alternative-level assignments.

For a full example with fixed brands and constraints, use [coffee-conjoint.json](examples/survey-format/coffee-conjoint.json). See [conjoint fields](docs/reference/survey-format.md#conjoint-blocks) for the complete configuration.

## MaxDiff

Write a block with `kind="maxdiff"`. Define the item pool, subset design and best/worst response:

```python
maxdiff_definition = {
    "schema_version": "0.1", "id": "shopping_features", "questions": [],
    "blocks": [{
        "id": "features", "kind": "maxdiff",
        "items": [
            {"id": "price", "label": "Low price"},
            {"id": "quality", "label": "High quality"},
            {"id": "delivery", "label": "Fast delivery"},
            {"id": "returns", "label": "Easy returns"},
        ],
        "design": {"mode": "generated", "generator": "balanced_subsets",
                   "tasks_per_participant": 4, "items_per_task": 3},
        "presentation": {"instruction": "Choose the most and least important feature."},
        "response": {"type": "best_worst", "required": True,
                     "best_label": "Most important", "worst_label": "Least important"},
    }],
    "flow": [{"type": "show_block", "block": "features"}],
}
maxdiff = st.read_survey(maxdiff_definition)
```

Use `uniform_subsets` for random subsets or `balanced_subsets` for an item-balancing heuristic. Set `items_per_task`, `tasks_per_participant` and `duplicate_tasks`. Supplied versions are also supported.

Each task collects different best and worst items. Add an `anchor` for a follow-up question about the same items. Set `administration.unit="task"` for one task per call.

Run experimental surveys with the same configuration used in the README:

```python
experiment_plan = st.plan(conjoint, config)
print(experiment_plan.preview_prompt())
experiment_results = st.run(experiment_plan)
print(experiment_results.exposures)
```

Use `maxdiff` in place of `conjoint` for the other design. `save` writes `conjoint_exposures.csv` or `maxdiff_exposures.csv`, recording what each twin saw and selected. Preference-model estimation is outside the package.

See [feature-maxdiff.json](examples/survey-format/feature-maxdiff.json) and [MaxDiff fields](docs/reference/survey-format.md#maxdiff-blocks) for more settings. Define these blocks directly; QSF import does not reconstruct native scripted experiment designs.

## Save the definition

Write the standard definition to JSON, then load it with `read_survey`:

```python
import json
from pathlib import Path

Path("study.json").write_text(json.dumps(survey.standard, indent=2), encoding="utf-8")
```

Keep referenced picture files with the study when sharing this JSON. Saved simulation results retain the picture bytes used by the run.
