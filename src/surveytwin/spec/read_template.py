"""A survey template (what the QSF reader and the builder produce) in the standard format.

``survey_from_template`` reads every question, block and flow element of a
template into its standard form. Given the QSF the template was read from, it
also keeps each question's text as the QSF wrote it (with its HTML), so the
standard form loses nothing the export had.

What a field means is read the way the engine reads it (``qualtrics.py``). A
setting with no field of its own (a slider's grid lines, a block's lock) is kept
under ``source``, as are the settings themselves: the compiler writes a setting
as read for as long as it still means what the standard form says.
"""

from __future__ import annotations

import copy
from typing import Any

from ..qsf.question_types.base import strip_html
from .conditions import from_qualtrics
from .model import SCHEMA_VERSION
from .qualtrics import (
    SLIDER_SELECTORS,
    TEXT_SELECTORS,
    block_randomization_of,
    mc_selector_of,
    number_of,
    positive_int,
    randomization_of,
    repeat_of,
    required_of,
    text_checks_of,
)
from .text import split_text

IMAGE_STIMULUS = "[Image stimulus attached]"
TRUE = {"op": "true"}


def _media(items: Any) -> list[dict[str, Any]]:
    out = []
    for item in items or []:
        if isinstance(item, dict) and item.get("src"):
            media = {"source": str(item["src"])}
            for key in ("alt", "title"):
                if item.get(key):
                    media[key] = str(item[key])
            out.append(media)
    return out


def _ids(question: dict[str, Any], key: str, count: int) -> list[str]:
    ids = question.get(key)
    if isinstance(ids, list) and len(ids) == count:
        return [str(item) for item in ids]
    return [str(index + 1) for index in range(count)]


def _payload_labels(payload: dict[str, Any] | None, group: str) -> dict[str, str]:
    """Each item's label as the QSF wrote it, markup removed, by item ID."""
    items = payload.get(group) if isinstance(payload, dict) else None
    if not isinstance(items, dict):
        return {}
    out = {}
    for item_id, info in items.items():
        if isinstance(info, dict):
            out[str(item_id)] = strip_html(str(info.get("Display", "") or ""))
    return out


class TemplateReader:
    """Reads one template; ``findings`` lists what it noticed."""

    def __init__(self, template: dict[str, Any], *, payloads: dict[str, dict[str, Any]] | None = None, origin: str = "qsf"):
        self.template = template
        self.payloads = payloads or {}
        self.origin = origin
        self.blocks: dict[str, dict[str, Any]] = {}
        self.questions: dict[str, dict[str, Any]] = {}
        self.variables: dict[str, dict[str, Any]] = {}
        self.findings: list[str] = []
        self._flow_count = 0

    # ---------------------------------------------------------- survey

    def read(self) -> dict[str, Any]:
        flow = self.flow(self.template.get("Elements") or [])
        metadata = self.template.get("Metadata") if isinstance(self.template.get("Metadata"), dict) else {}
        survey: dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "id": str(metadata.get("SurveyID") or "survey"),
        }
        if metadata.get("SurveyName"):
            survey["title"] = str(metadata["SurveyName"])
        if metadata.get("SurveyDescription"):
            survey["description"] = str(metadata["SurveyDescription"])
        if metadata.get("SurveyLanguage"):
            survey["language"] = str(metadata["SurveyLanguage"])
        if self.variables:
            survey["variables"] = list(self.variables.values())
        survey["questions"] = list(self.questions.values())
        survey["blocks"] = list(self.blocks.values())
        survey["flow"] = flow
        source: dict[str, Any] = {"format": self.origin}
        if "Metadata" in self.template:
            source["metadata"] = copy.deepcopy(self.template["Metadata"])
        if "WebServices" in self.template:
            source["web_services"] = copy.deepcopy(self.template["WebServices"])
        extra = {key: copy.deepcopy(value) for key, value in self.template.items() if key not in {"Metadata", "Elements", "WebServices"}}
        if extra:
            source["template"] = extra
        if self.findings:
            source["findings"] = list(dict.fromkeys(self.findings))
        survey["source"] = source
        return survey

    # ---------------------------------------------------------- flow

    def flow(self, elements: list[Any]) -> list[dict[str, Any]]:
        out = []
        for element in elements:
            if isinstance(element, dict):
                out.append(self.flow_node(element))
        return out

    def _node_id(self, element: dict[str, Any], kind: str) -> str:
        flow_id = str(element.get("FlowID", "") or "").strip()
        if flow_id and " " not in flow_id:
            return flow_id
        self._flow_count += 1
        return f"{kind}_{self._flow_count}"

    def flow_node(self, element: dict[str, Any]) -> dict[str, Any]:
        kind = element.get("ElementType")
        source: dict[str, Any] = {}
        if "FlowID" not in element:
            source["no_flow_id"] = True
        elif str(element.get("FlowID") or "").strip() != str(element.get("FlowID")) or " " in str(element.get("FlowID")) or not element.get("FlowID"):
            source["FlowID"] = element.get("FlowID")
        children_key = None
        node: dict[str, Any]
        if kind == "Block":
            return self.show_block(element)
        if kind == "EmbeddedData":
            node = {"id": self._node_id(element, "set"), "type": "set_variables", "assignments": self.assignments(element)}
            extra = {k: copy.deepcopy(v) for k, v in element.items() if k not in {"ElementType", "FlowID", "EmbeddedData", "Declared"}}
            if extra:
                source["element"] = extra
        elif kind == "Branch":
            node = {"id": self._node_id(element, "branch"), "type": "branch"}
            if "BranchLogic" in element:
                node["when"] = from_qualtrics(element["BranchLogic"]) or dict(TRUE)
                source["branch_logic"] = copy.deepcopy(element["BranchLogic"])
            else:
                node["when"] = dict(TRUE)
                source["no_logic"] = True
            children_key = "then"
        elif kind == "Randomizer":
            raw = element.get("SelectElements", 1)
            take = positive_int(raw) or 1
            node = {"id": self._node_id(element, "randomize"), "type": "randomize", "take": take}
            if raw != take:
                source["SelectElements"] = raw
            if "EvenPresentation" in element:
                node["evenly"] = bool(element["EvenPresentation"]) if isinstance(element["EvenPresentation"], bool) else element["EvenPresentation"] is True
                if not isinstance(element["EvenPresentation"], bool):
                    source["EvenPresentation"] = element["EvenPresentation"]
            children_key = "children"
        elif kind == "Group":
            node = {"id": self._node_id(element, "group"), "type": "group", "title": str(element.get("Description", "") or "")}
            if element.get("Description") is None or not isinstance(element.get("Description"), str):
                source["Description"] = element.get("Description")
            children_key = "children"
        elif kind == "EndSurvey":
            node = {"id": self._node_id(element, "end"), "type": "end"}
            if element.get("Options"):
                source["Options"] = copy.deepcopy(element["Options"])
        else:
            # A flow element of no known kind that holds others (the reader keeps
            # those it can run): its steps run as a group.
            node = {"id": self._node_id(element, "group"), "type": "group", "title": ""}
            source["element_type"] = kind
            source["keys"] = sorted(element)
            children_key = "children"
            self.findings.append(f"Flow element {element.get('FlowID') or '?'} of type {kind!r} runs only the steps inside it.")
        if children_key:
            node[children_key] = self.flow(element.get("Elements") or [])
            if "Elements" not in element:
                source["no_elements"] = True
        if source:
            node["source"] = source
        return node

    def assignments(self, element: dict[str, Any]) -> list[dict[str, Any]]:
        out = []
        for row in element.get("EmbeddedData") or []:
            if not isinstance(row, dict):
                continue
            field = str(row.get("Field", "") or "")
            value = row.get("Value", "")
            item: dict[str, Any] = {"variable": field, "value": split_text(value) if isinstance(value, str) else str(value)}
            extra = {k: copy.deepcopy(v) for k, v in row.items() if k not in {"Field", "Value"}}
            if not isinstance(value, str):
                extra["Value"] = copy.deepcopy(value)
            if extra:
                item["source"] = extra
            out.append(item)
            self._variable(field, row, outside=False)
        for row in element.get("Declared") or []:
            if not isinstance(row, dict):
                continue
            field = str(row.get("Field", "") or "")
            item = {"variable": field, "from_outside": True}
            extra = {k: copy.deepcopy(v) for k, v in row.items() if k != "Field"}
            if extra:
                item["source"] = extra
            out.append(item)
            self._variable(field, row, outside=True)
        return out

    def _variable(self, field: str, row: dict[str, Any], *, outside: bool) -> None:
        if not field or field in self.variables:
            return
        kind = str(row.get("VariableType", "") or "").strip().lower()
        variable: dict[str, Any] = {"id": field}
        if kind in {"number", "numeric"}:
            variable["type"] = "number"
        if outside:
            variable["from_outside"] = True
        self.variables[field] = variable

    def show_block(self, element: dict[str, Any]) -> dict[str, Any]:
        block_id = str(element.get("BlockID", "") or "")
        experiment = element.get("Experiment")
        if isinstance(experiment, dict) and isinstance(experiment.get("block"), dict):
            # a conjoint or MaxDiff block: its definition, not the task questions made from it
            self.blocks.setdefault(block_id, copy.deepcopy(experiment["block"]))
        elif block_id not in self.blocks:
            self.blocks[block_id] = self.block(element)
        node: dict[str, Any] = {"type": "show_block", "block": block_id}
        return node

    def block(self, element: dict[str, Any]) -> dict[str, Any]:
        block_id = str(element.get("BlockID", "") or "")
        questions = [q for q in element.get("Questions") or [] if isinstance(q, dict)]
        for question in questions:
            qid = str(question.get("QuestionID", "") or "")
            if qid not in self.questions:
                self.questions[qid] = self.question(question)
        out: dict[str, Any] = {
            "id": block_id,
            "kind": "survey",
            "title": str(element.get("BlockName", "") or ""),
            "questions": [str(q.get("QuestionID", "") or "") for q in questions],
        }
        source: dict[str, Any] = {"type": element.get("BlockType", "")}
        options = element.get("Options")
        if "Options" in element:
            source["options"] = copy.deepcopy(options)
            randomization = block_randomization_of(options)
            if randomization:
                out["randomization"] = randomization
            repeat = repeat_of(options)
            if repeat:
                for row in repeat["rows"]:
                    row["values"] = {key: split_text(value) for key, value in row["values"].items()}
                out["repeat"] = repeat
        extra = {k: copy.deepcopy(v) for k, v in element.items() if k not in {"ElementType", "BlockName", "BlockType", "BlockID", "Questions", "Options"}}
        if extra:
            source["element"] = extra
        out["source"] = source
        return out

    # ---------------------------------------------------------- questions

    def question(self, q: dict[str, Any]) -> dict[str, Any]:
        qid = str(q.get("QuestionID", "") or "")
        qtype = str(q.get("QuestionType", "") or "")
        payload = self.payloads.get(qid)
        source: dict[str, Any] = {"format": self.origin, "type": qtype}
        if "RawQuestionType" in q:
            source["raw_type"] = q["RawQuestionType"]
        out: dict[str, Any] = {"id": qid, "export_name": str(q.get("QuestionName", "") or "")}
        if "QuestionName" not in q:
            source["no_export_name"] = True
        out["content"] = self.content(q, payload, source)
        if q.get("KeepLines") and not (out["content"][0].get("format") == "html"):
            source["keep_lines"] = True  # the text was laid out already (a table read earlier)
        if q.get("ExactType"):
            source["exact_type"] = True
        if "DisplayLogic" in q:
            source["display_logic"] = copy.deepcopy(q["DisplayLogic"])
            condition = from_qualtrics(q["DisplayLogic"])
            if condition is not None:
                out["show_if"] = condition
        if isinstance(q.get("SkipLogic"), list) and q["SkipLogic"]:
            source["skip_logic"] = copy.deepcopy(q["SkipLogic"])
            out["skip"] = [self.skip_rule(rule) for rule in q["SkipLogic"] if isinstance(rule, dict)]
        elif "SkipLogic" in q:
            source["skip_logic"] = copy.deepcopy(q["SkipLogic"])
        if "Settings" in q:
            source["settings"] = copy.deepcopy(q["Settings"])
        settings = q.get("Settings") if isinstance(q.get("Settings"), dict) else {}
        for key, name in (
            ("RecodeValues", "recode_values"),
            ("Randomization", "randomization"),
            ("DefaultEntries", "default_entries"),
            ("ChoiceDisplayLogic", "choice_logic"),
            ("AnswerDisplayLogic", "answer_logic"),
            ("RecodeValuesUnsupported", "recode_unsupported"),
            ("Range", "range"),
            ("DefaultChoices", "default_choices"),
            ("DefaultRanks", "default_ranks"),
        ):
            if key in q:
                source[name] = copy.deepcopy(q[key])

        reader = {
            "MC": self.choice,
            "TE": self.text_entry,
            "Matrix": self.matrix,
            "Slider": self.slider,
            "CS": self.constant_sum,
            "Rank": self.rank,
            "SBS": self.side_by_side,
        }.get(qtype)
        flags = {"is_descriptive": None, "is_timing": "timing", "is_meta": "meta_info", "is_captcha": "captcha", "is_unsupported": "unsupported"}
        kinds = [flag for flag in flags if q.get(flag)]
        presentation: dict[str, Any] = {}
        if kinds and kinds[0] != "is_descriptive":
            out["kind"] = flags[kinds[0]]
        elif kinds:
            pass  # an instruction: no response
        elif qtype == "DB":
            source["no_flag"] = True  # text only, though not flagged as such
        elif reader is not None:
            response, presentation = reader(q, settings, payload, source)
            out["response"] = response
        else:
            out["kind"] = "unsupported"
            source["no_flag"] = True
        flag_values = {flag: q[flag] for flag in flags if flag in q and q[flag] is not True}
        if flag_values:
            source["flags"] = flag_values
        extra = {k: copy.deepcopy(v) for k, v in q.items() if k not in _KNOWN_KEYS.get(qtype, _COMMON_KEYS)}
        if extra:
            source["extra"] = extra
            self.findings.append(f"{qid}: kept {', '.join(sorted(extra))} as read.")
        if presentation:
            out["presentation"] = presentation
        out["source"] = source
        return out

    def content(self, q: dict[str, Any], payload: dict[str, Any] | None, source: dict[str, Any]) -> list[dict[str, Any]]:
        media = _media(q.get("QuestionMedia"))
        text = q.get("QuestionText", "")
        html = payload.get("QuestionText") if isinstance(payload, dict) else None
        item: dict[str, Any] = {"type": "text", "text": split_text(html), "format": "html"} if isinstance(html, str) else {}
        if item:
            # the QSF's own text, when it still reads as this question's text (an
            # edit after reading changes the text; then the text as edited is kept)
            from ..engine.text import question_text

            if question_text({"content": [item] + [{"type": "image", **m} for m in media]}, native=False)[0] != text:
                item = {}
        if not item:
            if text == IMAGE_STIMULUS and media:
                text = ""
            item = {"type": "text", "text": split_text(str(text or ""))}
            if not isinstance(text, str):
                source["question_text"] = copy.deepcopy(text)
        if "QuestionMedia" in q and not media:
            source["question_media"] = copy.deepcopy(q["QuestionMedia"])
        return [item] + [{"type": "image", **m} for m in media]

    def skip_rule(self, rule: dict[str, Any]) -> dict[str, Any]:
        locator = str(rule.get("ChoiceLocator") or rule.get("Locator") or "")
        condition = str(rule.get("Condition", "") or "")
        parts = locator.split("/")
        when: dict[str, Any]
        if locator.startswith("q://") and len(parts) == 5 and parts[3] == "SelectableChoice" and condition in {"Selected", "NotSelected"}:
            when = {"op": "selected" if condition == "Selected" else "not_selected", "question": parts[2], "option": parts[4]}
        else:
            when = {"op": "source", "qualtrics": copy.deepcopy(rule)}
        destination = str(rule.get("SkipToDestination", "") or "")
        to = {"ENDOFBLOCK": "end_of_block", "ENDOFSURVEY": "end_of_survey"}.get(destination, destination or "end_of_block")
        out: dict[str, Any] = {"when": when, "to": to}
        if str(rule.get("SkipLogicID", "") or "").strip() and " " not in str(rule["SkipLogicID"]):
            out["id"] = str(rule["SkipLogicID"])
        if rule.get("Description"):
            out["description"] = str(rule["Description"])
        return out

    # ---- items

    def _items(
        self,
        q: dict[str, Any],
        labels_key: str,
        ids_key: str,
        media_key: str,
        *,
        raw_labels: dict[str, str],
        logic: dict[str, Any] | None,
        codes: dict[str, Any] | None,
        source: dict[str, Any],
    ) -> list[dict[str, Any]]:
        labels = q.get(labels_key) if isinstance(q.get(labels_key), list) else []
        ids = _ids(q, ids_key, len(labels))
        if ids_key in q and not (isinstance(q.get(ids_key), list) and len(q[ids_key]) == len(labels)):
            source.setdefault("odd_lists", {})[ids_key] = copy.deepcopy(q[ids_key])
        media = q.get(media_key) if isinstance(q.get(media_key), list) else None
        if media is not None and len(media) != len(labels):
            source.setdefault("odd_lists", {})[media_key] = copy.deepcopy(media)
            media = None
        items = []
        for index, label in enumerate(labels):
            item_id = ids[index]
            text = str(label) if label is not None else ""
            if item_id in raw_labels and raw_labels[item_id] == "":
                text = ""
            item: dict[str, Any] = {"id": item_id, "label": split_text(text)}
            if codes and item_id in codes:
                item["code"] = codes[item_id]
            item_media = _media(media[index]) if media is not None else []
            if item_media:
                item["media"] = item_media
            if logic and isinstance(logic.get(item_id), dict) and logic[item_id]:
                condition = from_qualtrics(logic[item_id])
                if condition is not None:
                    item["show_if"] = condition
            items.append(item)
        return items

    def _required(self, settings: dict[str, Any]) -> bool | str:
        return required_of(settings.get("ForceResponse"))

    # ---- per type: each returns (response, presentation)

    def choice(self, q, settings, payload, source):
        mode, control = mc_selector_of(settings.get("Selector"))
        codes = q.get("RecodeValues") if isinstance(q.get("RecodeValues"), dict) else None
        options = self._items(
            q, "Options", "OptionsID", "OptionMedia",
            raw_labels=_payload_labels(payload, "Choices"),
            logic=q.get("ChoiceDisplayLogic"), codes=codes, source=source,
        )
        write_ins = {str(entry.get("OptionID")) for entry in q.get("TextEntryOptions") or [] if isinstance(entry, dict)}
        for option in options:
            if option["id"] in write_ins:
                option["write_in"] = True
        response: dict[str, Any] = {"type": "choice", "mode": mode, "options": options, "required": self._required(settings)}
        low, high = positive_int(settings.get("MinChoices")), positive_int(settings.get("MaxChoices"))
        if low:
            response["min_selected"] = low
        if high:
            response["max_selected"] = high
        defaults = [str(entry.get("OptionID")) for entry in q.get("DefaultChoices") or [] if isinstance(entry, dict)]
        if defaults:
            response["default"] = defaults
        randomization = randomization_of(q.get("Randomization"))
        if randomization:
            response["randomization"] = randomization
        self._keep_ids(q, source, "OptionsID", [o["id"] for o in options], recoded=bool(codes))
        return response, ({"control": control} if control else {})

    def _keep_ids(self, q, source, key, ids, *, recoded: bool, always: bool = False) -> None:
        """Note an ID list the old reader kept although the default rule drops it (or the reverse)."""
        default = ids == [str(i) for i in range(1, len(ids) + 1)]
        expected = always or not default or recoded
        if (key in q) != expected:
            source.setdefault("id_lists", {})[key] = key in q

    def _text_response(self, settings: dict[str, Any]) -> dict[str, Any]:
        response: dict[str, Any] = {"type": "text"}
        checks = text_checks_of(settings)
        response.update(checks)
        return response

    def text_entry(self, q, settings, payload, source):
        selector = str(settings.get("Selector", "") or "")
        if selector == "FORM":
            fields = self._items(
                q, "Rows", "RowsID", "RowsMedia",
                raw_labels=_payload_labels(payload, "Choices"),
                logic=q.get("ChoiceDisplayLogic"), codes=None, source=source,
            )
            defaults = q.get("DefaultChoices") if isinstance(q.get("DefaultChoices"), list) else []
            if len(defaults) == len(fields):
                for field, default in zip(fields, defaults):
                    if default is not None:
                        field["default"] = split_text(str(default))
            for field in fields:
                field.pop("code", None)
            response: dict[str, Any] = {"type": "form", "fields": fields, "required": self._required(settings)}
            checks = self._text_response(settings)
            if len(checks) > 1:
                response["field_response"] = checks
            self._keep_ids(q, source, "RowsID", [f["id"] for f in fields], recoded=False)
            if "Rows" not in q:
                source["no_rows"] = True
            return response, {}
        response = self._text_response(settings)
        response["required"] = self._required(settings)
        defaults = q.get("DefaultChoices")
        if isinstance(defaults, list) and defaults:
            response["default"] = split_text(str(defaults[0]))
        control = TEXT_SELECTORS.get(selector)
        return response, ({"control": control} if control else {})

    def matrix(self, q, settings, payload, source):
        selector = str(settings.get("Selector", "") or "")
        sub_selector = str(settings.get("SubSelector", "") or "")
        codes = q.get("RecodeValues") if isinstance(q.get("RecodeValues"), dict) else None
        rows = self._items(
            q, "Rows", "RowsID", "RowsMedia",
            raw_labels=_payload_labels(payload, "Choices"),
            logic=q.get("ChoiceDisplayLogic"), codes=None, source=source,
        )
        columns = self._items(
            q, "Columns", "ColumnsID", "ColumnsMedia",
            raw_labels=self._answer_labels(payload),
            logic=q.get("AnswerDisplayLogic"), codes=codes, source=source,
        )
        if selector == "Bipolar" and payload is not None:
            raw_rows = _payload_labels(payload, "Choices")
            for row in rows:
                raw = raw_rows.get(row["id"])
                if raw is None:
                    continue
                left, sep, right = raw.partition(":")
                if sep and left.strip() and right.strip():
                    row["label"] = split_text(raw)
                    row["left_label"] = split_text(left.strip())
                    row["right_label"] = split_text(right.strip())
        response: dict[str, Any] = {"type": "matrix", "rows": rows, "required": self._required(settings)}
        presentation: dict[str, Any] = {}
        if selector == "TE":
            response["mode"] = "per_cell"
            response["columns"] = columns
            response["cell_response"] = self._text_response(settings)
        elif selector == "CS":
            response["mode"] = "per_cell"
            response["columns"] = columns
            cell: dict[str, Any] = {"type": "number"}
            value_range = q.get("Range") if isinstance(q.get("Range"), dict) else {}
            for key, name in (("Min", "min"), ("Max", "max")):
                number = number_of(value_range.get(key)) if key in value_range else None
                if number is not None:
                    cell[name] = number
            response["cell_response"] = cell
            total = number_of(settings.get("ChoiceTotal"))
            if total is not None:
                response["constraints"] = [{"type": "sum_equals", "axis": "row", "total": total}]
        else:
            response["mode"] = "per_row"
            row_response: dict[str, Any] = {
                "type": "choice",
                "mode": "multiple" if sub_selector.upper() == "MULTIPLEANSWER" else "single",
                "options": columns,
            }
            low, high = positive_int(settings.get("MinChoices")), positive_int(settings.get("MaxChoices"))
            if low:
                row_response["min_selected"] = low
            if high:
                row_response["max_selected"] = high
            response["row_response"] = row_response
            if selector == "Bipolar":
                presentation["control"] = "bipolar"
        randomization = randomization_of(q.get("Randomization"))
        if randomization:
            response["randomization"] = randomization
        self._keep_ids(q, source, "RowsID", [r["id"] for r in rows], recoded=False)
        self._keep_ids(q, source, "ColumnsID", [c["id"] for c in columns], recoded=bool(codes))
        return response, presentation

    def _answer_labels(self, payload: dict[str, Any] | None) -> dict[str, str]:
        answers = payload.get("Answers") if isinstance(payload, dict) else None
        if not isinstance(answers, dict) or not answers:
            return {}
        first = next(iter(answers.values()))
        if isinstance(first, dict) and "Display" not in first:
            nested = {str(k): strip_html(str(v.get("Display", "") or "")) for k, v in first.items() if isinstance(v, dict)}
            if nested:
                return nested
        return _payload_labels(payload, "Answers")

    def slider(self, q, settings, payload, source):
        statements = self._items(
            q, "Statements", "StatementsID", "StatementsMedia",
            raw_labels=_payload_labels(payload, "Choices"),
            logic=q.get("ChoiceDisplayLogic"), codes=None, source=source,
        )
        defaults = {
            str(entry.get("StatementID")): entry.get("Value")
            for entry in q.get("DefaultChoices") or []
            if isinstance(entry, dict)
        }
        for row in statements:
            if row["id"] in defaults:
                value = number_of(defaults[row["id"]])
                row["default"] = value if value is not None else str(defaults[row["id"]])
        number: dict[str, Any] = {"type": "number"}
        value_range = q.get("Range") if isinstance(q.get("Range"), dict) else {}
        for key, name in (("Min", "min"), ("Max", "max")):
            value = number_of(value_range.get(key)) if key in value_range else None
            if value is not None:
                number[name] = value
        step = number_of(settings.get("NumericStep")) if settings.get("NumericStep") is not None else None
        if step is not None:
            number["step"] = step
        if settings.get("NumericDecimals") is not None:
            decimals = number_of(settings.get("NumericDecimals"))
            if isinstance(decimals, int) and decimals >= 0:
                number["decimals"] = decimals
        if truthy_setting(settings.get("NotApplicable")):
            number["not_applicable"] = {"label": str(settings.get("NotApplicableLabel", "") or "").strip() or "Not Applicable"}
        response: dict[str, Any] = {
            "type": "matrix",
            "mode": "per_row",
            "rows": statements,
            "row_response": number,
            "required": self._required(settings),
        }
        randomization = randomization_of(q.get("Randomization"))
        if randomization:
            response["randomization"] = randomization
        self._keep_ids(q, source, "StatementsID", [s["id"] for s in statements], recoded=False)
        if "Statements" not in q:
            source["no_rows"] = True
        control = SLIDER_SELECTORS.get(str(settings.get("Selector", "") or "").strip().upper())
        return response, ({"control": control} if control else {})

    def constant_sum(self, q, settings, payload, source):
        codes = q.get("RecodeValues") if isinstance(q.get("RecodeValues"), dict) else None
        items = self._items(
            q, "Rows", "RowsID", "RowsMedia",
            raw_labels=_payload_labels(payload, "Choices"),
            logic=q.get("ChoiceDisplayLogic"), codes=codes, source=source,
        )
        response: dict[str, Any] = {"type": "allocation", "items": items, "required": self._required(settings)}
        total = number_of(settings.get("ChoiceTotal"))
        if total is not None:
            response["total"] = total
        value_range = q.get("Range") if isinstance(q.get("Range"), dict) else None
        if value_range is not None:
            for key, name in (("Min", "min"), ("Max", "max")):
                value = number_of(value_range.get(key)) if key in value_range else None
                if value is not None:
                    response[name] = value
        defaults = {}
        for entry in q.get("DefaultEntries") or []:
            if isinstance(entry, dict) and entry.get("Kind") == "value" and "ItemID" in entry and "SubID" not in entry:
                value = number_of(entry.get("Value"))
                defaults[str(entry["ItemID"])] = value if value is not None else str(entry.get("Value"))
        if defaults:
            response["default"] = defaults
        randomization = randomization_of(q.get("Randomization"))
        if randomization:
            response["randomization"] = randomization
        self._keep_ids(q, source, "RowsID", [i["id"] for i in items], recoded=bool(codes))
        if "Rows" not in q:
            source["no_rows"] = True
        return response, {}

    def rank(self, q, settings, payload, source):
        codes = q.get("RecodeValues") if isinstance(q.get("RecodeValues"), dict) else None
        items = self._items(
            q, "Options", "OptionsID", "OptionMedia",
            raw_labels=_payload_labels(payload, "Choices"),
            logic=q.get("ChoiceDisplayLogic"), codes=codes, source=source,
        )
        response: dict[str, Any] = {"type": "ranking", "items": items, "required": self._required(settings)}
        low, high = positive_int(settings.get("MinChoices")), positive_int(settings.get("MaxChoices"))
        if low:
            response["min_ranked"] = low
        if high:
            response["max_ranked"] = high
        defaults = []
        for entry in q.get("DefaultRanks") or []:
            if isinstance(entry, dict):
                rank = positive_int(entry.get("Rank"))
                if rank:
                    defaults.append({"item": str(entry.get("OptionID")), "rank": rank})
        if defaults:
            response["default"] = defaults
        randomization = randomization_of(q.get("Randomization"))
        if randomization:
            response["randomization"] = randomization
        self._keep_ids(q, source, "OptionsID", [i["id"] for i in items], recoded=bool(codes), always=True)
        return response, {}

    def side_by_side(self, q, settings, payload, source):
        fields = []
        for sub in q.get("sub_questions") or []:
            if not isinstance(sub, dict):
                continue
            fields.append(self._sbs_field(sub))
        # The question's own scale and statements, as the reader keeps them
        source["sbs"] = {key: copy.deepcopy(q[key]) for key in ("Rows", "RowsID", "RowsMedia", "Options", "OptionsID", "OptionMedia") if key in q}
        response: dict[str, Any] = {
            "type": "group",
            "layout": "side_by_side",
            "fields": fields,
            "required": self._required(settings),
        }
        randomization = randomization_of(q.get("Randomization"))
        if randomization:
            response["randomization"] = randomization
        return response, {}

    def _sbs_field(self, sub: dict[str, Any]) -> dict[str, Any]:
        sub_source: dict[str, Any] = {}
        rows = self._items(sub, "Rows", "RowsID", "RowsMedia", raw_labels={}, logic=None, codes=None, source=sub_source)
        codes = sub.get("RecodeValues") if isinstance(sub.get("RecodeValues"), dict) else None
        options = self._items(sub, "Options", "OptionsID", "OptionsMedia", raw_labels={}, logic=None, codes=codes, source=sub_source)
        settings = sub.get("Settings") if isinstance(sub.get("Settings"), dict) else {}
        selector = str(sub.get("selector", "") or "")
        defaults: dict[str, list[str]] = {}
        for entry in sub.get("DefaultChoices") or []:
            if isinstance(entry, dict):
                defaults.setdefault(str(entry.get("RowID")), []).append(str(entry.get("OptionID")))
        for row in rows:
            if row["id"] in defaults:
                row["default"] = defaults[row["id"]]
        if selector.upper() == "TE":
            row_response: dict[str, Any] = self._text_response(settings)
        else:
            row_response = {
                "type": "choice",
                "mode": "multiple" if str(sub.get("subselector", "") or "").upper() == "MULTIPLEANSWER" else "single",
                "options": options,
            }
        matrix: dict[str, Any] = {
            "type": "matrix",
            "mode": "per_row",
            "rows": rows,
            "row_response": row_response,
            "required": required_of(settings.get("ForceResponse")),
        }
        field: dict[str, Any] = {
            "id": str(sub.get("id", "") or ""),
            "label": split_text(str(sub.get("text", "") or "")),
            "export_name": str(sub.get("QuestionName", "") or ""),
        }
        media = _media(sub.get("QuestionMedia"))
        if media:
            field["media"] = media
        field["response"] = matrix
        for key in ("question_type", "selector", "subselector", "QuestionID", "Settings", "RecodeValues", "DefaultChoices"):
            if key in sub:
                sub_source[key] = copy.deepcopy(sub[key])
        for key in ("RowsID", "OptionsID"):
            ids = [r["id"] for r in (rows if key == "RowsID" else options)]
            default = ids == [str(i) for i in range(1, len(ids) + 1)]
            if (key in sub) == default:
                sub_source.setdefault("id_lists", {})[key] = key in sub
        extra = {k: copy.deepcopy(v) for k, v in sub.items() if k not in _SBS_SUB_KEYS}
        if extra:
            sub_source["extra"] = extra
        field["source"] = sub_source
        return field


def truthy_setting(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


_COMMON_KEYS = frozenset(
    {
        "QuestionID",
        "QuestionText",
        "QuestionType",
        "QuestionName",
        "RawQuestionType",
        "QuestionMedia",
        "DisplayLogic",
        "SkipLogic",
        "Settings",
        "RecodeValues",
        "Randomization",
        "DefaultEntries",
        "ChoiceDisplayLogic",
        "AnswerDisplayLogic",
        "RecodeValuesUnsupported",
        "KeepLines",
        "ExactType",
        "is_descriptive",
        "is_timing",
        "is_meta",
        "is_captcha",
        "is_unsupported",
    }
)
_KNOWN_KEYS = {
    "MC": _COMMON_KEYS | {"Options", "OptionsID", "OptionMedia", "TextEntryOptions", "DefaultChoices"},
    "TE": _COMMON_KEYS | {"Rows", "RowsID", "RowsMedia", "DefaultChoices"},
    "Matrix": _COMMON_KEYS | {"Rows", "RowsID", "RowsMedia", "Columns", "ColumnsID", "ColumnsMedia", "Range"},
    "Slider": _COMMON_KEYS | {"Statements", "StatementsID", "StatementsMedia", "Range", "DefaultChoices"},
    "CS": _COMMON_KEYS | {"Rows", "RowsID", "RowsMedia", "Range"},
    "Rank": _COMMON_KEYS | {"Options", "OptionsID", "OptionMedia", "DefaultRanks"},
    "SBS": _COMMON_KEYS | {"Rows", "RowsID", "RowsMedia", "Options", "OptionsID", "OptionMedia", "sub_questions"},
}
_SBS_SUB_KEYS = frozenset(
    {
        "id",
        "text",
        "question_type",
        "selector",
        "subselector",
        "QuestionID",
        "QuestionName",
        "Options",
        "OptionsID",
        "OptionsMedia",
        "Rows",
        "RowsID",
        "RowsMedia",
        "QuestionMedia",
        "Settings",
        "RecodeValues",
        "DefaultChoices",
    }
)


def qsf_payloads(qsf: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
    """Each question's QSF payload by question ID."""
    out: dict[str, dict[str, Any]] = {}
    for element in (qsf or {}).get("SurveyElements") or []:
        if isinstance(element, dict) and element.get("Element") == "SQ" and isinstance(element.get("Payload"), dict):
            qid = str(element["Payload"].get("QuestionID", "") or "")
            if qid and qid not in out:
                out[qid] = element["Payload"]
    return out


def survey_from_template(
    template: dict[str, Any],
    *,
    qsf: dict[str, Any] | None = None,
    origin: str | None = None,
) -> dict[str, Any]:
    """A survey template in the standard format (with the QSF, its question texts as written)."""
    return TemplateReader(
        template,
        payloads=qsf_payloads(qsf) if qsf is not None else None,
        origin=origin or ("qsf" if qsf is not None else "template"),
    ).read()
