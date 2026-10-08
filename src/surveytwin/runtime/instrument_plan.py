"""Decide the execution mode and build each twin's survey, without files.

Ported from the website's ``build_qsf_instrument`` and ``PipelineService``. The
website writes the template and every participant file to disk and reads them
back; this module keeps everything in memory but applies the same JSON round
trips, so the result is identical.
"""

from __future__ import annotations

import copy
import json
from typing import Any

from ..engine import render
from ..engine.definition import Definition
from ..engine.static import survey_copies
from ..prompts.instrument import _filter_qsf_template_blocks
from ..qsf.parse_qsf import generate_template_format, parse_qsf_data
from ..spec.read_template import survey_from_template


def _json_round_trip(value: Any) -> Any:
    return json.loads(json.dumps(value, indent=2, ensure_ascii=False))


def parse_template(qsf_data: dict[str, Any], exclude_questions: list[str] | None = None) -> dict[str, Any]:
    """The website's template: ``generate_template_format(parse_qsf_file(...))``."""
    return generate_template_format(parse_qsf_data(qsf_data, exclude_questions))


def excluded_question_ids(qsf_data: dict[str, Any], excluded_block_ids: list[str] | None) -> list[str]:
    """Question IDs inside the excluded blocks, as ``PipelineService`` finds them."""
    excluded_block_set = {
        str(block_id).strip()
        for block_id in (excluded_block_ids or [])
        if isinstance(block_id, str) and block_id.strip()
    }
    questions: set[str] = set()
    if not excluded_block_set:
        return []
    all_blocks = parse_qsf_data(qsf_data).get("all_blocks", {})
    if isinstance(all_blocks, dict):
        for block_id in excluded_block_set:
            block_data = all_blocks.get(block_id)
            if not isinstance(block_data, dict):
                continue
            block_question_ids = block_data.get("block_elements", [])
            if not isinstance(block_question_ids, list):
                continue
            for question_id in block_question_ids:
                if isinstance(question_id, str) and question_id.strip():
                    questions.add(question_id.strip())
    return sorted(questions)


def remove_template_questions(template: dict[str, Any], question_ids: Any) -> dict[str, Any]:
    """The template without these questions, as parsing the QSF without them gives.

    Blocks left with no questions disappear; container elements (branches,
    randomizers, groups) stay with whatever elements remain. Checked equal to
    ``parse_template(qsf, question_ids)`` on every test and study QSF.
    """
    out = copy.deepcopy(template)
    ids = {str(q) for q in (question_ids or []) if str(q).strip()}
    if not ids:
        return out

    def prune(elements: Any) -> Any:
        if not isinstance(elements, list):
            return elements
        kept: list[Any] = []
        for element in elements:
            if isinstance(element, dict) and element.get("ElementType") == "Block":
                questions = [
                    q for q in element.get("Questions") or []
                    if not (isinstance(q, dict) and str(q.get("QuestionID", "")) in ids)
                ]
                if not questions:
                    continue
                element["Questions"] = questions
            elif isinstance(element, dict) and isinstance(element.get("Elements"), list):
                element["Elements"] = prune(element["Elements"])
            kept.append(element)
        return kept

    out["Elements"] = prune(out.get("Elements", []))
    return out


def question_ids_in_blocks(template: dict[str, Any], block_ids: Any) -> list[str]:
    """Question IDs inside these blocks (equal to ``excluded_question_ids`` for a parsed QSF)."""
    wanted = {str(b).strip() for b in (block_ids or []) if str(b).strip()}
    found: set[str] = set()
    if not wanted:
        return []

    def walk(elements: Any) -> None:
        if not isinstance(elements, list):
            return
        for element in elements:
            if not isinstance(element, dict):
                continue
            if element.get("ElementType") == "Block" and str(element.get("BlockID", "")) in wanted:
                for q in element.get("Questions") or []:
                    if isinstance(q, dict) and str(q.get("QuestionID", "")).strip():
                        found.add(str(q["QuestionID"]).strip())
            walk(element.get("Elements"))

    walk(template.get("Elements", []))
    return sorted(found)


def build_instrument(
    qsf_data: dict[str, Any],
    persona_count: int,
    *,
    excluded_block_ids: list[str] | None = None,
    exclude_questions: list[str] | None = None,
    seed: int = 42,
    mode: str = "auto",
    borrow_unset_embedded_data: bool = False,
    initial_embedded_data: dict[str, str] | None = None,
) -> dict[str, Any]:
    """The instrument for a QSF: parse it, then ``build_instrument_from_template``
    (its questions read with the QSF's own texts, as ``read_qsf`` reads them)."""
    return build_instrument_from_template(
        parse_template(qsf_data, list(exclude_questions or []) or None),
        persona_count,
        excluded_block_ids=excluded_block_ids,
        seed=seed,
        mode=mode,
        borrow_unset_embedded_data=borrow_unset_embedded_data,
        initial_embedded_data=initial_embedded_data,
        qsf=qsf_data,
    )


def _definition_of(template: dict[str, Any], source: str, qsf: dict[str, Any] | None = None) -> dict[str, Any]:
    """A template read into the standard form (for a caller that has no definition);
    with the QSF it came from, its questions keep the QSF's own texts."""
    return survey_from_template(template, qsf=qsf, origin={"survey_builder": "survey_builder", "standard": "standard"}.get(source, "qsf"))


def build_instrument_from_template(
    full_template: dict[str, Any],
    persona_count: int,
    *,
    excluded_block_ids: list[str] | None = None,
    excluded_question_ids: list[str] | None = None,
    seed: int = 42,
    mode: str = "auto",
    borrow_unset_embedded_data: bool = False,
    initial_embedded_data: dict[str, str] | None = None,
    source: str = "qsf",
    respondent_ids: list[str] | None = None,
    definition: dict[str, Any] | None = None,
    qsf: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """The instrument for a survey template (a script or test that has one): the
    template, without the dropped blocks, read into the standard format (with
    ``qsf``, the QSF it came from; or ``definition``, when given), then
    ``build_survey_instrument``."""
    template = copy.deepcopy(full_template)
    if excluded_block_ids:
        # ``excluded_question_ids``: questions already gone (excluded when read); a
        # screen-out reading only gone questions goes with the blocks
        template = _filter_qsf_template_blocks(template, excluded_block_ids, excluded_question_ids=excluded_question_ids)
    if definition is None:
        definition = _definition_of(template, source, qsf)
    return build_survey_instrument(
        definition,
        persona_count,
        seed=seed,
        mode=mode,
        borrow_unset_embedded_data=borrow_unset_embedded_data,
        initial_embedded_data=initial_embedded_data,
        source=source,
        respondent_ids=respondent_ids,
    )


def build_survey_instrument(
    definition: dict[str, Any],
    persona_count: int,
    *,
    seed: int = 42,
    mode: str = "auto",
    borrow_unset_embedded_data: bool = False,
    initial_embedded_data: dict[str, str] | None = None,
    source: str = "qsf",
    respondent_ids: list[str] | None = None,
) -> dict[str, Any]:
    """What a run of ``definition`` (the survey in the standard format, without
    dropped blocks: ``Survey.standard``) needs: whether it runs static or staged,
    and for a static run each twin's copy, its prompt questions, survey text and
    pictures.

    ``respondent_ids``: the twins the static copies are for, in order. Random-value
    draws are then keyed by twin, as the staged runtime keys them, so a twin draws
    the same values in either mode. Without them, copies are keyed by position.
    ``mode`` is ``auto`` (staged when later content depends on an answer),
    ``staged`` (always correct, more calls) or ``static`` (refused by the caller
    when staging is required; this function only reports ``refused``).
    """
    from ..engine.analysis import analyze
    from ..results.layout import Layout

    runtime_analysis = analyze(definition)
    title = str(definition.get("title") or "Untitled Survey")
    if runtime_analysis.get("requires_staging") and mode == "static":
        return {"source": source, "execution_mode": "refused", "title": title, "definition": definition, "analysis": runtime_analysis}
    the_definition = Definition(definition)
    if runtime_analysis.get("requires_staging") or mode == "staged":
        written = [shown for _block, questions in Layout(definition).placements() for shown in questions]
        if not render.prompt_questions(written):
            raise ValueError("No survey questions remain after applying QSF block exclusions.")
        return {"source": source, "execution_mode": "staged", "title": title, "definition": definition, "analysis": runtime_analysis}

    # Static: each twin's copy from the survey definition.
    copies, generated = survey_copies(
        the_definition,
        max(persona_count, 1),
        seed=seed,
        respondent_ids=respondent_ids,
        initial_variables=initial_embedded_data,
        borrow_unset=borrow_unset_embedded_data,
    )
    instances: list[dict[str, Any]] = []
    for pid, twin_copy in enumerate(copies, start=1):
        shown = [q for block in twin_copy["blocks"] for q in block["questions"]]
        instance = {
            "survey_pid": f"pid_{pid}",
            "questions": render.prompt_questions(shown),
            "prompt_sequence_text": render.sequence_text(shown),
            "prompt_sequence_media": render.sequence_media(shown),
            "embedded_data": twin_copy["variables"],
            "assignments": twin_copy["assignments"],
            "blocks": twin_copy["blocks"],
        }
        # each conjoint or MaxDiff task this copy shows, as drawn for its twin
        # (only for surveys with such blocks, so other instruments are unchanged)
        if twin_copy.get("experiments"):
            instance["experiments"] = twin_copy["experiments"]
        instances.append(instance)
    if not any(instance["questions"] for instance in instances):
        raise ValueError("No survey questions remain after applying QSF block exclusions.")
    return {
        "source": source,
        "execution_mode": "static",
        "title": title,
        "definition": definition,
        "instances": instances,
        "analysis": runtime_analysis,
        "generation": generated,
    }
