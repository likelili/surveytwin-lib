"""Each survey picture is sent where the survey shows it.

Before, every <img> was left out of a question's text, and the prompt sent all
of a survey's pictures after the whole survey text, each under "Visual context
for <question>" with that question's text repeated; the survey text only said
"[Question includes N image(s).]" after the question's options. Now the survey
text carries a marker where each picture sits (prompts/picture_markers.py), and
the request sends the picture at that point after a short label, such as "Image
2 of 4 attached to QID1". A picture shown twice is sent twice. Option, row and
statement pictures come right after their line. A picture with no place in the
survey text (a question answered at an earlier stage, a rerun) is sent after it
with its question's text, its pictures in place there too. A prompt that sends
no pictures shows the labels; when the pictures could not be sent, the label
says so.
"""

from __future__ import annotations

import asyncio
from typing import Any

import surveytwin as st
from fakes import accept_all_images, scripted
from flow_kit import block, qsf

from surveytwin.answers import executor
from surveytwin.answers.rerun import pictures_by_question
from surveytwin.engine import render
from surveytwin.engine.definition import Definition
from surveytwin.engine.staged import StagedRuntime
from surveytwin.engine.text import picture_text, question_text
from surveytwin.prompts.picture_markers import picture_markers_as_text
from surveytwin.results.layout import Layout

LOGO, CLASSIC, SPORTY, SLIM = (f"https://example.org/{name}.png" for name in ("logo", "classic", "sporty", "slim"))


def _parts(user_content: list[dict[str, Any]]) -> list[str]:
    """The request's user message as text pieces, each picture as <name.png>."""
    return [
        f"<{str(part['image_url']).rsplit('/', 1)[-1]}>" if part.get("type") == "input_image" else part["text"]
        for part in user_content
    ]


def _payload(qid: str, qtype: str, selector: str, text: str, **extra: Any) -> dict[str, Any]:
    return {"QuestionID": qid, "DataExportTag": qid, "QuestionType": qtype, "Selector": selector,
            "QuestionText": text, "Configuration": {"QuestionDescriptionOption": "UseText"},
            "Validation": {"Settings": {"ForceResponse": "OFF", "Type": "None"}}, **extra}


def _conjoint_qsf() -> dict[str, Any]:
    """The shape of a rating conjoint: instructions that show and name the styles,
    then a profile whose Style row holds only a picture."""
    instructions = (
        f'<div><img src="{LOGO}"></div><p>Rate several coats.</p><p>The three styles are shown below:</p>'
        f'<div><img src="{CLASSIC}"> <img src="{SPORTY}"> <img src="{SLIM}"></div>'
        "<div>Style 1 (Classic) Style 2 (Sporty) Style 3 (Slim-fit)</div>"
    )
    profile = (
        "<div><b>Profile 1</b></div><table>"
        f'<tr><td>Style</td><td><img src="{CLASSIC}"></td></tr>'
        "<tr><td>Color</td><td>Grey</td></tr><tr><td>Price</td><td>$1,250</td></tr></table>"
        "How much do you like this coat?"
    )
    choices = {str(i): {"Display": str(i)} for i in range(1, 8)}
    questions = {
        "QID1": _payload("QID1", "DB", "TB", instructions),
        "QID2": _payload("QID2", "MC", "SAVR", profile, SubSelector="TX", Choices=choices, ChoiceOrder=list(range(1, 8))),
        "QID3": _payload("QID3", "TE", "SL", "Any comments on the coats?"),
    }
    return qsf({"BL_A": ("Main", ["QID1", "QID2", "QID3"])}, questions, [block("BL_A")])


def _shown(survey: st.Survey) -> list[dict[str, Any]]:
    return [question for _block, questions in Layout(survey.standard).placements() for question in questions]


def _rendered(survey: st.Survey | None = None) -> tuple[str, list[dict[str, Any]], list[dict[str, Any]]]:
    shown = _shown(survey or st.read_qsf(_conjoint_qsf()))
    return render.sequence_text(shown), render.sequence_media(shown), render.prompt_questions(shown)


class _Caller:
    def __init__(self, chat):
        self._chat = chat

    async def responses_input_text(self, *, input_items, max_tokens):  # pragma: no cover - not reached
        raise AssertionError("no pictures can be sent")

    async def chat_json(self, *, system_prompt, user_prompt, temperature, max_tokens):
        return await self._chat(user_prompt)


# ---- where a question's text shows its pictures --------------------------------------------


def test_the_text_marks_where_each_picture_sits():
    cases = {
        '<p>Look at this ad.</p><img src="https://x.org/ad.png" alt="Ad"><p>Do you like it?</p>':
            ("Look at this ad.\n\nDo you like it?", "Look at this ad.\n\n[[picture 1]]\n\nDo you like it?"),
        'The logo <img src="https://x.org/l.png"> is new.':
            ("The logo is new.", "The logo [[picture 1]] is new."),
        '<p>Before</p><img src="https://x.org/a.png"><p>Again</p><img src="https://x.org/a.png">':
            ("Before\n\nAgain", "Before\n\n[[picture 1]]\n\nAgain\n\n[[picture 1]]"),
        'Written escaped: &lt;img src="https://x.org/e.png"&gt; here.':
            ("Written escaped: here.", "Written escaped: [[picture 1]] here."),
        '<img src="https://x.org/only.png">':
            ("[Image stimulus attached]", "[[picture 1]]"),
        '<table><tr><td>Style</td><td><img src="https://x.org/s1.png"></td></tr><tr><td>Color</td><td>Grey</td></tr></table>':
            ("Style\nColor | Grey", "Style | [[picture 1]]\nColor | Grey"),
    }
    for html, (text, with_pictures) in cases.items():
        survey = st.read_qsf(qsf({"BL_1": ("A", ["QID1"])}, {"QID1": _payload("QID1", "DB", "TB", html)}, [block("BL_1")]))
        question = survey.standard["questions"][0]
        assert question_text(question, native=False)[0] == text  # unchanged: it feeds exports, pipes and previews
        assert picture_text(question, native=False) == with_pictures


def test_a_picture_with_no_place_in_the_text_and_an_unreadable_one_are_not_marked():
    graphic = _payload("QID1", "DB", "GRB", "Rate the shoe.", Graphics="IM_shoe", GraphicsDescription="Shoe")
    data = qsf({"BL_1": ("A", ["QID1"])}, {"QID1": graphic}, [block("BL_1")])
    data["SurveyEntry"]["SurveyBrandID"] = "acme"  # completes the picture's address
    survey = st.read_qsf(data)
    definition = Definition(survey.standard)
    assert definition.brand == "acme"
    question = survey.standard["questions"][0]
    assert any(item["type"] == "image" for item in question["content"])
    assert picture_text(question, native=False, brand=definition.brand) == ""
    unreadable = _payload("QID1", "DB", "TB", '<p>A</p><img src="data:image/png;base64,AAAA"><p>B</p>')
    survey = st.read_qsf(qsf({"BL_1": ("A", ["QID1"])}, {"QID1": unreadable}, [block("BL_1")]))
    assert picture_text(survey.standard["questions"][0], native=False) == ""


def test_a_written_survey_shows_its_pictures_where_its_content_puts_them():
    question = {"id": "q1", "content": [{"type": "text", "text": "Look at this shoe."}, {"type": "image", "source": "https://x.org/shoe.png"},
                                        {"type": "text", "text": "Would you buy it?"}]}
    assert picture_text(question, native=True) == "Look at this shoe.\n\n[[picture 1]]\n\nWould you buy it?"


# ---- the survey text ----------------------------------------------------------------------


def test_the_survey_text_marks_each_picture_at_its_place():
    text, media, _questions = _rendered()
    assert "[[image QID1~q1 Image 1 of 4 attached to QID1]]\n\nRate several coats." in text
    assert (
        "The three styles are shown below:\n\n[[image QID1~q2 Image 2 of 4 attached to QID1]] "
        "[[image QID1~q3 Image 3 of 4 attached to QID1]] [[image QID1~q4 Image 4 of 4 attached to QID1]]\n"
        "Style 1 (Classic) Style 2 (Sporty) Style 3 (Slim-fit)"
    ) in text
    assert "| Style | [[image QID2~q1 Image attached to Q1]] |" in text
    assert "includes" not in text and "image(s)" not in text
    assert [entry["key"] for item in media for entry in item["media"]] == [
        "QID1~q1", "QID1~q2", "QID1~q3", "QID1~q4", "QID2~q1"]


def test_a_picture_with_no_place_in_the_text_comes_before_it_and_item_pictures_after_their_line():
    shoe = {"type": "image", "source": "https://x.org/shoe.png", "title": "Shoe"}
    definition = {
        "schema_version": "0.1", "id": "s",
        "questions": [
            {"id": "QID1", "content": [shoe], "response": {"type": "text"}},
            {"id": "QID2", "content": [{"type": "text", "text": "Which logo?"}],
             "response": {"type": "choice", "mode": "single", "options": [
                 {"id": "1", "label": "A", "media": [{"source": "https://x.org/a.png"}]}, {"id": "2", "label": "B"}]}},
        ],
        "blocks": [{"id": "b1", "kind": "survey", "questions": ["QID1", "QID2"]}],
        "flow": [{"type": "show_block", "block": "b1"}],
    }
    text = render.sequence_text(_shown(st.read_survey(definition)))
    assert "Q1:\n[[image QID1~q1 Image attached to Q1. Title: Shoe]]\n\nWhich" not in text  # each question keeps its own lines
    assert "Q1:\n[[image QID1~q1 Image attached to Q1. Title: Shoe]]\nQuestion Type" in text
    assert "1 - A [[image QID2~o1.1 Image for option 'A' in Q2]]\n" in text


def test_a_survey_without_pictures_reads_as_before():
    survey = st.read_qsf(qsf({"BL_1": ("A", ["QID1"])}, {"QID1": _payload("QID1", "TE", "SL", "<p>Plain</p><p>text</p>")}, [block("BL_1")]))
    text, media, _questions = _rendered(survey)
    assert media == [] and "[[" not in text and "Plain\n\ntext" in text


# ---- the request --------------------------------------------------------------------------


def test_the_request_sends_each_picture_at_its_place_and_repeats_a_repeated_one():
    text, media, questions = _rendered()
    items = executor._build_multimodal_input_items(
        persona_text="Persona", questions=questions, survey_sequence_text=text, survey_sequence_media=media)
    parts = _parts(items[1]["content"])
    assert [part for part in parts if part.startswith("<")] == [
        "<logo.png>", "<classic.png>", "<sporty.png>", "<slim.png>", "<classic.png>"]
    assert parts[parts.index("<sporty.png>") - 1] == " [Image 3 of 4 attached to QID1]"
    profile = parts.index("<classic.png>", parts.index("<slim.png>"))
    assert parts[profile - 1].endswith("| Style | [Image attached to Q1]")
    assert parts[profile + 1].startswith(" |\n|---|---|\n| Color | Grey |")
    assert not any("Visual context" in part or "[[image" in part for part in parts)


def test_a_picture_the_model_cannot_load_is_labelled_where_it_was():
    text, media, questions = _rendered()
    sendable, skipped, _labels = executor._leave_out_unavailable_pictures(media, questions, {SPORTY})
    assert not skipped  # a descriptive picture is only left out
    items = executor._build_multimodal_input_items(
        persona_text="Persona", questions=questions, survey_sequence_text=text, survey_sequence_media=sendable)
    joined = "".join(_parts(items[1]["content"]))
    assert "[Image 3 of 4 attached to QID1: not shown]" in joined
    assert "<sporty.png>" not in joined and "<slim.png>" in joined


def test_a_call_that_sends_no_pictures_says_where_they_were():
    text, media, questions = _rendered()
    seen: dict[str, str] = {}

    async def chat(user_prompt):
        seen["user"] = user_prompt
        return {"answers": [{"question_id": "QID3", "answer_value": "No", "answer_label": "No", "answer_raw": "No"}]}

    asyncio.run(executor._llm_answer_questions(
        _Caller(chat), "Persona", questions, survey_sequence_text=text, survey_sequence_media=media,
        blocked_image_urls=[LOGO, CLASSIC, SPORTY, SLIM]))
    # the profile, whose picture cannot be loaded, is not asked; the text still
    # shows where every picture was
    assert "[Image 1 of 4 attached to QID1: not shown]\n\nRate several coats." in seen["user"]
    assert "| Style | [Image attached to Q1: not shown] |" in seen["user"]
    assert "[[image" not in seen["user"]


def test_a_preview_shows_where_the_pictures_go():
    text, _media, questions = _rendered()
    _system, _persona, questions_prompt = executor.build_simulation_prompt_parts(
        "Persona", questions, survey_sequence_text=text)
    assert "| Style | [Image attached to Q1] |" in questions_prompt
    assert "[[image" not in questions_prompt and "not shown" not in questions_prompt
    assert picture_markers_as_text("a [[image K~q1 Image attached to Q1]] b", shown=False) == (
        "a [Image attached to Q1: not shown] b")


def test_a_batch_line_keeps_each_picture_at_its_place():
    text, media, questions = _rendered()
    messages, _max_tokens = executor.build_persona_chat_messages(
        persona_text="Persona", questions=questions, survey_sequence_text=text, survey_sequence_media=media)
    content = messages[1]["content"]
    kinds = [part["type"] for part in content]
    assert kinds.count("image_url") == 5
    assert content[kinds.index("image_url") - 1]["text"].endswith("[Image 1 of 4 attached to QID1]")


# ---- pictures with no place in the survey text ------------------------------------------


def test_a_picture_of_a_question_answered_at_an_earlier_stage_keeps_its_place_in_that_text():
    ad = _payload("QID1", "MC", "SAVR", '<p>Look at this ad.</p><img src="https://x.org/ad.png"><p>Do you like it?</p>',
                  SubSelector="TX", Choices={"1": {"Display": "Yes"}, "2": {"Display": "No"}}, ChoiceOrder=[1, 2])
    follow_up = _payload("QID2", "TE", "SL", "Why did you say ${q://QID1/ChoiceGroup/SelectedChoices}?")
    survey = st.read_qsf(qsf({"BL_A": ("A", ["QID1", "QID2"])}, {"QID1": ad, "QID2": follow_up}, [block("BL_A")]))
    runtime = StagedRuntime(Definition(survey.standard), seed=42, persona_count=1)
    state = runtime.new_state(persona_id="pid_1", persona_index=0)
    first = runtime.render_next_stage(state)
    assert "Look at this ad.\n\n[[image QID1~q1 Image attached to Q1]]\n\nDo you like it?" in first.survey_sequence_text
    runtime.apply_answers(state, questions=first.questions, answers=[
        {"question_id": q["id"], "answer_value": "Yes", "answer_label": "Yes", "answer_raw": "Yes"} for q in first.questions])
    later = runtime.render_next_stage(state)
    assert [item["prompt_label"] for item in later.survey_sequence_media] == ["an earlier question"]
    items = executor._build_multimodal_input_items(
        persona_text="Persona", questions=later.questions, survey_sequence_text=later.survey_sequence_text,
        survey_sequence_media=later.survey_sequence_media)
    parts = _parts(items[1]["content"])
    at = parts.index("<ad.png>")
    assert parts[at - 1] == "Visual context for an earlier question: Look at this ad.\n\n[Image attached to an earlier question]"
    assert parts[at + 1] == "\n\nDo you like it?"


def test_a_rerun_shows_each_picture_in_its_question_text():
    survey = st.read_qsf(_conjoint_qsf())
    blocks = [questions for _block, questions in Layout(survey.standard).placements()]
    entries = pictures_by_question(blocks)["QID2"]
    assert [entry["question_id"] for entry in entries] == ["QID1", "QID2"]
    items = executor.build_rerun_input_items(
        "Persona", [{"id": "QID2", "text": "How much do you like this coat?", "type": "single_choice",
                     "options": [str(i) for i in range(1, 8)]}],
        prior_answers={}, flagged_qids=["QID2"], pictures=entries)
    parts = _parts(items[1]["content"])
    profile = parts.index("<classic.png>", parts.index("<slim.png>"))
    assert parts[profile - 1].endswith("| Style | [Image attached to QID2]")
    assert parts[profile + 1].startswith(" |\n|---|---|\n| Color | Grey |")
    assert parts.count("<classic.png>") == 2


def test_a_run_sends_each_picture_at_its_place(mini_bank):
    """End to end through the public API, with a model that records what it is sent."""
    seen: list[list[str]] = []

    def recorder(request: st.ModelRequest) -> st.Reply:
        seen.append([f"<{part['url'].rsplit('/', 1)[-1]}>" if part["type"] == "image" else part["text"]
                     for part in request.content])
        return scripted(request)

    survey = st.read_qsf(_conjoint_qsf())
    model = st.Model.from_function(recorder, name="recorder", images=True)
    config = st.configure(bank=mini_bank, sample_size=1, seed=5, model=model)
    st.run(st.plan(survey, config), image_checker=accept_all_images)
    assert [part for part in seen[0] if part.startswith("<")] == [
        "<logo.png>", "<classic.png>", "<sporty.png>", "<slim.png>", "<classic.png>"]
    profile = seen[0].index("<classic.png>", seen[0].index("<slim.png>"))
    assert seen[0][profile - 1].endswith("| Style | [Image attached to Q1]")
