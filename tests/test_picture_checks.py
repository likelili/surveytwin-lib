"""A picture is checked to its end when the survey is read, not only by its first
bytes: a damaged or cut-off file is refused before any model sees it, and so is a
``data:`` address that does not hold a picture. No picture library is needed."""

from __future__ import annotations

import base64
import json
import os
import sys
from pathlib import Path

import pytest

import surveytwin as st
from surveytwin.spec.pictures import picture_problem

# Tiny pictures of each kind, written by an image library (4 x 3 pixels).
SAMPLES = {
    "png": "iVBORw0KGgoAAAANSUhEUgAAAAQAAAADCAIAAAA7ljmRAAAAE0lEQVR4nGM8ISfHAANMcBY6BwAm2gEKUjNC5gAAAABJRU5ErkJggg==",
    "jpeg": (
        "/9j/4AAQSkZJRgABAQAAAQABAAD/2wBDAAYEBQYFBAYGBQYHBwYIChAKCgkJChQODwwQFxQYGBcUFhYaHSUfGhsjHBYWICwgIyYnKSopGR8tMC0oMCUoKSj/"
        "2wBDAQcHBwoIChMKChMoGhYaKCgoKCgoKCgoKCgoKCgoKCgoKCgoKCgoKCgoKCgoKCgoKCgoKCgoKCgoKCgoKCgoKCj/wAARCAADAAQDASIAAhEBAxEB/8QAHwAAAQUB"
        "AQEBAQEAAAAAAAAAAAECAwQFBgcICQoL/8QAtRAAAgEDAwIEAwUFBAQAAAF9AQIDAAQRBRIhMUEGE1FhByJxFDKBkaEII0KxwRVS0fAkM2JyggkKFhcYGRolJicoKSo0"
        "NTY3ODk6Q0RFRkdISUpTVFVWV1hZWmNkZWZnaGlqc3R1dnd4eXqDhIWGh4iJipKTlJWWl5iZmqKjpKWmp6ipqrKztLW2t7i5usLDxMXGx8jJytLT1NXW19jZ2uHi4+Tl"
        "5ufo6erx8vP09fb3+Pn6/8QAHwEAAwEBAQEBAQEBAQAAAAAAAAECAwQFBgcICQoL/8QAtREAAgECBAQDBAcFBAQAAQJ3AAECAxEEBSExBhJBUQdhcRMiMoEIFEKRobHB"
        "CSMzUvAVYnLRChYkNOEl8RcYGRomJygpKjU2Nzg5OkNERUZHSElKU1RVVldYWVpjZGVmZ2hpanN0dXZ3eHl6goOEhYaHiImKkpOUlZaXmJmaoqOkpaanqKmqsrO0tba3"
        "uLm6wsPExcbHyMnK0tPU1dbX2Nna4uPk5ebn6Onq8vP09fb3+Pn6/9oADAMBAAIRAxEAPwDgKKKK+eP2E//Z"
    ),
    "gif": "R0lGODdhBAADAIEAAMgeHgAAAAAAAAAAACwAAAAABAADAAAICAABCBxIUGBAADs=",
    "webp": "UklGRjoAAABXRUJQVlA4IC4AAACwAQCdASoEAAMAAUAmJaACdLoABDAAAP7x3I/4DdfFtMv/vYL/3YL/3YL/WwAA",
    "webp_lossless": "UklGRh4AAABXRUJQVlA4TBEAAAAvA4AAAAdQjyLXo/+BiOh/AAA=",
}
PICTURES = {name: base64.b64decode(data) for name, data in SAMPLES.items()}


def one_picture_survey(source: str) -> dict:
    return {
        "schema_version": "0.1",
        "id": "picture",
        "questions": [{
            "id": "seen",
            "content": [{"type": "image", "source": source}, {"type": "text", "text": "Describe the picture."}],
            "response": {"type": "text"},
        }],
        "blocks": [{"id": "main", "kind": "survey", "questions": ["seen"]}],
        "flow": [{"type": "show_block", "block": "main"}],
    }


@pytest.mark.parametrize("name", sorted(PICTURES))
def test_a_complete_picture_of_each_kind_is_read(name, tmp_path):
    assert picture_problem(PICTURES[name])[1] == ""
    (tmp_path / "p.bin").write_bytes(PICTURES[name])
    survey = st.read_survey(one_picture_survey("p.bin"), folder=tmp_path)
    assert list(survey._pictures) == ["p.bin"]


@pytest.mark.parametrize("name", sorted(PICTURES))
def test_a_cut_off_picture_is_refused(name, tmp_path):
    cut = PICTURES[name][: len(PICTURES[name]) // 2]
    (tmp_path / "p.bin").write_bytes(cut)
    with pytest.raises(st.SurveyFormatError, match=r"\$\.questions\[0\]\.content\[0\]\.source: .*p\.bin .*cut off"):
        st.read_survey(one_picture_survey("p.bin"), folder=tmp_path)


def test_a_file_with_only_a_pictures_first_bytes_is_refused(tmp_path):
    # the reviewer's case: a PNG signature and then something else
    (tmp_path / "broken.png").write_bytes(b"\x89PNG\r\n\x1a\nNOT_AN_IMAGE")
    with pytest.raises(st.SurveyFormatError, match="broken.png does not start with an image header"):
        st.read_survey(one_picture_survey("broken.png"), folder=tmp_path)


def test_damaged_data_in_a_png_is_refused():
    damaged = bytearray(PICTURES["png"])
    damaged[-20] ^= 0xFF  # inside the image data: its checksum no longer matches
    assert "checksum does not match" in picture_problem(bytes(damaged))[1]


def test_bytes_after_a_pictures_end_are_allowed():
    assert picture_problem(PICTURES["png"] + bytes(400))[1] == ""
    assert picture_problem(PICTURES["jpeg"] + b"\x00\x00")[1] == ""


def test_a_data_address_is_checked_too():
    good = "data:image/png;base64," + SAMPLES["png"]
    assert st.read_survey(one_picture_survey(good)).standard["questions"][0]["content"][0]["source"] == good
    for source, why in (
        ("data:image/png;base64," + base64.b64encode(b"\x89PNG\r\n\x1a\nNOT_AN_IMAGE").decode(), "does not start with an image header"),
        ("data:image/png;base64,QUJD", "not a PNG, JPEG, WEBP or GIF picture"),
        ("data:image/png;base64,***", "not a PNG, JPEG, WEBP or GIF picture"),
        ("data:image/svg+xml,<svg/>", "must hold base64 data"),
        ("data:image/jpeg;base64," + SAMPLES["png"], "says image/jpeg, but the picture is image/png"),
    ):
        with pytest.raises(st.SurveyFormatError, match=why) as caught:
            st.read_survey(one_picture_survey(source))
        assert caught.value.findings[0].code == "picture.data"


@pytest.mark.skipif(sys.platform == "win32" or os.geteuid() == 0, reason="needs a file the user cannot read")
def test_a_file_that_cannot_be_read_is_a_finding_not_a_crash(tmp_path):
    locked = tmp_path / "locked.png"
    locked.write_bytes(PICTURES["png"])
    locked.chmod(0)
    try:
        with pytest.raises(st.SurveyFormatError, match="locked.png cannot be read"):
            st.read_survey(one_picture_survey("locked.png"), folder=tmp_path)
    finally:
        locked.chmod(0o600)


def test_an_added_question_with_a_damaged_picture_is_refused(tmp_path):
    (tmp_path / "cut.gif").write_bytes(PICTURES["gif"][:20])
    survey = st.new_survey("Pictures", folder=tmp_path)
    with pytest.raises(st.SurveyFormatError, match="cut.gif .*cut off"):
        survey.add_question("instruction", image="cut.gif")
    assert json.dumps(survey.standard)  # the survey it was called on is unchanged
