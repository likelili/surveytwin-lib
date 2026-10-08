"""Where each answer goes in the export files, from the survey definition.

The export files name their columns the way a Qualtrics export does: the
question's export name, with a statement, option or field ID after it, a repeat
row's number before it, ``_TEXT`` for a text box, and an ImportId for each
column. ``Layout`` works these out from the survey definition (and, for a run
made before any answer, each twin's copy, whose options can carry piped values):
the columns, their descriptions (the question as written, after the text shown
just before it), each option's export value, and the export name of every
answer ID. The rules are those of the website's export tool, which read the
survey template; the files come out the same (``scripts/engine_diff.py``).
"""

from __future__ import annotations

import re
from typing import Any, Callable

from ..engine import render
from ..engine.definition import Definition
from ..engine.experiment import maxdiff_task_questions
from ..engine.realize import shown_question
from ..experiments.tasks import _conjoint_options, _task_ids, question_id as task_question_id
from ..spec.text import join_text

_LOOP_FIELD_RE = re.compile(r"\$\{lm://([^}]+)\}")


def normalize(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def natural_key(question_id: str) -> tuple[int, str]:
    match = re.search(r"(\d+)", question_id)
    return (int(match.group(1)), question_id) if match else (10**9, question_id)


def _prefix_key(prefix: str) -> tuple[int, str]:
    match = re.search(r"\d+", prefix)
    return (int(match.group(0)), prefix) if match else (10**9, prefix)


def _loop_response_ids(question_ids: set[str], base: str) -> list[tuple[str, str]]:
    pattern = re.compile(rf"^(.+?)_{re.escape(base)}$")
    found = [(match.group(1), response_id) for response_id in question_ids if (match := pattern.fullmatch(response_id))]
    return sorted(found, key=lambda item: (_prefix_key(item[0]), item[1]))


def _loop_row_prefixes(question_ids: set[str], *, base: str, row_ids: list[str]) -> list[str]:
    prefixes: set[str] = set()
    for row_id in row_ids:
        pattern = re.compile(rf"^(.+?)_{re.escape(base)}_{re.escape(row_id)}$")
        prefixes.update(match.group(1) for response_id in question_ids if (match := pattern.fullmatch(response_id)))
    return sorted(prefixes, key=_prefix_key)


def _loop_text_prefixes(question_ids: set[str], *, base: str, option_ids: list[str]) -> list[str]:
    prefixes: set[str] = set()
    for option_id in option_ids:
        pattern = re.compile(rf"^(.+?)_{re.escape(base)}_{re.escape(option_id)}_TEXT$")
        prefixes.update(match.group(1) for response_id in question_ids if (match := pattern.fullmatch(response_id)))
    return sorted(prefixes, key=_prefix_key)


def _merge_context(context: str, description: str) -> str:
    return f"{context} | {description}" if context else description


def _row_text(question_text: str, row_text: str) -> str:
    qt, rt = question_text.rstrip(), row_text.lstrip()
    if qt.endswith("...") and rt.startswith("..."):
        return re.sub(r"[,\s.]+$", "", qt) + ", " + re.sub(r"^\.+\s*", "", rt)
    return f"{question_text} - {row_text}"


def _replace_terminal_id(response_id: str, authored_id: str, export_id: str) -> str:
    if not authored_id or authored_id == export_id:
        return response_id
    suffix = f"_{authored_id}"
    return f"{response_id[:-len(suffix)]}_{export_id}" if response_id.endswith(suffix) else response_id


def _render_loop_text(value: Any, context: dict[str, Any] | None) -> str:
    text = str(value or "")
    if not text or not isinstance(context, dict):
        return text

    def replace(match: re.Match[str]) -> str:
        token = str(match.group(1) or "").strip()
        if token == "CurrentLoopNumber":
            return str(context.get("current_loop_number", ""))
        if token == "TotalLoops":
            return str(context.get("total_loops", ""))
        if token.startswith("Field/"):
            fields = context.get("fields")
            if isinstance(fields, dict):
                return str(fields.get(token.removeprefix("Field/").strip(), ""))
        return ""

    return _LOOP_FIELD_RE.sub(replace, text)


# ---------------------------------------------------------------- one question's items


class Q:
    """What the export files read of one question (as written, or as one twin saw it)."""

    def __init__(self, shown: dict[str, Any]):
        self.shown = shown
        self.view = render.Shown(shown)
        self.question = shown["question"]
        self.id = str(self.question.get("id", "") or "").strip()
        self.name = str(self.question.get("export_name", "") or "").strip() or self.id
        self.kind = self.view.kind
        self.text = normalize(self.view.text)

    def code_of(self, item_id: str) -> str:
        """An item's export value in a column name: its recode, else its ID."""
        return normalize(self.view.codes.get(str(item_id), item_id))

    def value_of(self, item_id: str) -> str:
        """An item's export value in the values file: its recode, else its ID."""
        return str(self.view.codes.get(str(item_id), item_id)).strip()

    def rows(self) -> list[tuple[str, str]]:
        """(ID, label) of a grid's statements, a form's fields or an allocation's items."""
        return [(row.id, normalize(row.label)) for row in self.view.rows]

    def columns(self) -> list[tuple[str, str]]:
        return [(c.id, normalize(c.label) or f"Scale point {c.id}") for c in self.view.columns]

    def options(self) -> list[tuple[str, str]]:
        return [(o.id, normalize(o.label) or f"Option {o.id}") for o in self.view.options]

    def write_ins(self) -> list[tuple[str, str]]:
        return [(o.id, normalize(o.label) or f"Option {o.id}") for o in self.view.options if o.raw.get("write_in")]

    def statements(self) -> list[tuple[str, str]]:
        statements = self.view.statements
        if not statements:
            return [("1", "")]
        return [(s.id, normalize(s.label)) for s in statements]

    def ranked(self) -> list[tuple[str, str]]:
        return [(i.id, normalize(i.label) or f"Item {i.id}") for i in self.view.rank_items]

    def group_fields(self) -> list[dict[str, Any]]:
        """Each column of a side-by-side question, with its rows (the question's own
        rows when the column lists none)."""
        source = self.question.get("source") if isinstance(self.question.get("source"), dict) else {}
        sbs = source.get("sbs") if isinstance(source.get("sbs"), dict) else {}
        out = []
        for index, field in enumerate((self.question.get("response") or {}).get("fields") or [], start=1):
            field_source = field.get("source") if isinstance(field.get("source"), dict) else {}
            field_id = str(field.get("id", "") or index)
            matrix = field.get("response") or {}
            rows = matrix.get("rows") or []
            row_response = matrix.get("row_response") or {}
            if rows:
                items = [(str(r["id"]), normalize(label)) for r, label in zip(rows, _labels(rows, "Image row"))]
                row_source = "sub_question"
            else:
                labels = sbs.get("Rows") if isinstance(sbs.get("Rows"), list) else []
                ids = sbs.get("RowsID") if isinstance(sbs.get("RowsID"), list) else [str(i + 1) for i in range(len(labels))]
                items = [(str(i), normalize(labels[n]) if n < len(labels) else "") for n, i in enumerate(ids) if str(i).strip()]
                row_source = "question"
            options = [] if row_response.get("type") == "text" else row_response.get("options") or []
            out.append(
                {
                    "qid": str(field_source.get("QuestionID") or f"{self.id}#{field_id}"),
                    "name": str(field["export_name"]).strip() if "export_name" in field else f"{self.question.get('export_name') or self.id}#{field_id}",
                    "label": join_text(field.get("label", "")),
                    "text_entry": row_response.get("type") == "text",
                    "rows": items,
                    "row_source": row_source,
                    "options": [(str(o["id"]), label) for o, label in zip(options, _labels(options, "Image option"))],
                    "codes": {str(o["id"]): o["code"] for o in options if "code" in o},
                }
            )
        return out


def _labels(items: list[dict[str, Any]], image_label: str) -> list[str]:
    from ..engine.text import item_labels

    return item_labels(items, image_label)


def expected_ids(q: Q) -> set[str]:
    """The answer IDs a question can have (one per row, option, field, cell...)."""
    out: set[str] = set()
    qid = q.id
    if not qid or q.kind in {"instruction"} or q.question.get("kind") in {"timing", "meta_info", "captcha"}:
        return out
    if q.kind == "form":
        return {f"{qid}_{row_id}" for row_id, _ in q.rows()}
    if q.kind == "text":
        return {qid}
    if q.kind == "grid" and q.view.multiple:
        return {f"{qid}_{row_id}_{column_id}" for row_id, _ in q.rows() for column_id, _ in q.columns()}
    if q.kind in {"grid_text", "grid_sum"}:
        columns = q.columns()
        for row_id, _ in q.rows():
            if len(columns) > 1:
                out |= {f"{qid}_{row_id}_{column_id}" for column_id, _ in columns}
            else:
                out.add(f"{qid}_{row_id}")
        return out
    if q.kind in {"allocation", "grid"}:
        return {f"{qid}_{row_id}" for row_id, _ in q.rows()}
    if q.kind == "group":
        for field in q.group_fields():
            out |= {f"{field['qid']}_{row_id}" for row_id, _ in field["rows"]}
        return out
    if q.kind == "slider":
        return {f"{qid}_{statement_id}" for statement_id, _ in q.statements()}
    if q.kind == "ranking":
        return {f"{qid}_{item_id}" for item_id, _ in q.ranked()}
    if q.kind == "choice" and q.view.multiple:
        return {f"{qid}_{option_id}" for option_id, _ in q.options()} | {f"{qid}_{option_id}_TEXT" for option_id, _ in q.write_ins()}
    if q.kind == "choice":
        return {qid} | {f"{qid}_{option_id}_TEXT" for option_id, _ in q.write_ins()}
    return {qid}


# ---------------------------------------------------------------- the survey


def experiment_skeleton(block: dict[str, Any]) -> list[dict[str, Any]]:
    """A conjoint or MaxDiff block's questions before any twin is drawn (stable IDs)."""
    if block.get("kind") == "conjoint":
        instruction = join_text((block.get("presentation") or {}).get("instruction", "")) or "Which of these would you choose?"
        note = "(The alternatives of each task are drawn for each twin.)"
        response = block.get("response") or {}
        outside = response.get("outside_option") or None
        out = []
        for task in _task_ids(block):
            options = [{"id": option_id, "label": label} for option_id, label in _conjoint_options(block)]
            if outside is not None and outside.get("code") is not None:
                for option in options:
                    if option["id"] == str(outside["id"]):
                        option["code"] = outside["code"]
            qid = task_question_id(str(block["id"]), task)
            out.append(
                {
                    "question": {
                        "id": qid,
                        "export_name": qid,
                        "content": [{"type": "text", "text": f"{instruction}\n\n{note}"}],
                        "response": {"type": "choice", "mode": "single", "options": options, "required": response.get("required", False)},
                    },
                    "lines": True,
                    "native": True,
                }
            )
        return out
    return [q for task in _task_ids(block) for q in maxdiff_task_questions(block, task, list(block.get("items") or []), skeleton=True)]


class Layout:
    """The export columns and value maps of a survey definition."""

    def __init__(self, definition: dict[str, Any] | None, copies: list[list[dict[str, Any]]] | None = None):
        self.definition = Definition(definition) if definition else None
        self.copies = copies or []

    # ---- every placement of every block, in flow order

    def placements(self) -> list[tuple[dict[str, Any], list[dict[str, Any]]]]:
        if self.definition is None:
            return []
        found: list[tuple[dict[str, Any], list[dict[str, Any]]]] = []
        definition = self.definition

        def walk(nodes: list[dict[str, Any]]) -> None:
            for node in nodes:
                if not isinstance(node, dict):
                    continue
                if node.get("type") == "show_block":
                    block = definition.blocks.get(str(node.get("block")))
                    if block is None:
                        continue
                    if block.get("kind") in {"conjoint", "maxdiff"}:
                        found.append((block, experiment_skeleton(block)))
                    else:
                        questions = [
                            shown_question(definition.questions[str(qid)], native=definition.native, brand=definition.brand)
                            for qid in block.get("questions") or []
                            if str(qid) in definition.questions
                        ]
                        if questions:
                            found.append((block, questions))
                for key in ("then", "else", "children"):
                    if isinstance(node.get(key), list):
                        walk(node[key])

        walk(definition.flow)
        return found

    @staticmethod
    def _loop_contexts(block: dict[str, Any]) -> dict[str, dict[str, Any]]:
        repeat = block.get("repeat") or {}
        rows = repeat.get("rows") or []
        return {
            str(row["id"]): {
                "fields": {str(k): join_text(v) for k, v in (row.get("values") or {}).items()},
                "current_loop_number": number,
                "total_loops": len(rows),
            }
            for number, row in enumerate(rows, start=1)
        }

    # ---- text, IDs, export names

    def text_by_id(self) -> dict[str, str]:
        """Each question's text as written, one line (the first placement's)."""
        out: dict[str, str] = {}
        for _block, questions in self.placements():
            for shown in questions:
                q = Q(shown)
                if q.id and q.id not in out and q.text:
                    out[q.id] = q.text
        return out

    def expected_ids(self) -> tuple[set[str], set[str]]:
        """(every answer ID the survey can have, a repeated block's once per row, and
        each twin's copy's; the IDs of repeated questions without a row, which no
        column takes)."""
        out: set[str] = set()
        repeated: set[str] = set()
        for block, questions in self.placements():
            rows = [str(r["id"]) for r in (block.get("repeat") or {}).get("rows") or []]
            for shown in questions:
                ids = expected_ids(Q(shown))
                if rows:
                    out |= {f"{row}_{answer_id}" for answer_id in ids for row in rows}
                    repeated |= ids
                else:
                    out |= ids
        for copy in self.copies:
            for shown in copy:
                out |= expected_ids(Q(shown))
        return out, repeated

    def aliaser(self) -> Callable[[str], list[str]]:
        """A function from an answer ID to the IDs it is exported under: itself and
        its question's export name in place of the question ID (``QID5_2`` ->
        ``tag_2``, ``3_QID5`` -> ``3_tag``)."""
        names: dict[str, str] = {}
        for _block, questions in self.placements():
            for shown in questions:
                q = Q(shown)
                pairs = [(q.id, str(q.question.get("export_name", "") or "").strip())]
                if q.kind == "group":
                    pairs += [(field["qid"], field["name"]) for field in q.group_fields()]
                for qid, name in pairs:
                    if qid and name and name != qid:
                        names[qid] = name
        ordered = sorted(names, key=len, reverse=True)

        def aliases(question_id: str) -> list[str]:
            base = str(question_id or "").strip()
            if not base:
                return []
            out = [base]
            match = re.match(r"^(\d+)_(.+)$", base)
            prefix, core = (f"{match.group(1)}_", match.group(2)) if match else ("", base)
            for qid in ordered:
                if core == qid or core.startswith(f"{qid}_"):
                    out.append(f"{prefix}{names[qid]}{core[len(qid):]}")
                    break
            return out

        return aliases

    # ---- value maps

    def value_maps(self) -> dict[str, dict[str, str]]:
        """For each answer ID (and export name), each option label's export value."""
        out: dict[str, dict[str, str]] = {}

        def register(qid: str, name: str, mapping: dict[str, str]) -> None:
            if not mapping:
                return
            if qid:
                out.setdefault(qid, {}).update(mapping)
            if name:
                out.setdefault(name, {}).update(mapping)

        def choice_map(q: Q, items: list[Any]) -> dict[str, str]:
            return {normalize(item.label): q.value_of(item.id) for item in items if normalize(item.label)}

        for _block, questions in self.placements():
            for shown in questions:
                q = Q(shown)
                name = str(q.question.get("export_name", "") or "").strip()
                if not q.id:
                    continue
                if q.kind in {"choice", "ranking"}:
                    register(q.id, name, choice_map(q, q.view.options if q.kind == "choice" else q.view.rank_items))
                    if q.kind == "choice" and q.view.multiple:
                        selected = {"1": "1", "Yes": "1", "Selected": "1", "selected": "1"}
                        for option_id, label in q.options():
                            mapping = {**selected, label: "1"}
                            out[f"{q.id}_{option_id}"] = mapping
                            if name:
                                out[f"{name}_{option_id}"] = mapping
                    continue
                if q.kind == "grid":
                    mapping = {str(c.label).strip(): q.value_of(c.id) for c in q.view.columns if str(c.label).strip()}
                    if not mapping:
                        continue
                    if q.view.multiple:
                        selected = {"1": "1", "Yes": "1", "Selected": "1", "selected": "1"}
                        for row_id, _label in q.rows():
                            for column_id, column_label in q.columns():
                                cell = {**selected, column_label: "1"}
                                out[f"{q.id}_{row_id}_{column_id}"] = cell
                                if name:
                                    out[f"{name}_{row_id}_{column_id}"] = cell
                        continue
                    out[q.id] = mapping
                    if name:
                        out[name] = mapping
                    for row_id, _label in q.rows():
                        out[f"{q.id}_{row_id}"] = mapping
                        if name:
                            out[f"{name}_{row_id}"] = mapping
                    continue
                if q.kind == "group":
                    for field in q.group_fields():
                        if field["text_entry"]:
                            continue
                        mapping = {
                            str(label).strip(): str(field["codes"].get(option_id, option_id)).strip()
                            for option_id, label in field["options"]
                            if str(label).strip()
                        }
                        if not field["qid"] or not mapping:
                            continue
                        out[field["qid"]] = mapping
                        if field["name"]:
                            out[field["name"]] = mapping
                        for row_id, _text in field["rows"]:
                            out[f"{field['qid']}_{row_id}"] = mapping
                            if field["name"]:
                                out[f"{field['name']}_{row_id}"] = mapping
        # each twin's copy: options as drawn (piped values, repeat rows)
        for copy in self.copies:
            for shown in copy:
                q = Q(shown)
                if q.id and q.kind in {"choice", "ranking"}:
                    register(q.id, str(q.question.get("export_name", "") or "").strip(), choice_map(q, q.view.options if q.kind == "choice" else q.view.rank_items))
        return out

    # ---- the columns

    def columns(self, *, question_ids: set[str], text_by_id: dict[str, str], fallback_text_by_id: dict[str, str] | None = None) -> list[dict[str, str]]:
        """The export columns, in survey order: each with its answer ID, header,
        description and ImportId; answers no question names last."""
        output: list[dict[str, str]] = []
        seen: set[str] = set()
        seen_import: set[str] = set()
        state: dict[str, Any] = {"contexts": {}}

        def append(*, response_id: str, header: str, description: str, import_id: str) -> None:
            if not response_id or response_id not in question_ids or response_id in seen or import_id in seen_import:
                return
            row = re.match(r"^(\d+)_", response_id)
            contexts = state["contexts"]
            if row and row.group(1) in contexts and "${lm://" in description:
                description = normalize(_render_loop_text(description, contexts[row.group(1)])) or description
            seen.add(response_id)
            seen_import.add(import_id)
            output.append({"question_id": response_id, "header": header, "description": description, "import_id": import_id})

        for block, questions in self.placements():
            context_parts: list[str] = []
            last_was_text = False
            loop_contexts = self._loop_contexts(block) if block.get("kind") == "survey" else {}
            for shown in questions:
                q = Q(shown)
                if q.kind == "instruction":
                    gated = bool(q.question.get("show_if"))
                    if q.text and not gated:
                        context_parts = context_parts + [q.text] if last_was_text and context_parts else [q.text]
                    elif gated:
                        context_parts = []
                    last_was_text = not gated
                else:
                    last_was_text = False
                state["contexts"] = loop_contexts
                _question_columns(q, " ".join(context_parts).strip(), loop_contexts, question_ids, text_by_id, append, seen)

        for response_id in sorted(question_ids - seen - seen_import, key=natural_key):
            output.append(
                {
                    "question_id": response_id,
                    "header": response_id,
                    "description": normalize(text_by_id.get(response_id) or (fallback_text_by_id or {}).get(response_id, response_id)),
                    "import_id": response_id,
                }
            )
        return output


def _question_columns(
    q: Q,
    context: str,
    loop_contexts: dict[str, dict[str, Any]],
    question_ids: set[str],
    text_by_id: dict[str, str],
    append: Callable[..., None],
    seen: set[str],
) -> None:
    """One question's columns (a port of the website export tool's rules)."""
    qid = q.id
    if not qid:
        return
    name = q.name
    text = q.text or normalize(text_by_id.get(qid, name))

    def described(response_id: str, fallback: str, *, prefix: str = "") -> str:
        description = normalize(text_by_id.get(response_id, ""))
        if not description and prefix:
            description = normalize(_render_loop_text(fallback, loop_contexts.get(prefix)))
        return description or normalize(fallback)

    def header(base: str, prefix: str) -> str:
        return f"{prefix}_{base}" if prefix else base

    def item_description(response_id: str, item_text: str) -> str:
        description = normalize(text_by_id.get(response_id, ""))
        if not description:
            description = text
            if item_text:
                description = f"{description} - {normalize(item_text)}"
        return description

    def collapsed(response_id: str, prefix: str = "") -> None:
        append(
            response_id=response_id,
            header=header(name, prefix),
            description=_merge_context(context, normalize(text_by_id.get(response_id, text))),
            import_id=response_id,
        )

    def collapsed_and_rows() -> None:
        # exports of older runs that had already collapsed the answers
        collapsed(qid)
        for prefix, response_id in _loop_response_ids(question_ids, qid):
            collapsed(response_id, prefix)

    kind = q.kind
    if kind == "form":
        rows = q.rows()
        row_ids = [row_id for row_id, _ in rows]

        def form(response_id: str, row_id: str, row_text: str, prefix: str = "") -> None:
            append(response_id=response_id, header=header(f"{name}_{row_id}", prefix), description=_merge_context(context, item_description(response_id, row_text)), import_id=response_id)

        for row_id, row_text in rows:
            form(f"{qid}_{row_id}", row_id, row_text)
        for prefix in _loop_row_prefixes(question_ids, base=qid, row_ids=row_ids):
            for row_id, row_text in rows:
                form(f"{prefix}_{qid}_{row_id}", row_id, row_text, prefix)
        collapsed_and_rows()
        return
    if kind == "text":

        def text_entry(response_id: str, prefix: str = "") -> None:
            append(
                response_id=response_id,
                header=header(name, prefix),
                description=_merge_context(context, described(response_id, text, prefix=prefix)),
                import_id=f"{response_id}_TEXT",
            )

        text_entry(qid)
        for prefix, response_id in _loop_response_ids(question_ids, qid):
            text_entry(response_id, prefix)
        return
    if kind in {"allocation", "grid_sum"}:

        def amount(response_id: str, row_id: str, row_text: str, prefix: str = "") -> None:
            export_row = q.code_of(row_id)
            append(
                response_id=response_id,
                header=header(f"{name}_{export_row}", prefix),
                description=_merge_context(context, item_description(response_id, row_text)),
                import_id=_replace_terminal_id(response_id, row_id, export_row),
            )

        rows = q.rows()
        for row_id, row_text in rows:
            amount(f"{qid}_{row_id}", row_id, row_text)
        for prefix in _loop_row_prefixes(question_ids, base=qid, row_ids=[r for r, _ in rows]):
            for row_id, row_text in rows:
                amount(f"{prefix}_{qid}_{row_id}", row_id, row_text, prefix)
        if kind == "allocation":
            collapsed_and_rows()
        return
    if kind == "grid_text":

        def box(response_id: str, row_id: str, row_text: str, prefix: str = "") -> None:
            append(
                response_id=response_id,
                header=header(f"{name}_{row_id}", prefix),
                description=_merge_context(context, item_description(response_id, row_text)),
                import_id=f"{response_id}_TEXT",
            )

        rows = q.rows()
        for row_id, row_text in rows:
            box(f"{qid}_{row_id}", row_id, row_text)
        for prefix in _loop_row_prefixes(question_ids, base=qid, row_ids=[r for r, _ in rows]):
            for row_id, row_text in rows:
                box(f"{prefix}_{qid}_{row_id}", row_id, row_text, prefix)
        return
    if kind == "grid":
        rows = q.rows()
        if q.view.multiple:
            for row_id, row_text in rows:
                for column_id, column_text in q.columns():
                    response_id = f"{qid}_{row_id}_{column_id}"
                    export_column = q.code_of(column_id)
                    description = normalize(text_by_id.get(response_id, ""))
                    if not description:
                        parts = [text]
                        if row_text:
                            parts.append(normalize(row_text))
                        if column_text:
                            parts.append(normalize(column_text))
                        description = " - ".join(part for part in parts if part)
                    append(
                        response_id=response_id,
                        header=f"{name}_{row_id}_{export_column}",
                        description=_merge_context(context, description),
                        import_id=_replace_terminal_id(response_id, column_id, export_column),
                    )
            return

        def row_entry(response_id: str, row_id: str, row_text: str, prefix: str = "") -> None:
            description = normalize(text_by_id.get(response_id, ""))
            if not description:
                description = text
                if row_text:
                    description = _row_text(text, normalize(row_text))
            append(response_id=response_id, header=header(f"{name}_{row_id}", prefix), description=_merge_context(context, description), import_id=response_id)

        def whole(response_id: str, prefix: str = "") -> None:
            export_name = header(name, prefix)
            import_id = response_id
            description = normalize(text_by_id.get(response_id, text))
            if len(rows) == 1:
                row_id, row_text = rows[0]
                export_name = f"{export_name}_{row_id}"
                import_id = f"{response_id}_{row_id}"
                if row_text:
                    description = _row_text(text, row_text)
            append(response_id=response_id, header=export_name, description=_merge_context(context, description), import_id=import_id)

        row_ids = [r for r, _ in rows]
        if len(rows) == 1:
            row_id, row_text = rows[0]
            suffixed = f"{qid}_{row_id}"
            if qid in question_ids:
                whole(qid)
                seen.add(suffixed)
            elif suffixed in question_ids:
                row_entry(suffixed, row_id, row_text)
            for prefix in _loop_row_prefixes(question_ids, base=qid, row_ids=row_ids):
                row_entry(f"{prefix}_{qid}_{row_id}", row_id, row_text, prefix)
            return
        for row_id, row_text in rows:
            row_entry(f"{qid}_{row_id}", row_id, row_text)
        for prefix in _loop_row_prefixes(question_ids, base=qid, row_ids=row_ids):
            for row_id, row_text in rows:
                row_entry(f"{prefix}_{qid}_{row_id}", row_id, row_text, prefix)
        whole(qid)
        for prefix, response_id in _loop_response_ids(question_ids, qid):
            whole(response_id, prefix)
        return
    if kind == "group":
        for field in q.group_fields():
            sub_qid, sub_name = field["qid"], field["name"] or field["qid"]
            sub_label = normalize(field["label"])

            def cell(response_id: str, row_id: str, row_text: str, prefix: str = "") -> None:
                description = normalize(text_by_id.get(response_id, ""))
                if not description:
                    parts = [text]
                    ordered = [sub_label, normalize(row_text)] if field["row_source"] == "sub_question" else [normalize(row_text), sub_label]
                    parts += [part for part in ordered if part]
                    description = " - ".join(part for part in parts if part)
                append(
                    response_id=response_id,
                    header=header(f"{sub_name}_{row_id}" if sub_name else response_id, prefix),
                    description=_merge_context(context, description),
                    import_id=f"{response_id}_TEXT" if field["text_entry"] else response_id,
                )

            if not sub_qid:
                continue
            for row_id, row_text in field["rows"]:
                cell(f"{sub_qid}_{row_id}", row_id, row_text)
            for prefix in _loop_row_prefixes(question_ids, base=sub_qid, row_ids=[r for r, _ in field["rows"]]):
                for row_id, row_text in field["rows"]:
                    cell(f"{prefix}_{sub_qid}_{row_id}", row_id, row_text, prefix)
        return
    if kind == "slider":
        statements = q.statements()
        for statement_id, statement_text in statements:
            suffixed = f"{qid}_{statement_id}"
            if len(statements) == 1 and qid in question_ids:
                response_id = qid
            elif suffixed in question_ids:
                response_id = suffixed
            else:
                continue
            description = normalize(text_by_id.get(response_id, ""))
            if not description:
                description = text
                if statement_text:
                    description = f"{description} - {normalize(statement_text)}"
            append(
                response_id=response_id,
                header=f"{name}_{statement_id}" if statement_id else name,
                description=_merge_context(context, description),
                import_id=f"{qid}_{statement_id}" if statement_id else response_id,
            )
        return
    if kind == "ranking":

        def ranked(response_id: str, item_id: str, item_text: str, prefix: str = "") -> None:
            export_item = q.code_of(item_id)
            append(
                response_id=response_id,
                header=header(f"{name}_{export_item}", prefix),
                description=_merge_context(context, item_description(response_id, item_text)),
                import_id=_replace_terminal_id(response_id, item_id, export_item),
            )

        items = q.ranked()
        for item_id, item_text in items:
            ranked(f"{qid}_{item_id}", item_id, item_text)
        for prefix in _loop_row_prefixes(question_ids, base=qid, row_ids=[i for i, _ in items]):
            for item_id, item_text in items:
                ranked(f"{prefix}_{qid}_{item_id}", item_id, item_text, prefix)
        return

    def write_in(response_id: str, option_id: str, option_text: str, prefix: str = "") -> None:
        export_option = q.code_of(option_id)
        description = normalize(text_by_id.get(response_id, ""))
        if not description:
            description = text
            if option_text:
                description = f"{description} - {normalize(option_text)}"
            description = f"{description} - Text"  # the write-in box, as Qualtrics names it
        append(
            response_id=response_id,
            header=header(f"{name}_{export_option}_TEXT", prefix),
            description=_merge_context(context, description),
            import_id=response_id.replace(f"_{option_id}_TEXT", f"_{export_option}_TEXT"),
        )

    if kind == "choice" and q.view.multiple:

        def option_entry(response_id: str, option_id: str, option_text: str, prefix: str = "") -> None:
            export_option = q.code_of(option_id)
            append(
                response_id=response_id,
                header=header(f"{name}_{export_option}", prefix),
                description=_merge_context(context, item_description(response_id, option_text)),
                import_id=_replace_terminal_id(response_id, option_id, export_option),
            )

        options = q.options()
        write_ins = q.write_ins()
        for option_id, option_text in options:
            option_entry(f"{qid}_{option_id}", option_id, option_text)
        for prefix in _loop_row_prefixes(question_ids, base=qid, row_ids=[o for o, _ in options]):
            for option_id, option_text in options:
                option_entry(f"{prefix}_{qid}_{option_id}", option_id, option_text, prefix)
        for option_id, option_text in write_ins:
            write_in(f"{qid}_{option_id}_TEXT", option_id, option_text)
        for prefix in _loop_text_prefixes(question_ids, base=qid, option_ids=[o for o, _ in write_ins]):
            for option_id, option_text in write_ins:
                write_in(f"{prefix}_{qid}_{option_id}_TEXT", option_id, option_text, prefix)
        collapsed_and_rows()
        return
    if kind == "choice" and q.write_ins():
        write_ins = q.write_ins()
        collapsed(qid)
        loop_items = _loop_response_ids(question_ids, qid)
        for prefix, response_id in loop_items:
            collapsed(response_id, prefix)
        for option_id, option_text in write_ins:
            write_in(f"{qid}_{option_id}_TEXT", option_id, option_text)
        prefixes = {prefix for prefix, _ in loop_items}
        prefixes.update(_loop_text_prefixes(question_ids, base=qid, option_ids=[o for o, _ in write_ins]))
        for prefix in sorted(prefixes, key=_prefix_key):
            for option_id, option_text in write_ins:
                write_in(f"{prefix}_{qid}_{option_id}_TEXT", option_id, option_text, prefix)
        return
    append(response_id=qid, header=name, description=_merge_context(context, normalize(text_by_id.get(qid, text))), import_id=qid)
    for prefix, response_id in _loop_response_ids(question_ids, qid):
        append(
            response_id=response_id,
            header=f"{prefix}_{name}",
            description=_merge_context(context, described(response_id, text, prefix=prefix)),
            import_id=response_id,
        )
