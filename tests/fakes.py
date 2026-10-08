"""A scripted model for tests: answers every question deterministically, no network."""

from __future__ import annotations

import json
import re

import surveytwin as st

_SCHEMA_MARK = "\n\nRespond using JSON schema"


def answer_questions(user_text: str) -> list[dict[str, str]]:
    start = user_text.rindex("Questions JSON:\n") + len("Questions JSON:\n")
    end = user_text.index(_SCHEMA_MARK, start)
    questions = json.loads(user_text[start:end])
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
