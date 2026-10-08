"""A survey definition (the standard format) indexed for the engine."""

from __future__ import annotations

from typing import Any

from ..spec.text import join_text


class Definition:
    """A survey in the standard format: its questions and blocks by ID, its flow,
    and whether it was written in the standard format (``native``) rather than
    read from a QSF or made with the builder."""

    def __init__(self, survey: dict[str, Any]):
        self.survey = survey
        self.questions: dict[str, dict[str, Any]] = {str(q["id"]): q for q in survey.get("questions") or []}
        self.blocks: dict[str, dict[str, Any]] = {str(b["id"]): b for b in survey.get("blocks") or []}
        self.flow: list[dict[str, Any]] = list(survey.get("flow") or [])
        source = survey.get("source") if isinstance(survey.get("source"), dict) else {}
        self.native = source.get("format") not in {"qsf", "template", "survey_builder"}

    @property
    def title(self) -> str:
        return str(self.survey.get("title") or "Untitled Survey")

    @property
    def brand(self) -> str:
        """The Qualtrics brand of a survey read from a QSF (it completes picture addresses)."""
        source = self.survey.get("source") if isinstance(self.survey.get("source"), dict) else {}
        metadata = source.get("metadata") if isinstance(source.get("metadata"), dict) else {}
        return str(metadata.get("SurveyBrandID", "") or "")


def flow_id(node: dict[str, Any]) -> str:
    """The ID a flow step's draws and records are keyed by: the source's flow ID as
    written when it had one, else the step's ID; empty for a source step that had none."""
    source = node.get("source") if isinstance(node.get("source"), dict) else {}
    if source.get("no_flow_id"):
        return ""
    if "FlowID" in source:
        return str(source["FlowID"] or "")
    return str(node.get("id") or "")


def assignment_fields(node: dict[str, Any]) -> tuple[str, ...]:
    """The variables a set-variables step sets (not those it only declares), sorted."""
    return tuple(
        sorted(
            str(a.get("variable", "") or "").strip()
            for a in node.get("assignments") or []
            if not a.get("from_outside") and str(a.get("variable", "") or "").strip()
        )
    )


def is_assignment_arms(children: list[dict[str, Any]]) -> bool:
    """A randomizer whose arms each set the same variables (Qualtrics' random
    assignment by last-write-wins): one arm is chosen and run last."""
    if len(children) < 2:
        return False
    sets = []
    for child in children:
        if not isinstance(child, dict) or child.get("type") != "set_variables":
            return False
        fields = assignment_fields(child)
        if not fields:
            return False
        sets.append(fields)
    return len(set(sets)) == 1


def assignment_value(assignment: dict[str, Any]) -> Any:
    """What a set-variables step writes: its value as written (pipes as tokens); a
    value the source wrote as a number stays that number."""
    source = assignment.get("source") if isinstance(assignment.get("source"), dict) else {}
    value = join_text(assignment.get("value", ""))
    if "Value" in source and str(source["Value"]) == value:
        return source["Value"]
    return value
