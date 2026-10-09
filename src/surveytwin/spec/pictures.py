"""Pictures a survey written in the standard format names by a file path.

A picture's ``source`` is a web link (``https://...``), a ``data:image/...``
address, or the path of a picture file. A path is read when the survey is read:
an absolute path as written, a relative path from the folder of the survey's
JSON file (or the ``folder`` given to ``read_survey``). The file must be a PNG,
JPEG, WEBP or GIF picture of at most 20 MB; its type is read from its content.

The survey keeps each path as written (``Survey.standard``). A run sends the
picture inside the request, as a ``data:`` address, because a model cannot open
a file on this computer; a saved run keeps one copy of each picture.
"""

from __future__ import annotations

import base64
import copy
from pathlib import Path
from typing import Any, Iterator

from .validate import Finding

MAX_PICTURE_BYTES = 20 * 1024 * 1024
PICTURE_KINDS = "PNG, JPEG, WEBP or GIF"


def is_file_source(source: Any) -> bool:
    """Whether a picture source names a file (not a web link or a data address)."""
    if not isinstance(source, str) or not source.strip():
        return False
    lowered = source.strip().lower()
    return not lowered.startswith(("http://", "https://", "data:"))


def picture_places(value: Any, path: str = "$") -> Iterator[tuple[str, dict[str, Any]]]:
    """Every picture a definition names, with the path of the field that holds it:
    image content (``{"type": "image", "source": ...}``) and every ``media`` item
    (options, rows, columns, items, conjoint levels)."""
    if isinstance(value, dict):
        if value.get("type") == "image" and isinstance(value.get("source"), str):
            yield path, value
        for key, child in value.items():
            if key == "media" and isinstance(child, list):
                for index, item in enumerate(child):
                    if isinstance(item, dict) and isinstance(item.get("source"), str):
                        yield f"{path}.media[{index}]", item
            elif isinstance(child, (dict, list)):
                yield from picture_places(child, f"{path}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            yield from picture_places(child, f"{path}[{index}]")


def _mime_type(head: bytes) -> str | None:
    """The picture type a file's first bytes show (None when it is none of ours)."""
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if head.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if head.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "image/webp"
    return None


def _read_picture(source: str, folder: Path) -> tuple[str, str]:
    """(the picture as a data address, "") or ("", why it cannot be used)."""
    path = Path(source.strip()).expanduser()
    if not path.is_absolute():
        path = folder / path
    if not path.is_file():
        return "", f"no picture file at {path}"
    size = path.stat().st_size
    if size > MAX_PICTURE_BYTES:
        return "", f"{path} is {size / 1024 / 1024:.1f} MB; a picture can be at most {MAX_PICTURE_BYTES // 1024 // 1024} MB"
    data = path.read_bytes()
    mime = _mime_type(data[:16])
    if mime is None:
        return "", f"{path} is not a {PICTURE_KINDS} picture"
    return f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}", ""


def read_pictures(
    definition: dict[str, Any], folder: Path, known: dict[str, str] | None = None
) -> tuple[dict[str, str], list[Finding]]:
    """Each picture file the definition names, read: ``{source as written: data
    address}``, and a finding for every one that cannot be used. ``folder`` is
    where a relative path starts. A file already in ``known`` (read before) is
    taken from it, not read again."""
    pictures: dict[str, str] = {}
    findings: list[Finding] = []
    failed: set[str] = set()
    for place, item in picture_places(definition):
        source = item["source"]
        if not is_file_source(source) or source in pictures or source in failed:
            continue
        if known and source in known:
            pictures[source] = known[source]
            continue
        address, problem = _read_picture(source, folder)
        if problem:
            failed.add(source)
            findings.append(Finding(f"{place}.source", problem, "picture.file"))
            continue
        pictures[source] = address
    return pictures, findings


def picture_file_address(source: str, folder: Path | None = None) -> str:
    """One picture as a request carries it: a web link or a ``data:`` address as
    given, a picture file read (from ``folder``, or the current folder, when the
    path is relative). Raises ``ConfigError`` when the file cannot be used."""
    if not is_file_source(source):
        return str(source)
    address, problem = _read_picture(str(source), folder or Path.cwd())
    if problem:
        from ..errors import ConfigError

        raise ConfigError(problem)
    return address


def with_pictures(definition: dict[str, Any], pictures: dict[str, str]) -> dict[str, Any]:
    """A copy of the definition with each picture file replaced by the picture it
    holds (its ``data:`` address), as a run sends it."""
    if not pictures:
        return definition
    out = copy.deepcopy(definition)
    for _place, item in picture_places(out):
        if item["source"] in pictures:
            item["source"] = pictures[item["source"]]
    return out
