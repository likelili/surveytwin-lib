"""Re-asking flagged answers, ported from the website's ``services/v3/rerun.py``
and ``run_instrument.py``.

Only a twin's flagged (skipped or out-of-range) questions are re-asked, shown in
the full survey with the twin's other answers filled in and the flagged cells
marked "[NEEDS ANSWER]", with the pictures the twin saw. The re-answer goes
through the same matcher; if it still does not map, the cell stays flagged. A
repair is stored beside the original answer (``answer_repaired``,
``repair_method="rerun_llm"``), never over it. A flagged question whose picture
cannot be loaded is not re-asked: a question is never answered without its
picture.
"""

from __future__ import annotations

import asyncio
import html
import json
import re
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any

from ..providers.context import call_context
from ..providers.openai_client import extract_json_string
from . import executor as se

REPAIR_METHOD_RERUN = "rerun_llm"
RERUN_MAX_TOKENS = 8000
RERUN_CONCURRENCY = 8

_HTML_TAG_RE = re.compile(r"<[^>]+>")


def _public_text(value: Any) -> str:
    text = html.unescape(str(value or ""))
    text = _HTML_TAG_RE.sub(" ", text)
    return re.sub(r"\s+", " ", text).strip()


def _qnum(qid: str) -> tuple[int, str]:
    match = re.search(r"(\d+)", qid or "")
    return (int(match.group(1)) if match else 10**9, qid or "")


def _is_flagged(status: str) -> bool:
    return str(status or "").strip() in se.FLAGGED_ANSWER_STATUSES


Blocks = list  # blocks of shown questions: each a list of shown questions, in order


def survey_blocks(definition: dict[str, Any] | None) -> list[list[dict[str, Any]]]:
    """The survey's blocks as written (every placement, in flow order), each a list
    of its questions as shown questions."""
    from ..results.layout import Layout

    return [questions for _block, questions in Layout(definition).placements()]


def copy_blocks(blocks: Any) -> list[list[dict[str, Any]]]:
    """A twin's copy or a stage (shown blocks) as lists of shown questions."""
    return [list(block.get("questions") or []) for block in blocks or [] if isinstance(block, dict)]


def _option_labels(shown: dict[str, Any]) -> list[str]:
    """The labels a question lists as its options (a grid's scale points)."""
    from ..engine import render
    from ..engine.staged import _group_options

    view = render.Shown(shown)
    if view.kind == "choice":
        return [o.label for o in view.options]
    if view.kind == "ranking":
        return [i.label for i in view.rank_items]
    if view.kind in {"grid", "grid_text", "grid_sum"}:
        return [c.label for c in view.columns]
    if view.kind == "group":
        return [str(o["label"]) for o in _group_options(shown["question"])]
    return []


def full_question_options(blocks: Blocks, aliaser) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for questions in blocks:
        for shown in questions:
            qid = str(shown["question"].get("id", "") or "").strip()
            labels = [_public_text(label) for label in _option_labels(shown) if _public_text(label)]
            if not qid or not labels:
                continue
            for alias in aliaser(qid):
                out.setdefault(str(alias), labels)
    return out


def _is_text_only(shown: dict[str, Any]) -> bool:
    from ..engine import render

    return render.Shown(shown).kind == "instruction"


def _text(shown: dict[str, Any]) -> str:
    from ..engine import render

    return render.Shown(shown).text


def descriptive_context_by_question(blocks: Blocks) -> dict[str, str]:
    """For each question, the text shown just before it in its block (text that is
    shown only on a condition starts again)."""
    out: dict[str, str] = {}
    for questions in blocks:
        active_parts: list[str] = []
        last_was_descriptive = False
        for shown in questions:
            if _is_text_only(shown):
                text = _public_text(_text(shown))
                gated = bool(shown["question"].get("show_if"))
                if text and not gated:
                    if last_was_descriptive and active_parts:
                        active_parts.append(text)
                    else:
                        active_parts = [text]
                elif gated:
                    active_parts = []
                last_was_descriptive = not gated
                continue
            last_was_descriptive = False
            qid = str(shown["question"].get("id", "") or "").strip()
            context = " ".join(active_parts).strip()
            if qid and context:
                out[qid] = context
    return out


def shown_questions(blocks: Any) -> dict[str, dict[str, Any]]:
    """What a twin was shown for each answerable question of a copy or a stage
    (shown blocks): its options in the order shown, and the descriptive text above
    it, with piped and random values filled in."""
    from ..engine import render

    shown: dict[str, dict[str, Any]] = {}
    as_lists = copy_blocks(blocks)
    if not as_lists:
        return shown
    context = descriptive_context_by_question(as_lists)
    for question in render.prompt_questions([q for block in as_lists for q in block]):
        qid = str(question.get("id", "") or "").strip()
        if qid:
            shown[qid] = {
                "options": [_public_text(o) for o in question.get("options") or [] if _public_text(o)],
                "context": question_context_for(context, qid),
            }
    return shown


def piped_question_ids(definition: dict[str, Any] | None) -> set[str]:
    """Answer IDs whose options or descriptive context pipe or draw a value
    (``${...}``): for these a rerun needs what the twin was shown."""
    from ..results.layout import Layout

    aliaser = Layout(definition).aliaser()
    blocks = survey_blocks(definition)
    options = full_question_options(blocks, aliaser)
    piped = {qid for qid, labels in options.items() if any("${" in label for label in labels)}
    for qid, text in descriptive_context_by_question(blocks).items():
        if "${" in text:
            piped |= {str(alias) for alias in aliaser(qid)} | {qid}
    return piped


def pictures_by_question(blocks: Blocks) -> dict[str, list[dict[str, Any]]]:
    """The pictures a respondent saw with each question, keyed by question id,
    as sequence-media entries in the order shown: those of the descriptive
    elements above it, by the rule of descriptive_context_by_question, then its
    own (in its text, its options, its rows)."""
    from ..engine import render

    out: dict[str, list[dict[str, Any]]] = {}
    for questions in blocks:
        active: list[dict[str, Any]] = []
        last_was_descriptive = False
        for shown in questions:
            view = render.Shown(shown)
            qid = str(shown["question"].get("id", "") or "").strip()
            descriptive = view.kind == "instruction"
            media = render.question_media_entries(shown)
            entry = {
                "question_id": qid,
                "question_type": "DB" if descriptive else render._qualtrics_type(view),
                "question_text": _public_text(view.text),
                "prompt_label": "descriptive text" if descriptive else qid,
                "media": media,
            }
            # the question's text with each picture at its place
            with_pictures = render.text_with_pictures(view, media)
            if with_pictures:
                entry["question_text_with_pictures"] = with_pictures
            if descriptive:
                gated = bool(shown["question"].get("show_if"))
                if media and not gated:
                    active = [*active, entry] if last_was_descriptive else [entry]
                elif gated:
                    active = []
                last_was_descriptive = not gated
                continue
            last_was_descriptive = False
            seen = [*active, entry] if media else list(active)
            if qid and seen:
                out[qid] = seen
    return out


def best_worst_roles(blocks: Blocks) -> dict[str, tuple[str, str]]:
    """Each MaxDiff best or worst question's (group, role), by question ID."""
    out: dict[str, tuple[str, str]] = {}
    for questions in blocks:
        for shown in questions:
            pair = shown.get("best_worst") if isinstance(shown, dict) else None
            if isinstance(pair, dict) and pair.get("group") and pair.get("role") in {"best", "worst"}:
                out[str(shown["question"].get("id", ""))] = (str(pair["group"]), str(pair["role"]))
    return out


def ranking_items(blocks: Blocks) -> dict[str, dict[str, Any]]:
    """Each ranking item's answer ID (``<question>_<item>``) with its ranking: the
    question, the item, the number of items and the limits on how many to rank."""
    from ..engine import render

    out: dict[str, dict[str, Any]] = {}
    for questions in blocks:
        for shown in questions:
            view = render.Shown(shown)
            if view.kind != "ranking":
                continue
            low, high = view.limits()
            items = view.rank_items
            for item in items:
                out[f"{view.id}_{item.id}"] = {"group": view.id, "item": item.id, "label": item.label, "count": len(items), "min": low, "max": high}
    return out


def broken_rankings(values: dict[str, str], rankings: dict[str, dict[str, Any]], touched: set[str]) -> list[str]:
    """The rankings (question IDs) among those with a ``touched`` item whose ranks
    (``values``, by answer ID) are not a complete ranking the question allows: a
    rank given twice or skipped, more items ranked than allowed, or fewer than it
    requires (every item, without a minimum). An item with no usable rank (left
    out, blank, not a whole number) counts as unranked."""
    groups: dict[str, list[str]] = {}
    for answer_id, info in rankings.items():
        groups.setdefault(info["group"], []).append(answer_id)
    broken = []
    for group, answer_ids in groups.items():
        if not touched & set(answer_ids):
            continue
        ranks = []
        for answer_id in answer_ids:
            match = re.fullmatch(r"\s*(\d+)(?:\.0+)?\s*", str(values.get(answer_id, "") or ""))
            ranks.append(int(match.group(1)) if match else None)
        info = rankings[answer_ids[0]]
        if se.ranking_problem(ranks, min_items=info["min"], max_items=info["max"], complete=True):
            broken.append(group)
    return broken


def same_item_pairs(labels: dict[str, str], roles: dict[str, tuple[str, str]]) -> list[tuple[str, str]]:
    """The (best, worst) question IDs whose answers (``labels``, by question ID)
    name the same item."""
    groups: dict[str, dict[str, str]] = {}
    for qid, (group, role) in roles.items():
        groups.setdefault(group, {})[role] = qid
    out = []
    for pair in groups.values():
        best, worst = pair.get("best"), pair.get("worst")
        if not best or not worst:
            continue
        picked = se._normalize_option_text(labels.get(best, ""))
        if picked and picked == se._normalize_option_text(labels.get(worst, "")):
            out.append((best, worst))
    return out


def _question_key(by_qid: dict[str, Any], question_id: str) -> str:
    """The question id for a response-row question id, which may carry
    a sub-question suffix (QID12_3) or a loop prefix (1_QID12)."""
    qid = str(question_id or "").strip()
    if not qid or qid in by_qid:
        return qid
    base = re.sub(r"^\d+_", "", qid)
    return re.sub(r"_(?:TEXT|\d+)(?:_TEXT)?$", "", base)


def question_context_for(descriptive_by_qid: dict[str, str], question_id: str) -> str:
    return descriptive_by_qid.get(_question_key(descriptive_by_qid, question_id), "")


def rerun_pictures(blocks: Blocks, questions: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, str]]:
    """The pictures the twin saw with its questions, each once, in the order of
    the questions (sequence-media entries), and each question's id in the
    survey, which names the entry of its own pictures."""
    by_qid = pictures_by_question(blocks)
    entries: list[dict[str, Any]] = []
    seen: set[str] = set()
    owners: dict[str, str] = {}
    for question in questions:
        qid = str(question.get("id", ""))
        for entry in by_qid.get(_question_key(by_qid, qid), []):
            if entry["question_type"] != "DB":
                owners[qid] = entry["question_id"]
            if entry["question_id"] not in seen:
                seen.add(entry["question_id"])
                entries.append(entry)
    return entries, owners


def _observed_options(rows: list[dict[str, Any]]) -> dict[str, list[str]]:
    by_q: dict[str, list[str]] = {}
    seen: dict[str, set[str]] = defaultdict(set)
    for r in rows:
        qid = str(r.get("question_id", "")).strip()
        if not qid or _is_flagged(r.get("answer_status", "")):
            continue
        label = str(r.get("answer_label") or r.get("answer_value") or "").strip()
        if label and label not in seen[qid]:
            seen[qid].add(label)
            by_q.setdefault(qid, []).append(label)
    return by_q


# A text or number answer has no option list: other twins' answers are not options.
_FREE_ANSWER_TYPES = frozenset({"open_text", "numeric"})


def build_rerun_context(
    rows: list[dict[str, Any]],
    persona_id: str,
    *,
    definition: dict[str, Any] | None,
    shown: dict[str, dict[str, Any]] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, str], list[str]]:
    """``shown``: what this twin was shown (from the run's audit), used before the
    survey as written, so options and descriptive text carry the values the twin
    saw rather than the written ``${...}`` tokens."""
    from ..results.layout import Layout

    shown = shown or {}
    options_by_q = _observed_options(rows)
    blocks = survey_blocks(definition)
    full_options = full_question_options(blocks, Layout(definition).aliaser())
    context_by_qid = descriptive_context_by_question(blocks)
    twin_rows = [r for r in rows if str(r.get("persona_id", "")).strip() == persona_id]
    questions: list[dict[str, Any]] = []
    prior: dict[str, str] = {}
    flagged: list[str] = []
    seen_qids: set[str] = set()
    for r in sorted(twin_rows, key=lambda x: _qnum(str(x.get("question_id", "")))):
        qid = str(r.get("question_id", "")).strip()
        if not qid or qid in seen_qids:
            continue
        seen_qids.add(qid)
        text = str(r.get("question_text", ""))
        seen = shown.get(qid) or {}
        context = seen.get("context") if "context" in seen else question_context_for(context_by_qid, qid)
        if context and context not in text:
            text = f"{context} | {text}" if text else context
        qtype = str(r.get("question_type") or "open_text")
        if "options" in seen:
            options = list(seen["options"] or [])  # what the twin was shown, none included
        else:
            options = full_options.get(qid) or ([] if qtype in _FREE_ANSWER_TYPES else options_by_q.get(qid, []))
        questions.append({"id": qid, "text": text, "type": qtype, "options": options})
        repaired = str(r.get("answer_repaired") or "").strip()
        if _is_flagged(r.get("answer_status", "")) and not repaired:
            flagged.append(qid)
        else:
            prior[qid] = repaired or str(r.get("answer_label") or r.get("answer_value") or "")
    return questions, prior, flagged


def flagged_twins(rows: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = defaultdict(int)
    for r in rows:
        if _is_flagged(r.get("answer_status", "")) and not str(r.get("answer_repaired") or "").strip():
            pid = str(r.get("persona_id", "")).strip()
            if pid:
                counts[pid] += 1
    return {k: v for k, v in counts.items() if v > 0}


async def rerun_twin(
    *,
    caller: Any,
    rows: list[dict[str, Any]],
    persona_id: str,
    persona_text: str,
    definition: dict[str, Any] | None,
    repaired_by: str = "surveytwin",
    shown: dict[str, dict[str, Any]] | None = None,
    blocked_image_urls: list[str] | None = None,
    images: bool = True,
) -> dict[str, Any]:
    """Re-ask one twin's flagged questions; repairs are written into ``rows``.

    The pictures the twin saw go with the call. ``blocked_image_urls`` (the
    images the run left out) and, with ``images=False`` (a model without image
    input), every picture cannot be sent: a flagged question that shows one is
    not re-asked and stays flagged (counted as ``not_asked``), and a picture in
    descriptive text is only left out. So is a picture the provider refuses."""
    questions, prior, flagged = build_rerun_context(rows, persona_id, definition=definition, shown=shown)
    if not flagged:
        return {"persona_id": persona_id, "flagged": 0, "repaired": 0}
    blocks = survey_blocks(definition)
    pictures, owners = rerun_pictures(blocks, questions)
    roles = best_worst_roles(blocks)
    rankings = ranking_items(blocks)
    for question in questions:
        if question["id"] in roles:
            # a MaxDiff best or worst question: asked with the rule that the two differ
            question["best_worst_group_id"], question["best_worst_role"] = roles[question["id"]]
        if question["id"] in rankings:
            # a ranking item: asked with its rule (a unique rank from 1 to N)
            info = rankings[question["id"]]
            question.update({"parent_question_id": info["group"], "rank_order_item_id": info["item"], "rank_order_count": info["count"]})
            if info["min"] is not None:
                question["rank_order_min_items"] = info["min"]
            if info["max"] is not None:
                question["rank_order_max_items"] = info["max"]
    unavailable = {str(url).strip() for url in (blocked_image_urls or []) if str(url).strip()}
    if not images:
        unavailable |= se._sequence_media_urls(pictures)
    flagged_set = set(flagged)
    flagged_questions = [{**q, "parent_question_id": owners.get(q["id"], "")} for q in questions if q["id"] in flagged_set]
    raw: Any = None
    not_asked: set[str] = set()
    for _ in range(len(se._sequence_media_urls(pictures)) + 1):
        sendable, not_asked, _ = se._leave_out_unavailable_pictures(pictures, flagged_questions, unavailable)
        asked = [qid for qid in flagged if qid not in not_asked]
        if not asked:
            break
        try:
            with call_context(twin_id=persona_id, stage=None, purpose="rerun"):
                if sendable:
                    items = se.build_rerun_input_items(persona_text, questions, prior_answers=prior, flagged_qids=asked, pictures=sendable)
                    raw = json.loads(extract_json_string(await caller.responses_input_text(input_items=items, max_tokens=RERUN_MAX_TOKENS)))
                else:
                    system_prompt, user_prompt = se.build_rerun_prompt_parts(persona_text, questions, prior_answers=prior, flagged_qids=asked)
                    raw = await caller.chat_json(system_prompt=system_prompt, user_prompt=user_prompt, temperature=0.3, max_tokens=RERUN_MAX_TOKENS)
        except Exception as exc:  # noqa: BLE001
            if sendable and se._is_image_input_error(exc):
                sent = se._sequence_media_urls(sendable)
                unavailable |= se._pictures_named_in_error(exc, sent) or sent
                continue
            return {"persona_id": persona_id, "flagged": len(flagged), "repaired": 0, "error": str(exc)[:300]}
        break
    answers = raw.get("answers") if isinstance(raw, dict) else None
    qmap = {q["id"]: q for q in questions}
    flagged_set -= not_asked
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    found: dict[str, str] = {}
    for a in answers or []:
        if not isinstance(a, dict):
            continue
        qid = str(a.get("question_id", "")).strip()
        if qid not in flagged_set or qid not in qmap:
            continue
        norm = se._normalize_answer_for_question(a, {**qmap[qid], "force_response": True})
        label = str(norm.get("answer_label") or norm.get("answer_value") or "").strip()
        if label and not _is_flagged(norm.get("answer_status", "")):
            found[qid] = label
    # Best and worst must still differ: a repair that names the item its pair's
    # other answer names is not stored, and the answer stays flagged.
    same_item = [pair for pair in same_item_pairs({**prior, **found}, roles) if any(qid in found for qid in pair)]
    for pair in same_item:
        for qid in pair:
            found.pop(qid, None)
    # A ranking must still be one the question allows, with the ranks the twin
    # already gave: otherwise none of its repairs is stored, and it stays flagged.
    broken = broken_rankings({**prior, **found}, rankings, set(found))
    for group in broken:
        for answer_id, info in rankings.items():
            if info["group"] == group:
                found.pop(answer_id, None)
    for qid, label in found.items():
        for row in rows:
            if str(row.get("persona_id", "")) == persona_id and str(row.get("question_id", "")) == qid:
                row["answer_repaired"] = label
                row["repair_method"] = REPAIR_METHOD_RERUN
                row["repaired_by"] = repaired_by
                row["repaired_at"] = now
    result = {"persona_id": persona_id, "flagged": len(flagged), "repaired": len(found)}
    if not_asked:
        result["not_asked"] = len(not_asked)
    if same_item:
        result["same_item"] = len(same_item)
    if broken:
        result["broken_rankings"] = len(broken)
    return result


async def rerun_all(
    *,
    caller: Any,
    rows: list[dict[str, Any]],
    persona_texts: dict[str, str],
    definition: dict[str, Any] | None,
    concurrency: int = RERUN_CONCURRENCY,
    shown_by_twin: dict[str, dict[str, dict[str, Any]]] | None = None,
    blocked_image_urls: list[str] | None = None,
    images: bool = True,
) -> dict[str, Any]:
    targets = [pid for pid in flagged_twins(rows) if pid in persona_texts]
    missing = [pid for pid in flagged_twins(rows) if pid not in persona_texts]
    sem = asyncio.Semaphore(max(1, int(concurrency)))

    async def one(pid: str) -> dict[str, Any]:
        async with sem:
            return await rerun_twin(
                caller=caller,
                rows=rows,
                persona_id=pid,
                persona_text=persona_texts[pid],
                definition=definition,
                shown=(shown_by_twin or {}).get(pid),
                blocked_image_urls=blocked_image_urls,
                images=images,
            )

    outcomes = await asyncio.gather(*[one(pid) for pid in targets])
    return {
        "twins": len(targets),
        "flagged": sum(o.get("flagged", 0) for o in outcomes),
        "repaired": sum(o.get("repaired", 0) for o in outcomes),
        "not_asked": sum(o.get("not_asked", 0) for o in outcomes),
        "same_item": sum(o.get("same_item", 0) for o in outcomes),
        "broken_rankings": sum(o.get("broken_rankings", 0) for o in outcomes),
        "errors": [o for o in outcomes if o.get("error")],
        "missing_persona_text": missing,
    }
