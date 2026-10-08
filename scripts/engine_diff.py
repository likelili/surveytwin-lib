"""Compare what two versions of the library produce, through the public API only.

Each survey is read (``read_qsf`` or ``read_survey``), checked, planned and run
with a scripted model (deterministic answers) and a scripted image check (no
network), in its own mode and, for a survey that runs static, also staged.
Everything a user can see is recorded: the check, the plan's mode and warnings, every request sent to the
model, every answer, the run's warnings, each twin's audit record, every export
file and the report. Run ``dump`` under each version (PYTHONPATH pointing at its
``src``), then ``compare`` the two files.

    PYTHONPATH=<old>/src python scripts/engine_diff.py dump old.json [SURVEY ...]
    PYTHONPATH=src      python scripts/engine_diff.py dump new.json [SURVEY ...]
    python scripts/engine_diff.py compare old.json new.json

Without surveys, the test fixtures and examples are used. A folder adds every
QSF and JSON file in it; ``@list.txt`` adds the paths listed in that file.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import logging
import os
import sys
import tempfile
from pathlib import Path
from typing import Any

logging.disable(logging.CRITICAL)
ROOT = Path(__file__).resolve().parents[1]
BANK = ROOT / "examples" / "example-bank"

# Fields that change from run to run (times, random run IDs), not with the survey.
VOLATILE = {"started_at", "finished_at", "elapsed_s", "run_id", "latency_ms", "request_id", "generated_at", "library_version", "sha256", "checksum_sha256"}


def scripted_answers(user_text: str) -> dict[str, Any]:
    """Deterministic replies that exercise the answer rules: code prefixes,
    numbers out of range, omitted questions, unknown options, and, for MaxDiff,
    the same item as best and worst."""
    marker = "Questions JSON:\n"
    if marker not in user_text:
        return {"answers": []}
    start = user_text.rindex(marker) + len(marker)
    end = user_text.find("\n\nRespond using JSON schema", start)
    if end < 0:
        end = user_text.find("\n\nYour previous answers", start)
    try:
        questions = json.loads(user_text[start:end])
    except ValueError:
        return {"answers": []}
    answers = []
    for q in questions:
        h = int(hashlib.sha256(str(q.get("id")).encode()).hexdigest()[:8], 16)
        if h % 11 == 0:
            continue
        options = q.get("options") or []
        if options:
            pick = options[h % len(options)]
            if h % 5 == 1:
                pick = f"{(h % len(options)) + 1} - {pick}"
            elif h % 7 == 2:
                pick = pick.upper()
            elif h % 13 == 3:
                pick = "Something not on the list"
            if q.get("best_worst_role") and h % 3 == 0:
                pick = options[0]
            value = pick
        elif q.get("numeric_max") not in (None, ""):
            value = str(float(q.get("numeric_max")) + (5 if h % 3 == 0 else -0.37))
        elif q.get("type") == "numeric":
            value = str(h % 100)
        else:
            value = f"text {h % 97}"
        answers.append({"question_id": q["id"], "answer_value": value, "answer_label": value, "answer_raw": value})
    return {"answers": answers}


def _model():
    import surveytwin as st

    def reply(request):
        user = request.user if isinstance(request.user, str) else json.dumps(request.user)
        return st.Reply(text=json.dumps(scripted_answers(user)), input_tokens=10, output_tokens=10)

    return st.Model.from_function(reply, name="scripted", images=True)


def image_check(url: str) -> dict[str, str]:
    """No network: one image address in four counts as unreachable, the rest load."""
    if int(hashlib.sha256(url.encode()).hexdigest()[:8], 16) % 4 == 0:
        return {"status": "inaccessible", "detail": "HTTP 404"}
    return {"status": "supported", "detail": "image/png"}


def _clean(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _clean(v) for k, v in value.items() if k not in VOLATILE}
    if isinstance(value, list):
        return [_clean(v) for v in value]
    return value


def _read(path: Path):
    import surveytwin as st

    if path.suffix.lower() == ".json":
        return st.read_survey(path)
    return st.read_qsf(path)


def _exports(results) -> dict[str, Any]:
    with tempfile.TemporaryDirectory() as tmp:
        try:
            paths = results.write_exports(tmp, report=False, text_files=True)
        except Exception as exc:  # noqa: BLE001
            return {"error": f"{type(exc).__name__}: {exc}"}
        out: dict[str, Any] = {}
        for kind, path in sorted(paths.items()):
            path = Path(path)
            if path.suffix == ".csv":
                out[kind] = list(csv.reader(io.StringIO(path.read_text(encoding="utf-8"))))
            elif path.suffix == ".json":
                out[kind] = _clean(json.loads(path.read_text(encoding="utf-8")))
        for sub in ("questions", "answers"):
            folder = Path(tmp) / sub
            if folder.is_dir():
                out[sub] = {p.name: p.read_text(encoding="utf-8") for p in sorted(folder.glob("*.txt"))}
        try:
            out["report"] = _clean(results.report(None, format="json"))
        except Exception as exc:  # noqa: BLE001
            out["report"] = {"error": f"{type(exc).__name__}: {exc}"}
        return out


def dump_one(path: Path, *, twins: int = 4, seed: int = 7) -> dict[str, Any]:
    import surveytwin as st

    record: dict[str, Any] = {}
    try:
        survey = _read(path)
    except Exception as exc:  # noqa: BLE001
        return {"read_error": f"{type(exc).__name__}: {exc}"}
    record["survey"] = repr(survey)
    try:
        record["check"] = str(survey.check())
        record["blocks"] = str(survey.preview_blocks())
    except Exception as exc:  # noqa: BLE001
        record["check_error"] = f"{type(exc).__name__}: {exc}"
    config = st.configure(bank=str(BANK), sample_size=twins, seed=seed, model=_model(), accept_missing_images=True)
    for mode in ("auto", "staged"):
        try:
            plan = st.plan(survey, config, mode=mode)
        except Exception as exc:  # noqa: BLE001
            record[mode] = {"plan_error": f"{type(exc).__name__}: {exc}"}
            continue
        if mode == "staged" and record.get("auto", {}).get("mode") == "staged":
            continue  # the same plan
        run: dict[str, Any] = {"mode": plan.mode, "plan_warnings": list(plan.warnings), "twins": [t.id for t in plan.twins]}
        try:
            results = st.run(plan, keep_prompts=True, image_checker=image_check)
        except Exception as exc:  # noqa: BLE001
            run["run_error"] = f"{type(exc).__name__}: {exc}"
            record[mode] = run
            continue
        run["prompts"] = _clean(results.prompts)
        run["rows"] = _clean(results.rows)
        run["warnings"] = list(results.warnings)
        run["twin_records"] = _clean(results.twins)
        run["audit"] = _clean(results.audit)
        run["exports"] = _exports(results)
        record[mode] = run
    return record


def default_surveys() -> list[Path]:
    return sorted((ROOT / "tests" / "fixtures" / "qsf").glob("*.qsf")) + sorted((ROOT / "examples").glob("*.qsf")) + sorted(
        (ROOT / "examples" / "survey-format").glob("*.json")
    )


def dump(out: Path, surveys: list[Path]) -> None:
    import surveytwin as st

    files: list[Path] = []
    targets: list[Path] = []
    for target in surveys or default_surveys():
        if str(target).startswith("@"):
            targets += [Path(line) for line in Path(str(target)[1:]).read_text().split() if line]
        else:
            targets.append(Path(target))
    for target in targets:
        files +=  sorted(list(target.rglob("*.qsf")) + list(target.rglob("*.json"))) if target.is_dir() else [target]
    data = {"library": st.__file__, "surveys": {}}
    for path in files:
        data["surveys"][str(path)] = dump_one(path)
        print(f"dumped {path}", file=sys.stderr)
    out.write_text(json.dumps(data, ensure_ascii=False, indent=0, default=str), encoding="utf-8")


def _diff(a: Any, b: Any, path: str, out: list[str], limit: int) -> None:
    if len(out) >= limit:
        return
    if isinstance(a, dict) and isinstance(b, dict):
        for key in list(a) + [k for k in b if k not in a]:
            if key not in a:
                out.append(f"{path}.{key}: only in the second ({json.dumps(b[key], ensure_ascii=False)[:200]})")
            elif key not in b:
                out.append(f"{path}.{key}: only in the first ({json.dumps(a[key], ensure_ascii=False)[:200]})")
            else:
                _diff(a[key], b[key], f"{path}.{key}", out, limit)
        return
    if isinstance(a, list) and isinstance(b, list):
        if len(a) != len(b):
            out.append(f"{path}: {len(a)} items, then {len(b)}")
        for index, (x, y) in enumerate(zip(a, b)):
            _diff(x, y, f"{path}[{index}]", out, limit)
        return
    if a != b:
        out.append(f"{path}: {json.dumps(a, ensure_ascii=False)[:300]}  !=  {json.dumps(b, ensure_ascii=False)[:300]}")


def compare(first: Path, second: Path, limit: int = 8) -> int:
    a = json.loads(first.read_text(encoding="utf-8"))["surveys"]
    b = json.loads(second.read_text(encoding="utf-8"))["surveys"]
    same = 0
    for name in list(a) + [n for n in b if n not in a]:
        if name not in a or name not in b:
            print(f"ONLY IN ONE  {name}")
            continue
        found: list[str] = []
        _diff(a[name], b[name], "$", found, limit)
        if found:
            print(f"DIFFERS      {name}")
            for line in found:
                print(f"    {line}")
        else:
            same += 1
    print(f"\n{same}/{len(set(a) | set(b))} surveys give the same output")
    return 0 if same == len(set(a) | set(b)) else 1


# Python orders sets by a per-process hash seed (the report joins one), and a
# retried call waits a second; neither belongs in a comparison.
STEADY = {"PYTHONHASHSEED": "0", "SIMULATION_RETRY_BACKOFF_S": "0"}

if __name__ == "__main__":
    if any(os.environ.get(k) != v for k, v in STEADY.items()):
        os.execve(sys.executable, [sys.executable, *sys.argv], {**os.environ, **STEADY})
    command, *rest = sys.argv[1:]
    if command == "dump":
        dump(Path(rest[0]), [Path(r) for r in rest[1:]])
    elif command == "compare":
        sys.exit(compare(Path(rest[0]), Path(rest[1])))
    else:
        raise SystemExit(__doc__)
