"""Product rules an application may add on top of survey correctness.

The library checks only what makes a survey wrong. The SurveyTwin website adds
its own rules (at most 40 questions, a title is required, a warning on
questions that ask for demographics the persona already carries). They live
here as ``WEBSITE_POLICY`` so an application opts in; nothing applies them by
default.
"""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class BuilderPolicy:
    """``max_questions``: a cap on the number of questions (None: no cap).
    ``require_title``: a blank title is an error.
    ``warn_on_demographics``: warn on questions that ask for age, gender and
    other attributes a persona already carries."""

    max_questions: int | None = None
    require_title: bool = False
    warn_on_demographics: bool = False


DEFAULT_POLICY = BuilderPolicy()
# The website's rules (``survey_draft.MAX_DRAFT_QUESTIONS`` and ``validate_draft``).
WEBSITE_POLICY = BuilderPolicy(max_questions=40, require_title=True, warn_on_demographics=True)

# The website's phrases (``survey_draft._DEMOGRAPHIC_HINTS``), matched as whole
# words, so "your agency" no longer matches "your age".
DEMOGRAPHIC_HINTS = (
    "your age",
    "how old are you",
    "your gender",
    "your sex",
    "your race",
    "your ethnicity",
    "your income",
    "household income",
    "your education",
    "highest level of education",
    "employment status",
    "marital status",
    "your religion",
    "political party",
    "zip code",
    "state do you live",
)
_DEMOGRAPHIC_RE = re.compile(r"\b(?:" + "|".join(re.escape(h) for h in DEMOGRAPHIC_HINTS) + r")\b", re.IGNORECASE)


def looks_demographic(text: str) -> bool:
    return bool(_DEMOGRAPHIC_RE.search(str(text or "")))
