"""The standard survey format, version 0.1: every object type, declared once.

A survey has four parts:

- **content**: what a question shows (text, images, tables), with piped values;
- **responses**: what it asks for (a choice, text, a number, a grid, a ranking,
  an allocation, fields side by side, a best/worst pick);
- **flow**: how it is administered (blocks, conditions, variables, randomizers,
  repeats, endings);
- **experiments**: conjoint and MaxDiff blocks, configured by their pools and
  rules; the engine generates each participant's tasks.

``Survey`` below is the root. ``check_structure`` validates a parsed JSON value
against these declarations; ``json_schema`` returns them as a JSON Schema.
"""

from __future__ import annotations

from typing import Any

from .schema import (
    ID_PATTERN,
    AnyValue,
    Bool,
    Const,
    Either,
    Field,
    Lazy,
    ListOf,
    MapOf,
    Num,
    Obj,
    Problem,
    Scalar,
    Str,
    Tagged,
    to_json_schema,
)

SCHEMA_VERSION = "0.1"


def F(type_: Any, required: bool = False, doc: str = "") -> Field:
    return Field(type_, required, doc)


ID = Str(pattern=ID_PATTERN, nonempty=True)
NAME = Str(nonempty=True)
Int = Num(integer=True)
PositiveInt = Num(integer=True, minimum=1)

# ---------------------------------------------------------------- piped values

ANSWER_FIELDS = (
    "selected_choices",
    "selected_choices_recode",
    "selected_choices_text_entry",
    "unselected_choices",
    "displayed_choices",
    "undisplayed_choices",
    "all_choices",
    "selected_count",
    "choice_label",
    "choice_recode",
    "text_entry",
    "number_entry",
    "question_text",
)

Ref = Tagged(
    "Ref",
    "scope",
    {
        "variable": Obj(
            "VariableRef",
            {"scope": F(Const("variable"), True), "variable": F(NAME, True, "The variable's name.")},
            doc="A survey variable (Qualtrics embedded data): ${e://Field/name}.",
        ),
        "answer": Obj(
            "AnswerRef",
            {
                "scope": F(Const("answer"), True),
                "question": F(ID, True, "The question answered earlier."),
                "field": F(Str(enum=ANSWER_FIELDS), True, "Which part of the answer."),
                "item": F(Str(nonempty=True), doc="An option, statement or field ID, for fields that name one."),
            },
            doc="Part of an earlier answer: ${q://QID/...}.",
        ),
        "loop": Obj(
            "LoopRef",
            {
                "scope": F(Const("loop"), True),
                "field": F(NAME, doc="A column of the repeat table."),
                "value": F(Str(enum=("current_index", "total")), doc="The repeat's position or length."),
            },
            doc="A value of the current repeat row: ${lm://Field/1}, ${lm://CurrentLoopNumber}, ${lm://TotalLoops}.",
        ),
        "random": Obj(
            "RandomRef",
            {
                "scope": F(Const("random"), True),
                "kind": F(Str(enum=("int", "float")), True),
                "min": F(Num(), True),
                "max": F(Num(), True),
            },
            doc="A random number drawn once per participant and place: ${rand://int/1:10}.",
        ),
        "source": Obj(
            "SourceRef",
            {"scope": F(Const("source"), True), "expression": F(Str(nonempty=True), True)},
            doc="A piped expression of the source platform with no standard form, kept as written.",
        ),
    },
    doc="A piped value.",
)

TextPart = Obj("TextPart", {"text": F(Str(), True)}, doc="Literal text.")
RefPart = Obj("RefPart", {"ref": F(Ref, True)}, doc="A piped value.")
Part = Either(
    "TextPart or RefPart",
    [TextPart, RefPart],
    pick=lambda v: (RefPart if "ref" in v else TextPart) if isinstance(v, dict) else None,
)
Template = Obj("Template", {"parts": F(ListOf(Part, 1), True)}, doc="Text with piped values, in order.")
TEXT_VALUE_DOC = "Text, or {\"parts\": [...]} when it pipes values."
TextValue = Either(
    "text or {parts}",
    [Str(), Template],
    doc=TEXT_VALUE_DOC,
    pick=lambda v: Str() if isinstance(v, str) else (Template if isinstance(v, dict) else None),
)

# ---------------------------------------------------------------- media and content

PICTURE_SOURCE_DOC = (
    "A web link, a data: address, or the path of a PNG, JPEG, WEBP or GIF file: absolute, "
    "or relative to the folder of the survey's JSON file."
)

Media = Obj(
    "Media",
    {
        "source": F(Str(nonempty=True), True, PICTURE_SOURCE_DOC),
        "alt": F(Str()),
        "title": F(Str()),
    },
    doc="An image.",
)

TableColumn = Obj("TableColumn", {"id": F(ID, True), "label": F(TextValue, True)})
TableRow = Obj(
    "TableRow",
    {
        "id": F(ID, True),
        "label": F(TextValue),
        "cells": F(MapOf(TextValue), True, "Cell text by column ID."),
    },
)

Content = Tagged(
    "Content",
    "type",
    {
        "text": Obj(
            "TextContent",
            {
                "type": F(Const("text"), True),
                "text": F(TextValue, True),
                "format": F(Str(enum=("plain", "html")), doc="'plain' (the default) or 'html' as exported by a platform."),
            },
            doc="Text, kept with its line and paragraph breaks.",
        ),
        "image": Obj(
            "ImageContent",
            {
                "type": F(Const("image"), True),
                "source": F(Str(nonempty=True), True, PICTURE_SOURCE_DOC),
                "alt": F(Str()),
                "title": F(Str()),
            },
            doc="An image shown with the question.",
        ),
        "table": Obj(
            "TableContent",
            {
                "type": F(Const("table"), True),
                "columns": F(ListOf(TableColumn, 1), True),
                "rows": F(ListOf(TableRow, 1), True),
                "caption": F(TextValue),
                "corner": F(TextValue, doc="The label above the row labels."),
            },
            doc="A table shown as stimulus (not a response grid): every cell keeps its row and column.",
        ),
    },
    doc="Something a question shows.",
)

# ---------------------------------------------------------------- conditions

COMPARE_OPS = (
    "eq",
    "ne",
    "lt",
    "lte",
    "gt",
    "gte",
    "contains",
    "not_contains",
    "matches",
    "empty",
    "not_empty",
)

_CONDITION = Lazy(lambda: Condition)

ChoiceCondition = Obj(
    "ChoiceCondition",
    {
        "op": F(Str(enum=("selected", "not_selected")), True),
        "question": F(ID, True),
        "option": F(ID, True, "The option ID (for a grid, a scale point's ID: selected when any statement got it)."),
        "loop": F(Str(enum=("all", "any")), doc="In a repeated block: every row ('all') or some row ('any')."),
        "description": F(Str(), doc="How the source platform describes this condition."),
    },
    doc="Whether an option was selected.",
)
DisplayCondition = Obj(
    "DisplayCondition",
    {
        "op": F(Str(enum=("displayed", "not_displayed")), True),
        "question": F(ID, True),
        "option": F(ID),
        "description": F(Str()),
    },
    doc="Whether a question (or one of its options) was shown.",
)
Comparison = Obj(
    "Comparison",
    {
        "op": F(Str(enum=COMPARE_OPS), True),
        "left": F(Ref, True, "A variable, part of an answer, or a repeat value."),
        "right": F(
            Either("value", [Scalar(), Template], pick=lambda v: Template if isinstance(v, dict) else Scalar()),
            doc="The value compared with; omitted for 'empty' and 'not_empty'.",
        ),
        "ignore_case": F(Bool()),
        "description": F(Str()),
    },
    doc="A comparison of a value with another.",
)
ConditionGroup = Obj(
    "ConditionGroup",
    {"op": F(Str(enum=("and", "or")), True), "args": F(ListOf(_CONDITION, 1), True)},
    doc="All ('and') or any ('or') of several conditions.",
)
NotCondition = Obj("NotCondition", {"op": F(Const("not"), True), "arg": F(_CONDITION, True)})
ConstantCondition = Obj(
    "ConstantCondition",
    {"op": F(Str(enum=("true", "false")), True), "description": F(Str())},
    doc="Always true or always false ('If True' / 'If False').",
)
SourceCondition = Obj(
    "SourceCondition",
    {
        "op": F(Const("source"), True),
        "qualtrics": F(AnyValue(), True, "The condition as the source platform wrote it."),
        "description": F(Str()),
    },
    doc="A condition with no standard form (a device, quota or location test), kept as written.",
)
Condition = Tagged(
    "Condition",
    "op",
    {
        "and": ConditionGroup,
        "or": ConditionGroup,
        "not": NotCondition,
        "selected": ChoiceCondition,
        "not_selected": ChoiceCondition,
        "displayed": DisplayCondition,
        "not_displayed": DisplayCondition,
        **{op: Comparison for op in COMPARE_OPS},
        "true": ConstantCondition,
        "false": ConstantCondition,
        "source": SourceCondition,
    },
    doc="A condition, as a tree. Combined conditions are read left to right.",
)

SkipRule = Obj(
    "SkipRule",
    {
        "id": F(ID),
        "when": F(Condition, True),
        "to": F(Str(nonempty=True), True, "A later question's ID, 'end_of_block' or 'end_of_survey'."),
        "description": F(Str()),
        "source": F(AnyValue()),
    },
    doc="After answering, jump ahead when a condition holds.",
)

# ---------------------------------------------------------------- responses

Required = Either(
    "true, false or 'request'",
    [Bool(), Const("request")],
    pick=lambda v: Bool() if isinstance(v, bool) else Const("request"),
)

Option = Obj(
    "Option",
    {
        "id": F(ID, True),
        "label": F(TextValue, True),
        "code": F(Scalar(), doc="The value exported for this option (a recode); the ID when absent."),
        "media": F(ListOf(Media)),
        "write_in": F(Bool(), doc="The option has a text box for the respondent's own words."),
        "show_if": F(_CONDITION, doc="The option is shown only when this holds."),
    },
    doc="An answer option, a scale point, a ranked item or an allocation item.",
)

Draw = Obj("Draw", {"draw": F(Str(enum=("shuffled", "subset")), True)}, doc="A position filled by a draw.")
Slot = Either(
    "an option ID or {draw}",
    [ID, Draw],
    pick=lambda v: ID if isinstance(v, str) else (Draw if isinstance(v, dict) else None),
)

Randomization = Obj(
    "Randomization",
    {
        "order": F(
            Str(enum=("shuffle", "random_reverse", "positions", "source")),
            True,
            "'shuffle': a random order (of 'take' drawn at random, when set); 'random_reverse': "
            "reversed for a random half of participants; 'positions': each position pinned or drawn; "
            "'source': a source platform setting with no standard form, kept and reported, not run.",
        ),
        "take": F(PositiveInt, doc="('shuffle') Show only this many, drawn at random."),
        "consistent": F(Bool(), doc="('random_reverse') Reverse every such question for the same participants."),
        "positions": F(ListOf(Slot), doc="('positions') One entry per shown position: a pinned option, or a draw."),
        "shuffled": F(ListOf(ID), doc="('positions') The options drawn into 'shuffled' slots, in random order."),
        "subset": F(ListOf(ID), doc="('positions') The options a 'subset' slot draws from."),
        "subset_take": F(PositiveInt, doc="('positions') How many the 'subset' draws."),
        "hidden": F(ListOf(ID), doc="('positions') Options never shown."),
        "qualtrics": F(AnyValue(), doc="('source') The setting as the source platform wrote it."),
    },
    doc="How options (or rows) are ordered for each participant.",
)

TEXT_FORMATS = (
    "number",
    "email",
    "phone",
    "date",
    "zip",
    "us_state",
    "text_only",
)

TextResponse = Obj(
    "TextResponse",
    {
        "type": F(Const("text"), True),
        "required": F(Required),
        "min_length": F(Num(integer=True, minimum=0)),
        "max_length": F(PositiveInt),
        "format": F(Str(enum=TEXT_FORMATS), doc="A named content check."),
        "number": F(
            Obj("NumberCheck", {"min": F(Num()), "max": F(Num()), "decimals": F(Num(integer=True, minimum=0))}),
            doc="The limits of a 'number' format.",
        ),
        "default": F(TextValue, doc="Text already in the box."),
    },
    doc="Free text.",
)

NumberResponse = Obj(
    "NumberResponse",
    {
        "type": F(Const("number"), True),
        "required": F(Required),
        "min": F(Num()),
        "max": F(Num()),
        "step": F(Num(minimum=0)),
        "decimals": F(Num(integer=True, minimum=0)),
        "not_applicable": F(Obj("NotApplicable", {"label": F(TextValue, True)}), doc="An answer that is not a number."),
    },
    doc="A number (typed, or on a slider, scale or stars).",
)

ChoiceResponse = Obj(
    "ChoiceResponse",
    {
        "type": F(Const("choice"), True),
        "mode": F(Str(enum=("single", "multiple")), True),
        "options": F(ListOf(Option, 1), True),
        "required": F(Required),
        "min_selected": F(Num(integer=True, minimum=0)),
        "max_selected": F(PositiveInt),
        "default": F(ListOf(ID), doc="Options selected before the respondent answers."),
        "randomization": F(Randomization),
    },
    doc="One option, or several.",
)

_RESPONSE = Lazy(lambda: Response)

MatrixRow = Obj(
    "MatrixRow",
    {
        "id": F(ID, True),
        "label": F(TextValue, True),
        "media": F(ListOf(Media)),
        "show_if": F(_CONDITION),
        "left_label": F(TextValue, doc="A bipolar row's left end."),
        "right_label": F(TextValue, doc="A bipolar row's right end."),
        "default": F(
            Either("a value or option IDs", [Scalar(), ListOf(ID)], pick=lambda v: ListOf(ID) if isinstance(v, list) else Scalar()),
            doc="The row's preset answer: a number for a number row, option IDs for a choice row.",
        ),
    },
    doc="A statement of a grid.",
)
MatrixColumn = Obj(
    "MatrixColumn",
    {
        "id": F(ID, True),
        "label": F(TextValue, True),
        "code": F(Scalar()),
        "media": F(ListOf(Media)),
        "show_if": F(_CONDITION),
    },
    doc="A column of a grid that asks one answer per cell.",
)
SumConstraint = Obj(
    "SumConstraint",
    {
        "type": F(Const("sum_equals"), True),
        "axis": F(Str(enum=("row", "column")), True),
        "total": F(Num(), True),
    },
)

MatrixResponse = Obj(
    "MatrixResponse",
    {
        "type": F(Const("matrix"), True),
        "mode": F(Str(enum=("per_row", "per_cell")), True, "One answer per row, or one per row and column."),
        "rows": F(ListOf(MatrixRow, 1), True),
        "row_response": F(_RESPONSE, doc="('per_row') The response every row takes."),
        "columns": F(ListOf(MatrixColumn), doc="('per_cell') The columns."),
        "cell_response": F(_RESPONSE, doc="('per_cell') The response every cell takes."),
        "constraints": F(ListOf(SumConstraint)),
        "required": F(Required),
        "randomization": F(Randomization, doc="How the rows are ordered."),
    },
    doc="A grid of statements.",
)

FormField = Obj(
    "FormField",
    {
        "id": F(ID, True),
        "label": F(TextValue, True),
        "media": F(ListOf(Media)),
        "show_if": F(_CONDITION),
        "default": F(TextValue, doc="Text already in the box."),
    },
)
FormResponse = Obj(
    "FormResponse",
    {
        "type": F(Const("form"), True),
        "fields": F(ListOf(FormField, 1), True),
        "field_response": F(Lazy(lambda: TextResponse), doc="The checks every field's text gets (length, format)."),
        "required": F(Required),
    },
    doc="Several labelled text boxes.",
)

RankDefault = Obj("RankDefault", {"item": F(ID, True), "rank": F(PositiveInt, True)})
RankingResponse = Obj(
    "RankingResponse",
    {
        "type": F(Const("ranking"), True),
        "items": F(ListOf(Option, 1), True),
        "required": F(Required),
        "min_ranked": F(Num(integer=True, minimum=0)),
        "max_ranked": F(PositiveInt),
        "default": F(ListOf(RankDefault)),
        "randomization": F(Randomization),
    },
    doc="A strict order of items, 1 the highest.",
)

AllocationResponse = Obj(
    "AllocationResponse",
    {
        "type": F(Const("allocation"), True),
        "items": F(ListOf(Option, 1), True),
        "total": F(Num(), doc="What the amounts add up to; 100 when absent."),
        "required": F(Required),
        "min": F(Num(), doc="The least one item may get."),
        "max": F(Num(), doc="The most one item may get."),
        "default": F(MapOf(Scalar()), doc="Preset amounts by item ID."),
        "randomization": F(Randomization),
    },
    doc="An amount split across items that adds up to a total.",
)

GroupField = Obj(
    "GroupField",
    {
        "id": F(ID, True),
        "label": F(TextValue, True),
        "export_name": F(Str(), doc="The column's name in exports."),
        "media": F(ListOf(Media)),
        "response": F(Lazy(lambda: MatrixResponse), True, "The column: its statements and the answer each takes."),
        "source": F(AnyValue()),
    },
    doc="One column of a side-by-side question.",
)
GroupResponse = Obj(
    "GroupResponse",
    {
        "type": F(Const("group"), True),
        "layout": F(Const("side_by_side"), True),
        "fields": F(ListOf(GroupField, 1), True),
        "required": F(Required),
        "randomization": F(Randomization, doc="How each column's options are ordered."),
    },
    doc="Several grids side by side, each column with its own statements and scale.",
)

BestWorstResponse = Obj(
    "BestWorstResponse",
    {
        "type": F(Const("best_worst"), True),
        "best_label": F(TextValue, True),
        "worst_label": F(TextValue, True),
        "required": F(Required),
        "options_source": F(Const("shown_items")),
    },
    doc="The best and the worst of the items shown, two different ones.",
)

Response = Tagged(
    "Response",
    "type",
    {
        "choice": ChoiceResponse,
        "text": TextResponse,
        "number": NumberResponse,
        "matrix": MatrixResponse,
        "form": FormResponse,
        "ranking": RankingResponse,
        "allocation": AllocationResponse,
        "group": GroupResponse,
        "best_worst": BestWorstResponse,
    },
    doc="What a question asks for.",
)

# ---------------------------------------------------------------- questions

Presentation = Obj(
    "Presentation",
    {
        "control": F(
            Str(
                enum=(
                    "list",
                    "horizontal",
                    "columns",
                    "dropdown",
                    "select_box",
                    "single_line",
                    "multi_line",
                    "essay",
                    "password",
                    "slider",
                    "bar",
                    "stars",
                    "number_scale",
                    "drag_and_drop",
                    "text_boxes",
                    "bipolar",
                    "carousel",
                    "graphic",
                )
            ),
            doc="How the response is shown (it can change how the question is described to the model, never what is asked).",
        ),
    },
)

QuestionSource = Obj(
    "QuestionSource",
    {
        "format": F(Str()),
        "type": F(Str()),
        "settings": F(MapOf(AnyValue()), doc="Source settings that change nothing simulated, kept as read."),
        "notes": F(ListOf(Str())),
    },
    open_keys=True,
)

Question = Obj(
    "Question",
    {
        "id": F(ID, True),
        "export_name": F(Str(), doc="The column name in exports (Qualtrics DataExportTag)."),
        "content": F(ListOf(Content), True),
        "response": F(Response, doc="Absent for an instruction, which is shown and never answered."),
        "kind": F(
            Str(enum=("timing", "meta_info", "captcha", "unsupported")),
            doc="A source question that is never simulated.",
        ),
        "show_if": F(Condition),
        "skip": F(ListOf(SkipRule)),
        "presentation": F(Presentation),
        "source": F(QuestionSource),
    },
    doc="A question, or an instruction when it has no response.",
)

# ---------------------------------------------------------------- experiments: conjoint

Level = Obj(
    "Level",
    {
        "id": F(ID, True),
        "label": F(TextValue, True),
        "value": F(Scalar(), doc="A number or price value, distinct from the label."),
        "weight": F(Num(minimum=0), doc="Relative chance of drawing this level; 0 never draws it. Default 1."),
        "media": F(ListOf(Media)),
    },
)
Attribute = Obj(
    "Attribute",
    {
        "id": F(ID, True),
        "name": F(TextValue, True),
        "type": F(Str(enum=("categorical", "numeric", "price"))),
        "currency": F(Str()),
        "unit": F(Str()),
        "levels": F(ListOf(Level, 1), True),
    },
)
Alternative = Obj(
    "Alternative",
    {
        "id": F(ID, True),
        "label": F(TextValue, True),
        "fixed_levels": F(MapOf(ID), doc="Attribute ID to the level this alternative always has."),
    },
)
Alternatives = Obj(
    "Alternatives",
    {
        "mode": F(Str(enum=("generic", "fixed")), True),
        "count": F(PositiveInt, doc="('generic') Alternatives per task."),
        "label": F(Str(), doc="('generic') Label template; {letter} and {number} name the position. Default 'Option {letter}'."),
        "items": F(ListOf(Alternative, 1), doc="('fixed') The alternatives every task shows."),
    },
)
Applicability = Obj(
    "Applicability",
    {
        "row_attribute": F(ID, True),
        "row_levels": F(ListOf(ID, 1), True),
        "column_attribute": F(ID, True),
        "column_levels": F(ListOf(ID, 1), True),
        "allowed": F(ListOf(ListOf(Num(integer=True, minimum=0, maximum=1), 1), 1), True, "1 where the row level may go with the column level."),
    },
    doc="Which levels of two attributes may appear together in one profile.",
)
ConjointConstraints = Obj(
    "ConjointConstraints",
    {
        "applicability": F(ListOf(Applicability)),
        "forbidden_profiles": F(ListOf(MapOf(ID)), doc="Each map of attribute ID to level ID is a combination never shown in one profile."),
    },
)
DesignAlternative = Obj("DesignAlternative", {"id": F(ID, True), "levels": F(MapOf(ID), True)})
DesignTask = Obj("DesignTask", {"id": F(ID, True), "alternatives": F(ListOf(DesignAlternative, 1), True)})
DesignVersion = Obj("DesignVersion", {"id": F(ID, True), "tasks": F(ListOf(DesignTask, 1), True)})
ConjointDesign = Obj(
    "ConjointDesign",
    {
        "mode": F(Str(enum=("generated", "supplied")), True),
        "generator": F(Str(enum=("random_valid_profiles",))),
        "tasks_per_participant": F(PositiveInt),
        "generation_timing": F(Str(enum=("block_entry",))),
        "duplicates": F(Str(enum=("allow", "within_task", "within_participant")), doc="Where two identical profiles may not appear. Default 'within_task'."),
        "max_attempts": F(PositiveInt, doc="Draws tried per profile before the design is called infeasible. Default 1000."),
        "versions": F(ListOf(DesignVersion, 1), doc="('supplied') Complete task versions."),
        "assignment": F(Str(enum=("random", "by_index")), doc="('supplied') How a participant gets a version."),
    },
)
OrderSetting = Obj(
    "OrderSetting",
    {
        "mode": F(Str(enum=("fixed", "shuffle")), True),
        "scope": F(Str(enum=("participant", "task")), doc="Draw the order once per participant, or once per task."),
        "fixed_positions": F(MapOf(PositiveInt), doc="ID to a 1-based position kept while the rest shuffle."),
    },
)
ConjointPresentation = Obj(
    "ConjointPresentation",
    {
        "instruction": F(TextValue),
        "layout": F(
            Str(enum=("table", "list")),
            doc="How a task shows its alternatives: a table with a column per alternative and a row per attribute ('table', the default), or each alternative with a line per attribute ('list').",
        ),
        "attribute_order": F(OrderSetting),
        "alternative_order": F(OrderSetting),
    },
)
OutsideOption = Obj("OutsideOption", {"id": F(ID, True), "label": F(TextValue, True), "code": F(Scalar())})
ConjointResponse = Obj(
    "ConjointResponse",
    {
        "type": F(Const("choice"), True),
        "required": F(Required),
        "outside_option": F(OutsideOption, doc="An extra option such as 'None of these'."),
    },
)
Administration = Obj(
    "Administration",
    {"unit": F(Str(enum=("task", "block")), doc="Ask all tasks of the block in one model call, each with its own answer ('block', the default), or one task per call ('task').")},
)
ConjointBlock = Obj(
    "ConjointBlock",
    {
        "id": F(ID, True),
        "kind": F(Const("conjoint"), True),
        "title": F(Str()),
        "attributes": F(ListOf(Attribute, 1), True),
        "alternatives": F(Alternatives, True),
        "constraints": F(ConjointConstraints),
        "design": F(ConjointDesign, True),
        "presentation": F(ConjointPresentation),
        "response": F(ConjointResponse),
        "administration": F(Administration),
    },
    doc="A choice-based conjoint: the engine draws each participant's profiles from the attribute levels and rules.",
)

# ---------------------------------------------------------------- experiments: MaxDiff

MaxDiffItem = Obj(
    "MaxDiffItem",
    {"id": F(ID, True), "label": F(TextValue, True), "code": F(Scalar()), "media": F(ListOf(Media))},
)
MaxDiffDesign = Obj(
    "MaxDiffDesign",
    {
        "mode": F(Str(enum=("generated", "supplied")), True),
        "generator": F(Str(enum=("uniform_subsets", "balanced_subsets"))),
        "tasks_per_participant": F(PositiveInt),
        "items_per_task": F(Num(integer=True, minimum=2)),
        "generation_timing": F(Str(enum=("block_entry",))),
        "duplicate_tasks": F(Str(enum=("allow", "forbid")), doc="Whether a participant may see the same subset twice. Default 'forbid'."),
        "max_attempts": F(PositiveInt),
        "versions": F(
            ListOf(Obj("MaxDiffVersion", {"id": F(ID, True), "tasks": F(ListOf(ListOf(ID, 2), 1), True)}), 1),
            doc="('supplied') Each version's tasks, as lists of item IDs.",
        ),
        "assignment": F(Str(enum=("random", "by_index"))),
    },
)
MaxDiffPresentation = Obj(
    "MaxDiffPresentation",
    {"instruction": F(TextValue), "item_order": F(OrderSetting)},
)
Anchor = Obj(
    "Anchor",
    {
        "id": F(ID, True),
        "content": F(ListOf(Content), True),
        "response": F(ChoiceResponse, True),
    },
    doc="A follow-up after each task about the same items (anchored MaxDiff).",
)
MaxDiffBlock = Obj(
    "MaxDiffBlock",
    {
        "id": F(ID, True),
        "kind": F(Const("maxdiff"), True),
        "title": F(Str()),
        "items": F(ListOf(MaxDiffItem, 2), True),
        "design": F(MaxDiffDesign, True),
        "presentation": F(MaxDiffPresentation),
        "response": F(BestWorstResponse, True),
        "anchor": F(Anchor),
        "administration": F(Administration),
    },
    doc="A best/worst (MaxDiff) block: the engine draws each participant's item subsets.",
)

# ---------------------------------------------------------------- blocks and flow

BlockRandomization = Obj(
    "BlockRandomization",
    {
        "order": F(
            Str(enum=("shuffle", "source")),
            True,
            "'shuffle': the block's questions in a random order; 'source': a setting with no standard form, kept and reported, not run.",
        ),
        "qualtrics": F(AnyValue(), doc="('source') The setting as the source platform wrote it."),
    },
)
RepeatRow = Obj(
    "RepeatRow",
    {"id": F(ID, True), "values": F(MapOf(TextValue), True, "The row's values by field name; ${lm://Field/name} pipes one.")},
)
Repeat = Obj(
    "Repeat",
    {
        "rows": F(ListOf(RepeatRow, 1), True, "One run of the block per row, in this order."),
        "order": F(Str(enum=("fixed", "shuffle")), doc="'shuffle' runs the rows in a random order."),
        "take": F(PositiveInt, doc="('shuffle') Run only this many rows, drawn at random."),
    },
    doc="The block runs once per row (Qualtrics Loop & Merge); its question IDs get the row ID in front (1_QID5).",
)
SurveyBlock = Obj(
    "SurveyBlock",
    {
        "id": F(ID, True),
        "kind": F(Const("survey"), True),
        "title": F(Str()),
        "questions": F(ListOf(ID), True, "The block's questions, in order."),
        "randomization": F(BlockRandomization),
        "repeat": F(Repeat),
        "source": F(AnyValue()),
    },
)
Block = Tagged(
    "Block",
    "kind",
    {"survey": SurveyBlock, "conjoint": ConjointBlock, "maxdiff": MaxDiffBlock},
)

_FLOW = Lazy(lambda: FlowNode)

Assignment = Obj(
    "Assignment",
    {
        "variable": F(NAME, True),
        "value": F(TextValue, doc="The value set; text may pipe other values."),
        "from_outside": F(Bool(), doc="The value comes from outside the survey (a panel, a link)."),
        "source": F(AnyValue()),
    },
)
FlowNode = Tagged(
    "FlowNode",
    "type",
    {
        "show_block": Obj(
            "ShowBlock",
            {"id": F(ID), "type": F(Const("show_block"), True), "block": F(ID, True)},
        ),
        "set_variables": Obj(
            "SetVariables",
            {
                "id": F(ID, True),
                "type": F(Const("set_variables"), True),
                "assignments": F(ListOf(Assignment), True),
            },
        ),
        "branch": Obj(
            "Branch",
            {
                "id": F(ID, True),
                "type": F(Const("branch"), True),
                "when": F(Condition, True),
                "then": F(ListOf(_FLOW), True, "The steps run when the condition holds."),
                "else": F(ListOf(_FLOW), doc="The steps run when it does not (decided before 'then' runs)."),
                "source": F(AnyValue()),
            },
        ),
        "randomize": Obj(
            "Randomize",
            {
                "id": F(ID, True),
                "type": F(Const("randomize"), True),
                "take": F(PositiveInt, True),
                "evenly": F(Bool(), doc="Balance the arms across participants."),
                "children": F(ListOf(_FLOW), True),
                "source": F(AnyValue()),
            },
        ),
        "group": Obj(
            "Group",
            {
                "id": F(ID, True),
                "type": F(Const("group"), True),
                "title": F(Str()),
                "children": F(ListOf(_FLOW), True),
                "source": F(AnyValue()),
            },
        ),
        "end": Obj(
            "End",
            {"id": F(ID, True), "type": F(Const("end"), True), "source": F(AnyValue())},
        ),
    },
    doc="A step of the survey flow.",
)

# ---------------------------------------------------------------- the survey

Variable = Obj(
    "Variable",
    {
        "id": F(NAME, True),
        "type": F(Str(enum=("string", "number", "boolean"))),
        "description": F(Str()),
        "from_outside": F(Bool(), doc="The value comes from outside the survey (a panel, a link)."),
    },
)

Survey = Obj(
    "Survey",
    {
        "schema_version": F(Str(enum=(SCHEMA_VERSION,)), True),
        "id": F(ID, True),
        "title": F(Str()),
        "description": F(Str()),
        "language": F(Str()),
        "variables": F(ListOf(Variable)),
        "questions": F(ListOf(Question)),
        "blocks": F(ListOf(Block)),
        "flow": F(ListOf(FlowNode)),
        "source": F(AnyValue(), doc="Where an imported survey came from, and what the reader found."),
    },
    doc="A survey in the standard format.",
)


def check_structure(data: Any) -> list[Problem]:
    """Every structural problem in a survey definition (empty when it is well formed)."""
    problems: list[Problem] = []
    Survey.check(data, "$", problems)
    return problems


def json_schema() -> dict[str, Any]:
    """The format as a JSON Schema document."""
    return to_json_schema(Survey, title="surveytwin standard survey format", version=SCHEMA_VERSION)
