"""Readable printing for the objects people print while setting up a run.

A ``Listing`` is an ordinary list that prints as a table; everything else
here renders one object as a few lines of plain text.
"""

from __future__ import annotations

from typing import Any, Callable


class Listing(list):
    """A list that prints as a table (``print(survey.preview_blocks())``)."""

    def __init__(self, items: Any = (), *, title: str = "", render: Callable[[list[Any]], list[str]] | None = None) -> None:
        super().__init__(items)
        self._title = title
        self._render = render

    def __str__(self) -> str:
        lines = [self._title] if self._title else []
        lines += self._render(list(self)) if self._render else [str(item) for item in self]
        return "\n".join(lines)


def table(rows: list[list[str]], indent: str = "  ") -> list[str]:
    """Left-aligned columns; the last column is not padded."""
    if not rows:
        return []
    widths = [max(len(r[i]) for r in rows if i < len(r)) for i in range(max(len(r) for r in rows))]
    out = []
    for r in rows:
        cells = [c.ljust(widths[i]) if i < len(r) - 1 else c for i, c in enumerate(r)]
        out.append((indent + "  ".join(cells)).rstrip())
    return out


def short_list(values: list[str], limit: int = 8) -> str:
    values = [str(v) for v in values]
    if len(values) <= limit:
        return ", ".join(values)
    return ", ".join(values[:limit]) + f", and {len(values) - limit} more"


def plural(n: int, word: str) -> str:
    return f"{n:,} {word}" if n == 1 else f"{n:,} {word}s"
