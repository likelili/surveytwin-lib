"""Pictures a survey written in the standard format names by a file path.

A picture's ``source`` is a web link (``https://...``), a ``data:image/...``
address, or the path of a picture file. A path is read when the survey is read:
an absolute path as written, a relative path from the folder of the survey's
JSON file (or the ``folder`` given to ``read_survey``). The file must be a PNG,
JPEG, WEBP or GIF picture of at most 20 MB; its type is read from its content,
and its structure is checked to its end (a PNG's chunks and their checksums and
its complete image data, a JPEG's segments up to its end marker, a GIF's blocks,
a WEBP's chunks), so a damaged or cut-off file is refused when the survey is read
rather than by the model. A picture written into the survey as a ``data:``
address is checked the same way.

The survey keeps each path as written (``Survey.standard``). A run sends the
picture inside the request, as a ``data:`` address, because a model cannot open
a file on this computer; a saved run keeps one copy of each picture.
"""

from __future__ import annotations

import base64
import binascii
import copy
import re
import zlib
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


# ---------------------------------------------------------------- structure checks
#
# Each returns "" for a complete picture, else what is wrong with it. They read
# the file's own structure (no decoder library): enough to refuse a damaged or
# cut-off file, not a guarantee that every decoder accepts every pixel.


def _chunk_label(kind: bytes) -> str:
    """'its 'IDAT' chunk', or 'a chunk' when the chunk's name is itself damaged."""
    return f"its {kind.decode('latin-1')!r} chunk" if kind.isalpha() else "a chunk"


def _png_problem(data: bytes) -> str:
    pos, first, idat = 8, True, []
    while True:
        if pos + 12 > len(data):
            return "ends before its last chunk (IEND): the file is cut off"
        length = int.from_bytes(data[pos : pos + 4], "big")
        kind = data[pos + 4 : pos + 8]
        if first and (kind != b"IHDR" or length != 13):
            return "does not start with an image header (IHDR)"
        end = pos + 12 + length
        if length > 0x7FFFFFFF or end > len(data):
            return f"ends inside {_chunk_label(kind)}: the file is cut off"
        body = data[pos + 8 : pos + 8 + length]
        if zlib.crc32(kind + body) & 0xFFFFFFFF != int.from_bytes(data[end - 4 : end], "big"):
            return f"has damaged data in {_chunk_label(kind)} (its checksum does not match)"
        if first:
            if not int.from_bytes(body[0:4], "big") or not int.from_bytes(body[4:8], "big"):
                return "has no width or height"
            first = False
        elif kind == b"IDAT":
            idat.append(body)
        elif kind == b"IEND":
            break
        pos = end
    if not idat:
        return "has no image data (IDAT)"
    stream = zlib.decompressobj()
    try:
        stream.decompress(b"".join(idat), 1 << 20)
        while stream.unconsumed_tail:
            stream.decompress(stream.unconsumed_tail, 1 << 20)
    except zlib.error:
        return "has damaged image data"
    return "" if stream.eof else "has incomplete image data: the file is cut off"


# start-of-frame markers (the ones that are not DHT, JPG or DAC)
_JPEG_FRAMES = {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF}


def _jpeg_problem(data: bytes) -> str:
    pos, frame, scan = 2, False, False
    while pos < len(data):
        if data[pos] != 0xFF:
            return "has damaged structure (a segment does not start with a marker)"
        while pos < len(data) and data[pos] == 0xFF:
            pos += 1
        if pos >= len(data):
            break
        marker = data[pos]
        pos += 1
        if marker == 0xD9:  # end of image
            if not frame:
                return "has no image frame"
            return "" if scan else "has no image data"
        if marker == 0x01 or 0xD0 <= marker <= 0xD7:
            continue  # markers with no length
        if pos + 2 > len(data):
            break
        length = int.from_bytes(data[pos : pos + 2], "big")
        if length < 2 or pos + length > len(data):
            return "ends inside a segment: the file is cut off"
        segment = data[pos + 2 : pos + length]
        if marker in _JPEG_FRAMES:
            if len(segment) < 6 or not int.from_bytes(segment[3:5], "big"):
                return "has a damaged image frame"
            frame = True
        pos += length
        if marker == 0xDA:  # start of scan: coded data runs to the next real marker
            scan = True
            while True:
                found = data.find(b"\xff", pos)
                if found < 0 or found + 1 >= len(data):
                    return "ends inside its image data, before its end marker: the file is cut off"
                following = data[found + 1]
                if following == 0x00 or 0xD0 <= following <= 0xD7 or following == 0xFF:
                    pos = found + 1
                    continue
                pos = found
                break
    return "ends before its end marker: the file is cut off"


def _gif_sub_blocks(data: bytes, pos: int) -> int | None:
    """The position after a run of data sub-blocks (None when the file ends inside it)."""
    while pos < len(data):
        size = data[pos]
        pos += 1
        if size == 0:
            return pos
        pos += size
    return None


def _gif_problem(data: bytes) -> str:
    if len(data) < 13:
        return "is too short to be a GIF picture: the file is cut off"
    if not int.from_bytes(data[6:8], "little") or not int.from_bytes(data[8:10], "little"):
        return "has no width or height"
    pos, images, ended = 13, 0, False
    if data[10] & 0x80:
        pos += 3 * (2 ** ((data[10] & 0x07) + 1))
        if pos > len(data):
            return "ends inside its colour table: the file is cut off"
    while pos < len(data):
        block = data[pos]
        pos += 1
        if block == 0x3B:  # trailer
            ended = True
            break
        if block == 0x21:  # an extension: its label, then data sub-blocks
            after = _gif_sub_blocks(data, pos + 1)
        elif block == 0x2C:  # an image: descriptor, colour table, code size, data
            if pos + 9 > len(data):
                return "ends inside an image descriptor: the file is cut off"
            flags = data[pos + 8]
            pos += 9
            if flags & 0x80:
                pos += 3 * (2 ** ((flags & 0x07) + 1))
            after = _gif_sub_blocks(data, pos + 1) if pos < len(data) else None
            images += 1
        else:
            return "has damaged structure (an unknown block)"
        if after is None:
            return "ends inside a block: the file is cut off"
        pos = after
    if images:
        return ""
    return "has no image" if ended else "ends before its first image: the file is cut off"


def _webp_problem(data: bytes) -> str:
    if len(data) < 20:
        return "is too short to be a WEBP picture: the file is cut off"
    end = 8 + int.from_bytes(data[4:8], "little")
    if end > len(data):
        return "is shorter than its header says: the file is cut off"
    pos, image = 12, False
    while pos + 8 <= end:
        kind = data[pos : pos + 4]
        size = int.from_bytes(data[pos + 4 : pos + 8], "little")
        body = data[pos + 8 : pos + 8 + size]
        if pos + 8 + size > end:
            return f"ends inside {_chunk_label(kind.strip())}: the file is cut off"
        if kind == b"VP8 ":
            if size < 10 or body[3:6] != b"\x9d\x01\x2a":
                return "has a damaged image (VP8) chunk"
            if not int.from_bytes(body[6:8], "little") & 0x3FFF or not int.from_bytes(body[8:10], "little") & 0x3FFF:
                return "has no width or height"
            image = True
        elif kind == b"VP8L":
            if size < 5 or body[0] != 0x2F:
                return "has a damaged image (VP8L) chunk"
            image = True
        elif kind == b"VP8X" and size < 10:
            return "has a damaged header (VP8X) chunk"
        elif kind == b"ANMF":
            image = True
        pos += 8 + size + (size & 1)
    return "" if image else "has no image data"


_PROBLEMS = {"image/png": _png_problem, "image/jpeg": _jpeg_problem, "image/gif": _gif_problem, "image/webp": _webp_problem}


def picture_problem(data: bytes) -> tuple[str | None, str]:
    """(the picture's type, "") for a complete PNG, JPEG, GIF or WEBP picture;
    (its type or None, what is wrong) otherwise."""
    mime = _mime_type(data[:16])
    if mime is None:
        return None, f"is not a {PICTURE_KINDS} picture"
    try:
        problem = _PROBLEMS[mime](data)
    except (IndexError, ValueError) as exc:  # a structure the checks could not follow
        problem = f"has damaged structure ({exc})"
    return mime, problem


def _read_picture(source: str, folder: Path) -> tuple[str, str]:
    """(the picture as a data address, "") or ("", why it cannot be used)."""
    path = Path(source.strip()).expanduser()
    if not path.is_absolute():
        path = folder / path
    try:
        if not path.is_file():
            return "", f"no picture file at {path}"
        size = path.stat().st_size
        if size > MAX_PICTURE_BYTES:
            return "", f"{path} is {size / 1024 / 1024:.1f} MB; a picture can be at most {MAX_PICTURE_BYTES // 1024 // 1024} MB"
        data = path.read_bytes()
    except OSError as exc:
        return "", f"{path} cannot be read ({exc.strerror or exc})"
    mime, problem = picture_problem(data)
    if problem:
        return "", f"{path} {problem}"
    return f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}", ""


_DATA_ADDRESS = re.compile(r"^data:(?P<mime>[\w.+/-]+)?(?P<params>(?:;[^;,]*)*),(?P<data>.*)$", re.DOTALL)


def _data_address_problem(source: str) -> str:
    """What is wrong with a picture written into the survey as a ``data:``
    address ("" when it is a complete PNG, JPEG, GIF or WEBP picture)."""
    match = _DATA_ADDRESS.match(source.strip())
    if not match or ";base64" not in match.group("params").lower():
        return "a picture's data: address must hold base64 data (data:image/png;base64,...)"
    try:
        data = base64.b64decode(match.group("data"), validate=False)
    except (binascii.Error, ValueError):
        return "a picture's data: address does not hold valid base64 data"
    if len(data) > MAX_PICTURE_BYTES:
        return f"a picture's data: address holds {len(data) / 1024 / 1024:.1f} MB; a picture can be at most {MAX_PICTURE_BYTES // 1024 // 1024} MB"
    mime, problem = picture_problem(data)
    if problem:
        return f"the picture in this data: address {problem}"
    declared = (match.group("mime") or "").lower()
    if declared and declared != mime and not (declared == "image/jpg" and mime == "image/jpeg"):
        return f"this data: address says {declared}, but the picture is {mime}"
    return ""


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
        if source.strip().lower().startswith("data:") and source not in failed:
            problem = _data_address_problem(source)
            if problem:
                failed.add(source)
                findings.append(Finding(f"{place}.source", problem, "picture.data"))
            continue
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
