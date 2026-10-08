"""The README's printed output is what the code prints (lines of "..." mark cuts)."""

from __future__ import annotations

import contextlib
import io
from pathlib import Path

import surveytwin as st

ROOT = Path(__file__).resolve().parent.parent


def _output_blocks() -> list[list[str]]:
    blocks, current, language = [], None, ""
    for line in (ROOT / "README.md").read_text(encoding="utf-8").splitlines():
        if line.startswith("```"):
            if current is None:
                current, language = [], line[3:].strip()
            else:
                if language == "":
                    blocks.append(current)
                current = None
            continue
        if current is not None:
            current.append(line)
    return blocks


def _readme_steps(monkeypatch) -> set[str]:
    monkeypatch.chdir(ROOT)
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        survey = st.read_qsf("examples/example_survey.qsf")
        print(survey)
        print(survey.check())
        print(survey.preview_blocks())
        trimmed = survey.drop_blocks(["BL_KS_MC_1"])
        print(trimmed.drop_report)
        bank = st.bank("examples/example-bank")
        print(bank)
        print(bank.representations())
        print(bank.filter_options())
        print(bank.preview_filter({"region": ["South", "West"], "age": {"max": 50}}, sample_size=3))
        config = st.configure(
            bank="examples/example-bank",
            filters={"region": ["South", "West"], "age": {"max": 50}},
            sample_size=3,
            seed=7,
            model="gpt-5.6-luna-medium",
        )
        print(config)
        plan = st.plan(trimmed, config)
        print(plan)
        for twin in plan.twins:
            print(twin)
        print(plan.preview_prompt("ex-03"))
        # the standard survey format section
        survey = st.read_survey("examples/survey-format/coffee-habits.json")
        print(survey, survey.kind)
        survey = st.read_survey("examples/survey-format/coffee-conjoint.json")
        print(survey, survey.questions["coffee_t1"].type)
    return set(out.getvalue().splitlines())


def test_readme_output_is_real(monkeypatch):
    real = _readme_steps(monkeypatch)
    diagram = ("read_survey(", "read_qsf(", "bank(", "configure(", "plan(", "run(")
    blocks = _output_blocks()
    assert len(blocks) >= 9
    invented = [
        line
        for block in blocks
        for line in block
        if line.strip() not in ("", "...") and not line.startswith(diagram) and line not in real
    ]
    assert invented == []
