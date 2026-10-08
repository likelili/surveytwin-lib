"""HTML question text as plain text that keeps its structure.

Used for every HTML question text, read from a QSF or written in the standard
format. Its lines are laid out as a browser shows them: a ``<br>`` ends a line, a
``<div>`` (or a list item) is a line of its own, a paragraph (or a heading, a
list, a table) is set apart by a blank line, and the line breaks of the HTML
source itself are spaces (except inside ``<pre>``). A list item starts with "- ",
or with its number in a numbered list.
A table keeps every header, row and cell (rows and columns with no text at all
are left out): a heading row (in ``<thead>``, or all ``<th>``) with one row of
values reads as "Heading: value. Heading: value", on one line, as the QSF reader
wrote such a table before, and so does a
two-column table with ``<th>`` labels down its first column; a longer table with
labels down the first column gives one "Label: values" line per row; two rows
with no heading cells keep their rows as lines; any other table becomes a
Markdown table. A table inside a table cell is read row by row into that cell,
separated by "; ".
"""

from __future__ import annotations

import re
from typing import Any, Callable

from bs4 import BeautifulSoup, NavigableString, Tag

# set apart by a blank line
_PARAGRAPH = {"p", "blockquote", "h1", "h2", "h3", "h4", "h5", "h6", "ul", "ol", "dl", "pre", "hr"}
# a line of its own
_LINE = {"div", "section", "article", "header", "footer", "center", "li", "dt", "dd", "tr"}
# break marks, laid out by ``_layout``
_SOFT, _PARA = "\x00", "\x01"
# What stands where a picture sits: called with the <img> tag (see html_text).
Picture = Callable[[Tag], "str | None"] | None
_SKIP = {"script", "style", "head", "title", "colgroup", "col"}


def has_table(text: Any) -> bool:
    return isinstance(text, str) and "<table" in text.lower()


def _inline(text: str) -> str:
    return re.sub(r"\s+", " ", text.replace("\xa0", " "))


def _rows(table: Tag) -> list[Tag]:
    """The table's own rows (not those of a table nested in a cell)."""
    rows = []
    for row in table.find_all("tr"):
        if row.find_parent("table") is table:
            rows.append(row)
    return rows


def _cell_text(cell: Tag, picture: Picture = None) -> str:
    parts: list[str] = []
    for child in cell.children:
        if isinstance(child, Tag) and child.name == "table":
            nested = [" ".join(_inline(c.get_text(" ")).strip() for c in row.find_all(["td", "th"]) if _inline(c.get_text(" ")).strip()) for row in _rows(child)]
            parts.append("; ".join(n for n in nested if n))
        elif isinstance(child, Tag):
            parts.append(_render(child, picture=picture))
        else:
            parts.append(str(child))
    # lines inside a cell (a <br>, a paragraph) are kept apart with "; "
    lines = [_inline(line).strip() for line in "".join(parts).split("\n")]
    return "; ".join(line for line in lines if line)


def _table(table: Tag, picture: Picture = None) -> str:
    grid = []  # each kept row's cell texts
    heads = []  # each kept row's cells: whether it is a heading cell (<th>)
    in_thead = []
    for row in _rows(table):
        cells = [c for c in row.find_all(["td", "th"]) if c.find_parent("tr") is row]
        values: list[str] = []
        marks: list[bool] = []
        for cell in cells:
            text = _cell_text(cell, picture)
            span = cell.get("colspan")
            try:
                span = max(1, min(int(str(span)), 50)) if span else 1
            except ValueError:
                span = 1
            values += [text] + [""] * (span - 1)
            marks += [cell.name == "th"] * span
        if any(values):
            grid.append(values)
            heads.append(marks)
            in_thead.append(row.find_parent("thead") is not None)
    if not grid:
        return ""
    width = max(len(row) for row in grid)
    grid = [row + [""] * (width - len(row)) for row in grid]
    heads = [row + [False] * (width - len(row)) for row in heads]
    keep = [index for index in range(width) if any(row[index] for row in grid)]
    grid = [[row[index] for index in keep] for row in grid]
    heads = [[row[index] for index in keep] for row in heads]
    if len(grid) == 1:
        return " | ".join(cell for cell in grid[0] if cell)
    # Which cells are headings comes from the markup: a heading row is in <thead>,
    # or has only <th> cells; labels down the first column are a <th> on every row
    # (a first row whose only text is its label is one of them).
    first = [mark for cell, mark in zip(grid[0], heads[0]) if cell]
    label_column = all(row[0] and mark[0] for row, mark in zip(grid, heads))
    heading_row = in_thead[0] or (all(first) and not (label_column and len(first) == 1))
    if label_column and not heading_row:
        if len(keep) == 2:
            # one record read down the table: "Label: value" pairs, as across it
            return ". ".join(f"{label}: {value}" if value else label for label, value in grid)
        return "\n".join(f"{row[0]}: " + " | ".join(row[1:]) for row in grid)
    if len(grid) == 2 and heading_row and not label_column:
        # a heading row and one row of values: "Heading: value" pairs
        return ". ".join(f"{head}: {value}" if head else value for head, value in zip(*grid) if head or value)
    if len(grid) == 2 and not heading_row:
        # two rows, nothing marking which cells are headings: each row as it is
        return "\n".join(" | ".join(cell for cell in row if cell) for row in grid)
    cells = [[cell.replace("|", "\\|") for cell in row] for row in grid]
    lines = ["| " + " | ".join(cells[0]) + " |", "|" + "---|" * len(keep)]
    lines += ["| " + " | ".join(row) + " |" for row in cells[1:]]
    return "\n".join(lines)


def _render(node: Any, pre: bool = False, picture: Picture = None) -> str:
    if isinstance(node, NavigableString):
        # the HTML source's own line breaks are spaces, as a browser shows them
        return str(node) if pre else re.sub(r"[ \t\r\n\f\v]+", " ", str(node))
    if not isinstance(node, Tag):
        return ""
    name = (node.name or "").lower()
    if name in _SKIP:
        return ""
    if name == "br":
        return "\n"
    if name == "table":
        return _PARA + _table(node, picture) + _PARA
    if name == "img":
        # a picture: its marker where it sits, when the caller places pictures
        marker = picture(node) if picture is not None else None
        return f" {marker} " if marker else ""
    inner = "".join(_render(child, pre or name == "pre", picture) for child in node.children)
    if name == "li":
        return _SOFT + _marker(node) + inner.strip(" ") + _SOFT
    if name in {"ul", "ol"} and node.find_parent("li") is not None:
        return _SOFT + inner + _SOFT  # a list inside a list item: its items follow on
    if name in _PARAGRAPH:
        return _PARA + inner + _PARA
    if name in _LINE:
        return _SOFT + inner + _SOFT
    return inner


def _marker(item: Tag) -> str:
    """"- " for an item of a bulleted list, "2. " for the second of a numbered one."""
    parent = item.find_parent(["ul", "ol"])
    if parent is None or parent.name != "ol":
        return "- "
    siblings = [li for li in parent.find_all("li") if li.find_parent(["ul", "ol"]) is parent]
    return f"{siblings.index(item) + 1}. "


def _layout(rendered: str) -> str:
    """Text with break marks as lines: a line of its own starts on a new line, a
    paragraph is set apart by one blank line, and a ``<br>`` always ends a line."""
    out = ""
    blank_before_next = False
    for token in re.split(f"({_SOFT}|{_PARA})", rendered):
        if token in (_SOFT, _PARA):
            if out.rsplit("\n", 1)[-1].strip():
                out += "\n"
            blank_before_next = blank_before_next or token == _PARA
            continue
        if blank_before_next and token.strip():
            if out.strip():
                out = out.rstrip(" ") + "\n" * (2 - len(out) + len(out.rstrip("\n")))
            blank_before_next = False
        out += token
    return out


def _lines(text: str) -> str:
    lines = [re.sub(r"[ \t\r\f\v\xa0]+", " ", line).strip() for line in text.split("\n")]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def html_text(html: str, picture: Picture = None) -> str:
    """The text of an HTML fragment, laid out in lines as a browser shows it.
    ``picture(tag)``, when given, names what stands where an ``<img>`` sits (a
    marker; None leaves nothing, as without it)."""
    soup = BeautifulSoup(str(html or ""), "html.parser")
    return _lines(_layout(_render(soup, picture=picture)))


def keep_lines(text: Any) -> str:
    """Text whose line breaks carry meaning, tidied without joining its lines:
    spaces inside a line collapse, blank runs become one empty line."""
    lines = [re.sub(r"[ \t\r\f\v\xa0]+", " ", line).strip() for line in str(text or "").split("\n")]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()
