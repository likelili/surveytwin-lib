"""Reading a QSF into a ``Survey``, previewing and dropping blocks, and checking it.

A ``Survey`` holds the survey in the standard format (``survey.standard``), which
planning, prompts and exports run on, and the same survey as a template
(``{"Metadata": ..., "Elements": ...}``, the website's form), which its
Qualtrics-shaped views read: its questions, blocks, flow, drops and checks.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
from dataclasses import dataclass, field
from functools import cached_property, lru_cache
from importlib import resources
from pathlib import Path
from typing import Any, Iterator

from ._display import Listing, plural, short_list, table
from .check.qsf_checks import (
    BLOCKING_IMAGE_STATUSES,
    survey_image_warnings,
    detect_unsupported_qsf_traits,
)
from .errors import QSFReadError, SurveyError
from .prompts.instrument import _filter_qsf_template_blocks
from .runtime.choice_logic import applies as choice_logic_applies, logic_of as choice_logic_of
from .runtime.instrument_plan import parse_template, remove_template_questions

_QID_RE = re.compile(r"QID\d+")
_Q_PIPE_RE = re.compile(r"\$\{q://(?:\d+_)?(QID\d+)")  # a Loop & Merge pipe reads "1_QID5"
_ED_FIELD_RE = re.compile(r"\$\{e://Field/([^}]+)\}")
_ANSWERABLE_TYPES = {"MC", "TE", "Matrix", "Slider", "CS", "Rank", "RO", "SBS"}


@lru_cache(maxsize=1)
def support_table() -> dict[str, Any]:
    text = resources.files("surveytwin").joinpath("data/support_table.json").read_text(encoding="utf-8")
    return json.loads(text)


def _feature_level(feature_id: str) -> str:
    if feature_id == "execution.staged":
        return "Full"
    for row in support_table()["features"]:
        if row["id"] == feature_id:
            return row["level"]
    return "Detected"


# ---------------------------------------------------------------------------
# records
# ---------------------------------------------------------------------------


@dataclass
class Finding:
    feature: str
    level: str
    source: str
    message: str


@dataclass
class BlockPreview:
    block_id: str
    name: str
    flow_path: list[str]
    question_counts: dict[str, int]
    answerable: int
    images: list[str]
    findings: list[Finding]
    screenout: bool
    referenced_by: list[str]
    dropped: bool = False

    def _row(self) -> list[str]:
        counts = ", ".join(f"{kind} {n}" for kind, n in self.question_counts.items()) or "no questions"
        flags = []
        if self.dropped:
            flags.append("DROPPED")
        if self.images:
            flags.append(plural(len(self.images), "image"))
        if self.screenout:
            flags.append("screen-out")
        if self.findings:
            flags.append(plural(len(self.findings), "finding"))
        return [self.block_id, repr(self.name), f"{self.answerable} to answer", counts, "  ".join(flags)]

    def __str__(self) -> str:
        lines = table([self._row()], indent="")
        for ref in self.referenced_by:
            lines.append(f"  read by the rest of the survey: {ref}")
        for f in self.findings:
            lines.append(f"  [{f.level}] {f.message}")
        return "\n".join(lines)


def _render_blocks(blocks: list[BlockPreview]) -> list[str]:
    lines: list[str] = []
    rows = table([b._row() for b in blocks])
    for block, row in zip(blocks, rows):
        lines.append(row)
        for ref in block.referenced_by:
            lines.append(f"      read by the rest of the survey: {ref}")
    return lines


@dataclass
class Dependency:
    kind: str
    location: str
    reads: str


@dataclass
class DropReport:
    """What dropping blocks (and excluding questions when reading) removed, and what
    in the rest of the survey still mentions a dropped question. A dropped question
    no longer exists: nothing asks it, and logic that reads it sees a question that
    was never shown, with no answer (as Qualtrics treats a question a respondent
    never saw). Display logic on single choices that reads one is listed apart
    (``choice_logic``): an option shown only when the dropped question was
    answered a certain way is hidden."""

    dropped_blocks: list[str] = field(default_factory=list)
    dropped_questions: list[str] = field(default_factory=list)
    pruned_branches: list[str] = field(default_factory=list)
    references: list[Dependency] = field(default_factory=list)
    choice_logic: list[Dependency] = field(default_factory=list)
    excluded_questions: list[str] = field(default_factory=list)
    moved_skips: list[str] = field(default_factory=list)

    def __str__(self) -> str:
        if not self.dropped_blocks and not self.excluded_questions:
            return "Nothing dropped."
        lines = []
        if self.dropped_blocks:
            lines += [
                f"Dropped blocks: {', '.join(self.dropped_blocks)}",
                f"Dropped questions ({len(self.dropped_questions)}): {short_list(self.dropped_questions, 12)}",
            ]
        if self.excluded_questions:
            lines.append(f"Excluded when read ({len(self.excluded_questions)}): {short_list(self.excluded_questions, 12)}")
        if self.pruned_branches:
            what = "dropped or excluded questions" if self.excluded_questions else "dropped questions"
            lines.append(f"Branches that read only {what} and run no block, removed with them: {', '.join(self.pruned_branches)}")
        if self.moved_skips:
            lines.append("Skip logic that jumped to an excluded question goes on to the next question kept, as for a question never shown: "
                         + "; ".join(self.moved_skips))
        if self.references:
            lines.append("Logic that reads a dropped question (it counts as never shown, with no answer):")
            lines += [f"  {d.kind} at {d.location} reads {d.reads}" for d in self.references]
        if self.choice_logic:
            lines.append("Display logic on choices that reads a dropped question (it counts as never shown, with no answer):")
            lines += [f"  {d.location} reads {d.reads}" for d in self.choice_logic]
        if self.dropped_blocks and not self.references and not self.choice_logic:
            lines.append("Nothing left in the survey reads the dropped questions.")
        if self.excluded_questions:
            lines.append("survey.check() lists the logic that reads an excluded question.")
        return "\n".join(lines)


@dataclass
class SurveyCheck:
    mode: str
    staging_reasons: list[str]
    findings: list[Finding]
    unsupported_questions: list[str]
    images: list[dict[str, str]]
    design_cells: int
    unset_embedded_fields: list[str]
    ok: bool
    reads_dropped: list[str] = field(default_factory=list)

    def __str__(self) -> str:
        lines = [f"Execution mode: {self.mode}"]
        if self.staging_reasons:
            lines.append("Staged because: " + "; ".join(self.staging_reasons))
        lines.append(f"Design cells: {self.design_cells}")
        if self.unsupported_questions:
            lines.append("Unsupported questions (blank column, never shown): " + ", ".join(self.unsupported_questions))
        if self.unset_embedded_fields:
            lines.append("Quoted embedded-data fields never set in the QSF (shown blank): " + ", ".join(self.unset_embedded_fields))
        for f in self.findings:
            lines.append(f"[{f.level}] {f.feature} {f.source}: {f.message}".rstrip())
        if self.reads_dropped:
            lines.append("Reads a dropped question (it counts as never shown, with no answer): " + "; ".join(self.reads_dropped))
        bad_images = [i for i in self.images if i.get("status") in BLOCKING_IMAGE_STATUSES or i.get("status") == "unresolved"]
        if self.images:
            unchecked = [i for i in self.images if i.get("status") == "not_checked"]
            if unchecked and len(unchecked) == len(self.images):
                lines.append(f"Images: {len(self.images)} found, links not tested (use network=True)")
            else:
                lines.append(f"Images: {len(self.images)} checked, {len(bad_images)} unreachable, behind a login, or not found")
        lines.append("OK" if self.ok else "Needs attention before a run")
        return "\n".join(lines)


class Question:
    """A read-only view of one QSF question in the template."""

    def __init__(self, raw: dict[str, Any], block_id: str) -> None:
        self.raw = raw
        self.block_id = block_id

    def __repr__(self) -> str:
        return f"Question({self.id!r}, {self.type}/{self.selector})"

    @property
    def id(self) -> str:
        return str(self.raw.get("QuestionID", ""))

    @property
    def export_tag(self) -> str:
        return str(self.raw.get("QuestionName") or self.raw.get("DataExportTag") or self.id)

    @property
    def type(self) -> str:
        return str(self.raw.get("RawQuestionType") or self.raw.get("QuestionType") or "")

    @property
    def selector(self) -> str:
        settings = self.raw.get("Settings") if isinstance(self.raw.get("Settings"), dict) else {}
        return str(settings.get("Selector") or self.raw.get("Selector") or "")

    @property
    def text(self) -> str:
        return str(self.raw.get("QuestionText", "") or "")

    @property
    def html(self) -> str:
        return str(self.raw.get("QuestionTextHTML") or self.raw.get("QuestionText") or "")

    @property
    def media(self) -> list[Any]:
        return list(self.raw.get("QuestionMedia") or [])

    @property
    def kind(self) -> str:
        qtype = str(self.raw.get("QuestionType", "") or "")
        if self.raw.get("is_unsupported"):
            return "unsupported"
        if qtype == "DB" or self.raw.get("is_descriptive"):
            return "descriptive"
        if qtype.upper() in {"TIMING"}:
            return "timing"
        if qtype.upper() in {"META"}:
            return "meta info"
        if qtype.upper() in {"CAPTCHA"}:
            return "captcha"
        return "answerable"

    @property
    def options(self) -> list[Any]:
        return list(self.raw.get("Options") or [])

    @property
    def option_ids(self) -> list[Any]:
        return list(self.raw.get("OptionsID") or [])

    @property
    def rows(self) -> list[Any]:
        return list(self.raw.get("Rows") or self.raw.get("Statements") or [])

    @property
    def columns(self) -> list[Any]:
        return list(self.raw.get("Columns") or self.raw.get("Options") or [])

    @property
    def statements(self) -> list[Any]:
        return list(self.raw.get("Statements") or [])

    @property
    def display_logic(self) -> Any:
        return self.raw.get("DisplayLogic")

    @property
    def skip_logic(self) -> Any:
        return self.raw.get("SkipLogic")

    @property
    def validation(self) -> Any:
        return self.raw.get("Validation")

    @property
    def randomization(self) -> Any:
        return self.raw.get("Randomization")

    @property
    def recode_values(self) -> Any:
        return self.raw.get("RecodeValues")

    @property
    def defaults(self) -> Any:
        return self.raw.get("DefaultChoices") or self.raw.get("DefaultChoiceLabels")

    @property
    def source(self) -> str:
        return f"SQ {self.id}"


# ---------------------------------------------------------------------------
# helpers over the template
# ---------------------------------------------------------------------------


def _walk(elements: Any, path: list[str] | None = None) -> Iterator[tuple[dict[str, Any], list[str]]]:
    if not isinstance(elements, list):
        return
    for element in elements:
        if not isinstance(element, dict):
            continue
        here = list(path or [])
        yield element, here
        kind = str(element.get("ElementType", "") or "")
        label = kind
        if kind == "Randomizer":
            label = f"Randomizer {element.get('FlowID', '')}".strip()
        elif kind == "Branch":
            label = f"Branch {element.get('FlowID', '')}".strip()
        elif kind == "Group":
            label = f"Group {element.get('Description') or element.get('FlowID', '')}".strip()
        yield from _walk(element.get("Elements"), here + ([label] if kind != "Block" else []))


def _blocks(template: dict[str, Any]) -> list[tuple[dict[str, Any], list[str]]]:
    seen: set[str] = set()
    out: list[tuple[dict[str, Any], list[str]]] = []
    for element, path in _walk(template.get("Elements", [])):
        if element.get("ElementType") == "Block":
            block_id = str(element.get("BlockID", "") or "")
            if block_id and block_id not in seen:
                seen.add(block_id)
                out.append((element, path))
    return out


def _skip_rules(question: dict[str, Any]) -> list[dict[str, Any]]:
    rules = question.get("SkipLogic")
    if isinstance(rules, dict):
        rules = list(rules.values())
    return [r for r in rules or [] if isinstance(r, dict)] if isinstance(rules, list) else []


def _qsf_block_orders(qsf: dict[str, Any] | None) -> dict[str, list[str]]:
    """Block ID -> its questions in the QSF's own order (excluded questions included)."""
    out: dict[str, list[str]] = {}
    for element in (qsf or {}).get("SurveyElements") or []:
        if not (isinstance(element, dict) and element.get("Element") == "BL"):
            continue
        payload = element.get("Payload")
        for block in (payload.values() if isinstance(payload, dict) else payload or []):
            if isinstance(block, dict) and block.get("ID"):
                out[str(block["ID"])] = [
                    str(e.get("QuestionID")) for e in block.get("BlockElements") or []
                    if isinstance(e, dict) and e.get("Type") == "Question" and e.get("QuestionID")
                ]
    return out


def _qids_in(value: Any) -> set[str]:
    return set(_QID_RE.findall(json.dumps(value, ensure_ascii=False))) if value else set()


def _find_dependencies(template: dict[str, Any], dropped_qids: set[str], dropped_blocks: set[str]) -> list[Dependency]:
    """Logic outside the dropped blocks that still reads a dropped question."""
    found: list[Dependency] = []
    for element, _path in _walk(template.get("Elements", [])):
        kind = str(element.get("ElementType", "") or "")
        flow_id = str(element.get("FlowID", "") or "")
        if kind == "Branch":
            for qid in sorted(_qids_in(element.get("BranchLogic")) & dropped_qids):
                found.append(Dependency("branch", f"FL {flow_id}", qid))
        if kind == "EmbeddedData":
            for row in element.get("EmbeddedData") or []:
                for qid in sorted(set(_Q_PIPE_RE.findall(json.dumps(row, ensure_ascii=False))) & dropped_qids):
                    found.append(Dependency("embedded_data", f"FL {flow_id}", qid))
        if kind == "Block" and str(element.get("BlockID", "")) not in dropped_blocks:
            looping = (element.get("Options") or {}).get("LoopingOptions") if isinstance(element.get("Options"), dict) else None
            for qid in sorted(set(_Q_PIPE_RE.findall(json.dumps(looping or {}, ensure_ascii=False))) & dropped_qids):
                found.append(Dependency("loop_fields", f"BL {element.get('BlockID', '')}", qid))
            for question in element.get("Questions") or []:
                if not isinstance(question, dict):
                    continue
                qid = str(question.get("QuestionID", ""))
                for dep in sorted(_qids_in(question.get("DisplayLogic")) & dropped_qids):
                    found.append(Dependency("display_logic", f"SQ {qid}", dep))
                for key, place in (("ChoiceDisplayLogic", "choice"), ("AnswerDisplayLogic", "scale point")):
                    by_item = question.get(key) if isinstance(question.get(key), dict) else {}
                    for item_id, logic in by_item.items():
                        for dep in sorted(_qids_in(logic) & dropped_qids):
                            found.append(Dependency("choice_display_logic", f"SQ {qid} {place} {item_id}", dep))
                for rule in _skip_rules(question):
                    destination = str(rule.get("SkipToDestination", "") or "").strip()
                    if destination in dropped_qids:
                        found.append(Dependency("skip_logic", f"SQ {qid}", destination))
                piped = set(_Q_PIPE_RE.findall(json.dumps(
                    {k: v for k, v in question.items() if k not in {"DisplayLogic", "SkipLogic", "ChoiceDisplayLogic", "AnswerDisplayLogic"}},
                    ensure_ascii=False)))
                for dep in sorted(piped & dropped_qids):
                    found.append(Dependency("piping", f"SQ {qid}", dep))
    unique: list[Dependency] = []
    seen: set[tuple[str, str, str]] = set()
    for dep in found:
        key = (dep.kind, dep.location, dep.reads)
        if key not in seen:
            seen.add(key)
            unique.append(dep)
    return unique


def _choice_display_logic(qsf: dict[str, Any] | None) -> dict[str, list[tuple[str, set[str]]]]:
    """QuestionID -> [(choice key, QIDs its logic reads)]: display logic on single
    choices (or matrix statements, scale points), from the QSF's own question
    payloads. Both runtimes apply it."""
    found: dict[str, list[tuple[str, set[str]]]] = {}
    for element in (qsf or {}).get("SurveyElements") or []:
        if not isinstance(element, dict) or element.get("Element") != "SQ" or not isinstance(element.get("Payload"), dict):
            continue
        payload = element["Payload"]
        qid = str(payload.get("QuestionID") or element.get("PrimaryAttribute") or "")
        for group in ("Choices", "Answers"):
            items = payload.get(group)
            if not isinstance(items, dict):
                continue
            for key, choice in items.items():
                if isinstance(choice, dict) and choice.get("DisplayLogic"):
                    found.setdefault(qid, []).append((str(key), _qids_in(choice["DisplayLogic"])))
    return found


def _quoted_fields(value: Any) -> set[str]:
    return set(_ED_FIELD_RE.findall(json.dumps(value, ensure_ascii=False))) if value else set()


def _set_fields(template: dict[str, Any]) -> set[str]:
    fields: set[str] = set()
    for element, _path in _walk(template.get("Elements", [])):
        if element.get("ElementType") == "EmbeddedData":
            for row in element.get("EmbeddedData") or []:
                if isinstance(row, dict) and str(row.get("Field", "")).strip():
                    fields.add(str(row["Field"]).strip())
                    fields.add(str(row["Field"]).strip().replace(" ", ""))
    return fields


_JS_SETS_FIELD = re.compile(r"set(JS)?EmbeddedData\(\s*['\"]([^'\"]+)['\"]")
_JS_SETS_ANY_FIELD = re.compile(r"set(?:JS)?EmbeddedData\(\s*([^'\"\s)][^,)]*)")
_JS_COMMENT = re.compile(r"/\*.*?\*/|(?<![:\\'\"])//[^\n]*", re.DOTALL)
# The row Qualtrics puts in a new embedded-data element before a field is chosen.
_PLACEHOLDER_FIELD = "Create New Field or Choose From Dropdown..."


def _scripts(qsf: dict[str, Any] | None, question_ids: set[str]) -> list[tuple[str, str]]:
    """(where, JavaScript) that runs in the survey: each asked question's script
    and the survey options' header and footer, comments removed."""
    found: list[tuple[str, str]] = []
    for element in (qsf or {}).get("SurveyElements") or []:
        if not isinstance(element, dict) or not isinstance(element.get("Payload"), dict):
            continue
        payload = element["Payload"]
        if element.get("Element") == "SQ":
            qid = str(payload.get("QuestionID") or element.get("PrimaryAttribute") or "")
            if qid in question_ids and isinstance(payload.get("QuestionJS"), str):
                found.append((f"SQ {qid}", payload["QuestionJS"]))
        elif element.get("Element") == "SO":
            for part in ("Header", "Footer"):
                if isinstance(payload.get(part), str) and "EmbeddedData" in payload[part]:
                    found.append((f"survey {part.lower()}", payload[part]))
    return [(where, _JS_COMMENT.sub("", script)) for where, script in found]


@dataclass
class EmbeddedField:
    """One embedded-data field: where the survey sets it and where it reads it.

    ``set_in``: survey-flow elements that set a value (``FL FL_3``);
    ``from_outside``: elements that take it from a panel, contact list or the
    survey link (Qualtrics' "set from panel or URL"); ``set_by_javascript``:
    questions whose JavaScript sets it, which the simulation does not run;
    ``read_by``: question text, other fields' values and logic that read it.
    """

    name: str
    set_in: list[str] = field(default_factory=list)
    from_outside: list[str] = field(default_factory=list)
    set_by_javascript: list[str] = field(default_factory=list)
    read_by: list[str] = field(default_factory=list)
    variable_type: str = ""

    @property
    def source(self) -> str:
        if self.set_in:
            return "set in the flow"
        if self.set_by_javascript:
            return "set by JavaScript"
        if self.from_outside:
            return "from a panel or the survey link"
        return "never set"

    def _row(self) -> list[str]:
        where = self.set_in or self.set_by_javascript or self.from_outside
        return [self.name, self.source, short_list(where, 3) if where else "-", ("read by " + short_list(self.read_by, 3)) if self.read_by else "not read"]


def _render_fields(fields: list[EmbeddedField]) -> list[str]:
    return table([f._row() for f in fields])


def _logic_condition_nodes(logic: Any) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []

    def walk(value: Any) -> None:
        if isinstance(value, dict):
            if "LogicType" in value:
                found.append(value)
            for child in value.values():
                walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)

    walk(logic)
    return found


def _embedded_field_accounting(template: dict[str, Any], qsf: dict[str, Any] | None, question_ids: set[str]) -> dict[str, EmbeddedField]:
    """Every embedded-data field the survey sets, declares, sets by JavaScript or reads."""
    from urllib.parse import unquote

    fields: dict[str, EmbeddedField] = {}

    def entry(name: Any) -> EmbeddedField:
        clean = unquote(str(name)).strip()
        return fields.setdefault(clean, EmbeddedField(clean))

    for element, _path in _walk(template.get("Elements", [])):
        kind = element.get("ElementType")
        flow = f"FL {element.get('FlowID', '')}"
        if kind == "EmbeddedData":
            for row in element.get("EmbeddedData") or []:
                if not isinstance(row, dict) or str(row.get("Field", "")).strip() in {"", _PLACEHOLDER_FIELD}:
                    continue
                record = entry(row["Field"])
                (record.from_outside if row.get("Value") in (None, "") else record.set_in).append(flow)
                record.variable_type = record.variable_type or str(row.get("VariableType") or "")
                for quoted in _quoted_fields(row.get("Value")):
                    entry(quoted).read_by.append(f"{flow} {row['Field']}")
            for row in element.get("Declared") or []:
                if isinstance(row, dict) and str(row.get("Field", "")).strip() not in {"", _PLACEHOLDER_FIELD}:
                    record = entry(row["Field"])
                    record.from_outside.append(flow)
                    record.variable_type = record.variable_type or str(row.get("VariableType") or "")
        if kind == "Branch":
            for condition in _logic_condition_nodes(element.get("BranchLogic")):
                if condition.get("LogicType") == "EmbeddedField" and str(condition.get("LeftOperand", "")).strip():
                    entry(condition["LeftOperand"]).read_by.append(f"branch {flow}")
        if kind == "Block":
            for question in element.get("Questions") or []:
                if not isinstance(question, dict):
                    continue
                qid = str(question.get("QuestionID", ""))
                content = {k: v for k, v in question.items() if k not in {"DisplayLogic", "SkipLogic", "ChoiceDisplayLogic", "AnswerDisplayLogic"}}
                for quoted in _quoted_fields(content):
                    entry(quoted).read_by.append(f"SQ {qid}")
                for condition in _logic_condition_nodes(question.get("DisplayLogic")):
                    if condition.get("LogicType") == "EmbeddedField" and str(condition.get("LeftOperand", "")).strip():
                        entry(condition["LeftOperand"]).read_by.append(f"display logic SQ {qid}")
                for condition in _logic_condition_nodes(choice_logic_of(question)):
                    if condition.get("LogicType") == "EmbeddedField" and str(condition.get("LeftOperand", "")).strip():
                        entry(condition["LeftOperand"]).read_by.append(f"choice display logic SQ {qid}")
    for where, script in _scripts(qsf, question_ids):
        for js_prefix, name in _JS_SETS_FIELD.findall(script):
            entry(("__js_" if js_prefix else "") + name).set_by_javascript.append(where)
    for record in fields.values():
        for attribute in ("set_in", "from_outside", "set_by_javascript", "read_by"):
            setattr(record, attribute, list(dict.fromkeys(getattr(record, attribute))))
    return fields


def _javascript_computing_field_names(qsf: dict[str, Any] | None, question_ids: set[str]) -> list[str]:
    """Questions whose JavaScript sets embedded-data fields under names it computes
    (``setEmbeddedData(keys[i], ...)``), so which fields they set cannot be read."""
    return [where for where, script in _scripts(qsf, question_ids) if _JS_SETS_ANY_FIELD.search(script)]


# ---------------------------------------------------------------------------
# the Survey
# ---------------------------------------------------------------------------


class _Unchanged:
    """The default of ``Survey.update_question``'s parameters: leave that part as it is."""

    def __repr__(self) -> str:
        return "unchanged"


_UNCHANGED: Any = _Unchanged()


class Survey:
    """One survey: its definition in the standard format, which every run reads, and
    views of its flow, blocks and questions.

    Get one from ``read_survey`` (a survey written in the standard format),
    ``read_qsf`` (a Qualtrics export), ``new_survey`` (an empty survey to fill), or
    ``surveytwin.builder.build_survey`` (a web form's draft). A survey never
    changes: ``add_question`` and the other edit methods, ``drop_blocks`` and the
    draft builder's edit functions return a new one.
    """

    def __init__(
        self,
        template: dict[str, Any],
        *,
        kind: str = "template",
        source_name: str = "",
        qsf: dict[str, Any] | None = None,
        raw_bytes: bytes | None = None,
        exclude_blocks: list[str] | None = None,
        exclude_questions: list[str] | None = None,
        unresolved_media: list[dict[str, Any]] | None = None,
        authoring: dict[str, dict[str, Any]] | None = None,
        edits: list[dict[str, Any]] | None = None,
        standard: dict[str, Any] | None = None,
        pictures: dict[str, str] | None = None,
    ) -> None:
        if template is None and kind == "standard" and isinstance(standard, dict):
            pass  # written in the standard format: its template is made when a view needs it
        elif not isinstance(template, dict) or "SurveyElements" in template or not isinstance(template.get("Elements"), list):
            raise SurveyError(
                "Survey(...) takes a parsed survey template; read a Qualtrics export with st.read_qsf(...) "
                "or build a survey with surveytwin.builder.build_survey(...)"
            )
        # The whole survey as read or built (None for one written in the standard
        # format, until a view needs it). Never modified: every change makes a new Survey.
        self._template = template
        self.kind = kind
        self.source_name = source_name
        self._qsf = qsf
        self._raw_bytes = raw_bytes if raw_bytes is not None else b""
        self.exclude_blocks = sorted({str(b).strip() for b in (exclude_blocks or []) if str(b).strip()})
        self.exclude_questions = sorted({str(q).strip() for q in (exclude_questions or []) if str(q).strip()})
        # Builder-only: image references the resolver could not turn into media,
        # and each builder question's draft form (never shown to twins).
        self._unresolved_media = [dict(m) for m in (unresolved_media or [])]
        self._authoring = copy.deepcopy(authoring or {})
        # What was changed after reading or building, in order.
        self.edits = [dict(e) for e in (edits or [])]
        # The survey in the standard format, as read (a written survey, or a QSF
        # read into that format). For a written survey it is the survey itself.
        self._standard = standard
        # The picture files a written survey names, read when it was read:
        # {path as written: data address}. A run sends these (spec/pictures.py).
        self._pictures = dict(pictures or {})
        # Warnings from the builder edit that made this survey (a policy's
        # demographic warning, ...); not part of its definition.
        self.edit_warnings: list[Any] = []
        # Warnings from reading a survey written in the standard format (a variable
        # never set, a block never shown); not part of its definition.
        self.read_warnings: list[Any] = []
        # Where a picture path an edit names starts when it is relative: the folder
        # of the survey's JSON file (or read_survey's ``folder``); None for the
        # current folder. Not saved with the survey.
        self._picture_folder: Path | None = None

    def _derive(self, **changes: Any) -> "Survey":
        """A new Survey with some fields replaced (the others carried over)."""
        fields = {
            "template": self._template,
            "kind": self.kind,
            "source_name": self.source_name,
            "qsf": self._qsf,
            "raw_bytes": self._raw_bytes,
            "exclude_blocks": self.exclude_blocks,
            "exclude_questions": self.exclude_questions,
            "unresolved_media": self._unresolved_media,
            "authoring": self._authoring,
            "edits": self.edits,
            "standard": self._standard,
            "pictures": self._pictures,
        }
        fields.update(changes)
        template = fields.pop("template")
        survey = Survey(template, **fields)
        survey._picture_folder = self._picture_folder
        return survey

    @cached_property
    def _base_template(self) -> dict[str, Any]:
        """The whole survey as a template: as read or built, or, for a survey written
        in the standard format, compiled from it (for the Qualtrics-shaped views)."""
        if self._template is not None:
            return self._template
        from .spec.compile import compile_survey

        return compile_survey(self._standard)

    def _with_pictures(self, definition: dict[str, Any]) -> dict[str, Any]:
        """``definition`` with each picture file it names replaced by the picture,
        as a run sends it (a copy when there are files; the same object otherwise)."""
        from .spec.pictures import with_pictures

        return with_pictures(definition, self._pictures)

    def _runnable_standard(self) -> dict[str, Any]:
        """The survey in the standard format as it runs (``standard``), its picture
        files read into the definition."""
        return self._with_pictures(self.standard)

    @property
    def _definition_edited(self) -> bool:
        """The last edit was made on the definition (``add_question`` and the other
        edit methods): the definition kept is the survey as it is now, and its
        template is compiled from it. An edit of the draft builder changes the
        template instead, and the definition is then read from the template."""
        return bool(self.edits) and self.edits[-1].get("on") == "definition"

    @property
    def _written(self) -> bool:
        """Written in the standard format and not edited since, or edited only on its
        definition: the definition is the survey, and its template only a view of it."""
        return self.kind == "standard" and self._standard is not None and (not self.edits or self._definition_edited)

    # -- saving -----------------------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        """The whole survey definition as JSON: the template with every rule and
        expression (``${e://...}``, ``${rand://...}``, logic, embedded-data
        declarations), never a twin's values; what was dropped or excluded; the
        builder's drafts and the edits; and the QSF file as read. ``from_dict``
        rebuilds the same survey (same checksum)."""
        import base64

        return {
            "format": "surveytwin-survey",
            "format_version": 1,
            "kind": self.kind,
            "source_name": self.source_name,
            "checksum_sha256": self.checksum,
            "definition_sha256": self._definition_digest(),
            "template": copy.deepcopy(self._base_template),
            "exclude_blocks": list(self.exclude_blocks),
            "exclude_questions": list(self.exclude_questions),
            "authoring": copy.deepcopy(self._authoring),
            "unresolved_media": [dict(m) for m in self._unresolved_media],
            "edits": [dict(e) for e in self.edits],
            **({"pictures": dict(self._pictures)} if self._pictures else {}),
            "qsf_base64": base64.b64encode(self._raw_bytes).decode("ascii") if self._raw_bytes and self.kind != "standard" else "",
            **(
                {
                    "standard": copy.deepcopy(self._standard),
                    "source_base64": base64.b64encode(self._raw_bytes).decode("ascii") if self._raw_bytes else "",
                }
                if self.kind == "standard" and self._standard is not None
                else {"standard": copy.deepcopy(self._standard)}
                if self._definition_edited
                else {}
            ),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Survey":
        """A survey saved with ``to_dict``. Refuses one whose checksum no longer matches."""
        import base64

        if not isinstance(data, dict) or data.get("format") != "surveytwin-survey":
            raise SurveyError("Not a saved survey (expected the output of Survey.to_dict())")
        raw = base64.b64decode(data["qsf_base64"]) if data.get("qsf_base64") else b""
        if not raw and data.get("source_base64"):
            raw = base64.b64decode(data["source_base64"])  # a survey written in the standard format, as read
        qsf = None
        if raw and data.get("kind") != "standard":
            try:
                qsf = json.loads(raw.decode("utf-8-sig"))
            except (UnicodeDecodeError, ValueError) as exc:
                raise SurveyError(f"The saved survey's QSF cannot be read: {exc}") from exc
        survey = cls(
            data["template"],
            kind=str(data.get("kind") or "template"),
            source_name=str(data.get("source_name") or ""),
            qsf=qsf,
            raw_bytes=raw or None,
            exclude_blocks=data.get("exclude_blocks"),
            exclude_questions=data.get("exclude_questions"),
            unresolved_media=data.get("unresolved_media"),
            authoring=data.get("authoring"),
            edits=data.get("edits"),
            standard=data.get("standard"),
            pictures=data.get("pictures"),
        )
        expected = str(data.get("checksum_sha256") or "")
        if expected and survey.checksum != expected:
            raise SurveyError("The saved survey does not match its checksum; it was changed after saving")
        expected_definition = str(data.get("definition_sha256") or "")
        if expected_definition and survey._definition_digest() != expected_definition:
            raise SurveyError("The saved survey's definition (template, drops, edits) was changed after saving")
        return survey

    def _definition_digest(self) -> str:
        """SHA-256 of everything ``from_dict`` rebuilds besides the QSF bytes: the
        template, what was dropped or excluded, drafts and edits."""
        definition = {
            "kind": self.kind,
            "template": self._base_template,
            "exclude_blocks": list(self.exclude_blocks),
            "exclude_questions": list(self.exclude_questions),
            "authoring": self._authoring,
            "unresolved_media": self._unresolved_media,
            "edits": self.edits,
        }
        if self._definition_edited:
            definition["standard"] = self._standard
        if self._pictures:
            definition["pictures"] = self._picture_digests()
        canonical = json.dumps(definition, sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=str)
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def _picture_digests(self) -> dict[str, str]:
        """SHA-256 of each picture file as read, by its path as written."""
        return {source: hashlib.sha256(uri.encode("utf-8")).hexdigest() for source, uri in sorted(self._pictures.items())}

    # -- identity ---------------------------------------------------------------

    def __repr__(self) -> str:
        return f"Survey({self.name!r}, {plural(len(self.questions), 'question')}, {plural(len(self.blocks), 'block')})"

    @property
    def qsf(self) -> dict[str, Any] | None:
        """The QSF the survey was read from (a copy), before any edits; None for a built survey."""
        return copy.deepcopy(self._qsf) if self._qsf is not None else None

    @property
    def raw_bytes(self) -> bytes:
        """The bytes of the QSF file as read; empty for a built survey."""
        return self._raw_bytes

    @property
    def edited(self) -> bool:
        return bool(self.edits)

    @property
    def unresolved_media(self) -> list[dict[str, Any]]:
        """Builder image references that did not resolve, for questions still in the survey."""
        present = set(self.questions)
        return [dict(m) for m in self._unresolved_media if str(m.get("question_id", "")) in present]

    @cached_property
    def checksum(self) -> str:
        """SHA-256 of the QSF file as read; for a survey edited with ``add_question``
        and the other edit methods, of its definition; for another built or edited
        survey, of its template. A survey that names picture files also covers the
        pictures as read."""
        if self.kind in {"qsf", "standard"} and not self.edits and self._raw_bytes:
            digest = hashlib.sha256(self._raw_bytes).hexdigest()
        elif self._definition_edited:
            canonical = json.dumps(self._standard, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
            digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        else:
            canonical = json.dumps(self._base_template, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
            digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        if not self._pictures:
            return digest
        both = json.dumps({"survey": digest, "pictures": self._picture_digests()}, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(both.encode("utf-8")).hexdigest()

    @property
    def source(self) -> dict[str, Any]:
        """Where the survey came from and what changed since, as recorded with results."""
        return {
            "kind": self.kind,
            "name": self.source_name,
            "checksum_sha256": self.checksum,
            "edits": [dict(e) for e in self.edits],
        }

    @cached_property
    def _read_exclusions(self) -> tuple[dict[str, Any], list[tuple[str, str, str]], list[str]]:
        """The template without the questions excluded when the survey was read, as if
        they never existed: a skip rule that jumped to one goes on to the next question
        of its block that is kept (as for a question never shown), and a branch whose
        question conditions read only excluded questions and that runs no block is
        removed (as for a dropped block). -> (template, moved skips, removed branches)"""
        template = remove_template_questions(self._base_template, self.exclude_questions)
        if not self.exclude_questions:
            return template, [], []
        excluded = {str(q) for q in self.exclude_questions}
        orders = _qsf_block_orders(self._qsf)
        moved: list[tuple[str, str, str]] = []
        # every placement of a block: the parser gives each its own copy of the questions
        for block, _path in _walk(template.get("Elements", [])):
            if block.get("ElementType") != "Block":
                continue
            kept = [str(q.get("QuestionID", "")) for q in block.get("Questions") or [] if isinstance(q, dict)]
            order = orders.get(str(block.get("BlockID", ""))) or kept
            for question in block.get("Questions") or []:
                if not isinstance(question, dict):
                    continue
                for rule in _skip_rules(question):
                    destination = str(rule.get("SkipToDestination", "") or "").strip()
                    if destination in excluded:
                        after = order[order.index(destination) + 1:] if destination in order else []
                        rule["SkipToDestination"] = next((q for q in after if q in kept), "ENDOFBLOCK")
                        entry = (str(question.get("QuestionID", "")), destination, rule["SkipToDestination"])
                        if entry not in moved:
                            moved.append(entry)
        before = {str(e.get("FlowID", "")) for e, _ in _walk(template.get("Elements", [])) if e.get("ElementType") == "Branch"}
        template = _filter_qsf_template_blocks(template, [], excluded_question_ids=excluded)
        after = {str(e.get("FlowID", "")) for e, _ in _walk(template.get("Elements", [])) if e.get("ElementType") == "Branch"}
        return template, moved, sorted(before - after)

    @cached_property
    def _full_template(self) -> dict[str, Any]:
        return self._read_exclusions[0]

    @cached_property
    def template(self) -> dict[str, Any]:
        """The survey as a template, with dropped blocks removed: what the
        Qualtrics-shaped views read. For a survey written in the standard format,
        compiled from the survey as it runs."""
        if self._written:
            from .spec.compile import compile_survey

            return compile_survey(self._dropped[0])
        template = self._full_template
        if self.exclude_blocks:
            # a screen-out reading only questions that are gone (dropped or excluded)
            # goes, whichever way each went
            template = _filter_qsf_template_blocks(template, self.exclude_blocks, excluded_question_ids=self.exclude_questions)
        return template

    @property
    def standard(self) -> dict[str, Any]:
        """The survey in the standard format (``docs/reference/survey-format.md``), as
        it runs: dropped blocks and excluded questions left out, edits applied. A copy."""
        return copy.deepcopy(self._dropped[0])

    @cached_property
    def _standard_full(self) -> dict[str, Any]:
        """The whole survey in the standard format, before blocks are dropped: as
        read when nothing was edited or excluded since, as edited when the last edit
        was made on it, else read from its template."""
        if self._standard is not None and (self._definition_edited or (not self.edits and not self.exclude_questions)):
            # (an edit on the definition started from the survey with its excluded
            # questions already taken out)
            return self._standard
        from .spec.read_template import survey_from_template

        origin = {"qsf": "qsf", "builder": "survey_builder", "standard": "standard"}.get(self.kind, "template")
        return survey_from_template(self._full_template, qsf=self._qsf if self.kind == "qsf" else None, origin=origin)

    @cached_property
    def _dropped(self) -> tuple[dict[str, Any], list[str]]:
        """(the survey as it runs, the flow IDs of branches removed with the dropped blocks)."""
        if not self.exclude_blocks:
            return self._standard_full, []
        from .engine.drops import without_blocks

        return without_blocks(self._standard_full, self.exclude_blocks, gone=self.exclude_questions)

    @property
    def metadata(self) -> dict[str, Any]:
        return dict(self.template.get("Metadata", {}))

    @property
    def id(self) -> str:
        return str(self.metadata.get("SurveyID", "") or "")

    @property
    def name(self) -> str:
        return str(self.metadata.get("SurveyName", "Untitled Survey") or "Untitled Survey")

    @property
    def language(self) -> str:
        return str(self.metadata.get("SurveyLanguage", "") or "")

    @property
    def flow(self) -> list[dict[str, Any]]:
        return copy.deepcopy(self.template.get("Elements", []))

    @cached_property
    def blocks(self) -> list[dict[str, Any]]:
        return [
            {
                "block_id": str(block.get("BlockID", "")),
                "name": str(block.get("BlockName") or block.get("Description") or ""),
                "question_ids": [str(q.get("QuestionID", "")) for q in block.get("Questions") or [] if isinstance(q, dict)],
            }
            for block, _path in _blocks(self.template)
        ]

    @cached_property
    def questions(self) -> dict[str, Question]:
        out: dict[str, Question] = {}
        for block, _path in _blocks(self.template):
            for question in block.get("Questions") or []:
                if isinstance(question, dict) and question.get("QuestionID"):
                    out.setdefault(str(question["QuestionID"]), Question(question, str(block.get("BlockID", ""))))
        return out

    @cached_property
    def _fields(self) -> dict[str, EmbeddedField]:
        return _embedded_field_accounting(self.template, self._qsf, set(self.questions))

    @property
    def embedded_fields(self) -> list[EmbeddedField]:
        """Every embedded-data field: how it gets its value (set in the flow, from a
        panel or the survey link, by JavaScript, or never) and what reads it."""
        fields = sorted(self._fields.values(), key=lambda f: f.name.lower())
        return Listing(fields, title=f"{plural(len(fields), 'embedded-data field')}:", render=_render_fields)

    # -- previewing and dropping blocks ------------------------------------------------

    def preview_blocks(self) -> list[BlockPreview]:
        """Every block in flow order, with question counts, images, findings and flags."""
        from .engine.drops import reads, screenout_blocks

        template = self._full_template
        screenouts = screenout_blocks(self._standard_full)
        all_qids_by_block = {
            str(block.get("BlockID", "")): {str(q.get("QuestionID", "")) for q in block.get("Questions") or [] if isinstance(q, dict)}
            for block, _ in _blocks(template)
        }
        previews: list[BlockPreview] = []
        for block, path in _blocks(template):
            block_id = str(block.get("BlockID", ""))
            counts: dict[str, int] = {}
            answerable = 0
            images: list[str] = []
            findings: list[Finding] = []
            for q in block.get("Questions") or []:
                if not isinstance(q, dict):
                    continue
                question = Question(q, block_id)
                counts[question.type or "?"] = counts.get(question.type or "?", 0) + 1
                if question.kind == "answerable":
                    answerable += 1
                if question.kind == "unsupported":
                    findings.append(Finding("question_type.unsupported", "Not supported", question.source, f"{question.type} is not simulated; its column stays blank"))
                for media in q.get("QuestionMedia") or []:
                    if isinstance(media, dict) and media.get("src"):
                        images.append(str(media["src"]))
            others = [Dependency(*d) for d in reads(self._standard_full, all_qids_by_block.get(block_id, set()), {block_id})]
            previews.append(
                BlockPreview(
                    block_id=block_id,
                    name=str(block.get("BlockName") or block.get("Description") or ""),
                    flow_path=path,
                    question_counts=counts,
                    answerable=answerable,
                    images=images,
                    findings=findings,
                    screenout=block_id in screenouts,
                    referenced_by=[f"{d.kind} at {d.location} reads {d.reads}" for d in others],
                    dropped=block_id in set(self.exclude_blocks),
                )
            )
        return Listing(previews, title=f"{plural(len(previews), 'block')}, in flow order:", render=_render_blocks)

    def drop_blocks(self, block_ids: list[str]) -> "Survey":
        """A new Survey without these blocks. Their questions no longer exist:
        nothing asks them, no output lists them, and logic elsewhere that reads
        one sees a question never shown (see ``drop_report``). A drop that would
        leave nothing to answer raises ``DraftError`` (``survey.no_answerable``)."""
        known = {b["block_id"] for b in (self._block_ids_full())}
        requested = [str(b).strip() for b in block_ids if str(b).strip()]
        unknown = [b for b in requested if b not in known]
        if unknown:
            raise SurveyError(f"Unknown block ID(s): {', '.join(unknown)}. Known: {', '.join(sorted(known))}")
        dropped = self._derive(exclude_blocks=sorted(set(self.exclude_blocks) | set(requested)))
        if self._answerable_count and not dropped._answerable_count:
            # The rule building and editing apply: a survey keeps something to answer.
            raise _no_answerable_error(f"Dropping {', '.join(requested)} would leave no answerable question.")
        return dropped

    # -- editing ---------------------------------------------------------------------

    def add_question(
        self,
        type: str,
        text: Any = None,
        *,
        id: str | None = None,
        block: str | None = None,
        position: int | None = None,
        after: str | None = None,
        before: str | None = None,
        image: Any = None,
        options: Any = None,
        rows: Any = None,
        columns: Any = None,
        scale: Any = None,
        items: Any = None,
        fields: Any = None,
        **settings: Any,
    ) -> "Survey":
        """A new Survey with one more question, written into the definition.

        ``type`` is one of ``instruction``, ``single_choice``, ``multiple_choice``,
        ``text``, ``number``, ``grid``, ``number_grid``, ``text_grid``,
        ``sum_grid``, ``form``, ``ranking``, ``allocation``, ``side_by_side``.
        ``options``, ``rows``, ``columns``, ``scale``, ``items`` and ``fields``
        are lists of labels (each gets the ID of its place: "1", "2", ...) or of
        objects in the standard format. Any other field of the question or its
        response is a keyword (``required=True``, ``max_selected=2``,
        ``show_if={...}``). The question goes into ``block`` at ``position`` (1
        is first), or next to the question ``after`` or ``before`` names, or last
        in the survey's only block (a survey with none gets block "main"). See
        docs/reference/survey.md."""
        from .builder.standard import add_question

        return add_question(
            self, type, text, id=id, block=block, position=position, after=after, before=before, image=image,
            options=options, rows=rows, columns=columns, scale=scale, items=items, fields=fields, **settings,
        )

    def update_question(
        self,
        question_id: str,
        *,
        type: Any = _UNCHANGED,
        text: Any = _UNCHANGED,
        image: Any = _UNCHANGED,
        options: Any = _UNCHANGED,
        rows: Any = _UNCHANGED,
        columns: Any = _UNCHANGED,
        scale: Any = _UNCHANGED,
        items: Any = _UNCHANGED,
        fields: Any = _UNCHANGED,
        **settings: Any,
    ) -> "Survey":
        """A new Survey with a question changed: only what is given changes. A
        setting given as None is removed (``show_if=None``); ``image=None`` removes
        the question's pictures. A new ``type`` keeps the lists and the required
        setting the new type also takes; it is refused when other parts of the
        survey read the question and the new type answers differently."""
        from .builder.standard import update_question

        return update_question(
            self, question_id, type=type, text=text, image=image, options=options, rows=rows, columns=columns,
            scale=scale, items=items, fields=fields, **settings,
        )

    def move_question(
        self,
        question_id: str,
        *,
        block: str | None = None,
        position: int | None = None,
        after: str | None = None,
        before: str | None = None,
    ) -> "Survey":
        """A new Survey with a question moved: into ``block`` (at ``position``, 1
        is first, or last), to another ``position`` in its block, or next to the
        question ``after`` or ``before`` names. Refused when logic or piped text
        would then read a question asked after it."""
        from .builder.standard import move_question

        return move_question(self, question_id, block=block, position=position, after=after, before=before)

    def swap_questions(self, first: str, second: str) -> "Survey":
        """A new Survey where two questions trade places (in one block or two)."""
        from .builder.standard import swap_questions

        return swap_questions(self, first, second)

    def remove_question(self, question_id: str) -> "Survey":
        """A new Survey without the question. Refused while other parts of the
        survey (conditions, skips, piped text, set values) read it."""
        from .builder.standard import remove_question

        return remove_question(self, question_id)

    def add_block(
        self,
        block_id: str,
        *,
        title: str | None = None,
        position: int | None = None,
        after: str | None = None,
        before: str | None = None,
    ) -> "Survey":
        """A new Survey with an empty block, shown in the flow next to the block
        ``after`` or ``before`` names, at ``position`` in the top level of the
        flow (1 is first), or last."""
        from .builder.standard import add_block

        return add_block(self, block_id, title=title, position=position, after=after, before=before)

    def remove_block(self, block_id: str) -> "Survey":
        """A new Survey without the block, its questions and the steps that show it.
        Refused while other parts of the survey read its questions."""
        from .builder.standard import remove_block

        return remove_block(self, block_id)

    def group_into_block(
        self,
        question_ids: list[str],
        block_id: str,
        *,
        title: str | None = None,
        after: str | None = None,
        before: str | None = None,
    ) -> "Survey":
        """A new Survey where existing questions are gathered, in the order listed,
        into a new block. The block is shown right after the block of the first
        question listed, or next to the block ``after`` or ``before`` names."""
        from .builder.standard import group_into_block

        return group_into_block(self, question_ids, block_id, title=title, after=after, before=before)

    @cached_property
    def _answerable_count(self) -> int:
        """Answerable questions some twin can be asked: not switched off with "If
        False" (their own display condition or a branch around their block)."""
        from .engine.analysis import always_false
        from .engine.definition import Definition
        from .engine.static import branch_parts
        from .results.layout import experiment_skeleton

        definition = Definition(self._dropped[0])
        live: set[str] = set()

        def walk(nodes: list[Any], off: bool) -> None:
            for node in nodes:
                if not isinstance(node, dict):
                    continue
                if node.get("type") == "show_block" and not off:
                    block = definition.blocks.get(str(node.get("block")))
                    if block is None:
                        continue
                    if block.get("kind") in {"conjoint", "maxdiff"}:
                        live.update(str(shown["question"]["id"]) for shown in experiment_skeleton(block) if shown["question"].get("response"))
                        continue
                    for qid in block.get("questions") or []:
                        question = definition.questions.get(str(qid))
                        if question is not None and question.get("response") and not question.get("kind") and not always_false(question.get("show_if")):
                            live.add(str(qid))
                elif node.get("type") == "branch":
                    for condition, steps in branch_parts(node):
                        walk(steps, off or always_false(condition))
                else:
                    for key in ("children",):
                        if isinstance(node.get(key), list):
                            walk(node[key], off)

        walk(definition.flow, False)
        return len(live)

    @cached_property
    def drop_report(self) -> DropReport:
        """What the dropped blocks removed and what still mentions their questions,
        worked out from the survey as it is now (so an edit never leaves it stale)."""
        from .engine.drops import block_questions, reads

        moved, read_pruned = self._read_exclusions[1:] if self.exclude_questions else ([], [])
        excluded = {
            "excluded_questions": sorted(str(q) for q in self.exclude_questions),
            "moved_skips": [f"SQ {qid} to {new} (was {old})" for qid, old, new in moved],
        }
        if not self.exclude_blocks:
            return DropReport(pruned_branches=[f"FL {p}" for p in read_pruned], **excluded) if self.exclude_questions else DropReport()
        dropped = list(self.exclude_blocks)
        blocks = {str(b.get("id")): b for b in self._standard_full.get("blocks") or []}
        dropped_qids = {q for block_id in dropped for q in block_questions(blocks.get(block_id) or {})}
        definition, pruned = self._dropped
        found = [Dependency(*d) for d in reads(definition, dropped_qids, set(dropped))]
        references = [d for d in found if d.kind != "choice_display_logic"]
        choice_logic = [d for d in found if d.kind == "choice_display_logic"]  # listed apart
        return DropReport(
            dropped_blocks=dropped,
            dropped_questions=sorted(dropped_qids),
            pruned_branches=[f"FL {p}" for p in sorted(set(read_pruned) | set(pruned))],
            references=references,
            choice_logic=choice_logic,
            **excluded,
        )

    @cached_property
    def _reads_missing(self) -> list[Dependency]:
        """Logic that reads a question the survey does not have at all (deleted in
        Qualtrics, outside every block of the flow, or excluded when read). Like a
        dropped question, it counts as never shown."""
        from .engine.drops import missing_reads, shown_questions

        return [Dependency(*d) for d in missing_reads(self._dropped[0], shown_questions(self._standard_full))]

    def _block_ids_full(self) -> list[dict[str, Any]]:
        return [{"block_id": str(b.get("BlockID", ""))} for b, _ in _blocks(self._full_template)]

    # -- the check ---------------------------------------------------------------

    def check(self, *, network: bool = False, url_checker: Any = None, embedded_data: dict[str, Any] | None = None) -> SurveyCheck:
        """Execution mode, support findings, unsupported questions, images, design cells.

        ``embedded_data``: the values you will pass to ``configure(embedded_data=...)``;
        fields they supply are not reported as blank, and a key nothing reads is."""
        from .engine.analysis import analyze, undrawable_random_tokens

        standard = self._runnable_standard()
        analysis = analyze(standard)
        traits = analysis.get("traits", {})
        reasons: list[str] = []
        if traits.get("question_logic"):
            reasons.append("branch or display logic reads an answer")
        if traits.get("question_display_logic"):
            reasons.append("display logic on questions")
        if traits.get("skip_logic"):
            reasons.append("skip logic")
        if traits.get("question_piped_text"):
            reasons.append("piping of an earlier answer (${q://...})")
        if traits.get("end_survey"):
            reasons.append("an end-of-survey element")
        if traits.get("task_boundaries"):
            reasons.append("conjoint or MaxDiff tasks asked one at a time")
        findings: list[Finding] = []
        # Traits only the QSF file shows (JavaScript, carry-forward, ...): about the
        # file as read, so they can still mention content an edit has removed.
        raw = self._raw_bytes if self.kind == "qsf" else b""
        for message in detect_unsupported_qsf_traits(raw):
            if message.startswith("References embedded-data fields"):
                continue  # each such field is reported below, with where its value comes from
            findings.append(Finding(_trait_feature(message), _feature_level(_trait_feature(message)), "", message))
        for message in analysis.get("warnings", []):
            feature = _analysis_feature(message)
            findings.append(Finding(feature, _feature_level(feature), "", str(message)))
        unsupported = [q.id for q in self.questions.values() if q.kind == "unsupported"]
        for qid in unsupported:
            q = self.questions[qid]
            findings.append(Finding("question_type.unsupported", "Not supported", q.source, f"{q.type}/{q.selector} is not simulated; its column stays blank"))
        supplied = {str(key) for key in (embedded_data or {})}
        set_compact = {name.replace(" ", "") for name, f in self._fields.items() if f.set_in}
        computing = _javascript_computing_field_names(self._qsf, set(self.questions))
        unset: list[str] = []
        for name, record in sorted(self._fields.items()):
            if not record.read_by or record.set_in or name in supplied or name.replace(" ", "") in set_compact:
                continue
            unset.append(name)
            where = short_list(record.read_by, 3)
            if record.set_by_javascript:
                findings.append(Finding("piped_text.embedded_data", "Not supported", where,
                    f"Field {name!r} is set by the survey's JavaScript ({short_list(record.set_by_javascript, 3)}), which the "
                    "simulation does not run: twins see it blank where respondents saw a value. Pass "
                    "configure(embedded_data={...}) if you know the value."))
            elif computing and not record.from_outside:
                findings.append(Finding("piped_text.embedded_data", "Not supported", where,
                    f"Field {name!r} is read but never set in the flow; the survey's JavaScript ({short_list(computing, 3)}) sets "
                    "fields under names it computes, probably this one, and the simulation does not run it: twins likely see "
                    "it blank where respondents saw a value. Pass configure(embedded_data={...}) if you know the value."))
            elif record.from_outside:
                findings.append(Finding("piped_text.embedded_data", "Partial", where,
                    f"Field {name!r} comes from a panel, contact list or the survey link in Qualtrics "
                    f"({short_list(record.from_outside, 3)}): twins see it blank unless you pass configure(embedded_data={{...}})."))
            else:
                findings.append(Finding("piped_text.embedded_data", "Full", where,
                    f"Field {name!r} is read but never set in the QSF; twins see it blank, as Qualtrics respondents would. "
                    "If the study filled it from outside, pass configure(embedded_data={...})."))
        for key in sorted(supplied - set(self._fields)):
            findings.append(Finding("piped_text.embedded_data", "Detected", "",
                f"embedded_data supplies {key!r}, but the survey never reads or sets it; check the spelling."))
        checker = url_checker
        if checker is None and not network:
            checker = _offline_checker
        images = survey_image_warnings(
            standard,
            raw_content=raw,
            url_checker=checker,
            check_limit=200,
            concurrency=8 if network else 1,
        )
        for item in images:
            if str(item.get("url", "")).lower().startswith("data:image/"):
                # An image embedded in the survey (a data URI) is sent as it is; nothing to reach.
                item["status"] = "supported"
                item["detail"] = "embedded image"
                item["message"] = f"Image embedded in the survey: {item.get('label')}. It is sent to the model."
                continue
            if item.get("detail") == _NOT_CHECKED:
                item["status"] = "not_checked"
                item["detail"] = "reachability not checked (pass network=True to test the link)"
                item["message"] = f"Image found: {item.get('label')}. Reachability not checked (pass network=True to test the link)."
        for media in self.unresolved_media:
            images.append(
                {
                    "status": "unresolved",
                    "label": f"{media.get('question')} image",
                    "url": "",
                    "context": "builder",
                    "question_id": str(media.get("question_id", "")),
                    "prompt_label": str(media.get("question", "")),
                    "detail": str(media.get("reference", "")),
                    "message": (
                        f"Image not found: {media.get('reference')!r} on {media.get('question')}. "
                        "The image resolver returned nothing for it; the run stops unless accept_missing_images=True."
                    ),
                }
            )
        for qid, choices in _choice_display_logic(self._qsf).items():
            if qid in self.questions:
                keys = ", ".join(key for key, _reads in choices)
                if choice_logic_applies(self.questions[qid].raw) and choice_logic_of(self.questions[qid].raw):
                    level = _feature_level("question_option.choice_display_logic")
                    message = f"Display logic on choice(s) {keys} is applied: each is shown only when its condition holds."
                elif choice_logic_applies(self.questions[qid].raw):
                    # a survey saved before the reader kept this logic
                    level = "Detected"
                    message = (f"Display logic on choice(s) {keys} is not applied: this survey was saved before it was kept, "
                               "so every choice is shown. Read the QSF again to apply it.")
                else:  # the rows of a side-by-side question, say
                    level = "Detected"
                    kind = str(self.questions[qid].raw.get("RawQuestionType") or self.questions[qid].raw.get("QuestionType") or "")
                    message = f"Display logic on item(s) {keys} of this {kind} question is not applied; every item is shown."
                findings.append(Finding("question_option.choice_display_logic", level, f"SQ {qid}", message))
        for where, token in undrawable_random_tokens(standard):
            findings.append(
                Finding(
                    "piped_text.random",
                    "Not supported",
                    where,
                    f"{token} cannot be drawn: Qualtrics' forms are ${{rand://int/MIN:MAX}}, ${{rand://float/MIN:MAX}} "
                    "and true/false, with numbers for bounds. It reaches the model as written.",
                )
            )
        for dep in self._reads_missing:
            if dep.reads in set(self.exclude_questions):
                why = "it was excluded when the survey was read"
            elif self.kind == "qsf":
                why = "deleted in Qualtrics, or in no block of the flow"
            else:
                why = "no question has that internal ID"
            effect = ("the rest of its block after the question is not asked" if dep.kind == "skip_logic"
                      else "it counts as never shown, with no answer")
            findings.append(
                Finding(
                    "logic.missing_question",
                    "Full" if dep.kind != "skip_logic" else "Partial",
                    dep.location,
                    f"{dep.kind.replace('_', ' ')} reads {dep.reads}, which this survey does not have "
                    f"({why}); {effect}.",
                )
            )
        _template, moved, read_pruned = self._read_exclusions
        for qid, old, new in moved:
            where = "the end of its block" if new == "ENDOFBLOCK" else new
            findings.append(Finding("logic.missing_question", "Full", f"SQ {qid}",
                f"skip logic jumps to {old}, which was excluded when the survey was read; it goes on to {where}, "
                "as for a question never shown."))
        for flow_id in read_pruned:
            findings.append(Finding("flow.branch", "Full", f"FL {flow_id}",
                "This branch reads only questions excluded when the survey was read and runs no block; it is removed, "
                "as a dropped block's would be."))
        blocking = [i for i in images if i.get("status") in BLOCKING_IMAGE_STATUSES or i.get("status") == "unresolved"]
        if not self._answerable_count:
            findings.append(Finding("survey.no_answerable", "Detected", "",
                "Nothing for a twin to answer: no question in the survey as it is now takes an answer "
                "(dropped, excluded, or only text and timing). plan() refuses it."))
        from .engine.definition import Definition
        from .engine.static import design_cells

        cells = design_cells(Definition(standard))
        return SurveyCheck(
            mode=str(analysis.get("execution_mode", "static")),
            staging_reasons=reasons,
            findings=findings,
            unsupported_questions=unsupported,
            images=images,
            design_cells=cells,
            unset_embedded_fields=unset,
            ok=not blocking and bool(self._answerable_count),
            reads_dropped=[f"{d.kind} at {d.location} reads {d.reads}" for d in self.drop_report.references],
        )


def _questions_to_exclude(data: dict[str, Any], names: Any) -> list[str]:
    """The internal IDs ``exclude_questions`` names: each entry an internal ID, or
    the export tag of exactly one question. Anything else is refused, so nothing
    is silently left in the survey."""
    ids: set[str] = set()
    by_tag: dict[str, list[str]] = {}
    for element in data.get("SurveyElements") or []:
        if isinstance(element, dict) and element.get("Element") == "SQ":
            payload = element.get("Payload") if isinstance(element.get("Payload"), dict) else {}
            qid = str(payload.get("QuestionID") or element.get("PrimaryAttribute") or "").strip()
            if qid:
                ids.add(qid)
                tag = str(payload.get("DataExportTag") or "").strip()
                if tag:
                    by_tag.setdefault(tag, []).append(qid)
    resolved: list[str] = []
    problems: list[str] = []
    for raw in [names] if isinstance(names, str) else list(names):
        key = str(raw).strip()
        if key in ids:
            resolved.append(key)
        elif len(by_tag.get(key, [])) == 1:
            resolved.append(by_tag[key][0])
        elif key in by_tag:
            problems.append(f"{key!r} is the export tag of {', '.join(by_tag[key])}; name the internal ID")
        else:
            problems.append(f"no question {key!r} in this QSF")
    if problems:
        raise SurveyError("exclude_questions: " + "; ".join(problems) + ".")
    return list(dict.fromkeys(resolved))


def _no_answerable_error(message: str) -> SurveyError:
    """The error building a survey with nothing to answer raises (``survey.no_answerable``)."""
    from .builder.draft import DraftIssue
    from .errors import DraftError

    return DraftError([DraftIssue("error", message, "survey.no_answerable")])


_NOT_CHECKED = "__surveytwin_not_checked__"


def _offline_checker(url: str) -> dict[str, str]:
    # The shared image analysis only knows the website's four statuses, so the
    # offline answer travels as "supported" with a marker and is relabeled
    # "not_checked" afterwards.
    return {"status": "supported", "detail": _NOT_CHECKED}


def _trait_feature(message: str) -> str:
    lowered = message.lower()
    if "javascript" in lowered:
        return "question_option.javascript"
    if "web service" in lowered:
        return "flow.web_service"
    if "embedded-data fields" in lowered:
        return "piped_text.embedded_data"
    if "question type" in lowered:
        return "question_type.unsupported"
    if "flow contains unsupported element" in lowered:
        return "flow.unsupported"
    if "carry forward" in lowered:
        return "question_option.carry_forward"
    if "pipes values" in lowered or "math" in lowered:
        return "piped_text.unsupported"
    if "custom validation" in lowered:
        return "question_option.validation"
    return "other"


def _analysis_feature(message: str) -> str:
    lowered = str(message).lower()
    if lowered.startswith("loop & merge asks every row"):  # (it names blocks, whose names can hold any word)
        return "block_option.loop_and_merge"
    if lowered.startswith("conjoint block "):
        return "experiment.conjoint"
    if lowered.startswith("maxdiff block "):
        return "experiment.maxdiff"
    if "rand://" in lowered:  # (before "staged passes": this note says it needs none)
        return "piped_text.random"
    if "staged passes" in lowered:
        return "execution.staged"
    if "piped text" in lowered and "q://" in lowered:
        return "piped_text.question"
    if "randomizer" in lowered:
        return "flow.randomizer"
    if "end-of-survey" in lowered:
        return "flow.end_of_survey"
    if "rand://" in lowered:
        return "piped_text.random"
    if "choice" in lowered and "random" in lowered:
        return "question_option.choice_randomization"
    if "loop" in lowered:
        return "block_option.loop_and_merge"
    if "display logic" in lowered:
        return "question_option.display_logic"
    if "skip" in lowered:
        return "question_option.skip_logic"
    if "branch" in lowered or "logic" in lowered:
        return "flow.branch"
    if "question randomization" in lowered or "block" in lowered:
        return "block_option.question_randomization"
    return "other"


def _through_standard_format(template: dict[str, Any], qsf: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """(the QSF in the standard format, which the survey runs; the template of its
    Qualtrics-shaped views): the survey is read into the standard format and
    compiled back from it. Checked equal to the QSF reader's template on every
    test and study QSF (scripts/format_roundtrip.py); if a QSF ever compiles
    differently, the views keep the QSF reader's template and the standard form
    says so in its findings. A QSF the standard form cannot hold is not read."""
    from .spec.compile import compile_survey
    from .spec.read_template import survey_from_template

    standard = survey_from_template(template, qsf=qsf)
    try:
        compiled: dict[str, Any] | None = compile_survey(standard)
    except Exception:  # noqa: BLE001 - the views then keep the reader's template
        compiled = None
    if compiled != template:
        standard["source"].setdefault("findings", []).append(
            "Compiled back, the standard form differs from the template the QSF reader gives; "
            "the survey runs from the standard form."
        )
        return standard, template
    return standard, compiled


def read_survey(source: str | Path | bytes | dict[str, Any], *, folder: str | Path | None = None) -> Survey:
    """Read a survey written in the standard format (JSON) from a path, bytes or a
    parsed value. Reading checks the whole definition first: any error raises
    ``SurveyFormatError``, whose ``findings`` give the path of each field; warnings
    (a variable never set, a block never shown) are kept in ``read_warnings``.
    The format is described in docs/reference/survey-format.md.

    A picture's ``source`` may be the path of a picture file: an absolute path is
    read as written, a relative path from the folder of the JSON file, or from
    ``folder`` for a survey given as bytes or a parsed value (the current folder
    when ``folder`` is not given). Each file is read now; a missing file or one
    that is not a PNG, JPEG, WEBP or GIF picture of at most 20 MB is an error."""
    from .errors import SurveyFormatError
    from .spec.compile import CompileError, compile_survey
    from .spec.pictures import read_pictures
    from .spec.validate import check_survey

    name = ""
    raw: bytes | None = None
    base = Path(folder).expanduser() if folder is not None else Path.cwd()
    if isinstance(source, dict):
        data = copy.deepcopy(source)
    else:
        if isinstance(source, (str, Path)) and not (isinstance(source, str) and source.lstrip().startswith("{")):
            path = Path(source)
            if not path.exists():
                raise SurveyError(f"{path}: no such file")
            raw = path.read_bytes()
            name = path.name
            if folder is None:
                base = path.expanduser().resolve().parent
        elif isinstance(source, (bytes, bytearray)):
            raw = bytes(source)
        else:
            raw = str(source).encode("utf-8")
        try:
            data = json.loads(raw.decode("utf-8-sig"))
        except (UnicodeDecodeError, ValueError) as exc:
            raise SurveyError(f"{name or 'survey'}: not a JSON file ({exc})") from exc
    if isinstance(data, dict) and "SurveyElements" in data:
        raise SurveyError(f"{name or 'survey'}: this is a Qualtrics export; read it with st.read_qsf(...)")
    findings = check_survey(data)
    if any(f.level == "error" for f in findings):
        raise SurveyFormatError(findings)
    pictures, picture_problems = read_pictures(data, base.resolve())
    if picture_problems:
        raise SurveyFormatError(picture_problems)
    try:
        template = compile_survey(data)
    except CompileError as exc:
        raise SurveyFormatError([], f"{name or 'survey'}: {exc}") from exc
    survey = Survey(
        template,
        kind="standard",
        source_name=name,
        raw_bytes=raw if raw is not None else json.dumps(data, ensure_ascii=False, sort_keys=True).encode("utf-8"),
        standard=data,
        pictures=pictures,
    )
    survey.read_warnings = [f for f in findings if f.level == "warning"]
    survey._picture_folder = base.resolve()
    if not survey._answerable_count:
        raise _no_answerable_error("The survey has no question a twin can answer.")
    return survey


def read_qsf(
    source: str | Path | bytes | dict[str, Any],
    *,
    exclude_blocks: list[str] | None = None,
    exclude_questions: list[str] | None = None,
) -> Survey:
    """Read a QSF from a path, bytes or parsed JSON. Reading never touches the network.

    ``exclude_blocks`` drops blocks, as ``drop_blocks`` does. ``exclude_questions``
    leaves questions out as if they did not exist: logic that reads one sees a
    question never shown, a skip rule that jumped to one goes on to the next
    question kept, and a screen-out that reads only excluded questions is removed
    (``drop_report`` lists what changed). Either is refused (``DraftError``,
    ``survey.no_answerable``) when it would leave nothing to answer."""
    name = ""
    raw: bytes | None = None
    if isinstance(source, dict):
        data = source
    else:
        if isinstance(source, (str, Path)) and not (isinstance(source, str) and source.lstrip().startswith("{")):
            path = Path(source)
            if not path.exists():
                raise QSFReadError(f"{path}: no such file")
            raw = path.read_bytes()
            name = path.name
        elif isinstance(source, (bytes, bytearray)):
            raw = bytes(source)
        else:
            raw = str(source).encode("utf-8")
        try:
            data = json.loads(raw.decode("utf-8-sig"))
        except (UnicodeDecodeError, ValueError) as exc:
            raise QSFReadError(f"{name or 'QSF'}: not a JSON file ({exc})") from exc
    if not isinstance(data, dict):
        raise QSFReadError(f"{name or 'QSF'}: the top level must be a JSON object")
    if exclude_questions:
        exclude_questions = _questions_to_exclude(data, exclude_questions)
    elements = data.get("SurveyElements")
    if not isinstance(elements, list) or not elements:
        raise QSFReadError(f"{name or 'QSF'}: no SurveyElements; this is not a Qualtrics QSF export")
    if not any(isinstance(e, dict) and e.get("Element") == "FL" for e in elements):
        raise QSFReadError(f"{name or 'QSF'}: no survey flow (FL element)")
    try:
        # Parse without the excluded questions, as the website does, so a question
        # the parser cannot read can still be excluded.
        template = parse_template(data, list(exclude_questions or []) or None)
        standard, template = _through_standard_format(template, data)
        survey = Survey(
            template,
            kind="qsf",
            source_name=name,
            qsf=data,
            raw_bytes=raw if raw is not None else json.dumps(data, ensure_ascii=False).encode("utf-8"),
            exclude_questions=exclude_questions,
            standard=standard,
        )
        _ = survey.template
    except QSFReadError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise QSFReadError(f"{name or 'QSF'}: could not be read: {exc}") from exc
    if exclude_questions and not survey._answerable_count:
        try:
            whole = Survey(parse_template(data, None), kind="qsf", qsf=data)
            before = whole._answerable_count
        except Exception:  # noqa: BLE001 - the file needs the exclusion to be read at all
            before = 0
        if before:
            raise _no_answerable_error(
                f"Excluding {', '.join(str(q) for q in exclude_questions)} would leave no answerable question."
            )
    if exclude_blocks:
        survey = survey.drop_blocks(list(exclude_blocks))
    return survey
