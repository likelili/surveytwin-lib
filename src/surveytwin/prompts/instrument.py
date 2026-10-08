"""Prompts from survey content in the old template shape.

The prompt is made by ``engine.render`` from shown questions in the standard
form; these functions read template elements (an old engine's survey copy, or a
template block) with ``engine.legacy`` first. ``_filter_qsf_template_blocks``
removes dropped blocks from a template, as the website does.
"""

from __future__ import annotations

import copy
import re
from typing import Any

from ..engine import render
from ..engine.legacy import shown_questions


def collect_instrument_questions(elements: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The prompt questions of template elements, in order."""
    return render.prompt_questions(shown_questions(elements))


def render_qsf_prompt_sequence(elements: list[dict[str, Any]]) -> str:
    """The survey text of template elements."""
    return render.sequence_text(shown_questions(elements))


def render_qsf_prompt_sequence_media(elements: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The pictures in the survey text of template elements, in order."""
    return render.sequence_media(shown_questions(elements))


def _filter_qsf_template_blocks(
    template: dict[str, Any],
    excluded_block_ids: list[str] | None,
    excluded_question_ids: Any = None,
) -> dict[str, Any]:
    excluded = {
        str(block_id).strip()
        for block_id in (excluded_block_ids or [])
        if str(block_id).strip()
    }
    # Questions already gone from the template (excluded when the survey was read)
    # orphan branches the same way a dropped block's questions do.
    gone = {str(q).strip() for q in (excluded_question_ids or []) if str(q).strip()}
    if not excluded and not gone:
        return template

    # QIDs that live inside the excluded blocks — used to prune flow logic
    # (branches / screenouts) orphaned once those blocks are dropped. Without this,
    # hiding e.g. a consent block leaves its "if not consented → end survey" branch
    # in the flow, and the survey is still (mis)classified as staged.
    excluded_qids: set[str] = set(gone)

    def collect_excluded_qids(nodes: Any) -> None:
        if not isinstance(nodes, list):
            return
        for node in nodes:
            if not isinstance(node, dict):
                continue
            if (
                str(node.get("ElementType", "") or "").strip() == "Block"
                and str(node.get("BlockID", "") or "").strip() in excluded
            ):
                for question in node.get("Questions") or []:
                    if isinstance(question, dict):
                        qid = str(question.get("QuestionID", "") or "").strip()
                        if qid:
                            excluded_qids.add(qid)
            collect_excluded_qids(node.get("Elements"))

    collect_excluded_qids(template.get("Elements"))

    def _subtree_has_content(nodes: Any) -> bool:
        """True if the subtree contains any Block or Question (i.e. real content),
        as opposed to only EndSurvey / embedded-data flow markers."""
        if not isinstance(nodes, list):
            return False
        for node in nodes:
            if not isinstance(node, dict):
                continue
            et = str(node.get("ElementType", "") or "").strip()
            if et in ("Block", "Question") or node.get("Questions"):
                return True
            if _subtree_has_content(node.get("Elements")):
                return True
        return False

    def _is_orphaned_branch(node: dict[str, Any]) -> bool:
        """Prune a branch only when it is a pure screenout orphaned by the excluded
        blocks: its condition references only excluded questions AND its subtree holds
        no real content (e.g. `Branch(if screened-out){ EndSurvey }`). A branch that
        gates real blocks (`Branch(if consented){ Block(body) }`) is left intact so
        excluding the gate question can never delete survey content. The subtree is
        judged as it will run, without the excluded blocks: `if not consented ->
        [goodbye block, End Survey]` goes when both blocks are excluded."""
        if str(node.get("ElementType", "") or "").strip() != "Branch":
            return False
        refs = set(re.findall(r"QID\d+", str(node.get("BranchLogic", "") or "")))
        if not (refs and refs <= excluded_qids):
            return False
        return not _subtree_has_content(node.get("Elements"))

    def filter_nodes(nodes: Any) -> list[dict[str, Any]]:
        output: list[dict[str, Any]] = []
        if not isinstance(nodes, list):
            return output
        for node in nodes:
            if not isinstance(node, dict):
                continue
            if (
                str(node.get("ElementType", "") or "").strip() == "Block"
                and str(node.get("BlockID", "") or "").strip() in excluded
            ):
                continue
            copied = copy.deepcopy(node)
            if isinstance(copied.get("Elements"), list):
                copied["Elements"] = filter_nodes(copied.get("Elements"))
            # Judged on the body as it will run, without the excluded blocks.
            if _is_orphaned_branch(copied):
                continue
            output.append(copied)
        return output

    filtered = copy.deepcopy(template)
    filtered["Elements"] = filter_nodes(filtered.get("Elements"))
    return filtered
