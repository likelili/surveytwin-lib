"""Utility functions for processing Qualtrics QSF question types."""

import html
import logging
import re
from typing import Any, Dict

from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)


def _normalize_text(value: Any) -> str:
    """Normalize HTML-escaped text into compact plain text."""
    normalized = html.unescape(str(value or ""))
    normalized = normalized.replace("\xa0", " ")
    normalized = re.sub(r"\s+", " ", normalized).strip()
    return normalized


def strip_html_table(text: str) -> str:
    """Strip HTML table into readable key-value format or remove all tags."""
    try:
        processed_text = html.unescape(text)
        soup = BeautifulSoup(processed_text, "html.parser")

        # Try parsing table headers and values
        headers = [th.get_text(strip=True) for th in soup.find_all("th")]
        first_row = soup.find("tbody")
        if not headers or not first_row:
            raise ValueError("No table detected")

        row = first_row.find("tr")
        if not row:
            raise ValueError("No <tr> in table")

        values = [td.get_text(strip=True) for td in row.find_all("td")]
        if len(headers) != len(values):
            raise ValueError("Header/value mismatch")

        # Format as text output
        parsed_dict = dict(zip(headers, values, strict=False))
        return 0, ". ".join(f"{k}: {v}" for k, v in parsed_dict.items())

    except Exception:
        return 1, ""


def strip_html(text: str) -> str:
    """Strip HTML tags from text and normalize whitespace."""
    if not text:
        return ""
    text = html.unescape(text)
    text = re.sub(r"<[^>]*>", " ", text)
    text = text.replace("&nbsp;", " ")
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _normalize_brand_host(survey_brand_id: Any) -> str:
    """Normalize a Qualtrics brand or host into a resolvable hostname."""
    brand = _normalize_text(survey_brand_id)
    if not brand:
        return ""

    brand = re.sub(r"^https?://", "", brand).rstrip("/")
    if "/" in brand:
        brand = brand.split("/", 1)[0]
    if not brand:
        return ""
    if "." in brand:
        return brand
    return f"{brand}.qualtrics.com"


def _qualtrics_graphic_src(value: Any, survey_brand_id: str = "") -> str:
    """Convert Qualtrics image references or IDs into a usable image URL."""
    src = _normalize_text(value)
    if not src:
        return ""

    if src.startswith("//"):
        return f"https:{src}"
    if re.match(r"^https?://", src):
        return src

    host = _normalize_brand_host(survey_brand_id)

    if src.startswith("/"):
        return f"https://{host}{src}" if host else src

    if "Graphic.php?" in src:
        if host:
            normalized_path = src if src.startswith("/") else f"/{src.lstrip('/')}"
            return f"https://{host}{normalized_path}"
        return src

    if src.startswith("IM_") and host:
        return f"https://{host}/CP/Graphic.php?IM={src}"

    return ""


def _append_media(
    output: list[dict[str, str]],
    seen: set[tuple[str, str, str]],
    item: dict[str, str],
) -> None:
    key = (
        item.get("src", ""),
        item.get("alt", ""),
        item.get("title", ""),
    )
    if not key[0] or key in seen:
        return
    seen.add(key)
    output.append(item)


def extract_image_media(value: Any, survey_brand_id: str = "") -> list[dict[str, str]]:
    """Extract image metadata from HTML content and structured Qualtrics image objects."""
    output: list[dict[str, str]] = []
    seen: set[tuple[str, str, str]] = set()

    def walk(item: Any) -> None:
        if not item:
            return

        if isinstance(item, list):
            for nested in item:
                walk(nested)
            return

        if isinstance(item, str):
            soup = BeautifulSoup(html.unescape(item), "html.parser")
            found_tag = False
            for tag in soup.find_all("img"):
                found_tag = True
                src = _qualtrics_graphic_src(
                    tag.get("src")
                    or tag.get("data-src")
                    or tag.get("data-imageurl")
                    or "",
                    survey_brand_id=survey_brand_id,
                )
                if not src:
                    continue

                media_item = {
                    "kind": "image",
                    "src": src,
                }
                alt = _normalize_text(tag.get("alt", ""))
                title = _normalize_text(tag.get("title", ""))
                if alt:
                    media_item["alt"] = alt
                if title:
                    media_item["title"] = title
                _append_media(output, seen, media_item)

            if found_tag:
                return

            direct_src = _qualtrics_graphic_src(item, survey_brand_id=survey_brand_id)
            if direct_src:
                _append_media(output, seen, {"kind": "image", "src": direct_src})
            return

        if isinstance(item, dict):
            if item.get("Image") is not None:
                walk(item.get("Image"))

            src = _qualtrics_graphic_src(
                item.get("src")
                or item.get("data-src")
                or item.get("data-imageurl")
                or item.get("ImageLocation")
                or item.get("imageLocation")
                or item.get("Graphics")
                or item.get("graphics")
                or item.get("Graphic")
                or item.get("graphic")
                or item.get("URL")
                or item.get("Url")
                or item.get("Source")
                or item.get("source")
                or "",
                survey_brand_id=survey_brand_id,
            )
            if src:
                media_item = {
                    "kind": "image",
                    "src": src,
                }
                alt = _normalize_text(item.get("alt") or item.get("Alt") or "")
                title = _normalize_text(
                    item.get("title")
                    or item.get("Title")
                    or item.get("GraphicsDescription")
                    or item.get("graphicsDescription")
                    or item.get("Display")
                    or ""
                )
                if alt:
                    media_item["alt"] = alt
                if title:
                    media_item["title"] = title
                _append_media(output, seen, media_item)

            for text_key in ("Display", "Text", "Description", "Label", "QuestionText"):
                nested_text = item.get(text_key)
                if isinstance(nested_text, str):
                    walk(nested_text)
            return

    walk(value)
    return output


def default_order(options_id: list) -> bool:
    """Check if the options are in default order."""
    return options_id == list(str(i) for i in range(1, len(options_id) + 1))


def get_common_settings(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Get common settings for all question types."""
    return {
        "Selector": payload.get("Selector", ""),
        "SubSelector": payload.get("SubSelector", ""),
        "ForceResponse": payload.get("Validation", {}).get("Settings", {}).get("ForceResponse", ""),
    }
