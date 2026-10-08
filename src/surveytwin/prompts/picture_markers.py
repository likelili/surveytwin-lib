"""Pictures at their place in the survey text.

The survey text a twin reads carries "[[image KEY LABEL]]" wherever the survey
shows a picture: where the QSF puts it in a question's text (between paragraphs,
inside a sentence, in a table cell), after the option, row or statement it
belongs to, or before the text of a question whose picture has no place in it.
KEY names the picture's sequence-media entry (prompts/instrument.py's
_question_prompt_media_entries); LABEL is what the model reads there, for example
"Image 2 of 4 attached to QID23".

When the request is built, the text is cut at each marker and the picture is
sent right there, after its label (answers/executor.py). A picture shown at two
places is sent at both, as a respondent sees it twice. A prompt that carries no
pictures (a text-only call, a preview) shows the label alone.
"""

from __future__ import annotations

import re

PICTURE_MARKER_RE = re.compile(r"\[\[image (\S+) ([^\]]*)\]\]")


def picture_marker(key: str, label: str) -> str:
    """The marker for one picture: brackets in its label would end it early."""
    clean = re.sub(r"\s+", " ", re.sub(r"[\[\]]", "", str(label or ""))).strip()
    return f"[[image {key} {clean}]]"


def picture_markers_as_text(text: str, *, shown: bool = True) -> str:
    """``text`` with each picture marker replaced by its label in brackets. With
    ``shown=False`` (a request that sends no pictures) the label says so."""
    if not text or "[[image " not in text:
        return text
    suffix = "" if shown else ": not shown"
    return PICTURE_MARKER_RE.sub(lambda match: f"[{match.group(2)}{suffix}]", text)
