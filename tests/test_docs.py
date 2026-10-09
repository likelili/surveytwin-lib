"""The reference in docs/reference/ covers the whole public interface: every
exported function, class and constant, every public method, property and field
of the classes, and every parameter. A name added to the library without its
entry fails here."""

from __future__ import annotations

import dataclasses
import inspect
import re
from functools import cached_property
from pathlib import Path

import surveytwin as st
from surveytwin import batch, builder

REFERENCE = Path(__file__).resolve().parents[1] / "docs" / "reference"
HEADING = re.compile(r"^(#{1,4}) (.*)$")


def _sections() -> dict[str, str]:
    """Heading text (the backticked name in it) -> the section's text, to the next
    heading of the same or a higher level."""
    sections: dict[str, str] = {}
    for page in sorted(REFERENCE.glob("*.md")):
        lines = page.read_text(encoding="utf-8").splitlines()
        heads = []
        in_code = False
        for i, line in enumerate(lines):
            if line.lstrip().startswith("```"):
                in_code = not in_code  # a "# comment" in a code example is not a heading
            elif not in_code and (m := HEADING.match(line)):
                heads.append((i, len(m.group(1)), m.group(2)))
        for n, (start, level, title) in enumerate(heads):
            end = next((i for i, lv, _t in heads[n + 1:] if lv <= level), len(lines))
            name = re.search(r"`([^`]+)`", title)
            if name:
                key = name.group(1).split("(")[0].strip()
                sections.setdefault(key, "\n".join(lines[start:end]))
    return sections


SECTIONS = _sections()
ALL_TEXT = "\n".join(page.read_text(encoding="utf-8") for page in REFERENCE.glob("*.md"))


def _section(*names: str) -> str:
    for name in names:
        for key, text in SECTIONS.items():
            if key == name or key.endswith("." + name):
                return text
    raise AssertionError(f"no heading for {names[0]} in docs/reference/")


def _parameters(fn) -> list[str]:
    try:
        params = inspect.signature(fn).parameters.values()
    except (TypeError, ValueError):
        return []
    return [p.name for p in params if p.name not in {"self", "cls"} and p.kind not in (p.VAR_POSITIONAL, p.VAR_KEYWORD)]


def _check_parameters(fn, section: str, label: str) -> list[str]:
    return [f"{label}: parameter `{p}`" for p in _parameters(fn) if f"`{p}`" not in section]


def _public_members(cls):
    for name, member in vars(cls).items():
        if name.startswith("_"):
            continue
        yield name, member


def _exported():
    for name in st.__all__:
        if name != "__version__":
            yield name, getattr(st, name)
    for name in builder.__all__:
        yield name, getattr(builder, name)
    for name in ("submit", "status", "collect"):
        yield name, getattr(batch, name)


def test_every_public_name_has_an_entry():
    missing: list[str] = []
    for name, obj in _exported():
        if inspect.ismodule(obj):
            continue
        if inspect.isclass(obj) or inspect.isfunction(obj):
            try:
                _section(name)
            except AssertionError:
                missing.append(name)
        elif f"`{name}`" not in ALL_TEXT:
            missing.append(name)
    for name in ("PROMPT_VERSION", "DEFAULT_SYSTEM_INSTRUCTION", "SIMULATION_FORMAT_CONTRACT", "RERUN_INSTRUCTION", "CT_SYSTEM_INSTRUCTION"):
        if f"`{name}`" not in ALL_TEXT:
            missing.append(f"prompts.{name}")
    assert not missing, "not documented in docs/reference/: " + ", ".join(missing)


def test_every_parameter_method_property_and_field_is_documented():
    missing: list[str] = []
    for name, obj in _exported():
        if inspect.isfunction(obj):
            missing += _check_parameters(obj, _section(name), name)
        if not inspect.isclass(obj) or issubclass(obj, Exception):
            continue
        cls_text = _section(name)
        for field in dataclasses.fields(obj) if dataclasses.is_dataclass(obj) else []:
            if not field.name.startswith("_") and f"`{field.name}`" not in cls_text:
                missing.append(f"{name}: field `{field.name}`")
        for member_name, member in _public_members(obj):
            if isinstance(member, (property, cached_property)):
                if f"`{member_name}`" not in cls_text:
                    missing.append(f"{name}: property `{member_name}`")
                continue
            fn = member.__func__ if isinstance(member, (classmethod, staticmethod)) else member
            if not inspect.isfunction(fn):
                continue
            try:
                section = _section(f"{name}.{member_name}")
            except AssertionError:
                missing.append(f"{name}.{member_name}")
                continue
            missing += _check_parameters(fn, section, f"{name}.{member_name}")
    assert not missing, "missing from docs/reference/:\n" + "\n".join(missing)


def test_the_reference_has_no_private_material():
    # the decisions log stays with the team, and the library has no prices
    assert "decisions.md" not in ALL_TEXT.lower()
    assert not re.search(r"\$\s?\d", ALL_TEXT)


def test_every_relative_link_resolves():
    # A link to another page of the repository points at a file that exists (a
    # link from docs/reference/ to the README is ../../README.md, not ../README.md).
    root = REFERENCE.parents[1]
    link = re.compile(r"\[[^\]]*\]\(([^)\s]+)\)")
    broken = []
    for page in [root / "README.md", *sorted((root / "docs").rglob("*.md"))]:
        text = re.sub(r"```.*?```", "", page.read_text(encoding="utf-8"), flags=re.S)
        for target in link.findall(text):
            if re.match(r"^[a-z]+:", target) or target.startswith("#"):
                continue  # a web address or a place on the same page
            if not (page.parent / target.split("#", 1)[0]).exists():
                broken.append(f"{page.relative_to(root)}: {target}")
    assert not broken, "links to files that do not exist:\n" + "\n".join(broken)
