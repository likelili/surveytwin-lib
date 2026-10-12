"""Write small QSFs with a survey flow by hand (branches, randomizers, groups,
loops, embedded data), for tests of the staged runtime. Payloads follow the
shapes real exports use."""

from __future__ import annotations

import itertools
import json
from typing import Any, Callable

from fakes import asked_questions

_flow_ids = itertools.count(10)


def mc(qid: str, text: str, options: list[str], display_logic: dict | None = None) -> dict[str, Any]:
    payload = {
        "QuestionID": qid, "DataExportTag": qid, "QuestionText": text, "QuestionType": "MC", "Selector": "SAVR", "SubSelector": "TX",
        "Configuration": {"QuestionDescriptionOption": "UseText"}, "Validation": {"Settings": {"ForceResponse": "OFF", "Type": "None"}},
        "Choices": {str(i): {"Display": o} for i, o in enumerate(options, 1)}, "ChoiceOrder": list(range(1, len(options) + 1)),
    }
    if display_logic:
        payload["DisplayLogic"] = display_logic
    return payload


def te(qid: str, text: str, display_logic: dict | None = None) -> dict[str, Any]:
    payload = {
        "QuestionID": qid, "DataExportTag": qid, "QuestionText": text, "QuestionType": "TE", "Selector": "SL",
        "Configuration": {"QuestionDescriptionOption": "UseText"}, "Validation": {"Settings": {"ForceResponse": "OFF", "Type": "None"}},
    }
    if display_logic:
        payload["DisplayLogic"] = display_logic
    return payload


def when(qid: str, locator: str, operator: str, right: str | None = None) -> dict[str, Any]:
    """Logic with one condition on a question, e.g. when("QID1", "SelectableChoice/1", "Selected")."""
    condition = {"LogicType": "Question", "QuestionID": qid, "QuestionIsInLoop": "no", "ChoiceLocator": f"q://{qid}/{locator}",
                 "Operator": operator, "QuestionIDFromLocator": qid, "LeftOperand": f"q://{qid}/{locator}", "Type": "Expression"}
    if right is not None:
        condition["RightOperand"] = right
    return {"0": {"0": condition, "Type": "If"}, "Type": "BooleanExpression"}


def when_field(field: str, value: str) -> dict[str, Any]:
    return {"0": {"0": {"LogicType": "EmbeddedField", "LeftOperand": field, "Operator": "EqualTo", "RightOperand": value, "Type": "Expression"},
                  "Type": "If"}, "Type": "BooleanExpression"}


def _fid() -> str:
    return f"FL_{next(_flow_ids)}"


def block(block_id: str) -> dict[str, Any]:
    return {"Type": "Standard", "ID": block_id, "FlowID": _fid()}


def branch(logic: dict[str, Any], *children: dict[str, Any], flow_id: str | None = None) -> dict[str, Any]:
    return {"Type": "Branch", "FlowID": flow_id or _fid(), "BranchLogic": logic, "Flow": list(children)}


def randomizer(pick: int, *children: dict[str, Any]) -> dict[str, Any]:
    return {"Type": "BlockRandomizer", "FlowID": _fid(), "SubSet": pick, "EvenPresentation": True, "Flow": list(children)}


def group(*children: dict[str, Any]) -> dict[str, Any]:
    return {"Type": "Group", "FlowID": _fid(), "Description": "Group", "Flow": list(children)}


def embedded(**fields: str) -> dict[str, Any]:
    return {"Type": "EmbeddedData", "FlowID": _fid(), "EmbeddedData": [{"Field": k, "Type": "Custom", "Value": v} for k, v in fields.items()]}


def end() -> dict[str, Any]:
    return {"Type": "EndSurvey", "FlowID": _fid()}


LOOP_COFFEE_TEA = {"Looping": "Static", "LoopingOptions": {"Static": {"1": {"1": "Coffee"}, "2": {"1": "Tea"}}, "Randomization": "None"}}


def qsf(blocks: dict[str, tuple], questions: dict[str, dict[str, Any]], flow: list[dict[str, Any]]) -> dict[str, Any]:
    """``blocks``: block ID -> (name, [question IDs]) or (name, [question IDs], block options)."""
    payload = {
        str(i): {"Type": "Standard", "Description": spec[0], "ID": block_id,
                 "BlockElements": [{"Type": "Question", "QuestionID": q} for q in spec[1]],
                 **({"Options": spec[2]} if len(spec) > 2 else {})}
        for i, (block_id, spec) in enumerate(blocks.items(), 1)
    }
    elements = [
        {"Element": "BL", "PrimaryAttribute": "Survey Blocks", "Payload": payload},
        {"Element": "FL", "PrimaryAttribute": "Survey Flow", "Payload": {"Flow": flow, "FlowID": "FL_1", "Type": "Root"}},
    ]
    elements += [{"Element": "SQ", "PrimaryAttribute": qid, "Payload": p} for qid, p in questions.items()]
    return {"SurveyEntry": {"SurveyID": "SV_FLOW", "SurveyName": "flow"}, "SurveyElements": elements}


def answering(rule: Callable[[dict[str, Any]], str | None]):
    """A model that answers each question with ``rule(question)``, or the first
    option (or a short text) when the rule gives None."""
    import surveytwin as st

    def reply(request):
        text = request.user
        questions = asked_questions(text)
        answers = []
        for q in questions:
            value = rule(q)
            if value is None:
                value = (q.get("options") or [None])[0] or f"answer to {q['id']}"
            answers.append({"question_id": q["id"], "answer_value": value, "answer_label": value, "answer_raw": value})
        return st.Reply(text=json.dumps({"answers": answers}), input_tokens=1, output_tokens=1)

    return st.Model.from_function(reply, name="rule")
