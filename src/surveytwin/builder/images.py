"""Turning a question's image reference into something a model can be sent.

A draft question names its image with ``image_id``. An application passes an
``image_resolver`` that maps its own references to an image URL or a
``data:`` URI (the website maps its stored image IDs to data URIs). Uploads,
storage, sizes and permissions stay with the application.

Without a resolver, ``default_image_resolver`` accepts an ``https://`` or
``http://`` URL, a ``data:image/...`` URI, or the path of a PNG, JPEG, WEBP or
GIF file on this computer. A reference nothing resolves is kept and reported by
``Survey.check()``; a run stops on it unless ``accept_missing_images=True``.
"""

from __future__ import annotations

import base64
from pathlib import Path
from typing import Callable

ImageResolver = Callable[[str], "str | None"]

IMAGE_TYPES = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp", ".gif": "image/gif"}


def default_image_resolver(reference: str) -> str | None:
    ref = str(reference or "").strip()
    if not ref:
        return None
    lowered = ref.lower()
    if lowered.startswith(("https://", "http://")) or lowered.startswith("data:image/"):
        return ref
    path = Path(ref).expanduser()
    mime = IMAGE_TYPES.get(path.suffix.lower())
    if mime and path.is_file():
        return f"data:{mime};base64,{base64.b64encode(path.read_bytes()).decode('ascii')}"
    return None


def resolve_image(reference: str, resolver: ImageResolver | None) -> tuple[str | None, str]:
    """(image source or None, why it did not resolve)."""
    use = resolver or default_image_resolver
    try:
        src = use(reference)
    except Exception as exc:  # noqa: BLE001 - an application's resolver failing is a missing image
        return None, f"the image resolver failed: {type(exc).__name__}: {exc}"
    if not src:
        return None, "the image resolver returned nothing"
    return str(src), ""
