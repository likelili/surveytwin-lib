"""A question is never answered without its picture.

When the model cannot load one of a survey's pictures (the image check found
the link dead and the run accepts missing images, the model takes no image
input, or the provider rejects it during the run), the question that shows the
picture is not asked and is recorded as skipped. A picture in descriptive text
is only left out: the questions after it are still asked.
"""

from __future__ import annotations

import asyncio
import json

import surveytwin as st
from fakes import accept_all_images, scripted_model
from flow_kit import block, mc, qsf

from surveytwin.answers import executor
from surveytwin.spec.read_template import survey_from_template

DEAD = "https://dead.example.com/ad.png"
LOGO = "https://ok.example.com/logo.png"
SKIPPED = executor.ANSWER_STATUS_SKIPPED


def _answer(qid: str, label: str) -> dict[str, str]:
    return {"question_id": qid, "answer_value": label, "answer_label": label, "answer_raw": label}


def _image_parts(input_items: list[dict]) -> list[str]:
    return [
        part["image_url"]
        for item in input_items
        for part in item.get("content", [])
        if isinstance(part, dict) and part.get("type") == "input_image"
    ]


class _Caller:
    def __init__(self, responses=None, chat=None):
        self._responses, self._chat = responses, chat

    async def responses_input_text(self, *, input_items, max_tokens):
        return await self._responses(input_items)

    async def chat_json(self, *, system_prompt, user_prompt, temperature, max_tokens):
        return await self._chat(user_prompt)


def _run(monkeypatch, *, questions, media=None, blocked=None, responses=None, chat=None, precomputed=None):
    monkeypatch.setattr(executor, "RETRY_BACKOFF_BASE_S", 0.0)
    notes: list[str] = []
    answers = asyncio.run(executor._llm_answer_questions(
        _Caller(responses, chat), "Persona text", questions,
        survey_sequence_media=media, blocked_image_urls=blocked, precomputed_answer_text=precomputed, notes=notes,
    ))
    return {a["question_id"]: a for a in answers}, notes


def test_a_dead_picture_in_descriptive_text_is_left_out_and_the_questions_after_it_are_asked(monkeypatch):
    sent: list[list[str]] = []

    async def responses(input_items):
        sent.append(_image_parts(input_items))
        return json.dumps({"answers": [_answer("QID2", "A"), _answer("QID3", "Yes")]})

    media = [
        {"question_id": "QID1", "question_type": "DB", "question_text": "Look at this ad.", "prompt_label": "QID1",
         "media": [{"kind": "image", "src": DEAD}]},
        {"question_id": "QID2", "question_type": "MC", "question_text": "Which logo?", "prompt_label": "Q1",
         "media": [{"kind": "image", "src": LOGO, "context": "option", "label": "A"}]},
    ]
    questions = [
        {"id": "QID2", "text": "Which logo?", "type": "single_choice", "options": ["A", "B"]},
        {"id": "QID3", "text": "Did you like the ad?", "type": "single_choice", "options": ["Yes", "No"]},
    ]
    answers, notes = _run(monkeypatch, questions=questions, media=media, blocked=[DEAD], responses=responses)
    assert sent == [[LOGO]]
    assert answers["QID2"]["answer_label"] == "A" and answers["QID3"]["answer_label"] == "Yes"
    assert all("answer_status" not in a for a in answers.values()) and notes == []


def test_a_multi_select_question_with_a_dead_option_picture_is_skipped_in_every_option(monkeypatch):
    async def chat(user_prompt):
        assert "Pick every brand you know" not in user_prompt
        return {"answers": [_answer("QID5", "Yes"), _answer("QID4_1", "Selected")]}

    options = [
        {"id": f"QID4_{n}", "text": "Pick every brand you know", "type": "single_choice",
         "options": ["Selected", "Not selected"], "parent_question_id": "QID4", "multi_answer_option_id": str(n)}
        for n in (1, 2, 3)
    ]
    questions = [*options, {"id": "QID5", "text": "Do you shop online?", "type": "single_choice", "options": ["Yes", "No"]}]
    media = [{"question_id": "QID4", "question_type": "MC", "prompt_label": "Q1",
              "media": [{"kind": "image", "src": DEAD, "context": "option", "label": "Acme"}]}]
    answers, _ = _run(monkeypatch, questions=questions, media=media, blocked=[DEAD], chat=chat)
    assert [answers[f"QID4_{n}"].get("answer_status") for n in (1, 2, 3)] == [SKIPPED] * 3
    assert answers["QID4_1"]["answer_label"] == "" and answers["QID5"]["answer_label"] == "Yes"


def test_a_batch_answer_to_a_question_whose_picture_is_dead_is_not_kept(monkeypatch):
    async def never(*_args):
        raise AssertionError("the batch answer is usable, no live call is needed")

    questions = [
        {"id": "Q1", "text": "Which one shows a shoe?", "type": "single_choice", "options": ["a", "b"],
         "question_media": [{"kind": "image", "src": DEAD}]},
        {"id": "Q2", "text": "Do you run?", "type": "single_choice", "options": ["Yes", "No"]},
    ]
    precomputed = json.dumps({"answers": [_answer("Q1", "b"), _answer("Q2", "Yes")]})
    answers, _ = _run(monkeypatch, questions=questions, blocked=[DEAD], responses=never, chat=never, precomputed=precomputed)
    assert answers["Q1"]["answer_status"] == SKIPPED and answers["Q2"]["answer_label"] == "Yes"


def test_when_the_provider_names_the_picture_only_its_question_is_skipped(monkeypatch):
    sent: list[list[str]] = []

    async def responses(input_items):
        pictures = _image_parts(input_items)
        sent.append(pictures)
        if DEAD in pictures:
            raise RuntimeError(f'Responses API error 400: {{"error": {{"message": "Error while downloading {DEAD}.", '
                               '"code": "invalid_image_url"}}')
        return json.dumps({"answers": [_answer("Q1", "x"), _answer("Q2", "A")]})

    questions = [
        {"id": "Q1", "text": "Describe the ad.", "type": "open_text", "question_media": [{"kind": "image", "src": DEAD}]},
        {"id": "Q2", "text": "Which logo?", "type": "single_choice", "options": ["A", "B"],
         "question_media": [{"kind": "image", "src": LOGO}]},
    ]
    answers, notes = _run(monkeypatch, questions=questions, responses=responses)
    assert sent == [[DEAD, LOGO], [LOGO]]
    assert answers["Q1"]["answer_status"] == SKIPPED and answers["Q2"]["answer_label"] == "A"
    assert notes == [f"the model could not load {DEAD}; Q1 was not asked and recorded as skipped"]


def test_when_the_provider_does_not_say_which_picture_failed_no_picture_question_is_answered(monkeypatch):
    async def responses(input_items):
        raise RuntimeError('Responses API error 400: {"error": {"message": "You uploaded an unsupported image."}}')

    async def chat(user_prompt):
        assert "Describe the ad." not in user_prompt and "Which logo?" not in user_prompt
        return {"answers": [_answer("Q3", "Yes")]}

    questions = [
        {"id": "Q1", "text": "Describe the ad.", "type": "open_text", "question_media": [{"kind": "image", "src": DEAD}]},
        {"id": "Q2", "text": "Which logo?", "type": "single_choice", "options": ["A", "B"],
         "question_media": [{"kind": "image", "src": LOGO}]},
        {"id": "Q3", "text": "Do you run?", "type": "single_choice", "options": ["Yes", "No"]},
    ]
    answers, notes = _run(monkeypatch, questions=questions, responses=responses, chat=chat)
    assert answers["Q1"]["answer_status"] == SKIPPED and answers["Q2"]["answer_status"] == SKIPPED
    assert answers["Q3"]["answer_label"] == "Yes"
    assert notes == [
        "the model could not load one of this survey's pictures and did not say which, so none were sent; "
        "Q1, Q2 were not asked and recorded as skipped"
    ]


def test_a_twin_whose_every_question_shows_a_dead_picture_makes_no_call(monkeypatch):
    async def never(*_args):
        raise AssertionError("nothing is left to ask")

    questions = [{"id": "Q1", "text": "Which one shows a shoe?", "type": "single_choice", "options": ["a", "b"],
                  "question_media": [{"kind": "image", "src": DEAD}]}]
    answers, notes = _run(monkeypatch, questions=questions, blocked=[DEAD], responses=never, chat=never)
    assert answers["Q1"]["answer_status"] == SKIPPED and notes == []


def _survey_with_a_question_picture():
    data = qsf({"BL_A": ("A", ["QID1", "QID2"])},
               {"QID1": mc("QID1", "Fruit?", ["Apple", "Pear"]), "QID2": mc("QID2", "Colour?", ["Red", "Blue"])},
               [block("BL_A")])
    for element in data["SurveyElements"]:
        payload = element.get("Payload") if element.get("Element") == "SQ" else None
        if payload and payload.get("QuestionID") == "QID1":
            payload["QuestionText"] = f'Fruit? <img src="{DEAD}">'
    return st.read_qsf(data)


def test_a_run_that_accepts_a_dead_question_picture_skips_that_question_and_says_so(mini_bank):
    login = lambda url: {"status": "auth_required", "detail": "HTTP 403"}  # noqa: E731
    plan = st.plan(_survey_with_a_question_picture(), st.configure(bank=mini_bank, sample_size=2, seed=5, model=scripted_model(),
                                                                   accept_missing_images=True))
    results = st.run(plan, image_checker=login)
    by_question = {}
    for row in results.rows:
        by_question.setdefault(row["question_id"], []).append(row)
    assert all(r.get("answer_status") == SKIPPED and r["answer_value"] == "" for r in by_question["QID1"])
    assert all(r["answer_value"] for r in by_question["QID2"])  # the other question is asked
    assert any("A question that shows one of them was not asked and is recorded as skipped" in w for w in results.warnings)
    # With the picture loadable, the same question is asked.
    loadable = st.run(st.plan(_survey_with_a_question_picture(), st.configure(bank=mini_bank, sample_size=2, seed=5, model=scripted_model())),
                      image_checker=accept_all_images)
    assert all(r["answer_value"] for r in loadable.rows if r["question_id"] == "QID1")


# ---- reruns send the pictures the twin saw ------------------------------------------------------------

AD = "https://img.example.com/ad.png"
LOGO_A = "https://img.example.com/logo-a.png"
LOGO_B = "https://img.example.com/logo-b.png"

_PICTURE_TEMPLATE = {"Elements": [{"ElementType": "Block", "BlockID": "BL_ad", "BlockName": "Ad", "Questions": [
    {"QuestionID": "QID1", "QuestionType": "DB", "QuestionText": "Look at this ad.", "QuestionMedia": [{"src": AD}]},
    {"QuestionID": "QID2", "QuestionType": "TE", "QuestionText": "What do you remember about the ad?",
     "Settings": {"Selector": "SL"}},
    {"QuestionID": "QID3", "QuestionType": "MC", "QuestionText": "Which logo was in the ad?",
     "Options": ["A", "B"], "OptionsID": ["1", "2"], "OptionMedia": [[{"src": LOGO_A}], [{"src": LOGO_B}]]},
    {"QuestionID": "QID4", "QuestionType": "MC", "QuestionText": "Do you shop online?",
     "Options": ["Yes", "No"], "OptionsID": ["1", "2"]},
]}]}


def _picture_rows():
    def row(qid, text, qtype, value="", status="skipped"):
        return {"persona_id": "p0", "question_id": qid, "question_text": text, "question_type": qtype,
                "answer_raw": value, "answer_value": value, "answer_label": value,
                **({"answer_status": status} if status else {})}

    return [row("QID2", "What do you remember about the ad?", "open_text"),
            row("QID3", "Which logo was in the ad?", "single_choice"),
            row("QID4", "Do you shop online?", "single_choice", value="Yes", status=None)]


def _picture_rerun(*, reject=(), blocked=None, images=True):
    from surveytwin.answers.rerun import rerun_twin

    calls: list[list[str]] = []
    texts: list[str] = []
    reply = json.dumps({"answers": [_answer("QID2", "a red car"), _answer("QID3", "A")]})

    async def responses(input_items):
        pictures = _image_parts(input_items)
        calls.append(pictures)
        texts.append(json.dumps(input_items))
        for url in reject:
            if url in pictures:
                raise RuntimeError(f'Responses API error 400: {{"error": {{"message": "Error while downloading {url}.", '
                                   '"code": "invalid_image_url"}}')
        return reply

    async def chat(user_prompt):
        calls.append([])
        texts.append(user_prompt)
        return json.loads(reply)

    rows = _picture_rows()
    result = asyncio.run(rerun_twin(caller=_Caller(responses, chat), rows=rows, persona_id="p0", persona_text="persona",
                                    definition=survey_from_template(_PICTURE_TEMPLATE), blocked_image_urls=blocked, images=images))
    repaired = {r["question_id"]: r.get("answer_repaired") or "" for r in rows}
    return result, calls, texts, repaired


def test_a_rerun_sends_the_pictures_the_twin_saw():
    result, calls, texts, repaired = _picture_rerun()
    assert calls == [[AD, LOGO_A, LOGO_B]]  # was a text-only call
    assert "Image for option 'A' in QID3." in texts[0] and "ONLY these questions: QID2, QID3" in texts[0]
    assert result == {"persona_id": "p0", "flagged": 2, "repaired": 2}
    assert repaired["QID2"] == "a red car" and repaired["QID3"] == "A"


def test_a_rerun_does_not_re_ask_a_question_whose_picture_cannot_be_loaded():
    result, calls, texts, repaired = _picture_rerun(blocked=[LOGO_B])
    assert calls == [[AD]] and "ONLY these questions: QID2." in texts[0]
    assert result == {"persona_id": "p0", "flagged": 2, "repaired": 1, "not_asked": 1}
    assert repaired["QID3"] == ""  # the model's "A" is not kept


def test_a_rerun_leaves_out_a_picture_the_provider_rejects():
    result, calls, _, _ = _picture_rerun(reject=(AD,))
    assert calls == [[AD, LOGO_A, LOGO_B], [LOGO_A, LOGO_B]]  # the ad is descriptive text: only left out
    assert result == {"persona_id": "p0", "flagged": 2, "repaired": 2}
    result, calls, _, repaired = _picture_rerun(reject=(LOGO_A,))
    assert calls == [[AD, LOGO_A, LOGO_B], [AD]]
    assert result == {"persona_id": "p0", "flagged": 2, "repaired": 1, "not_asked": 1} and repaired["QID3"] == ""


def test_a_rerun_with_a_model_that_takes_no_images_does_not_re_ask_picture_questions():
    result, calls, texts, repaired = _picture_rerun(images=False)
    assert calls == [[]] and "ONLY these questions: QID2." in texts[0]  # a text call, for QID2 only
    assert result == {"persona_id": "p0", "flagged": 2, "repaired": 1, "not_asked": 1}
    assert repaired["QID3"] == ""
