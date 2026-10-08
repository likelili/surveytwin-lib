"""How a question's content reads as text: its words, tables and item labels.

These are the rules every step shares (the survey a twin is shown, the prompt,
the exports): HTML text, from a QSF or written in the standard format, loses its
markup but keeps its paragraphs, line breaks, list items and tables, an item with
no label but a picture is called "[Image option 2]", and a question that shows
only a picture reads "[Image stimulus attached]".
"""

from __future__ import annotations

import html
from typing import Any, Callable

from ..qsf.html_text import html_text
from ..qsf.question_types.base import _normalize_text, _qualtrics_graphic_src
from ..spec.text import join_text

IMAGE_STIMULUS = "[Image stimulus attached]"


def render_table(table: dict[str, Any]) -> str:
    """A stimulus table as Markdown, every cell under its column."""
    columns = table.get("columns") or []
    header = [join_text(table.get("corner", ""))] + [join_text(c.get("label", "")) for c in columns]
    lines = []
    if table.get("caption"):
        lines.append(join_text(table["caption"]))
    lines.append("| " + " | ".join(header) + " |")
    lines.append("|" + "---|" * len(header))
    for row in table.get("rows") or []:
        cells = row.get("cells") or {}
        values = [join_text(row.get("label", ""))] + [join_text(cells.get(c["id"], "")) for c in columns]
        lines.append("| " + " | ".join(values) + " |")
    return "\n".join(lines)


def question_text(question: dict[str, Any], *, native: bool = True) -> tuple[str, bool]:
    """(the question's text as the model reads it, whether its line breaks count).

    HTML text, read from a QSF or written in the standard format, keeps its
    paragraphs, line breaks, list items and tables; the
    QSF reader writes the template's text the same way. Plain text keeps its
    lines when written in the standard format."""
    texts = []
    lines = native
    for item in question.get("content") or []:
        if item.get("type") == "text":
            raw = join_text(item.get("text", ""))
            if item.get("format") == "html":
                texts.append(html_text(_as_read(raw.strip(), native)))
                lines = True
            else:
                texts.append(raw)
        elif item.get("type") == "table":
            texts.append(render_table(item))
            lines = True
    text = texts[0] if len(texts) == 1 else "\n\n".join(t for t in texts if t)
    if not text and any(item.get("type") == "image" for item in question.get("content") or []):
        text = IMAGE_STIMULUS
    return text, lines


def item_labels(items: list[dict[str, Any]], image_label: str, empty: Callable[[int, dict[str, Any]], str] | None = None) -> list[str]:
    """Item labels as the model reads them: an unlabeled item with a picture is
    "[Image option 2]"; with no picture, ``empty`` names it (or it stays empty)."""
    out = []
    for position, item in enumerate(items, start=1):
        text = join_text(item.get("label", ""))
        if not text and item.get("media"):
            text = f"[{image_label} {position}]"
        elif not text and empty is not None:
            text = empty(position, item)
        out.append(text)
    return out


def media_items(media: Any) -> list[dict[str, str]]:
    """Pictures as the prompt lists them: ``{"kind": "image", "src", "alt"?, "title"?}``,
    the address and words trimmed, a picture with no address left out."""
    out: list[dict[str, str]] = []
    for item in media or []:
        if not isinstance(item, dict):
            continue
        src = str(item.get("source", "")).strip()
        if not src:
            continue
        normalized = {"kind": "image", "src": src}
        for key in ("alt", "title"):
            value = str(item.get(key, "") or "").strip()
            if value:
                normalized[key] = value
        out.append(normalized)
    return out


def question_images(question: dict[str, Any]) -> list[dict[str, str]]:
    """The pictures a question shows with its text."""
    return media_items(item for item in question.get("content") or [] if item.get("type") == "image")


def _as_read(raw: str, native: bool) -> str:
    """HTML as it is laid out: text read from a QSF has its entities resolved
    first, as the QSF reader always resolved them (an escaped tag is a tag)."""
    return raw if native else html.unescape(raw)


def picture_text(question: dict[str, Any], *, native: bool = True, brand: str = "") -> str:
    """The question's text as ``question_text`` gives it, with "[[picture N]]"
    where the survey shows its Nth picture (its image content items, from 1): in
    a survey read from a QSF, where an ``<img>`` of its text is that picture
    (``brand``: the survey's Qualtrics brand, to read the picture's address); in
    a survey written in the standard format, where the image stands among its
    content. Empty when no picture has a place there."""
    content = question.get("content") or []
    images = [item for item in content if item.get("type") == "image"]
    if not images:
        return ""
    places: dict[tuple[str, str, str], int] = {}
    for number, item in enumerate(images, start=1):
        key = (str(item.get("source", "") or ""), _normalize_text(item.get("alt", "")), _normalize_text(item.get("title", "")))
        places.setdefault(key, number)

    def picture(tag: Any) -> str | None:
        src = _qualtrics_graphic_src(tag.get("src") or tag.get("data-src") or tag.get("data-imageurl") or "", survey_brand_id=brand)
        number = places.get((src, _normalize_text(tag.get("alt", "")), _normalize_text(tag.get("title", ""))))
        return f"[[picture {number}]]" if number else None

    texts: list[str] = []
    number = 0
    for item in content:
        if item.get("type") == "text":
            raw = join_text(item.get("text", ""))
            if item.get("format") == "html":
                texts.append(html_text(_as_read(raw.strip(), native), None if native else picture))
            else:
                texts.append(raw)
        elif item.get("type") == "table":
            texts.append(render_table(item))
        elif item.get("type") == "image":
            number += 1
            if native:
                texts.append(f"[[picture {number}]]")
    text = texts[0] if len(texts) == 1 else "\n\n".join(t for t in texts if t)
    return text if "[[picture " in text else ""
