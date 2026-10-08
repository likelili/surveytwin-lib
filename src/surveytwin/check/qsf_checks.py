"""Survey checks that need the raw QSF: unsupported traits and image reachability.

Ported from the website's ``services/v3/qsf_preview.py`` (the check parts only;
the website's preview screen code is not part of the library).
"""

from __future__ import annotations

import html
import json
import re
import urllib.error
import urllib.request
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from bs4 import BeautifulSoup



_HTML_TAG_RE = re.compile(r"<[^>]+>")
_LM_PLACEHOLDER_RE = re.compile(r"\$\{lm://[^}]+\}")
_ED_PLACEHOLDER_RE = re.compile(r"\$\{e://[^}]+\}")
_ED_FIELD_PLACEHOLDER_RE = re.compile(r"\$\{e://Field/([^}]+)\}")
_SAFE_EMBEDDED_FIELD_RE = re.compile(r"^[A-Za-z0-9._-]+$")

# Question types with parser branches (see docs/reference/qsf-support-matrix.md).
# Anything else is kept as an inert unsupported question: warned here, skipped at runtime.
_SUPPORTED_QUESTION_TYPES = {"MC", "TE", "DB", "Matrix", "Slider", "CS", "Rank", "RO", "SBS", "Captcha", "Timing", "Meta"}
# Survey-flow element types the parser understands; unknown types (Authenticator,
# Quota, TableOfContents, ReferenceSurvey, SupplementalData, Conjoint, ...) are
# skipped by the runtime, so their presence deserves an upload warning.
_KNOWN_FLOW_TYPES = {"Root", "Standard", "Block", "BlockRandomizer", "Branch", "EmbeddedData", "Group", "EndSurvey", "WebService"}
# Piped-value families the simulation cannot resolve; they reach twins as literal text.
_UNSUPPORTED_PIPE_RE = re.compile(r"\$\{(date|geoip|loc|gr|quota|m|panels)://", re.IGNORECASE)
# Qualtrics math operations: $e{...} expressions are never computed.
_MATH_OPERATION_RE = re.compile(r"\$e\{")
# Carry-forward metadata keys: choices/answers built from a prior answer at runtime.
_CARRY_FORWARD_KEYS = ("DynamicChoices", "DynamicChoicesData", "DynamicAnswers")
_IMAGE_CHECK_LIMIT = 20
_IMAGE_SOURCE_KEYS = {
    "src",
    "data-src",
    "data-imageurl",
    "ImageLocation",
    "imageLocation",
    "Graphics",
    "graphics",
    "Graphic",
    "graphic",
    "URL",
    "Url",
    "Source",
    "source",
}
_IMAGE_AUTH_TEXT_RE = re.compile(
    r"\b(login|log in|sign in|authentication|unauthorized|forbidden)\b",
    re.IGNORECASE,
)
ImageUrlChecker = Callable[[str], dict[str, str]]

# Every image a run would send is checked before it starts (the website's run gate).
_IMAGE_GATE_CHECK_LIMIT = 200
_IMAGE_CHECK_CONCURRENCY = 8
BLOCKING_IMAGE_STATUSES = frozenset({"inaccessible", "auth_required"})


def _clean_text(value: str) -> str:
    cleaned = html.unescape(str(value or ""))
    cleaned = _HTML_TAG_RE.sub(" ", cleaned)
    cleaned = _LM_PLACEHOLDER_RE.sub("", cleaned)
    cleaned = _ED_PLACEHOLDER_RE.sub("", cleaned)
    cleaned = re.sub(r"\s{2,}", " ", cleaned).strip()
    return cleaned


def annotate_embedded_data_for_preview(text: str) -> str:
    """Preview/UX only (never runtime): replace an unresolved ``${e://Field/<f>}``
    reference with a labeled placeholder so a reviewer reading the prompt preview
    does not conclude the twin is shown the raw ``${...}`` code or the embedded-
    data field name. At runtime the staged runtime resolves these to the real
    per-scenario values (verified — e.g. junk_fees twins see actual fee
    descriptions, never ``${...}``), so this changes ONLY the previewed text."""
    return _ED_FIELD_PLACEHOLDER_RE.sub(
        lambda m: f"[embedded-data option: {m.group(1).strip()} — resolved per scenario at runtime]",
        str(text or ""),
    )


def _normalize_question_type(question: dict[str, Any]) -> str:
    raw = str(question.get("QuestionType", "open_text")).strip().lower()
    settings = question.get("Settings", {})
    if not isinstance(settings, dict):
        settings = {}

    text = str(question.get("QuestionText", "")).strip().lower()
    content_type = str(settings.get("ContentType", "") or "").strip().lower()
    selector = str(settings.get("Selector", "") or "").strip().lower()

    if raw == "mc":
        return "single_choice"
    if raw == "matrix":
        return "matrix"
    if raw in {"slider", "cs"}:
        return "numeric"
    if raw == "rank":
        return "single_choice"
    if raw == "db":
        return "descriptive"
    if raw == "te":
        numeric_hints = ["how many", "number of", "count", "0-10", "0 to 10", "from 0 to"]
        if content_type in {"numeric", "number", "integer", "float"}:
            return "numeric"
        if selector in {"sl", "form"} and any(hint in text for hint in numeric_hints):
            return "numeric"
        return "open_text"
    return raw or "open_text"


def detect_unsupported_qsf_traits(content: bytes) -> list[str]:
    """Return user-facing warnings for unsupported QSF traits detected in raw upload content."""
    try:
        qsf = json.loads(content.decode("utf-8"))
    except Exception:
        return []

    detected_question_js = 0
    detected_web_services = 0
    detected_carry_forward = 0
    detected_custom_validation = 0
    detected_math_operations = False
    unsupported_question_types: dict[str, int] = {}
    unknown_flow_types: set[str] = set()
    unsupported_pipe_families: set[str] = set()
    defined_embedded_fields: set[str] = set()
    referenced_embedded_fields: set[str] = set()

    def walk(value: Any) -> None:
        nonlocal detected_question_js, detected_web_services, detected_carry_forward
        nonlocal detected_custom_validation, detected_math_operations

        if isinstance(value, str):
            for match in _ED_FIELD_PLACEHOLDER_RE.finditer(value):
                field = str(match.group(1) or "").strip()
                if field:
                    referenced_embedded_fields.add(field)
            for match in _UNSUPPORTED_PIPE_RE.finditer(value):
                unsupported_pipe_families.add(str(match.group(1)).lower())
            if _MATH_OPERATION_RE.search(value):
                detected_math_operations = True
            return

        if isinstance(value, list):
            for item in value:
                walk(item)
            return

        if not isinstance(value, dict):
            return

        flow_type = str(value.get("Type", "")).strip()
        if flow_type == "WebService":
            detected_web_services += 1
        elif flow_type and "FlowID" in value and flow_type not in _KNOWN_FLOW_TYPES:
            unknown_flow_types.add(flow_type)

        if str(value.get("Element", "")).strip() == "SQ":
            payload = value.get("Payload")
            if isinstance(payload, dict):
                question_type = str(payload.get("QuestionType", "")).strip()
                if question_type and question_type not in _SUPPORTED_QUESTION_TYPES:
                    unsupported_question_types[question_type] = (
                        unsupported_question_types.get(question_type, 0) + 1
                    )

        embedded_rows = value.get("EmbeddedData")
        if flow_type == "EmbeddedData" and isinstance(embedded_rows, list):
            for row in embedded_rows:
                if not isinstance(row, dict):
                    continue
                field = str(row.get("Field", "")).strip()
                if field and "Value" in row:
                    defined_embedded_fields.add(field)

        for key, child in value.items():
            if key == "QuestionJS" and isinstance(child, str) and child.strip():
                detected_question_js += 1
            if key in _CARRY_FORWARD_KEYS and child:
                detected_carry_forward += 1
            if key == "CustomValidation" and child:
                detected_custom_validation += 1
            walk(child)

    walk(qsf)

    warnings: list[str] = []
    if detected_question_js > 0:
        noun = "question uses" if detected_question_js == 1 else f"{detected_question_js} questions use"
        warnings.append(
            f"{noun} Qualtrics JavaScript that the pipeline cannot execute."
        )
    if detected_web_services > 0:
        noun = "flow element calls an external web service" if detected_web_services == 1 else f"{detected_web_services} flow elements call external web services"
        warnings.append(
            f"{noun} the pipeline cannot reach."
        )

    missing_fields = sorted(
        field for field in referenced_embedded_fields
        if field not in defined_embedded_fields
    )
    if missing_fields:
        safe_examples = [field for field in missing_fields if _SAFE_EMBEDDED_FIELD_RE.fullmatch(field)][:4]
        field_list = ", ".join(safe_examples) if safe_examples else f"{len(missing_fields)} field(s)"
        warnings.append(
            f"References embedded-data fields that aren\u2019t defined in the QSF: {field_list}. "
            "These are likely set by an external CSV or contact list."
        )

    if unsupported_question_types:
        type_list = ", ".join(
            f"{qtype} \u00d7{count}" if count > 1 else qtype
            for qtype, count in sorted(unsupported_question_types.items())
        )
        warnings.append(
            f"Contains unsupported question type(s) the pipeline will skip rather than simulate: {type_list}."
        )

    if unknown_flow_types:
        flow_list = ", ".join(sorted(unknown_flow_types))
        warnings.append(
            f"Survey flow contains unsupported element type(s) that will be skipped: {flow_list}."
        )

    if detected_carry_forward > 0:
        noun = "question uses" if detected_carry_forward == 1 else f"{detected_carry_forward} questions use"
        warnings.append(
            f"{noun} Carry Forward choices; the pipeline cannot rebuild choices from prior answers, "
            "so the affected questions may show incomplete choice lists."
        )

    if unsupported_pipe_families:
        family_labels = {
            "date": "date/time", "geoip": "GeoIP location", "loc": "latitude/longitude",
            "gr": "scoring", "quota": "quota", "m": "contact list", "panels": "panel",
        }
        family_list = ", ".join(sorted(family_labels.get(f, f) for f in unsupported_pipe_families))
        warnings.append(
            f"Pipes values from sources the simulation cannot resolve ({family_list}); "
            "these appear to twins as literal placeholder text."
        )

    if detected_math_operations:
        warnings.append(
            "Uses Qualtrics math operations ($e{...}) that are not computed; formulas appear as literal text."
        )

    if detected_custom_validation > 0:
        noun = "question declares" if detected_custom_validation == 1 else f"{detected_custom_validation} questions declare"
        warnings.append(
            f"{noun} custom validation the pipeline does not execute; only standard response requirements are enforced."
        )

    return warnings


def _normalize_brand_host(survey_brand_id: Any) -> str:
    brand = _clean_text(str(survey_brand_id or ""))
    if not brand:
        return ""
    brand = re.sub(r"^https?://", "", brand).rstrip("/")
    if "/" in brand:
        brand = brand.split("/", 1)[0]
    if not brand:
        return ""
    return brand if "." in brand else f"{brand}.qualtrics.com"


def _resolve_qsf_image_ref(value: Any, survey_brand_id: str) -> str:
    src = html.unescape(str(value or "")).strip()
    if not src or src.startswith("data:"):
        return ""
    if src.startswith("//"):
        return f"https:{src}"
    if re.match(r"^https?://", src):
        return src

    host = _normalize_brand_host(survey_brand_id)
    if src.startswith("/"):
        return f"https://{host}{src}" if host else ""
    if "Graphic.php?" in src and host:
        normalized_path = src if src.startswith("/") else f"/{src.lstrip('/')}"
        return f"https://{host}{normalized_path}"
    if src.startswith("IM_") and host:
        return f"https://{host}/CP/Graphic.php?IM={src}"
    return ""


def _survey_brand_id_from_qsf(qsf: dict[str, Any]) -> str:
    entry = qsf.get("SurveyEntry") if isinstance(qsf.get("SurveyEntry"), dict) else {}
    brand = str(entry.get("SurveyBrandID", "") or "").strip()
    if brand:
        return brand

    for element in qsf.get("SurveyElements", []):
        if not isinstance(element, dict):
            continue
        payload = element.get("Payload") if isinstance(element.get("Payload"), dict) else {}
        brand = str(payload.get("SurveyBrandID", "") or "").strip()
        if brand:
            return brand
    return ""


def _img_refs_from_html(value: str) -> list[str]:
    raw = str(value or "")
    if "<img" not in raw.lower():
        return []
    soup = BeautifulSoup(html.unescape(raw), "html.parser")
    refs: list[str] = []
    for tag in soup.find_all("img"):
        ref = str(
            tag.get("src")
            or tag.get("data-src")
            or tag.get("data-imageurl")
            or ""
        ).strip()
        if ref:
            refs.append(ref)
    return refs


def _raw_qsf_image_references(content: bytes) -> list[dict[str, str]]:
    try:
        qsf = json.loads(content.decode("utf-8"))
    except Exception:
        return []
    if not isinstance(qsf, dict):
        return []

    survey_brand_id = _survey_brand_id_from_qsf(qsf)
    refs: list[dict[str, str]] = []
    seen: set[tuple[str, str, str]] = set()

    def add(raw_ref: Any, question_id: str) -> None:
        raw = str(raw_ref or "").strip()
        if not raw:
            return
        resolved = _resolve_qsf_image_ref(raw, survey_brand_id)
        key = (raw, resolved, question_id)
        if key in seen:
            return
        seen.add(key)
        refs.append(
            {
                "raw": raw,
                "resolved": resolved,
                "question_id": question_id,
            }
        )

    def walk(value: Any, current_question_id: str = "") -> None:
        if isinstance(value, list):
            for item in value:
                walk(item, current_question_id)
            return
        if isinstance(value, str):
            for ref in _img_refs_from_html(value):
                add(ref, current_question_id)
            return
        if not isinstance(value, dict):
            return

        question_id = str(value.get("QuestionID", "") or current_question_id).strip()
        image_payload = value.get("Image")
        if image_payload is not None:
            walk(image_payload, question_id)
        for key, raw_value in value.items():
            if key in _IMAGE_SOURCE_KEYS:
                add(raw_value, question_id)
            elif isinstance(raw_value, str):
                for ref in _img_refs_from_html(raw_value):
                    add(ref, question_id)
            else:
                walk(raw_value, question_id)

    walk(qsf)
    return refs


def _check_image_url(url: str) -> dict[str, str]:
    def request(method: str) -> tuple[int, str, bytes]:
        headers = {
            "Accept": "image/*,*/*;q=0.8",
            "User-Agent": "SurveyTwin-QSF-Preview/1.0",
        }
        if method == "GET":
            headers["Range"] = "bytes=0-1023"
        req = urllib.request.Request(url, method=method, headers=headers)
        with urllib.request.urlopen(req, timeout=3) as response:
            body = response.read(1024) if method == "GET" else b""
            return (
                int(getattr(response, "status", 200) or 200),
                str(response.headers.get("Content-Type", "") or "").lower(),
                body,
            )

    try:
        try:
            status, content_type, body = request("HEAD")
        except urllib.error.HTTPError as exc:
            if exc.code not in {405, 501}:
                raise
            status, content_type, body = request("GET")

        if status in {401, 403}:
            return {"status": "auth_required", "detail": f"HTTP {status}"}
        if status < 200 or status >= 300:
            return {"status": "inaccessible", "detail": f"HTTP {status}"}
        if content_type.startswith("image/"):
            return {"status": "supported", "detail": content_type}
        if not body:
            status, content_type, body = request("GET")
        body_text = body[:1024].decode("utf-8", errors="ignore")
        if _IMAGE_AUTH_TEXT_RE.search(body_text):
            return {"status": "auth_required", "detail": content_type or "login page"}
        if content_type.startswith("image/"):
            return {"status": "supported", "detail": content_type}
        if not content_type:
            return {"status": "supported", "detail": "reachable"}
        return {"status": "inaccessible", "detail": f"returned {content_type}"}
    except urllib.error.HTTPError as exc:
        if exc.code in {401, 403}:
            return {"status": "auth_required", "detail": f"HTTP {exc.code}"}
        return {"status": "inaccessible", "detail": f"HTTP {exc.code}"}
    except Exception as exc:
        return {"status": "inaccessible", "detail": exc.__class__.__name__}


def _image_warning_message(
    *,
    status: str,
    label: str,
    url: str,
    detail: str,
    location_count: int = 1,
) -> str:
    suffix = f" ({location_count} locations)" if location_count > 1 else ""
    if status == "supported":
        return f"Image found and supported: {label}{suffix}. It will be sent to the multimodal model."
    if status == "auth_required":
        return (
            f"Image requires Qualtrics auth: {label}{suffix}. "
            f"The URL may not be visible to the model ({detail})."
        )
    if status == "inaccessible":
        return (
            f"Image URL inaccessible: {label}{suffix}. "
            f"The model may not see it ({detail})."
        )
    if url:
        return f"Image skipped: {label}{suffix}. The URL was not usable by the runtime."
    return f"Image skipped: {label}{suffix}. The QSF image reference could not be resolved."


def _safe_image_check(checker: ImageUrlChecker, url: str) -> dict[str, str]:
    try:
        result = checker(url)
    except Exception as exc:  # a checker crash must read as "could not verify", never abort the run
        return {"status": "inaccessible", "detail": exc.__class__.__name__}
    return result if isinstance(result, dict) else {"status": "inaccessible", "detail": "no result"}


def survey_image_warnings(
    definition: dict[str, Any],
    *,
    raw_content: bytes = b"",
    url_checker: ImageUrlChecker | None = None,
    check_limit: int = _IMAGE_CHECK_LIMIT,
    concurrency: int = 1,
) -> list[dict[str, str]]:
    """The image checks of a survey definition: every picture its questions show (as
    written), each address checked once."""
    from ..engine import render
    from ..results.layout import Layout

    questions = [shown for _block, block_questions in Layout(definition).placements() for shown in block_questions]
    sequence = render.sequence_media(questions)
    return image_warnings(sequence, raw_content=raw_content, url_checker=url_checker, check_limit=check_limit, concurrency=concurrency)


def image_warnings(
    sequence: list[dict[str, Any]],
    *,
    raw_content: bytes = b"",
    url_checker: ImageUrlChecker | None = None,
    check_limit: int = _IMAGE_CHECK_LIMIT,
    concurrency: int = 1,
) -> list[dict[str, str]]:
    """Image checks for the pictures of a survey text (``sequence``: its media list)."""
    checker = url_checker or _check_image_url
    grouped: dict[str, dict[str, Any]] = {}
    for sequence_item in sequence:
        if not isinstance(sequence_item, dict):
            continue
        prompt_label = str(sequence_item.get("prompt_label", "") or "").strip()
        question_id = str(sequence_item.get("question_id", "") or "").strip()
        for media in sequence_item.get("media", []):
            if not isinstance(media, dict):
                continue
            src = str(media.get("src", "") or "").strip()
            if not src:
                continue
            context = str(media.get("context", "question") or "question").strip()
            label = str(media.get("label", "") or "").strip() or prompt_label or question_id or src
            item = grouped.setdefault(
                src,
                {
                    "url": src,
                    "label": label,
                    "context": context,
                    "question_id": question_id,
                    "prompt_label": prompt_label,
                    "locations": 0,
                },
            )
            item["locations"] = int(item.get("locations", 0)) + 1

    warnings: list[dict[str, str]] = []
    # URLs to check, in order, up to the limit. With concurrency > 1 they are
    # fetched in a small thread pool: every dead link costs a full timeout, so
    # checking a survey's images one by one took seconds per image.
    to_check = [src for src in grouped if re.match(r"^https?://", src)][: max(0, int(check_limit))]
    results: dict[str, dict[str, str]] = {}
    if to_check and int(concurrency) > 1:
        with ThreadPoolExecutor(max_workers=min(int(concurrency), len(to_check))) as pool:
            for src, result in zip(to_check, pool.map(lambda url: _safe_image_check(checker, url), to_check)):
                results[src] = result
    checkable = set(to_check)
    for src, item in grouped.items():
        label = str(item.get("label", "") or src)
        if not re.match(r"^https?://", src):
            status = "skipped"
            detail = "not an absolute URL"
        elif src not in checkable:
            status = "skipped"
            detail = f"URL check limit of {check_limit} reached"
        else:
            result = results.get(src) or _safe_image_check(checker, src)
            status = str(result.get("status", "inaccessible") or "inaccessible")
            detail = str(result.get("detail", "") or "").strip()
            if status not in {"supported", "auth_required", "inaccessible", "skipped"}:
                status = "inaccessible"
        warnings.append(
            {
                "status": status,
                "label": label,
                "url": src,
                "context": str(item.get("context", "")),
                "question_id": str(item.get("question_id", "")),
                "prompt_label": str(item.get("prompt_label", "")),
                "detail": detail,
                "message": _image_warning_message(
                    status=status,
                    label=label,
                    url=src,
                    detail=detail or status,
                    location_count=int(item.get("locations", 1) or 1),
                ),
            }
        )

    supported_urls = set(grouped.keys())
    for ref in _raw_qsf_image_references(raw_content):
        raw = str(ref.get("raw", "") or "").strip()
        resolved = str(ref.get("resolved", "") or "").strip()
        if resolved and resolved in supported_urls:
            continue
        if resolved:
            continue
        label = str(ref.get("question_id", "") or raw or "Unresolved image reference")
        warnings.append(
            {
                "status": "skipped",
                "label": label,
                "url": resolved,
                "context": "raw_qsf",
                "question_id": str(ref.get("question_id", "")),
                "prompt_label": "",
                "detail": raw,
                "message": _image_warning_message(
                    status="skipped",
                    label=label,
                    url=resolved,
                    detail=raw,
                ),
            }
        )

    deduped: list[dict[str, str]] = []
    seen: set[tuple[str, str, str]] = set()
    for warning in warnings:
        key = (
            str(warning.get("status", "")),
            str(warning.get("url", "")),
            str(warning.get("label", "")),
        )
        if key in seen:
            continue
        seen.add(key)
        deduped.append(warning)
    return deduped




def image_gate_findings(
    *,
    definition: dict[str, Any],
    content: bytes,
    url_checker: ImageUrlChecker | None = None,
) -> list[dict[str, str]]:
    """Every image the survey (its blocks as run) would show the model, with its reachability."""
    if not isinstance(definition, dict):
        return []
    return survey_image_warnings(
        definition,
        raw_content=content,
        url_checker=url_checker,
        check_limit=_IMAGE_GATE_CHECK_LIMIT,
        concurrency=_IMAGE_CHECK_CONCURRENCY,
    )


def blocking_image_findings(findings: list[dict[str, str]]) -> list[dict[str, str]]:
    """The images a run must not start with: unreachable, or behind a Qualtrics login."""
    return [item for item in findings if str(item.get("status", "")) in BLOCKING_IMAGE_STATUSES]
