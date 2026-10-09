"""The README and the question guide run as written: every Python block, in order,
in a scratch folder holding a copy of examples/, with a scripted model standing in
for the provider (no call leaves the machine). The facts the README states about
the examples are checked too."""

from __future__ import annotations

import asyncio
import base64
import json
import re
import shutil
from pathlib import Path

import surveytwin as st
from fakes import answer_questions

ROOT = Path(__file__).resolve().parent.parent
PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAQAAAADCAIAAAA7ljmRAAAAE0lEQVR4nGM8ISfHAANMcBY6BwAm2gEKUjNC5gAAAABJRU5ErkJggg==")


def python_blocks(page: str) -> list[str]:
    return re.findall(r"```python\n(.*?)```", (ROOT / page).read_text(encoding="utf-8"), re.S)


def stand_in(request: st.ModelRequest) -> st.Reply:
    answers = answer_questions(request.user)
    ranks: dict[str, int] = {}
    shown = request.user.split("Questions JSON:\n")[-1].split("\n\nRespond using JSON schema")[0]
    groups = {q["id"]: q.get("rank_order_group_id") for q in json.loads(shown)}
    for answer in answers:  # a ranking's items get 1, 2, 3, ...
        group = groups.get(answer["question_id"])
        if group:
            ranks[group] = ranks.get(group, 0) + 1
            answer["answer_value"] = answer["answer_label"] = answer["answer_raw"] = str(ranks[group])
    return st.Reply(text=json.dumps({"answers": answers}), input_tokens=10, output_tokens=10)


def test_the_readme_and_the_question_guide_run(tmp_path, monkeypatch):
    shutil.copytree(ROOT / "examples", tmp_path / "examples")
    (tmp_path / "images").mkdir()
    (tmp_path / "images" / "ad.png").write_bytes(PNG)  # the picture the guide's example names
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(st.Model, "openai", classmethod(lambda cls, name, **kwargs: st.Model.from_function(stand_in, name=name, images=True)))
    namespace: dict = {"asyncio": asyncio}
    for page in ("README.md", "questions-guide.md"):
        blocks = python_blocks(page)
        assert blocks, page
        for number, block in enumerate(blocks, start=1):
            # an example for async code runs in an event loop of its own here
            code = re.sub(r"await (st\.arun\(.*\))", r"asyncio.run(\1)", block)
            exec(compile(code, f"{page} block {number}", "exec"), namespace)
        if page == "README.md":
            # what the README says about the examples
            assert str(namespace["survey"]) == "Survey('Coffee habits', 14 questions, 7 blocks)"
            assert namespace["bank"].size == 8  # "eight invented personas"
            assert namespace["bank"].preview_filter(namespace["filters"]).matching == 3  # "Three example personas match"
            assert len(namespace["pilot_plan"].twins) == 1
            assert st.Results.load("runs/coffee-habits").answers == namespace["results"].answers
            assert namespace["built"].standard["blocks"][0]["questions"] == ["drink", "cups", "reason"]
    assert (tmp_path / "study.json").exists()  # the guide's last example writes the definition
    assert st.read_survey(tmp_path / "study.json").standard == namespace["survey"].standard
