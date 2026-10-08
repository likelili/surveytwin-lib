"""The standard format compiled to the survey template the engine runs.

Every question, block and flow step of a survey in the standard format becomes
the template element the engine reads (the shape ``qsf/parse_qsf.py`` produces).
Each field is written from what the standard form says. A setting the survey was
read with (kept under ``source``) is written as read for as long as it still
means what the standard form says, so a survey read from a QSF compiles to
exactly the template the QSF reader gives; once a field changes, its setting is
written anew.

How text reaches the model is decided here too: HTML text loses its markup the
way the QSF reader removes it, an item with no label but a picture is called
"[Image option 2]", and so on.
"""

from __future__ import annotations

import copy
from typing import Any, Callable

from ..engine.text import IMAGE_STIMULUS, item_labels, question_text, render_table  # noqa: F401 (shared rules)
from .conditions import from_qualtrics, to_qualtrics
from .qualtrics import (
    SLIDER_SELECTOR_NAMES,
    SLIDER_SELECTORS,
    TEXT_CHECK_KEYS,
    TEXT_SELECTORS,
    block_randomization_of,
    force_response_setting,
    mc_selector_of,
    mc_selector_setting,
    number_of,
    positive_int,
    randomization_of,
    randomization_setting,
    repeat_of,
    repeat_setting,
    required_of,
    text_checks_of,
    text_checks_settings,
)
from .text import join_text

TEXT_CONTROLS = {control: selector for selector, control in TEXT_SELECTORS.items()}


class CompileError(ValueError):
    """The survey cannot be compiled (a reference to a missing block or question)."""


def _default_ids(ids: list[str]) -> bool:
    return ids == [str(i) for i in range(1, len(ids) + 1)]


def _media_out(items: Any) -> list[dict[str, str]]:
    out = []
    for item in items or []:
        media = {"kind": "image", "src": str(item["source"])}
        for key in ("alt", "title"):
            if item.get(key):
                media[key] = str(item[key])
        out.append(media)
    return out


class _Settings:
    """A question's settings: kept as read while each group still means the same."""

    def __init__(self, raw: Any, base: dict[str, Any]):
        self.raw_given = isinstance(raw, dict)
        self.values: dict[str, Any] = copy.deepcopy(raw) if isinstance(raw, dict) else dict(base)
        self.changed: set[str] = set()

    def group(self, name: str, keys: tuple[str, ...], decode: Callable[[dict[str, Any]], Any], wanted: Any, encode: Callable[[Any], dict[str, Any]]) -> bool:
        """Keep ``keys`` as they are when they decode to ``wanted``; else write them. True when kept."""
        if self.raw_given and decode(self.values) == wanted:
            return True
        for key in keys:
            self.values.pop(key, None)
        self.values.update({key: value for key, value in encode(wanted).items() if value is not None})
        self.changed.add(name)
        return False


class Compiler:
    def __init__(self, survey: dict[str, Any]):
        self.survey = survey
        self.questions = {str(q["id"]): q for q in survey.get("questions") or []}
        self.blocks = {str(b["id"]): b for b in survey.get("blocks") or []}
        self._generated = 0
        source = survey.get("source") if isinstance(survey.get("source"), dict) else {}
        # Written in the standard format (not read from a QSF, a template or the builder)
        self.native = source.get("format") not in {"qsf", "template", "survey_builder"}

    # ---------------------------------------------------------- survey

    def compile(self) -> dict[str, Any]:
        source = self.survey.get("source") if isinstance(self.survey.get("source"), dict) else {}
        template: dict[str, Any] = {"Metadata": self.metadata(source), "Elements": self.flow(self.survey.get("flow") or [])}
        if "web_services" in source:
            template["WebServices"] = copy.deepcopy(source["web_services"])
        for key, value in (source.get("template") or {}).items():
            template.setdefault(key, copy.deepcopy(value))
        return template

    def metadata(self, source: dict[str, Any]) -> dict[str, Any]:
        meta = copy.deepcopy(source["metadata"]) if isinstance(source.get("metadata"), dict) else {}
        survey_id = str(self.survey.get("id") or "")
        if survey_id and not ("SurveyID" not in meta and survey_id == "survey" and "metadata" in source):
            meta["SurveyID"] = survey_id
        for key, name in (("title", "SurveyName"), ("description", "SurveyDescription"), ("language", "SurveyLanguage")):
            value = self.survey.get(key)
            if value:
                meta[name] = value
            else:
                meta.pop(name, None)
        return meta

    # ---------------------------------------------------------- flow

    def flow(self, nodes: list[dict[str, Any]]) -> list[dict[str, Any]]:
        out = []
        for node in nodes:
            element = self.flow_node(node)
            if isinstance(element, list):
                out.extend(element)
            elif element is not None:
                out.append(element)
        return out

    def else_branch(self, node: dict[str, Any], when: dict[str, Any]) -> dict[str, Any]:
        """'else' as a second branch on the opposite condition, right after the first.
        (``check_survey`` refuses an 'else' whose condition the 'then' steps can change.)"""
        element: dict[str, Any] = {"ElementType": "Branch", "FlowID": f"{node.get('id') or self._new_id('FL')}_else"}
        element["BranchLogic"] = to_qualtrics({"op": "not", "arg": when}, wrapper={})
        element["Elements"] = self.flow(node.get("else") or [])
        return element

    def _flow_id(self, node: dict[str, Any], element: dict[str, Any]) -> None:
        source = node.get("source") or {}
        if source.get("no_flow_id"):
            return
        element["FlowID"] = source["FlowID"] if "FlowID" in source else str(node.get("id") or self._new_id("FL"))

    def _new_id(self, prefix: str) -> str:
        self._generated += 1
        return f"{prefix}_{self._generated}"

    def _children(self, node: dict[str, Any], element: dict[str, Any], key: str) -> None:
        children = self.flow(node.get(key) or [])
        if children or not (node.get("source") or {}).get("no_elements"):
            element["Elements"] = children

    def flow_node(self, node: dict[str, Any]) -> dict[str, Any] | None:
        kind = node.get("type")
        source = node.get("source") or {}
        if kind == "show_block":
            return self.block(str(node.get("block")))
        element: dict[str, Any]
        if kind == "set_variables":
            element = {"ElementType": "EmbeddedData"}
            self._flow_id(node, element)
            rows, declared = [], []
            for assignment in node.get("assignments") or []:
                extra = assignment.get("source") or {}
                if assignment.get("from_outside"):
                    row = {"Field": assignment["variable"]}
                    row.update({k: copy.deepcopy(v) for k, v in extra.items()})
                    row.setdefault("Type", "Recipient")
                    declared.append(row)
                    continue
                value = join_text(assignment.get("value", ""))
                row = {"Field": assignment["variable"], "Value": value}
                for key, raw in extra.items():
                    if key == "Value":
                        if str(raw) == value:
                            row["Value"] = copy.deepcopy(raw)
                    else:
                        row[key] = copy.deepcopy(raw)
                rows.append(row)
            element["EmbeddedData"] = rows
            if declared:
                element["Declared"] = declared
            for key, value in (source.get("element") or {}).items():
                element.setdefault(key, copy.deepcopy(value))
            return element
        if kind == "branch":
            element = {"ElementType": "Branch"}
            self._flow_id(node, element)
            when = node.get("when") or {"op": "true"}
            if source.get("no_logic") and when == {"op": "true"}:
                pass
            elif "branch_logic" in source and (from_qualtrics(source["branch_logic"]) or {"op": "true"}) == when:
                element["BranchLogic"] = copy.deepcopy(source["branch_logic"])
            else:
                element["BranchLogic"] = to_qualtrics(when, wrapper={})
            self._children(node, element, "then")
            if node.get("else"):
                return [element, self.else_branch(node, when)]
            return element
        if kind == "randomize":
            element = {"ElementType": "Randomizer"}
            self._flow_id(node, element)
            raw = source.get("SelectElements")
            element["SelectElements"] = raw if raw is not None and (positive_int(raw) or 1) == node["take"] else node["take"]
            if "evenly" in node:
                raw_even = source.get("EvenPresentation")
                element["EvenPresentation"] = raw_even if "EvenPresentation" in source and (raw_even is True) == node["evenly"] else node["evenly"]
            self._children(node, element, "children")
            return element
        if kind == "group":
            if "element_type" in source:
                element = {}
                if source.get("element_type") is not None:
                    element["ElementType"] = source["element_type"]
                if "FlowID" in (source.get("keys") or []):
                    self._flow_id(node, element)
                self._children(node, element, "children")
                return element
            element = {"ElementType": "Group"}
            self._flow_id(node, element)
            element["Description"] = source["Description"] if "Description" in source else node.get("title", "")
            self._children(node, element, "children")
            return element
        if kind == "end":
            element = {"ElementType": "EndSurvey"}
            self._flow_id(node, element)
            element["Options"] = copy.deepcopy(source.get("Options", {}))
            return element
        raise CompileError(f"Unknown flow step type {kind!r}")

    def block(self, block_id: str) -> dict[str, Any] | None:
        block = self.blocks.get(block_id)
        if block is None:
            raise CompileError(f"The flow shows block {block_id!r}, which the survey does not define")
        if block.get("kind") in {"conjoint", "maxdiff"}:
            from ..experiments.tasks import compile_block

            return compile_block(block)
        questions = []
        for qid in block.get("questions") or []:
            if str(qid) not in self.questions:
                raise CompileError(f"Block {block_id!r} lists question {qid!r}, which the survey does not define")
            questions.append(self.question(self.questions[str(qid)]))
        if not questions:
            return None
        source = block.get("source") if isinstance(block.get("source"), dict) else {}
        element: dict[str, Any] = {
            "ElementType": "Block",
            "BlockName": block.get("title", ""),
            "BlockType": source.get("type", "Standard"),
            "BlockID": block_id,
            "Questions": questions,
        }
        options = self.block_options(block, source)
        if options is not None:
            element["Options"] = options
        for key, value in (source.get("element") or {}).items():
            element.setdefault(key, copy.deepcopy(value))
        return element

    def block_options(self, block: dict[str, Any], source: dict[str, Any]) -> dict[str, Any] | None:
        randomization = block.get("randomization")
        repeat = block.get("repeat")
        plain_repeat = None
        if repeat:
            plain_repeat = {**repeat, "rows": [{"id": r["id"], "values": {k: join_text(v) for k, v in (r.get("values") or {}).items()}} for r in repeat.get("rows") or []]}
        if "options" in source:
            raw = source["options"]
            if block_randomization_of(raw) == randomization and repeat_of(raw) == plain_repeat:
                return copy.deepcopy(raw)
            options = copy.deepcopy(raw) if isinstance(raw, dict) else {}
        elif not randomization and not repeat:
            return None
        else:
            options = {}
        if block_randomization_of(options) != randomization:
            for key in ("RandomizeQuestions", "Randomization"):
                options.pop(key, None)
            if randomization and randomization.get("order") == "source":
                options.update(copy.deepcopy(randomization.get("qualtrics") or {}))
            elif randomization:
                options["RandomizeQuestions"] = "RandomWithXPerPage"
            else:
                options["RandomizeQuestions"] = "false"
        if repeat_of(options) != plain_repeat:
            for key in ("Looping", "LoopingOptions", "TotalRandSubset"):
                options.pop(key, None)
            if plain_repeat:
                options.update(repeat_setting(plain_repeat))
            else:
                options["Looping"] = "None"
        return options

    # ---------------------------------------------------------- questions

    def question(self, question: dict[str, Any]) -> dict[str, Any]:
        source = question.get("source") if isinstance(question.get("source"), dict) else {}
        response = question.get("response")
        kind = question.get("kind")
        qtype = self.question_type(question, source)
        text, lines = question_text(question, native=self.native)
        out: dict[str, Any] = {
            "QuestionID": str(question["id"]),
            "QuestionText": text,
            "QuestionType": qtype,
        }
        if lines or source.get("keep_lines"):
            out["KeepLines"] = True
        if source.get("exact_type"):
            out["ExactType"] = True
        if not source.get("no_export_name"):
            out["QuestionName"] = question.get("export_name", "")
        if source.get("raw_type"):
            out["RawQuestionType"] = source["raw_type"]
        images = [item for item in question.get("content") or [] if item.get("type") == "image"]
        if images:
            out["QuestionMedia"] = _media_out(images)
        elif "question_media" in source:
            out["QuestionMedia"] = copy.deepcopy(source["question_media"])
        logic = self.display_logic(question.get("show_if"), source)
        if logic is not None:
            out["DisplayLogic"] = logic

        flags = dict(source.get("flags") or {})
        if kind in {"timing", "meta_info", "captcha", "unsupported"} and not source.get("no_flag"):
            flag = {"timing": "is_timing", "meta_info": "is_meta", "captcha": "is_captcha", "unsupported": "is_unsupported"}[kind]
            out[flag] = flags.pop(flag, True)
            out["Settings"] = copy.deepcopy(source.get("settings", {} if kind != "unsupported" else {"Selector": "", "SubSelector": ""}))
        elif response is None and kind is None:
            if not source.get("no_flag"):
                out["is_descriptive"] = flags.pop("is_descriptive", True)
            if "settings" in source or not source.get("no_flag"):
                out["Settings"] = copy.deepcopy(source.get("settings", {"Selector": "TB", "SubSelector": "", "ValidationType": "None"}))
        elif response is not None:
            compiler = {
                "choice": self.choice,
                "text": self.text,
                "number": self.number,
                "form": self.form,
                "matrix": self.matrix,
                "allocation": self.allocation,
                "ranking": self.ranking,
                "group": self.group,
            }.get(response.get("type"))
            if compiler is None:
                raise CompileError(f"{question['id']}: a {response.get('type')!r} response is compiled by the experiment step")
            compiler(question, response, source, out)
        elif "settings" in source:
            out["Settings"] = copy.deepcopy(source["settings"])
        for flag, value in flags.items():
            out.setdefault(flag, value)

        self.common(question, response, source, out)
        skip = self.skip_logic(question, source)
        if skip is not None:
            out["SkipLogic"] = skip
        for key, value in (source.get("extra") or {}).items():
            out.setdefault(key, copy.deepcopy(value))
        return out

    def question_type(self, question: dict[str, Any], source: dict[str, Any]) -> str:
        response = question.get("response")
        kind = question.get("kind")
        if "type" in source and source["type"]:
            known = {
                "MC": "choice",
                "TE": ("text", "form"),
                "Matrix": "matrix",
                "Slider": "matrix",
                "CS": "allocation",
                "Rank": "ranking",
                "SBS": "group",
            }
            expected = known.get(source["type"])
            rtype = response.get("type") if response else None
            if expected is None or (rtype in expected if isinstance(expected, tuple) else rtype == expected):
                if not (source["type"] == "Slider" and rtype == "matrix" and (response.get("row_response") or {}).get("type") != "number"):
                    return str(source["type"])
        if kind == "timing":
            return "Timing"
        if kind == "meta_info":
            return "Meta"
        if kind == "captcha":
            return "Captcha"
        if response is None:
            return "DB" if kind is None else str(source.get("type") or "Unsupported")
        rtype = response.get("type")
        if rtype == "choice":
            return "MC"
        if rtype in {"text", "form"}:
            return "TE"
        if rtype == "number":
            return "Slider"
        if rtype == "matrix":
            return "Slider" if (response.get("row_response") or {}).get("type") == "number" else "Matrix"
        return {"allocation": "CS", "ranking": "Rank", "group": "SBS"}.get(rtype, "DB")

    def display_logic(self, show_if: Any, source: dict[str, Any]) -> Any:
        if "display_logic" in source:
            raw = source["display_logic"]
            decoded = from_qualtrics(raw)
            if decoded == show_if:
                return copy.deepcopy(raw)
            if show_if is None:
                return None
            return to_qualtrics(show_if, wrapper={"inPage": False})
        if show_if is None:
            return None
        return to_qualtrics(show_if, wrapper={"inPage": False})

    def skip_logic(self, question: dict[str, Any], source: dict[str, Any]) -> Any:
        from .read_template import TemplateReader

        rules = question.get("skip")
        if "skip_logic" in source:
            raw = source["skip_logic"]
            if isinstance(raw, list) and raw:
                reader = TemplateReader({})
                if [reader.skip_rule(rule) for rule in raw if isinstance(rule, dict)] == (rules or []):
                    return copy.deepcopy(raw)
            elif not rules:
                return copy.deepcopy(raw)
        if not rules:
            return None
        out = []
        for index, rule in enumerate(rules, start=1):
            when = rule["when"]
            if when.get("op") == "source":
                out.append(copy.deepcopy(when["qualtrics"]))
                continue
            if when.get("op") not in {"selected", "not_selected"}:
                raise CompileError(f"{question['id']}: a skip rule can test only whether an option was selected in this format version")
            locator = f"q://{when['question']}/SelectableChoice/{when['option']}"
            to = rule["to"]
            out.append(
                {
                    "SkipLogicID": rule.get("id") or f"SL_{question['id']}_{index}",
                    "ChoiceLocator": locator,
                    "Condition": "Selected" if when["op"] == "selected" else "NotSelected",
                    "SkipToDestination": {"end_of_block": "ENDOFBLOCK", "end_of_survey": "ENDOFSURVEY"}.get(to, to),
                    "Locator": "",
                    "QuestionID": when["question"],
                }
            )
            if rule.get("description"):
                out[-1]["Description"] = rule["description"]
        return out

    def common(self, question: dict[str, Any], response: Any, source: dict[str, Any], out: dict[str, Any]) -> None:
        """Randomization, recodes, defaults and per-item logic, as the QSF reader adds them."""
        wanted = response.get("randomization") if isinstance(response, dict) else None
        if "randomization" in source and randomization_of(source["randomization"]) == wanted:
            out.setdefault("Randomization", copy.deepcopy(source["randomization"]))
        elif wanted:
            out.setdefault("Randomization", randomization_setting(wanted))
        codes = self.codes(response)
        if "recode_values" in source:
            raw = source["recode_values"]
            if isinstance(raw, dict) and all(raw.get(item_id) == code for item_id, code in codes.items()) and not any(
                item_id in raw for item_id in self.uncoded_items(response)
            ):
                out["RecodeValues"] = copy.deepcopy(raw)
            elif codes:
                out["RecodeValues"] = codes
        elif codes:
            out["RecodeValues"] = codes
        if "recode_unsupported" in source:
            out["RecodeValuesUnsupported"] = source["recode_unsupported"]
        entries = self.default_entries(question, response)
        if "default_entries" in source:
            raw = source["default_entries"]
            if _entries_agree(raw, entries, response):
                out["DefaultEntries"] = copy.deepcopy(raw)
            elif entries:
                out["DefaultEntries"] = entries
        elif entries:
            out["DefaultEntries"] = entries
        for key, name, items in (
            ("ChoiceDisplayLogic", "choice_logic", self.choice_items(response)),
            ("AnswerDisplayLogic", "answer_logic", self.answer_items(response)),
        ):
            raw = source.get(name) if isinstance(source.get(name), dict) else {}
            logic = {}
            for item in items:
                show_if = item.get("show_if")
                original = raw.get(item["id"])
                if show_if is not None:
                    logic[item["id"]] = to_qualtrics(show_if, original=original, wrapper={"inPage": False})
            # logic the source kept for items the survey does not show (an option
            # left out of analysis) stays as read
            shown = {i["id"] for i in items}
            logic.update({item_id: copy.deepcopy(value) for item_id, value in raw.items() if item_id not in shown})
            if name in source and logic:
                ordered = {item_id: logic[item_id] for item_id in raw if item_id in logic}
                ordered.update({item_id: value for item_id, value in logic.items() if item_id not in ordered})
                logic = ordered
            if logic:
                out[key] = logic

    # ---- items shared by several types

    @staticmethod
    def choice_items(response: Any) -> list[dict[str, Any]]:
        if not isinstance(response, dict):
            return []
        rtype = response.get("type")
        if rtype == "choice":
            return response.get("options") or []
        if rtype == "form":
            return response.get("fields") or []
        if rtype == "matrix":
            return response.get("rows") or []
        if rtype in {"allocation", "ranking"}:
            return response.get("items") or []
        return []

    @staticmethod
    def answer_items(response: Any) -> list[dict[str, Any]]:
        if isinstance(response, dict) and response.get("type") == "matrix":
            if response.get("mode") == "per_cell":
                return response.get("columns") or []
            row_response = response.get("row_response") or {}
            if row_response.get("type") == "choice":
                return row_response.get("options") or []
        return []

    def coded_items(self, response: Any) -> list[dict[str, Any]]:
        if not isinstance(response, dict):
            return []
        rtype = response.get("type")
        if rtype == "choice":
            return response.get("options") or []
        if rtype in {"allocation", "ranking"}:
            return response.get("items") or []
        if rtype == "matrix":
            return self.answer_items(response)
        return []

    def codes(self, response: Any) -> dict[str, Any]:
        return {item["id"]: item["code"] for item in self.coded_items(response) if "code" in item}

    def uncoded_items(self, response: Any) -> list[str]:
        return [item["id"] for item in self.coded_items(response) if "code" not in item]

    def default_entries(self, question: dict[str, Any], response: Any) -> list[dict[str, Any]]:
        """The normalized defaults (``DefaultEntries``) for what the response presets."""
        if not isinstance(response, dict):
            return []
        rtype = response.get("type")
        out: list[dict[str, Any]] = []
        if rtype == "choice":
            out = [{"Kind": "selected", "ItemID": item} for item in response.get("default") or []]
        elif rtype == "text" and "default" in response and join_text(response["default"]) != "":
            out = [{"Kind": "text", "Value": join_text(response["default"])}]
        elif rtype == "form":
            out = [
                {"Kind": "text", "ItemID": field["id"], "Value": join_text(field["default"])}
                for field in response.get("fields") or []
                if "default" in field and join_text(field["default"]) != ""
            ]
        elif rtype == "matrix":
            for row in response.get("rows") or []:
                default = row.get("default")
                if isinstance(default, list):
                    out += [{"Kind": "selected", "ItemID": row["id"], "SubID": column} for column in default]
                elif default is not None:
                    out.append({"Kind": "value", "ItemID": row["id"], "Value": str(default)})
        elif rtype == "allocation":
            out = [{"Kind": "value", "ItemID": item, "Value": str(value)} for item, value in (response.get("default") or {}).items()]
        elif rtype == "ranking":
            out = [{"Kind": "value", "ItemID": entry["item"], "Value": str(entry["rank"])} for entry in response.get("default") or []]
        return out

    # ---- settings shared by every answered type

    def _required_group(self, settings: _Settings, response: dict[str, Any]) -> None:
        kept = settings.group(
            "required",
            ("ForceResponse",),
            lambda s: required_of(s.get("ForceResponse")),
            response.get("required", False),
            lambda value: {"ForceResponse": force_response_setting(value)},
        )
        if not kept and "ForceResponseType" in settings.values:
            settings.values["ForceResponseType"] = settings.values["ForceResponse"]

    @staticmethod
    def _limits_group(settings: _Settings, low: Any, high: Any, keys=("MinChoices", "MaxChoices")) -> None:
        settings.group(
            "limits",
            keys,
            lambda s: (positive_int(s.get(keys[0])), positive_int(s.get(keys[1]))),
            (low, high),
            lambda value: {keys[0]: str(value[0]) if value[0] else None, keys[1]: str(value[1]) if value[1] else None},
        )

    @staticmethod
    def _text_group(settings: _Settings, checks: dict[str, Any]) -> None:
        wanted = {key: checks[key] for key in ("min_length", "max_length", "format", "number") if key in checks}
        settings.group("text", TEXT_CHECK_KEYS, text_checks_of, wanted, text_checks_settings)

    # ---- per type

    def choice(self, question, response, source, out):
        options = response.get("options") or []
        labels = item_labels(options, "Image option", lambda position, item: str(position))
        out["Options"] = labels
        ids = [o["id"] for o in options]
        codes = self.codes(response)
        has_codes = bool(codes) or bool(source.get("recode_values"))
        self._ids(out, "OptionsID", ids, has_codes, source)
        media = [_media_out(o.get("media")) for o in options]
        if any(media):
            out["OptionMedia"] = media
        write_ins = [{"OptionID": o["id"], "OptionText": label} for o, label in zip(options, labels) if o.get("write_in")]
        if write_ins:
            out["TextEntryOptions"] = write_ins
        defaults = set(response.get("default") or [])
        chosen = [{"OptionID": o["id"], "OptionText": label} for o, label in zip(options, labels) if o["id"] in defaults]
        if chosen:
            out["DefaultChoices"] = chosen
        settings = _Settings(source.get("settings"), {"SubSelector": "TX", "ValidationType": "None"})
        self._required_group(settings, response)
        presentation = question.get("presentation") or {}
        settings.group(
            "selector",
            ("Selector",),
            lambda s: mc_selector_of(s.get("Selector")),
            (response["mode"], presentation.get("control")),
            lambda value: {"Selector": mc_selector_setting(value[0], value[1])},
        )
        self._limits_group(settings, response.get("min_selected"), response.get("max_selected"))
        out["Settings"] = settings.values

    def _ids(self, out: dict[str, Any], key: str, ids: list[str], has_codes: bool, source: dict[str, Any], *, always: bool = False) -> None:
        keep = always or not _default_ids(ids) or has_codes
        override = (source.get("id_lists") or {}).get(key)
        if override is not None:
            keep = override
        if keep:
            out[key] = ids

    def number(self, question, response, source, out):
        """A lone number: a slider with one unlabeled statement, which the engine asks
        as one numeric answer under the question's own ID (as the builder does)."""
        as_grid = {
            "type": "matrix",
            "mode": "per_row",
            "rows": [{"id": "1", "label": ""}],
            "row_response": {k: v for k, v in response.items() if k not in {"type", "required"}} | {"type": "number"},
            "required": response.get("required", False),
        }
        self.slider(question, as_grid, {**source, "id_lists": {"StatementsID": True}}, out)

    def text(self, question, response, source, out):
        presentation = question.get("presentation") or {}
        settings = _Settings(source.get("settings"), {})
        settings.group(
            "selector",
            ("Selector",),
            lambda s: TEXT_SELECTORS.get(str(s.get("Selector", "") or "")),
            presentation.get("control") or "multi_line",
            lambda value: {"Selector": TEXT_CONTROLS.get(value, "ML")},
        )
        self._required_group(settings, response)
        self._text_group(settings, response)
        out["Settings"] = settings.values
        if "default" in response:
            out["DefaultChoices"] = [join_text(response["default"])]
        if self.native:
            # a written survey says what it asks: no guessing a number from the wording
            out["ExactType"] = True

    def form(self, question, response, source, out):
        fields = response.get("fields") or []
        if fields or not source.get("no_rows"):
            out["Rows"] = item_labels(fields, "Image field")
            ids = [f["id"] for f in fields]
            self._ids(out, "RowsID", ids, False, source)
            media = [_media_out(f.get("media")) for f in fields]
            if any(media):
                out["RowsMedia"] = media
        settings = _Settings(source.get("settings"), {"Selector": "FORM"})
        if settings.values.get("Selector") != "FORM":
            settings.values["Selector"] = "FORM"
        self._required_group(settings, response)
        self._text_group(settings, response.get("field_response") or {})
        out["Settings"] = settings.values
        if self.native:
            out["ExactType"] = True
        if any("default" in f for f in fields):
            out["DefaultChoices"] = [join_text(f["default"]) if "default" in f else None for f in fields]

    def matrix(self, question, response, source, out):
        row_response = response.get("row_response") or {}
        if response.get("mode") == "per_row" and row_response.get("type") == "number":
            self.slider(question, response, source, out)
            return
        presentation = question.get("presentation") or {}
        rows = response.get("rows") or []
        if response.get("mode") == "per_cell":
            columns = response.get("columns") or []
            cell = response.get("cell_response") or {}
            kind = "text" if cell.get("type") == "text" else "sum"
            row_mode = None
        else:
            columns = row_response.get("options") or []
            kind = "choice"
            row_mode = row_response.get("mode", "single")
        column_labels = item_labels(columns, "Image option", lambda position, item: str(item["id"]))
        row_labels = item_labels(rows, "Image row")
        if presentation.get("control") == "bipolar" and column_labels:
            first, last = column_labels[0].strip(), column_labels[-1].strip()
            for index, row in enumerate(rows):
                if "left_label" in row and "right_label" in row:
                    left, right = join_text(row["left_label"]), join_text(row["right_label"])
                    row_labels[index] = f"{left} to {right} (scale: {first} = more {left}; {last} = more {right})" if first and last else f"{left} to {right}"
        out["Rows"] = row_labels
        out["Columns"] = column_labels
        has_codes = bool(self.codes(response)) or bool(source.get("recode_values"))
        self._ids(out, "RowsID", [r["id"] for r in rows], False, source)
        self._ids(out, "ColumnsID", [c["id"] for c in columns], has_codes, source)
        row_media = [_media_out(r.get("media")) for r in rows]
        if any(row_media):
            out["RowsMedia"] = row_media
        column_media = [_media_out(c.get("media")) for c in columns]
        if any(column_media):
            out["ColumnsMedia"] = column_media

        def decode_kind(s: dict[str, Any]) -> tuple[Any, ...]:
            selector = str(s.get("Selector", "") or "")
            sub_selector = str(s.get("SubSelector", "") or "").upper()
            if selector == "TE":
                return ("text", None, None)
            if selector == "CS":
                return ("sum", None, None)
            return ("choice", "multiple" if sub_selector == "MULTIPLEANSWER" else "single", "bipolar" if selector == "Bipolar" else None)

        def encode_kind(value: tuple[Any, ...]) -> dict[str, Any]:
            if value[0] == "text":
                return {"Selector": "TE", "SubSelector": "Long"}
            if value[0] == "sum":
                return {"Selector": "CS", "SubSelector": "WVTB"}
            return {"Selector": "Bipolar" if value[2] == "bipolar" else "Likert", "SubSelector": "MultipleAnswer" if value[1] == "multiple" else "SingleAnswer"}

        settings = _Settings(source.get("settings"), {"ValidationType": "None"})
        settings.group(
            "kind",
            ("Selector", "SubSelector"),
            decode_kind,
            (kind, row_mode, "bipolar" if presentation.get("control") == "bipolar" and kind == "choice" else None),
            encode_kind,
        )
        self._required_group(settings, response)
        if kind == "choice":
            self._limits_group(settings, row_response.get("min_selected"), row_response.get("max_selected"))
        elif kind == "text":
            self._text_group(settings, response.get("cell_response") or {})
        else:
            constraints = [c for c in response.get("constraints") or [] if c.get("type") == "sum_equals"]
            total = constraints[0]["total"] if constraints else None
            settings.group(
                "total",
                ("ChoiceTotal",),
                lambda s: number_of(s.get("ChoiceTotal")),
                total,
                lambda value: {"ChoiceTotal": str(value) if value is not None else None},
            )
            cell = response.get("cell_response") or {}
            wanted = (cell.get("min"), cell.get("max"))
            raw = source.get("range")
            if isinstance(raw, dict) and (number_of(raw.get("Min")) if "Min" in raw else None, number_of(raw.get("Max")) if "Max" in raw else None) == wanted:
                out["Range"] = copy.deepcopy(raw)
            elif "range" in source or any(v is not None for v in wanted):
                out["Range"] = {"Min": wanted[0] if wanted[0] is not None else 0, "Max": wanted[1] if wanted[1] is not None else 100}
        out["Settings"] = settings.values

    def slider(self, question, response, source, out):
        presentation = question.get("presentation") or {}
        rows = response.get("rows") or []
        number = response.get("row_response") or {}
        if rows or not source.get("no_rows"):
            out["Statements"] = item_labels(rows, "Image statement")
            self._ids(out, "StatementsID", [r["id"] for r in rows], False, source)
            media = [_media_out(r.get("media")) for r in rows]
            if any(media):
                out["StatementsMedia"] = media
        wanted_range = (number.get("min"), number.get("max"))
        raw_range = source.get("range")
        if isinstance(raw_range, dict) and (
            number_of(raw_range.get("Min")) if "Min" in raw_range else None,
            number_of(raw_range.get("Max")) if "Max" in raw_range else None,
        ) == wanted_range:
            out["Range"] = copy.deepcopy(raw_range)
            range_kept = True
        else:
            ticks = raw_range.get("Ticks", 10) if isinstance(raw_range, dict) else 10
            out["Range"] = {"Min": wanted_range[0] if wanted_range[0] is not None else 0, "Max": wanted_range[1] if wanted_range[1] is not None else 100, "Ticks": ticks}
            range_kept = False
        settings = _Settings(source.get("settings"), {"SubSelector": "", "ValidationType": "None"})
        self._required_group(settings, response)
        settings.group(
            "selector",
            ("Selector",),
            lambda s: SLIDER_SELECTORS.get(str(s.get("Selector", "") or "").strip().upper()),
            presentation.get("control") or "slider",
            lambda value: {"Selector": SLIDER_SELECTOR_NAMES.get(value, "HSLIDER")},
        )
        step_kept = settings.group(
            "step",
            ("NumericStep",),
            lambda s: number_of(s.get("NumericStep")) if s.get("NumericStep") is not None else None,
            number.get("step"),
            lambda value: {"NumericStep": value},
        )
        decimals_kept = settings.group(
            "decimals",
            ("NumericDecimals",),
            lambda s: _decimals_of(s.get("NumericDecimals")),
            number.get("decimals"),
            lambda value: {"NumericDecimals": value},
        )
        not_applicable = number.get("not_applicable")
        settings.group(
            "not_applicable",
            ("NotApplicable", "NotApplicableLabel"),
            lambda s: (str(s.get("NotApplicableLabel", "") or "").strip() or "Not Applicable") if _truthy(s.get("NotApplicable")) else None,
            join_text(not_applicable["label"]) if not_applicable else None,
            lambda value: {"NotApplicable": True, "NotApplicableLabel": value} if value else {},
        )
        if (presentation.get("control") == "stars") and not (step_kept and decimals_kept and range_kept and settings.raw_given):
            for key in ("StarCount", "StarType"):
                settings.values.pop(key, None)
            if wanted_range[1] is not None:
                settings.values["StarCount"] = int(wanted_range[1])
            step = number.get("step")
            settings.values["StarType"] = "half" if step == 0.5 else ("discrete" if step == 1 else "continuous")
        out["Settings"] = settings.values
        defaults = [{"StatementID": r["id"], "Value": str(r["default"])} for r in rows if r.get("default") is not None and not isinstance(r.get("default"), list)]
        raw = source.get("default_choices")
        if isinstance(raw, list) and _slider_defaults_agree(raw, rows):
            out["DefaultChoices"] = copy.deepcopy(raw)
        elif defaults:
            out["DefaultChoices"] = defaults

    def allocation(self, question, response, source, out):
        items = response.get("items") or []
        if items or not source.get("no_rows"):
            out["Rows"] = item_labels(items, "Image option")
            has_codes = bool(self.codes(response)) or bool(source.get("recode_values"))
            self._ids(out, "RowsID", [i["id"] for i in items], has_codes, source)
            media = [_media_out(i.get("media")) for i in items]
            if any(media):
                out["RowsMedia"] = media
        settings = _Settings(source.get("settings"), {"Selector": "VRTL", "Type": "ChoicesTotal"})
        self._required_group(settings, response)
        settings.group(
            "total",
            ("ChoiceTotal",),
            lambda s: number_of(s.get("ChoiceTotal")) if "ChoiceTotal" in s else None,
            response.get("total"),
            lambda value: {"ChoiceTotal": str(value) if value is not None else None},
        )
        wanted = (response.get("min"), response.get("max"))
        raw = source.get("range")
        if isinstance(raw, dict) and (number_of(raw.get("Min")) if "Min" in raw else None, number_of(raw.get("Max")) if "Max" in raw else None) == wanted:
            out["Range"] = copy.deepcopy(raw)
        elif any(v is not None for v in wanted):
            out["Range"] = {"Min": wanted[0] if wanted[0] is not None else 0, "Max": wanted[1] if wanted[1] is not None else 100}
            settings.values["EnforceRange"] = "ON"
        out["Settings"] = settings.values

    def ranking(self, question, response, source, out):
        items = response.get("items") or []
        labels = item_labels(items, "Image option")
        out["Options"] = labels
        out["OptionsID"] = [i["id"] for i in items]
        media = [_media_out(i.get("media")) for i in items]
        if any(media):
            out["OptionMedia"] = media
        settings = _Settings(source.get("settings"), {"Selector": "DND", "SubSelector": "TX"})
        self._required_group(settings, response)
        self._limits_group(settings, response.get("min_ranked"), response.get("max_ranked"))
        out["Settings"] = settings.values
        by_id = dict(zip([i["id"] for i in items], labels))
        wanted = [{"item": d["item"], "rank": d["rank"]} for d in response.get("default") or []]
        raw = source.get("default_ranks")
        if isinstance(raw, list) and [
            {"item": str(e.get("OptionID")), "rank": positive_int(e.get("Rank"))} for e in raw if isinstance(e, dict) and positive_int(e.get("Rank"))
        ] == wanted:
            out["DefaultRanks"] = copy.deepcopy(raw)
        elif wanted:
            out["DefaultRanks"] = [
                {"OptionID": d["item"], "OptionText": by_id.get(d["item"], d["item"]), "Rank": str(d["rank"])}
                for d in sorted(wanted, key=lambda d: (d["rank"], d["item"]))
            ]

    def group(self, question, response, source, out):
        fields = response.get("fields") or []
        sub_questions = [self.sbs_field(question, field) for field in fields]
        sbs = source.get("sbs") if isinstance(source.get("sbs"), dict) else None
        if sbs is not None:
            for key, value in sbs.items():
                out[key] = copy.deepcopy(value)
            first = next((s for s in sub_questions if s.get("question_type") != "TE" and s.get("Options")), None)
            if first is not None and sbs.get("Options") != first["Options"]:
                out["Options"] = list(first["Options"])
                out["OptionsID"] = list(first.get("OptionsID") or [str(i + 1) for i in range(len(first["Options"]))])
        else:
            first = next((s for s in sub_questions if s.get("Options")), None)
            out["Rows"] = []
            out["Options"] = list(first["Options"]) if first else []
            out["OptionsID"] = list(first.get("OptionsID") or [str(i + 1) for i in range(len(first["Options"]))]) if first else []
        out["sub_questions"] = sub_questions
        settings = _Settings(source.get("settings"), {"Selector": "SBSMatrix"})
        self._required_group(settings, response)
        out["Settings"] = settings.values

    def sbs_field(self, question: dict[str, Any], field: dict[str, Any]) -> dict[str, Any]:
        source = field.get("source") if isinstance(field.get("source"), dict) else {}
        matrix = field.get("response") or {}
        rows = matrix.get("rows") or []
        row_response = matrix.get("row_response") or {}
        is_text = row_response.get("type") == "text"
        options = [] if is_text else (row_response.get("options") or [])
        sub: dict[str, Any] = {
            "id": field["id"],
            "text": join_text(field.get("label", "")),
            "question_type": source.get("question_type", "Matrix"),
            "selector": source.get("selector", "TE" if is_text else "Likert"),
            "subselector": source.get("subselector", "" if is_text else ("MultipleAnswer" if row_response.get("mode") == "multiple" else "SingleAnswer")),
            "QuestionID": source.get("QuestionID", f"{question['id']}#{field['id']}"),
            "QuestionName": field.get("export_name", f"{question.get('export_name') or question['id']}#{field['id']}"),
        }
        option_labels = item_labels(options, "Image option")
        sub["Options"] = option_labels
        option_ids = [o["id"] for o in options]
        id_lists = source.get("id_lists") or {}
        if id_lists.get("OptionsID", not _default_ids(option_ids)):
            sub["OptionsID"] = option_ids
        option_media = [_media_out(o.get("media")) for o in options]
        if any(option_media):
            sub["OptionsMedia"] = option_media
        sub["Rows"] = item_labels(rows, "Image row")
        row_ids = [r["id"] for r in rows]
        if id_lists.get("RowsID", not _default_ids(row_ids)):
            sub["RowsID"] = row_ids
        row_media = [_media_out(r.get("media")) for r in rows]
        if any(row_media):
            sub["RowsMedia"] = row_media
        media = _media_out(field.get("media"))
        if media:
            sub["QuestionMedia"] = media
        raw_settings = source.get("Settings")
        if raw_settings is not None or matrix.get("required") or (is_text and len(row_response) > 1):
            settings = _Settings(raw_settings, {})
            if raw_settings is not None or matrix.get("required"):
                settings.group(
                    "required",
                    ("ForceResponse",),
                    lambda s: required_of(s.get("ForceResponse")),
                    matrix.get("required", False),
                    lambda value: {"ForceResponse": force_response_setting(value)} if value else {},
                )
            if is_text:
                self._text_group(settings, row_response)
            if settings.values or raw_settings is not None:
                sub["Settings"] = settings.values
        codes = {o["id"]: o["code"] for o in options if "code" in o}
        raw_codes = source.get("RecodeValues")
        if isinstance(raw_codes, dict) and all(raw_codes.get(k) == v for k, v in codes.items()):
            sub["RecodeValues"] = copy.deepcopy(raw_codes)
        elif codes:
            sub["RecodeValues"] = codes
        by_id = dict(zip(option_ids or [str(i + 1) for i in range(len(option_labels))], option_labels))
        defaults = [
            {"RowID": r["id"], "OptionID": option_id, "OptionText": by_id.get(option_id, option_id)}
            for r in rows
            for option_id in (r.get("default") if isinstance(r.get("default"), list) else [])
        ]
        raw_defaults = source.get("DefaultChoices")
        if isinstance(raw_defaults, list) and sorted((str(d.get("RowID")), str(d.get("OptionID"))) for d in raw_defaults if isinstance(d, dict)) == sorted(
            (d["RowID"], d["OptionID"]) for d in defaults
        ):
            if raw_defaults:
                sub["DefaultChoices"] = copy.deepcopy(raw_defaults)
        elif defaults:
            sub["DefaultChoices"] = defaults
        for key, value in (source.get("extra") or {}).items():
            sub.setdefault(key, copy.deepcopy(value))
        return sub


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _decimals_of(value: Any) -> int | None:
    if value is None:
        return None
    number = number_of(value)
    return number if isinstance(number, int) and number >= 0 else None


def _slider_defaults_agree(raw: list[Any], rows: list[dict[str, Any]]) -> bool:
    wanted = {r["id"]: r["default"] for r in rows if r.get("default") is not None}
    found = {}
    for entry in raw:
        if isinstance(entry, dict):
            value = number_of(entry.get("Value"))
            found[str(entry.get("StatementID"))] = value if value is not None else str(entry.get("Value"))
    return found == wanted


def _entries_agree(raw: Any, entries: list[dict[str, Any]], response: Any) -> bool:
    """Whether normalized defaults read from the source still say what the response presets."""
    if not isinstance(raw, list):
        return not entries
    rtype = response.get("type") if isinstance(response, dict) else None

    def key(entry: dict[str, Any]) -> tuple[Any, ...]:
        value = entry.get("Value")
        if rtype in {"matrix", "allocation", "ranking"} and value is not None:
            number = number_of(value)
            value = number if number is not None else value
        return (entry.get("Kind"), entry.get("ItemID"), entry.get("SubID"), value)

    known: set[str] | None = None
    if rtype == "choice":
        known = {o["id"] for o in response.get("options") or []}
    raw_keys = sorted((key(e) for e in raw if isinstance(e, dict) and (known is None or e.get("ItemID") in known)), key=repr)
    return raw_keys == sorted((key(e) for e in entries), key=repr) or not entries and all(
        known is not None and e.get("ItemID") not in known for e in raw if isinstance(e, dict)
    )


def compile_survey(survey: dict[str, Any]) -> dict[str, Any]:
    """The template the engine runs for a survey in the standard format."""
    return Compiler(survey).compile()
