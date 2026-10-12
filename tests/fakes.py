"""A scripted model for tests: answers every question deterministically, no network."""

from __future__ import annotations

import json
import re

import surveytwin as st


def asked_questions(user_text: str) -> list[dict]:
    """The questions a prompt asks (``surveytwin.prompts.asked_questions``). Under
    "Answer as", a question's own entry (not a row's, an item's or a write-in
    box's) also gets ``printed_as``: the label printed above it."""
    printed: dict[str, str] = {}
    label = None
    block: list[str] | None = None
    for line in user_text.splitlines():
        if re.fullmatch(r"Q\d+:", line.strip()):
            label, block = line.strip()[:-1], None
        elif line == "Answer as:":
            block = []
        elif block is not None and line.startswith('{"id": '):
            block.append(json.loads(line)["id"])
            for own in block:  # the entry whose ID the others extend
                if label and all(o == own or o.startswith((f"{own}_", f"{own}#")) for o in block):
                    printed[own] = label
                else:
                    printed.pop(own, None)
        else:
            block = None
    return [{**q, **({"printed_as": printed[q["id"]]} if q["id"] in printed else {})} for q in st.prompts.asked_questions(user_text)]


def answer_questions(user_text: str) -> list[dict[str, str]]:
    questions = asked_questions(user_text)
    answers = []
    for q in questions:
        options = q.get("options") or []
        if options:
            value = options[0]
        elif q.get("numeric_min") not in (None, ""):
            value = str(q.get("numeric_min"))
        elif q.get("type") == "numeric":
            value = "1"
        else:
            value = f"answer to {q['id']}"
        answers.append({"question_id": q["id"], "answer_value": value, "answer_label": value, "answer_raw": value})
    return answers


def scripted(request: st.ModelRequest) -> st.Reply:
    answers = answer_questions(request.user)
    return st.Reply(text=json.dumps({"answers": answers}), input_tokens=len(request.system + request.user) // 4, output_tokens=40)


def scripted_model(**kwargs) -> st.Model:
    return st.Model.from_function(scripted, name="scripted", images=True, **kwargs)


def accept_all_images(url: str) -> dict[str, str]:
    return {"status": "supported", "detail": ""}
