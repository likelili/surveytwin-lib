"""A survey runs on its template, not its QSF. Removing questions from the template
must give exactly what re-parsing the QSF without them gives (the website's way)."""

from __future__ import annotations

import json

import pytest

import surveytwin as st
from conftest import QSF_DIR
from surveytwin.runtime.instrument_plan import (
    build_instrument,
    build_instrument_from_template,
    excluded_question_ids,
    parse_template,
    question_ids_in_blocks,
    remove_template_questions,
)

FILES = sorted(QSF_DIR.glob("*.qsf"))


@pytest.mark.parametrize("path", FILES, ids=lambda p: p.name)
def test_removing_questions_equals_reparsing(path):
    qsf = json.loads(path.read_text(encoding="utf-8"))
    full = parse_template(qsf)
    survey = st.read_qsf(path)
    for block in survey.blocks:
        ids = excluded_question_ids(qsf, [block["block_id"]])
        assert question_ids_in_blocks(full, [block["block_id"]]) == ids
        assert remove_template_questions(full, ids) == parse_template(qsf, ids)
    some = [q for i, q in enumerate(survey.questions) if i % 3 == 0]
    assert remove_template_questions(full, some) == parse_template(qsf, some)


@pytest.mark.parametrize("path", FILES, ids=lambda p: p.name)
def test_qsf_and_template_entry_points_agree(path):
    qsf = json.loads(path.read_text(encoding="utf-8"))
    survey = st.read_qsf(path)
    for dropped in [[]] + [[b["block_id"]] for b in survey.blocks[:3]]:
        try:
            a = build_instrument(qsf, 3, excluded_block_ids=dropped, seed=5)
        except ValueError:
            continue
        b = build_instrument_from_template(survey._full_template, 3, excluded_block_ids=dropped, seed=5, qsf=qsf)
        assert json.dumps(a, sort_keys=True, default=str) == json.dumps(b, sort_keys=True, default=str)


def test_read_time_exclusions_match_the_parser():
    path = QSF_DIR / "TEST_MC.qsf"
    qsf = json.loads(path.read_text(encoding="utf-8"))
    survey = st.read_qsf(path, exclude_questions=["QID4"])
    assert survey.template == parse_template(qsf, ["QID4"])
    assert "QID4" not in survey.questions


def test_checksums():
    path = QSF_DIR / "TEST_MC.qsf"
    import hashlib

    survey = st.read_qsf(path)
    assert survey.checksum == hashlib.sha256(path.read_bytes()).hexdigest()
    assert survey.source == {"kind": "qsf", "name": "TEST_MC.qsf", "checksum_sha256": survey.checksum, "edits": []}
    from surveytwin import builder

    edited = builder.update_question(survey, "QID6", text="Changed")
    assert edited.checksum != survey.checksum and edited.source["edits"][0]["op"] == "update_question"
