"""Qualtrics settings and their meaning in the standard format, both ways.

Each ``*_of`` function reads a setting as the engine reads it (same parsing, same
defaults); each ``*_setting`` function writes the setting for a meaning. The
reader uses the first, the compiler both: it keeps a setting as read while it
still means what the survey says, and writes a new one when it does not.
"""

from __future__ import annotations

import copy
from typing import Any

MULTI_MC_SELECTORS = frozenset({"MAVR", "MAHR", "MACOL", "MSB"})

# selector -> (mode, control)
MC_SELECTORS: dict[str, tuple[str, str]] = {
    "SAVR": ("single", "list"),
    "SAHR": ("single", "horizontal"),
    "SACOL": ("single", "columns"),
    "DL": ("single", "dropdown"),
    "SB": ("single", "select_box"),
    "MAVR": ("multiple", "list"),
    "MAHR": ("multiple", "horizontal"),
    "MACOL": ("multiple", "columns"),
    "MSB": ("multiple", "select_box"),
}
TEXT_SELECTORS: dict[str, str] = {"SL": "single_line", "ML": "multi_line", "ESTB": "essay", "PW": "password"}
SLIDER_SELECTORS: dict[str, str] = {"HSLIDER": "slider", "HBAR": "bar", "STAR": "stars", "NUMBERSCALE": "number_scale"}
SLIDER_SELECTOR_NAMES = {"slider": "HSLIDER", "bar": "HBAR", "stars": "STAR", "number_scale": "NumberScale"}

# Qualtrics content validation <-> the standard text format.
CONTENT_TYPES: dict[str, str] = {
    "ValidNumber": "number",
    "ValidEmail": "email",
    "ValidPhone": "phone",
    "ValidUSPhone": "phone",
    "ValidDate": "date",
    "ValidZip": "zip",
    "ValidUSZip": "zip",
    "ValidUSState": "us_state",
    "ValidTextOnly": "text_only",
}
CONTENT_TYPE_NAMES = {
    "number": "ValidNumber",
    "email": "ValidEmail",
    "phone": "ValidPhone",
    "date": "ValidDate",
    "zip": "ValidZip",
    "us_state": "ValidUSState",
    "text_only": "ValidTextOnly",
}


def positive_int(value: Any) -> int | None:
    """A whole number above 0, read as the engine reads limits (``"3"`` -> 3)."""
    try:
        parsed = int(str(value or "").strip())
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def number_of(value: Any) -> int | float | None:
    """A number as a JSON number (whole numbers as ints); None when it is not one."""
    if isinstance(value, bool):
        return None
    try:
        number = float(str(value).strip())
    except (TypeError, ValueError):
        return None
    if number != number or number in (float("inf"), float("-inf")):
        return None
    return int(number) if number.is_integer() else number


def truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


# ------------------------------------------------------------ required


def required_of(force_response: Any) -> bool | str:
    value = str(force_response or "").strip().upper()
    if value == "ON":
        return True
    if value == "REQUESTRESPONSE":
        return "request"
    return False


def force_response_setting(required: Any) -> str:
    if required is True:
        return "ON"
    if required == "request":
        return "RequestResponse"
    return "OFF"


# ------------------------------------------------------------ choice


def mc_selector_of(selector: Any) -> tuple[str, str | None]:
    """(mode, control) of a multiple-choice selector."""
    name = str(selector or "").strip().upper()
    if name in MC_SELECTORS:
        return MC_SELECTORS[name]
    return ("multiple" if name in MULTI_MC_SELECTORS else "single", None)


def mc_selector_setting(mode: str, control: str | None) -> str:
    for selector, meaning in MC_SELECTORS.items():
        if meaning == (mode, control or "list"):
            return selector
    return "MAVR" if mode == "multiple" else "SAVR"


# ------------------------------------------------------------ text checks


def text_checks_of(settings: dict[str, Any]) -> dict[str, Any]:
    """The checks a text entry's settings make, as the engine applies them:
    ``min_length``, ``max_length``, ``format`` and ``number`` (each only when set)."""
    validation_type = str(settings.get("ValidationType") or settings.get("Type") or "").strip().upper()
    low = positive_int(settings.get("MinChars"))
    high = positive_int(settings.get("TotalChars") or settings.get("MaxChars"))
    if validation_type == "TOTALCHAR":
        low = None
    elif validation_type == "MINCHAR":
        high = None
    elif validation_type == "CONTENTTYPE":
        low = high = None
    out: dict[str, Any] = {}
    if low is not None:
        out["min_length"] = low
    if high is not None:
        out["max_length"] = high
    content_type = str(settings.get("ContentType") or "").strip()
    if content_type:
        out["format"] = CONTENT_TYPES.get(content_type, content_type)
    valid_number = settings.get("ValidNumber")
    if isinstance(valid_number, dict):
        limits = {}
        for key, name in (("Min", "min"), ("Max", "max"), ("NumDecimals", "decimals")):
            if valid_number.get(key) not in (None, ""):
                limits[name] = number_of(valid_number.get(key))
        if limits:
            out["number"] = limits
    return out


def text_checks_settings(checks: dict[str, Any]) -> dict[str, Any]:
    """Settings that make these checks (the inverse of ``text_checks_of``)."""
    low, high = checks.get("min_length"), checks.get("max_length")
    text_format = checks.get("format")
    out: dict[str, Any] = {"ContentType": CONTENT_TYPE_NAMES.get(text_format, text_format) if text_format else ""}
    if text_format and (low or high):
        out["ValidationType"] = ""  # both apply only when neither validation type is named
    elif text_format:
        out["ValidationType"] = "ContentType"
    elif low and high:
        out["ValidationType"] = "CharRange"
    elif low:
        out["ValidationType"] = "MinChar"
    elif high:
        out["ValidationType"] = "TotalChar"
    else:
        out["ValidationType"] = "None"
    if low:
        out["MinChars"] = str(low)
    if high:
        out["TotalChars"] = str(high)
    if isinstance(checks.get("number"), dict) and checks["number"]:
        # as a Qualtrics export writes them: strings ("1", "168"), which the engine
        # passes to the model as written
        names = {"min": "Min", "max": "Max", "decimals": "NumDecimals"}
        out["ValidNumber"] = {names[key]: number_text(value) for key, value in checks["number"].items() if value is not None}
    return out


def number_text(value: Any) -> str:
    """A number as a Qualtrics export writes it: 1 -> "1", 1.5 -> "1.5"."""
    number = number_of(value)
    if number is None:
        return str(value)
    return str(number) if isinstance(number, int) else repr(number)


TEXT_CHECK_KEYS = ("ValidationType", "MinChars", "TotalChars", "MaxChars", "ContentType", "ValidNumber")


# ------------------------------------------------------------ randomization


def _token_draw(token: str) -> str | None:
    if token.startswith("{~") and token.endswith("~}") and len(token) > 4:
        return "subset" if "subset" in token.lower() else "shuffled"
    return None


def randomization_of(raw: Any) -> dict[str, Any] | None:
    """What a question's Qualtrics ``Randomization`` does in the engine; None when nothing."""
    if not isinstance(raw, dict) or not raw:
        return None
    kind = str(raw.get("Type", "") or "").strip()
    if not kind or kind.lower() in {"none", "no", "off"}:
        return None
    advanced = raw.get("Advanced")
    if advanced:
        if not isinstance(advanced, dict):
            return {"order": "source", "qualtrics": copy.deepcopy(raw)}
        reversal = advanced.get("ScaleReversal")
        fixed = advanced.get("FixedOrder")
        if (isinstance(reversal, list) and reversal) or not isinstance(fixed, list) or not fixed:
            return {"order": "source", "qualtrics": copy.deepcopy(raw)}
        out: dict[str, Any] = {"order": "positions", "positions": []}
        for entry in fixed:
            draw = _token_draw(str(entry))
            out["positions"].append({"draw": draw} if draw else str(entry))
        for key, name in (("RandomizeAll", "shuffled"), ("RandomSubSet", "subset"), ("Undisplayed", "hidden")):
            items = [str(item) for item in advanced.get(key) or [] if str(item).strip()]
            if items:
                out[name] = items
        take = positive_int(advanced.get("TotalRandSubset"))
        if take and out.get("subset"):
            out["subset_take"] = take
        return out
    key = kind.lower()
    if key == "all":
        return {"order": "shuffle"}
    if key == "subset":
        take = positive_int(raw.get("TotalRandSubset"))
        return {"order": "shuffle", "take": take} if take else None
    if key == "scalereversal":
        out = {"order": "random_reverse"}
        if truthy(raw.get("ConsistentScaleReversal")):
            out["consistent"] = True
        return out
    return None


def randomization_setting(randomization: dict[str, Any]) -> dict[str, Any]:
    order = randomization.get("order")
    if order == "shuffle":
        if randomization.get("take"):
            return {"Type": "SubSet", "TotalRandSubset": randomization["take"]}
        return {"Type": "All"}
    if order == "random_reverse":
        out: dict[str, Any] = {"Type": "ScaleReversal"}
        if randomization.get("consistent"):
            out["ConsistentScaleReversal"] = True
        return out
    if order == "positions":
        fixed = [
            ("{~SubSet~}" if slot["draw"] == "subset" else "{~Randomized~}") if isinstance(slot, dict) else str(slot)
            for slot in randomization.get("positions") or []
        ]
        advanced: dict[str, Any] = {"FixedOrder": fixed}
        for name, key in (("shuffled", "RandomizeAll"), ("subset", "RandomSubSet"), ("hidden", "Undisplayed")):
            advanced[key] = list(randomization.get(name) or [])
        if randomization.get("subset_take"):
            advanced["TotalRandSubset"] = randomization["subset_take"]
        return {"Type": "Advanced", "Advanced": advanced}
    if order == "source" and isinstance(randomization.get("qualtrics"), dict):
        return copy.deepcopy(randomization["qualtrics"])
    raise ValueError(f"A randomization with order {order!r} cannot be written as a Qualtrics setting")


# ------------------------------------------------------------ blocks


def block_randomization_of(options: Any) -> dict[str, Any] | None:
    if not isinstance(options, dict):
        return None
    mode = str(options.get("RandomizeQuestions", "") or "").strip()
    if mode.lower() in {"", "false", "none", "off", "no", "0"}:
        return None
    randomization = options.get("Randomization")
    advanced = randomization.get("Advanced") if isinstance(randomization, dict) else None
    if isinstance(advanced, dict) and any(
        key != "QuestionsPerPage" and value not in (None, "", [], {}, 0, "0") for key, value in advanced.items()
    ):
        return {"order": "source", "qualtrics": {"RandomizeQuestions": options.get("RandomizeQuestions"), "Randomization": copy.deepcopy(randomization)}}
    return {"order": "shuffle"}


def repeat_of(options: Any) -> dict[str, Any] | None:
    """A block's loop as the engine runs it: rows in ID order, shuffled or cut."""
    if not isinstance(options, dict):
        return None
    looping = options.get("LoopingOptions") if isinstance(options.get("LoopingOptions"), dict) else {}
    static = looping.get("Static")
    if str(options.get("Looping", "None") or "").strip() == "None" or not isinstance(static, dict) or not static:
        return None
    try:
        rows = sorted(static.items(), key=lambda item: int(str(item[0])))
    except (TypeError, ValueError):
        rows = sorted(static.items(), key=lambda item: str(item[0]))
    out: dict[str, Any] = {
        "rows": [
            {"id": str(row_id), "values": {str(k): str(v) for k, v in values.items()} if isinstance(values, dict) else {}}
            for row_id, values in rows
        ]
    }
    randomization = str(looping.get("Randomization", "None") or "").strip()
    if randomization in {"All", "Subset"}:
        out["order"] = "shuffle"
        take = positive_int(looping.get("TotalRandSubset") or options.get("TotalRandSubset"))
        if randomization == "Subset" and take:
            out["take"] = take
    return out


def repeat_setting(repeat: dict[str, Any]) -> dict[str, Any]:
    looping: dict[str, Any] = {
        "Static": {row["id"]: dict(row.get("values") or {}) for row in repeat.get("rows") or []},
        "Randomization": "None",
    }
    if repeat.get("order") == "shuffle":
        looping["Randomization"] = "Subset" if repeat.get("take") else "All"
        if repeat.get("take"):
            looping["TotalRandSubset"] = repeat["take"]
    return {"Looping": "Static", "LoopingOptions": looping}
