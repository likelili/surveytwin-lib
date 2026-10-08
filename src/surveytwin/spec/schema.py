"""A small declarative schema language for the standard survey format.

Every object type of the format is declared once, in ``model.py``, with this
language. The same declarations give:

- ``check``: strict structural validation of a parsed JSON value, reporting each
  problem with the path of the field (``$.questions[2].response.options[0].id``);
- ``to_json_schema``: a JSON Schema (draft 2020-12) for tools outside Python.

Unknown keys are errors, except keys starting with ``x-``, which any object may
carry as a named extension (kept, never executed).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Callable

EXTENSION_PREFIX = "x-"


@dataclass(frozen=True)
class Problem:
    """One thing wrong with a survey definition, at a path."""

    path: str
    message: str
    code: str = "schema"

    def __str__(self) -> str:
        return f"{self.path}: {self.message}"


def _child(path: str, key: str | int) -> str:
    return f"{path}[{key}]" if isinstance(key, int) else f"{path}.{key}"


def _kind(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true/false"
    if isinstance(value, (int, float)):
        return "a number"
    if isinstance(value, str):
        return "text"
    if isinstance(value, list):
        return "a list"
    if isinstance(value, dict):
        return "an object"
    return type(value).__name__


class Type:
    """A value type. Subclasses check values and describe themselves as JSON Schema."""

    doc: str = ""

    def check(self, value: Any, path: str, problems: list[Problem]) -> None:  # pragma: no cover - abstract
        raise NotImplementedError

    def schema(self, defs: dict[str, Any]) -> dict[str, Any]:  # pragma: no cover - abstract
        raise NotImplementedError

    def describe(self) -> str:
        return ""


@dataclass
class Str(Type):
    enum: tuple[str, ...] = ()
    pattern: str = ""
    nonempty: bool = False
    doc: str = ""

    def check(self, value: Any, path: str, problems: list[Problem]) -> None:
        if not isinstance(value, str):
            problems.append(Problem(path, f"must be text, not {_kind(value)}"))
            return
        if self.nonempty and not value.strip():
            problems.append(Problem(path, "must not be empty"))
        if self.enum and value not in self.enum:
            problems.append(Problem(path, f"must be one of {', '.join(repr(e) for e in self.enum)}, not {value!r}"))
        if self.pattern and value and not re.fullmatch(self.pattern, value):
            problems.append(Problem(path, f"{value!r} is not allowed here ({self.describe() or 'pattern ' + self.pattern})"))

    def schema(self, defs: dict[str, Any]) -> dict[str, Any]:
        out: dict[str, Any] = {"type": "string"}
        if self.enum:
            out["enum"] = list(self.enum)
        if self.pattern:
            out["pattern"] = f"^{self.pattern}$"
        if self.nonempty:
            out["minLength"] = 1
        if self.doc:
            out["description"] = self.doc
        return out

    def describe(self) -> str:
        if self.enum:
            return " | ".join(repr(e) for e in self.enum)
        return "text without spaces" if self.pattern == ID_PATTERN else "text"


@dataclass
class Num(Type):
    integer: bool = False
    minimum: float | None = None
    maximum: float | None = None
    doc: str = ""

    def check(self, value: Any, path: str, problems: list[Problem]) -> None:
        ok = isinstance(value, (int, float)) and not isinstance(value, bool)
        if self.integer:
            ok = ok and float(value).is_integer()
        if not ok:
            problems.append(Problem(path, f"must be {'a whole number' if self.integer else 'a number'}, not {_kind(value)}"))
            return
        if self.minimum is not None and value < self.minimum:
            problems.append(Problem(path, f"must be at least {self.minimum:g}"))
        if self.maximum is not None and value > self.maximum:
            problems.append(Problem(path, f"must be at most {self.maximum:g}"))

    def schema(self, defs: dict[str, Any]) -> dict[str, Any]:
        out: dict[str, Any] = {"type": "integer" if self.integer else "number"}
        if self.minimum is not None:
            out["minimum"] = self.minimum
        if self.maximum is not None:
            out["maximum"] = self.maximum
        if self.doc:
            out["description"] = self.doc
        return out

    def describe(self) -> str:
        return "whole number" if self.integer else "number"


@dataclass
class Bool(Type):
    doc: str = ""

    def check(self, value: Any, path: str, problems: list[Problem]) -> None:
        if not isinstance(value, bool):
            problems.append(Problem(path, f"must be true or false, not {_kind(value)}"))

    def schema(self, defs: dict[str, Any]) -> dict[str, Any]:
        return {"type": "boolean", **({"description": self.doc} if self.doc else {})}

    def describe(self) -> str:
        return "true/false"


@dataclass
class Const(Type):
    value: Any = None
    doc: str = ""

    def check(self, value: Any, path: str, problems: list[Problem]) -> None:
        if value != self.value:
            problems.append(Problem(path, f"must be {self.value!r}"))

    def schema(self, defs: dict[str, Any]) -> dict[str, Any]:
        return {"const": self.value}

    def describe(self) -> str:
        return repr(self.value)


@dataclass
class AnyValue(Type):
    """Any JSON value (used for retained source data, never executed)."""

    doc: str = ""

    def check(self, value: Any, path: str, problems: list[Problem]) -> None:
        return None

    def schema(self, defs: dict[str, Any]) -> dict[str, Any]:
        return {"description": self.doc} if self.doc else {}

    def describe(self) -> str:
        return "any value"


@dataclass
class Scalar(Type):
    """Text, a number or true/false."""

    doc: str = ""

    def check(self, value: Any, path: str, problems: list[Problem]) -> None:
        if not isinstance(value, (str, int, float, bool)):
            problems.append(Problem(path, f"must be text, a number or true/false, not {_kind(value)}"))

    def schema(self, defs: dict[str, Any]) -> dict[str, Any]:
        return {"type": ["string", "number", "boolean"], **({"description": self.doc} if self.doc else {})}

    def describe(self) -> str:
        return "text, number or true/false"


@dataclass
class ListOf(Type):
    item: Type = field(default_factory=AnyValue)
    min_items: int = 0
    doc: str = ""

    def check(self, value: Any, path: str, problems: list[Problem]) -> None:
        if not isinstance(value, list):
            problems.append(Problem(path, f"must be a list, not {_kind(value)}"))
            return
        if len(value) < self.min_items:
            problems.append(Problem(path, f"must have at least {self.min_items} item{'s' if self.min_items != 1 else ''}"))
        for index, item in enumerate(value):
            self.item.check(item, _child(path, index), problems)

    def schema(self, defs: dict[str, Any]) -> dict[str, Any]:
        out: dict[str, Any] = {"type": "array", "items": self.item.schema(defs)}
        if self.min_items:
            out["minItems"] = self.min_items
        if self.doc:
            out["description"] = self.doc
        return out

    def describe(self) -> str:
        return f"list of {self.item.describe()}"


@dataclass
class MapOf(Type):
    value: Type = field(default_factory=AnyValue)
    key_pattern: str = ""
    doc: str = ""

    def check(self, value: Any, path: str, problems: list[Problem]) -> None:
        if not isinstance(value, dict):
            problems.append(Problem(path, f"must be an object, not {_kind(value)}"))
            return
        for key, item in value.items():
            if self.key_pattern and not re.fullmatch(self.key_pattern, str(key)):
                problems.append(Problem(_child(path, str(key)), f"key {key!r} is not allowed here"))
            self.value.check(item, _child(path, str(key)), problems)

    def schema(self, defs: dict[str, Any]) -> dict[str, Any]:
        out: dict[str, Any] = {"type": "object", "additionalProperties": self.value.schema(defs)}
        if self.key_pattern:
            out["propertyNames"] = {"pattern": f"^{self.key_pattern}$"}
        if self.doc:
            out["description"] = self.doc
        return out

    def describe(self) -> str:
        return f"object of {self.value.describe()}"


@dataclass
class Field:
    type: Type
    required: bool = False
    doc: str = ""


class Obj(Type):
    """A named object type with a fixed set of fields."""

    def __init__(self, name: str, fields: dict[str, Field], *, doc: str = "", open_keys: bool = False) -> None:
        self.name = name
        self.fields = fields
        self.doc = doc
        self.open_keys = open_keys

    def check(self, value: Any, path: str, problems: list[Problem]) -> None:
        if not isinstance(value, dict):
            problems.append(Problem(path, f"must be an object ({self.name}), not {_kind(value)}"))
            return
        for key, spec in self.fields.items():
            if key in value:
                spec.type.check(value[key], _child(path, key), problems)
            elif spec.required:
                problems.append(Problem(_child(path, key), f"is required ({self.name})", "missing"))
        if not self.open_keys:
            for key in value:
                if key not in self.fields and not str(key).startswith(EXTENSION_PREFIX):
                    allowed = ", ".join(self.fields)
                    problems.append(Problem(_child(path, str(key)), f"unknown field for {self.name} (fields: {allowed})", "unknown"))

    def schema(self, defs: dict[str, Any]) -> dict[str, Any]:
        if self.name not in defs:
            defs[self.name] = {}  # reserve, for recursive types
            out: dict[str, Any] = {
                "type": "object",
                "properties": {key: _with_doc(spec.type.schema(defs), spec.doc) for key, spec in self.fields.items()},
            }
            required = [key for key, spec in self.fields.items() if spec.required]
            if required:
                out["required"] = required
            if not self.open_keys:
                out["additionalProperties"] = False
                out["patternProperties"] = {f"^{re.escape(EXTENSION_PREFIX)}": {}}
            if self.doc:
                out["description"] = self.doc
            defs[self.name] = out
        return {"$ref": f"#/$defs/{self.name}"}

    def describe(self) -> str:
        return self.name


class Lazy(Type):
    """A reference to a type declared later (recursive structures)."""

    def __init__(self, resolve: Callable[[], Type]) -> None:
        self._resolve = resolve

    def check(self, value: Any, path: str, problems: list[Problem]) -> None:
        self._resolve().check(value, path, problems)

    def schema(self, defs: dict[str, Any]) -> dict[str, Any]:
        return self._resolve().schema(defs)

    def describe(self) -> str:
        return self._resolve().describe()


class Tagged(Type):
    """One of several object types, chosen by the value of a tag field."""

    def __init__(self, name: str, tag: str, variants: dict[str, Obj], *, doc: str = "") -> None:
        self.name = name
        self.tag = tag
        self.variants = variants
        self.doc = doc

    def check(self, value: Any, path: str, problems: list[Problem]) -> None:
        if not isinstance(value, dict):
            problems.append(Problem(path, f"must be an object ({self.name}), not {_kind(value)}"))
            return
        tag = value.get(self.tag)
        if tag is None:
            problems.append(Problem(_child(path, self.tag), f"is required: one of {', '.join(repr(t) for t in self.variants)}", "missing"))
            return
        variant = self.variants.get(tag) if isinstance(tag, str) else None
        if variant is None:
            problems.append(Problem(_child(path, self.tag), f"must be one of {', '.join(repr(t) for t in self.variants)}, not {tag!r}"))
            return
        variant.check(value, path, problems)

    def schema(self, defs: dict[str, Any]) -> dict[str, Any]:
        if self.name not in defs:
            defs[self.name] = {}
            defs[self.name] = {
                "oneOf": [variant.schema(defs) for variant in self.variants.values()],
                **({"description": self.doc} if self.doc else {}),
            }
        return {"$ref": f"#/$defs/{self.name}"}

    def describe(self) -> str:
        return self.name


class Either(Type):
    """One of several types, tried in order; the first whose problems are fewest reports."""

    def __init__(self, name: str, options: list[Type], *, doc: str = "", pick: Callable[[Any], Type | None] | None = None) -> None:
        self.name = name
        self.options = options
        self.doc = doc
        self._pick = pick

    def check(self, value: Any, path: str, problems: list[Problem]) -> None:
        if self._pick is not None:
            chosen = self._pick(value)
            if chosen is None:
                problems.append(Problem(path, f"must be {self.describe()}, not {_kind(value)}"))
                return
            chosen.check(value, path, problems)
            return
        best: list[Problem] | None = None
        for option in self.options:
            found: list[Problem] = []
            option.check(value, path, found)
            if not found:
                return
            if best is None or len(found) < len(best):
                best = found
        problems.extend(best or [Problem(path, f"must be {self.describe()}")])

    def schema(self, defs: dict[str, Any]) -> dict[str, Any]:
        return {"anyOf": [option.schema(defs) for option in self.options], **({"description": self.doc} if self.doc else {})}

    def describe(self) -> str:
        return self.name


def _with_doc(schema: dict[str, Any], doc: str) -> dict[str, Any]:
    if doc and "$ref" not in schema:
        return {**schema, "description": doc}
    if doc:
        return {"allOf": [schema], "description": doc}
    return schema


ID_PATTERN = r"[^\s]+"


def to_json_schema(root: Obj, *, title: str, version: str) -> dict[str, Any]:
    """A JSON Schema document whose root is ``root``."""
    defs: dict[str, Any] = {}
    ref = root.schema(defs)
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": f"urn:surveytwin:survey-format:{version}",
        "title": title,
        **ref,
        "$defs": defs,
    }
