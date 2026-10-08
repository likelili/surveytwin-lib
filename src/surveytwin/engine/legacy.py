"""Survey content in the old template shape: read into the shapes the engine
uses, and written back for code that still reads that shape.

Runs saved before the engine ran on the standard definition stored each twin's
copy of the survey as template elements (Qualtrics-shaped blocks and
questions), and the template-based checks still hand over template blocks.
``shown_questions`` reads such elements into shown questions: each
question in the standard form (``spec.read_template``), with how its text reads
(``lines``), whether its survey was written in the standard format
(``native``), and a MaxDiff pick's group and role. ``template_elements`` writes
a twin's copy the other way, for ``Plan.twin_surveys`` (its ``elements``).
"""

from __future__ import annotations

import copy
from typing import Any

from ..spec.compile import Compiler
from ..spec.read_template import TemplateReader

# Keys the old engine added to a question it showed; the standard form keeps none of them.
_ENGINE_KEYS = ("KeepLines", "ExactType", "BestWorst")


def shown_question(question: dict[str, Any]) -> dict[str, Any]:
    """One template question as a shown question."""
    plain = {key: value for key, value in question.items() if key not in _ENGINE_KEYS}
    standard = TemplateReader({}).question(plain)
    record: dict[str, Any] = {
        "question": standard,
        "lines": bool(question.get("KeepLines")),
        "native": bool(question.get("ExactType")),
    }
    if isinstance(question.get("BestWorst"), dict):
        record["best_worst"] = copy.deepcopy(question["BestWorst"])
    return record


def template_questions(elements: Any) -> list[dict[str, Any]]:
    """Every question of template elements, in the order the old engine walked them:
    a block's questions, then the elements inside it."""
    out: list[dict[str, Any]] = []
    for element in elements if isinstance(elements, list) else []:
        if not isinstance(element, dict):
            continue
        if element.get("ElementType") == "Block":
            out += [q for q in element.get("Questions") or [] if isinstance(q, dict)]
        out += template_questions(element.get("Elements"))
    return out


def shown_questions(elements: Any) -> list[dict[str, Any]]:
    """A survey copy in the template shape, as shown questions in order."""
    return [shown_question(q) for q in template_questions(elements)]


def template_question(shown: dict[str, Any], compiler: Compiler) -> dict[str, Any]:
    """A shown question as the template question the old engine showed."""
    question = compiler.question(shown["question"])
    question.pop("KeepLines", None)
    question.pop("ExactType", None)
    if shown.get("lines"):
        question["KeepLines"] = True
    if shown.get("native") and question.get("QuestionType") == "TE":
        question["ExactType"] = True
    if isinstance(shown.get("best_worst"), dict):
        question["BestWorst"] = copy.deepcopy(shown["best_worst"])
    return question


def template_elements(blocks: Any, *, native: bool) -> list[dict[str, Any]]:
    """A twin's copy (its blocks of shown questions) as template elements."""
    compiler = Compiler({"source": {} if native else {"format": "qsf"}})
    return [
        {
            "ElementType": "Block",
            "BlockID": block["block"],
            "BlockName": block.get("title", ""),
            "BlockType": "Standard",
            "Questions": [template_question(shown, compiler) for shown in block.get("questions") or []],
        }
        for block in (blocks if isinstance(blocks, list) else [])
        if isinstance(block, dict)
    ]
