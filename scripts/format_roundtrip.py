"""Check that the standard format reproduces the QSF reader exactly.

For each QSF: read it the old way (``parse_template``), read that into the
standard format, check the result against the format's declarations, save it as
JSON and load it back, compile it, and compare with the old template value by
value. Any difference is printed with its path.

    python scripts/format_roundtrip.py [QSF files or folders ...]

With no arguments, the test fixtures are checked. Exit status 1 when any file
differs or does not conform.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Iterator

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from surveytwin.runtime.instrument_plan import parse_template  # noqa: E402
from surveytwin.spec.compile import compile_survey  # noqa: E402
from surveytwin.spec.model import check_structure  # noqa: E402
from surveytwin.spec.read_template import survey_from_template  # noqa: E402


def differences(left: Any, right: Any, path: str = "$") -> Iterator[str]:
    if isinstance(left, dict) and isinstance(right, dict):
        for key in left:
            if key not in right:
                yield f"{path}.{key}: only in the old template ({json.dumps(left[key], ensure_ascii=False)[:160]})"
            else:
                yield from differences(left[key], right[key], f"{path}.{key}")
        for key in right:
            if key not in left:
                yield f"{path}.{key}: only in the compiled template ({json.dumps(right[key], ensure_ascii=False)[:160]})"
        return
    if isinstance(left, list) and isinstance(right, list):
        if len(left) != len(right):
            yield f"{path}: {len(left)} items in the old template, {len(right)} compiled"
        for index, (a, b) in enumerate(zip(left, right)):
            yield from differences(a, b, f"{path}[{index}]")
        return
    if left != right or type(left) is not type(right):
        yield f"{path}: old {json.dumps(left, ensure_ascii=False)[:160]} != compiled {json.dumps(right, ensure_ascii=False)[:160]}"


def label(path: str, template: dict[str, Any]) -> str:
    """The question ID at a template path, for readable output."""
    node: Any = template
    found = ""
    for part in path.replace("[", ".[").split(".")[1:]:
        try:
            node = node[int(part[1:-1])] if part.startswith("[") else node[part]
        except (KeyError, IndexError, TypeError, ValueError):
            break
        if isinstance(node, dict) and "QuestionID" in node and "QuestionType" in node:
            found = f"{node['QuestionID']} ({node['QuestionType']})"
    return found


def check(path: Path, limit: int = 12) -> bool:
    qsf = json.loads(path.read_text(encoding="utf-8-sig"))
    old = parse_template(qsf)
    survey = survey_from_template(old, qsf=qsf)
    problems = check_structure(survey)
    survey = json.loads(json.dumps(survey))
    compiled = compile_survey(survey)
    diffs = list(differences(old, compiled))
    status = "ok" if not diffs and not problems else "DIFFERS" if diffs else "DOES NOT CONFORM"
    print(f"{status:16} {path}  ({len(survey['questions'])} questions, {len(diffs)} differences, {len(problems)} format problems)")
    for problem in problems[:limit]:
        print(f"    format: {problem.path}: {problem.message}")
    for diff in diffs[:limit]:
        where = label(diff.split(":", 1)[0], old)
        print(f"    {where + ': ' if where else ''}{diff}")
    return not diffs and not problems


def main(argv: list[str]) -> int:
    targets = [Path(a) for a in argv] or [ROOT / "tests" / "fixtures" / "qsf"]
    files: list[Path] = []
    for target in targets:
        files += sorted(target.rglob("*.qsf")) if target.is_dir() else [target]
    results = [check(f) for f in files]
    print(f"\n{sum(results)}/{len(results)} reproduce the QSF reader exactly")
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
