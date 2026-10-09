"""A survey written in the standard format can name its pictures by file path.

An absolute path is read as written; a relative path from the folder of the JSON
file (or ``read_survey(..., folder=...)``). The file is read when the survey is
read, the survey keeps the path as written, and a run sends the picture itself.
"""

from __future__ import annotations

import base64
import json
from pathlib import Path

import pytest

import surveytwin as st
from conftest import TESTS
from fakes import answer_questions

BANK = str(TESTS.parent / "examples" / "example-bank")
PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==")
GIF = b"GIF89a\x01\x00\x01\x00\x00\x00\x00;"


def survey_data(source: str, option_picture: str | None = None) -> dict:
    option = {"id": "like", "label": "Like"}
    if option_picture:
        option["media"] = [{"source": option_picture}]
    return {
        "schema_version": "0.1",
        "id": "ad_test",
        "title": "Ad test",
        "questions": [
            {
                "id": "ad",
                "content": [{"type": "text", "text": "Look at this ad."}, {"type": "image", "source": source, "alt": "the ad"}],
                "response": {"type": "choice", "mode": "single", "options": [option, {"id": "dislike", "label": "Dislike"}]},
            }
        ],
        "blocks": [{"id": "main", "kind": "survey", "questions": ["ad"]}],
        "flow": [{"type": "show_block", "block": "main"}],
    }


def study_folder(tmp_path: Path, source: str = "images/ad.png", **kwargs) -> Path:
    folder = tmp_path / "study"
    (folder / "images").mkdir(parents=True)
    (folder / "images" / "ad.png").write_bytes(PNG)
    (folder / "study.json").write_text(json.dumps(survey_data(source, **kwargs)), encoding="utf-8")
    return folder


def recording_model():
    requests: list[st.ModelRequest] = []

    def reply(request: st.ModelRequest) -> st.Reply:
        requests.append(request)
        return st.Reply(text=json.dumps({"answers": answer_questions(request.user)}), input_tokens=10, output_tokens=10)

    return st.Model.from_function(reply, name="recording", images=True), requests


def run(survey: st.Survey):
    model, requests = recording_model()
    results = st.run(st.plan(survey, st.configure(bank=BANK, sample_size=1, seed=1, model=model)))
    return results, requests


def test_a_relative_path_is_read_from_the_json_files_folder_and_the_picture_is_sent(tmp_path, monkeypatch):
    folder = study_folder(tmp_path)
    monkeypatch.chdir(tmp_path)  # not the survey's folder
    survey = st.read_survey(folder / "study.json")
    assert survey.standard["questions"][0]["content"][1]["source"] == "images/ad.png"  # kept as written
    _results, requests = run(survey)
    sent = [url for request in requests for url in request.images]
    assert sent == ["data:image/png;base64," + base64.b64encode(PNG).decode("ascii")]
    assert "images/ad.png" not in requests[0].user


def test_the_check_counts_a_picture_file_as_sent_with_the_survey(tmp_path):
    survey = st.read_survey(study_folder(tmp_path) / "study.json")
    check = survey.check()
    assert [(i["status"], i["detail"]) for i in check.images] == [("supported", "embedded image")]
    assert "0 unreachable" in str(check)


def test_an_absolute_path_is_read_wherever_the_survey_comes_from(tmp_path):
    picture = tmp_path / "elsewhere" / "ad.gif"
    picture.parent.mkdir()
    picture.write_bytes(GIF)
    survey = st.read_survey(survey_data(str(picture)))  # parsed JSON, no folder
    _results, requests = run(survey)
    assert requests[0].images == ["data:image/gif;base64," + base64.b64encode(GIF).decode("ascii")]


def test_folder_says_where_a_relative_path_starts_for_a_survey_given_as_data(tmp_path):
    folder = study_folder(tmp_path)
    data = json.loads((folder / "study.json").read_text(encoding="utf-8"))
    with pytest.raises(st.SurveyFormatError):
        st.read_survey(data, folder=tmp_path)  # images/ad.png is not under tmp_path
    survey = st.read_survey(data, folder=folder)
    assert survey._pictures == {"images/ad.png": "data:image/png;base64," + base64.b64encode(PNG).decode("ascii")}


def test_a_missing_file_is_an_error_naming_the_field(tmp_path):
    folder = study_folder(tmp_path, source="images/missing.png")
    with pytest.raises(st.SurveyFormatError) as error:
        st.read_survey(folder / "study.json")
    [finding] = error.value.findings
    assert finding.path == "$.questions[0].content[1].source" and finding.code == "picture.file"
    assert "no picture file at" in finding.message and "missing.png" in finding.message


def test_a_file_that_is_not_a_picture_is_an_error(tmp_path):
    folder = study_folder(tmp_path)
    (folder / "images" / "ad.png").write_text("not a picture", encoding="utf-8")
    with pytest.raises(st.SurveyFormatError, match="is not a PNG, JPEG, WEBP or GIF picture"):
        st.read_survey(folder / "study.json")


def test_a_picture_larger_than_the_limit_is_an_error(tmp_path, monkeypatch):
    from surveytwin.spec import pictures

    monkeypatch.setattr(pictures, "MAX_PICTURE_BYTES", 10)
    with pytest.raises(st.SurveyFormatError, match="a picture can be at most"):
        st.read_survey(study_folder(tmp_path) / "study.json")


def test_an_option_picture_is_read_and_sent_too(tmp_path):
    folder = study_folder(tmp_path, option_picture="images/ad.png")
    survey = st.read_survey(folder / "study.json")
    assert len(survey._pictures) == 1  # the same file, read once
    _results, requests = run(survey)
    assert len(requests[0].images) == 2  # the question's picture and the option's


def test_a_saved_run_and_its_survey_keep_the_pictures(tmp_path):
    folder = study_folder(tmp_path)
    picture = PNG + bytes(400)  # a picture big enough for the run's picture table
    (folder / "images" / "ad.png").write_bytes(picture)
    survey = st.read_survey(folder / "study.json")
    results, _requests = run(survey)
    saved = results.save(tmp_path / "run")
    stored = json.loads((saved / "results.json").read_text(encoding="utf-8"))["run"]["media"]
    assert list(stored.values()) == ["data:image/png;base64," + base64.b64encode(picture).decode("ascii")]
    (folder / "images" / "ad.png").unlink()  # the files are not needed again
    loaded = st.Results.load(saved).survey
    assert loaded.checksum == survey.checksum and loaded.standard == survey.standard
    _again, requests = run(loaded)
    assert requests[0].images[0].startswith("data:image/png;base64,")


def test_the_checksum_covers_the_pictures(tmp_path):
    first = st.read_survey(study_folder(tmp_path / "a") / "study.json")
    folder = study_folder(tmp_path / "b")
    (folder / "images" / "ad.png").write_bytes(GIF)  # same JSON, another picture
    second = st.read_survey(folder / "study.json")
    assert first.standard == second.standard and first.checksum != second.checksum
