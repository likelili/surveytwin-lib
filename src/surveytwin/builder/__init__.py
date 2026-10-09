"""Accept a survey from a web form, as a draft, instead of exporting a QSF.

    from surveytwin import builder
    survey = builder.build_survey({"title": "...", "questions": [...]})
    survey = builder.add_questions(survey, [{...}], after_question_id="Q2")
    survey = builder.apply_draft(survey, form_payload)   # a web form's edits; every question keeps its ID

A draft (``SurveyDraft``, or a web form's JSON payload) is only an input:
``build_survey`` turns it into a ``Survey``, the object every other step runs
on. The edit functions take and return a ``Survey``. See docs/builder.md.

To write or change a survey in Python, use ``st.new_survey`` and the survey's
own edit methods (``Survey.add_question`` and the others, in ``standard.py``):
they write the standard format directly, with every type and setting it has.
"""

from .build import DEFAULT_BLOCK_ID, build_survey, to_draft
from .draft import ANSWERABLE_TYPES, QUESTION_TYPES, SCALE_PRESETS, DraftIssue, DraftQuestion, SurveyDraft
from .edit import add_questions, apply_draft, remove_questions, reorder_questions, update_question
from .images import default_image_resolver
from .policy import DEFAULT_POLICY, WEBSITE_POLICY, BuilderPolicy
from .validate import assign_ids, planned_qids, validate_draft

__all__ = [
    "build_survey",
    "to_draft",
    "add_questions",
    "update_question",
    "remove_questions",
    "reorder_questions",
    "apply_draft",
    "validate_draft",
    "assign_ids",
    "planned_qids",
    "SurveyDraft",
    "DraftQuestion",
    "DraftIssue",
    "BuilderPolicy",
    "DEFAULT_POLICY",
    "WEBSITE_POLICY",
    "SCALE_PRESETS",
    "QUESTION_TYPES",
    "ANSWERABLE_TYPES",
    "DEFAULT_BLOCK_ID",
    "default_image_resolver",
]
