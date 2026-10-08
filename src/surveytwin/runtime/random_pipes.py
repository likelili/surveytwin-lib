"""Qualtrics random-value piped text: ``${rand://int/1:100}``, ``${rand://float/0.05:1.50}``.

Qualtrics draws these when the text is shown, every occurrence separately, and
keeps nothing unless a survey-flow embedded-data field is set to one. Surveys use
them as hidden experimental draws (a report that is right 75% of the time, a
rival that enters a market with probability p), so they have to be drawn. Left in
place, the literal token flows into embedded data and every ``draw <= 75`` branch
compares text instead of numbers, which silently changes the design.

Qualtrics documents three formats: an integer, a decimal with 13 decimal places,
and True/False as 1 or 0. Values are strings, like every embedded-data value.

A draw is keyed by the run seed, the respondent, where the token stands (an
embedded-data field ``ed/<FlowID>/<Field>``, or a question field
``q/<QuestionID>/<key>/...``) and its place among that field's tokens, so static
and staged runs draw the same value for the same twin, a re-render draws it
again, and the same token in two fields draws independently.
"""
from __future__ import annotations

import hashlib
import math
import random
import re
from typing import Any

RANDOM_PIPE_PATTERN = re.compile(r"\$\{rand://([A-Za-z]+)(?:/([^}]*))?\}")
_ANY_RANDOM_TOKEN = re.compile(r"\$\{rand://[^}]*\}")

# Keys whose strings are logic or identifiers, not text a respondent reads.
_NOT_CONTENT = frozenset(
    {"DisplayLogic", "SkipLogic", "ChoiceDisplayLogic", "AnswerDisplayLogic", "QuestionID", "QuestionName", "DataExportTag", "Randomization"}
)


def has_random_pipes(text: str) -> bool:
    return bool(text) and "${rand://" in text


def draw_random_pipe(kind: str, spec: str, rng: random.Random) -> str | None:
    """One draw for a ``rand://<kind>/<min>:<max>`` token; None when the token
    is not something we know how to draw (it is then left as-is)."""
    kind = str(kind or "").strip().lower()
    if kind in {"bool", "boolean"}:
        return "1" if rng.random() < 0.5 else "0"
    low_text, separator, high_text = str(spec or "").partition(":")
    if not separator:
        return None
    try:
        low, high = float(low_text), float(high_text)
    except ValueError:
        return None
    if not (math.isfinite(low) and math.isfinite(high)):
        return None
    if high < low:
        low, high = high, low
    if kind in {"int", "integer"}:
        lower, upper = math.ceil(low), math.floor(high)
        if lower > upper:
            return None
        return str(rng.randint(lower, upper))
    if kind in {"float", "real", "decimal", "number"}:
        return f"{rng.uniform(low, high):.13f}"
    return None


def random_value_rng(seed: Any, respondent: Any, key: str) -> random.Random:
    digest = hashlib.sha256(":".join([str(seed), str(respondent), "rand", key]).encode("utf-8")).hexdigest()
    return random.Random(int(digest[:16], 16))


def resolve_random_pipes_at(text: str, *, seed: Any, respondent: Any, path: str, drawn: dict[str, str] | None = None) -> str:
    """Draw every random-value token in ``text``, the string at ``path``.

    ``drawn`` (optional) keeps the values drawn so far by key, for a caller that
    renders the same text again."""
    if not has_random_pipes(text):
        return text
    position = 0

    def replace(match: re.Match[str]) -> str:
        nonlocal position
        key = f"{path}|{position}"
        position += 1
        if drawn is not None and key in drawn:
            return drawn[key]
        value = draw_random_pipe(match.group(1), match.group(2) or "", random_value_rng(seed, respondent, key))
        if value is None:
            return match.group(0)
        if drawn is not None:
            drawn[key] = value
        return value

    return RANDOM_PIPE_PATTERN.sub(replace, text)


def resolve_random_pipes_in_value(value: Any, *, seed: Any, respondent: Any, path: str) -> Any:
    """``value`` with every random-value token in its text drawn: strings, and the
    strings inside lists and dicts, each keyed by its own path (``Options/2``).
    Logic and identifiers are left alone."""
    if isinstance(value, str):
        return resolve_random_pipes_at(value, seed=seed, respondent=respondent, path=path)
    if isinstance(value, list):
        return [resolve_random_pipes_in_value(item, seed=seed, respondent=respondent, path=f"{path}/{index}") for index, item in enumerate(value)]
    if isinstance(value, dict):
        return {
            key: child if key in _NOT_CONTENT else resolve_random_pipes_in_value(child, seed=seed, respondent=respondent, path=f"{path}/{key}")
            for key, child in value.items()
        }
    return value


def random_token_problems(text: str) -> list[str]:
    """The random-value tokens in ``text`` that cannot be drawn: an unknown kind,
    bounds that are not two numbers, or piped text inside the token (Qualtrics
    takes literal bounds only; ``${rand://int/1:${e://Field/x}}`` reads as the
    token ``${rand://int/1:${e://Field/x}`` followed by ``}``)."""
    problems: list[str] = []
    for token in _ANY_RANDOM_TOKEN.findall(str(text or "")):
        match = RANDOM_PIPE_PATTERN.fullmatch(token)
        nested = "${" in token[len("${rand://") :]
        if nested or not match or draw_random_pipe(match.group(1), match.group(2) or "", random.Random(0)) is None:
            problems.append(token)
    return problems
