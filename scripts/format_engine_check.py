"""Check that the standard format's own fields carry everything the engine uses.

For each QSF, the survey is read into the standard format, and a second copy has
every setting kept "as read" (each ``source``) removed: the standard fields
only. The engine prepares both and runs both with the same scripted model (no
model calls):

- the runtime analysis (static or staged, and why);
- every twin's questions, prompt text and pictures (static surveys);
- every twin's answer rows, warnings and audit (static and staged runs).

Any difference means a field the engine uses has no home in the standard format.

    python scripts/format_engine_check.py [QSF files or folders ...]
"""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import logging
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))
logging.disable(logging.CRITICAL)

from format_roundtrip import differences  # noqa: E402
from surveytwin.runtime.instrument_plan import build_survey_instrument, parse_template  # noqa: E402
from surveytwin.spec.read_template import survey_from_template  # noqa: E402

# Keys of the engine's question records that nothing reads: no prompt, check or
# export uses them, so they may differ. ``text_validation_type`` names the Qualtrics
# validation type ("MinChar"); a QSF can carry a stale one ("None" with a minimum
# length the engine still applies), which the standard form writes anew.
UNREAD_KEYS = {"text_validation_type"}


def _drop_unread(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _drop_unread(v) for k, v in value.items() if k not in UNREAD_KEYS}
    if isinstance(value, list):
        return [_drop_unread(v) for v in value]
    return value


def without_provenance(survey: dict[str, Any]) -> dict[str, Any]:
    """The survey with every ``source`` record removed (the standard fields only),
    except the survey's source format: HTML text read from a QSF is laid out as
    the QSF reader lays it out, which is a rule of the reader, not a setting."""
    out = copy.deepcopy(survey)
    source_format = (out.pop("source", None) or {}).get("format")
    if source_format:
        out["source"] = {"format": source_format}
    for question in out.get("questions") or []:
        question.pop("source", None)
        response = question.get("response") or {}
        for field in response.get("fields") or []:
            field.pop("source", None)
    for block in out.get("blocks") or []:
        block.pop("source", None)

    def walk(nodes: list[dict[str, Any]]) -> None:
        for node in nodes:
            node.pop("source", None)
            for assignment in node.get("assignments") or []:
                assignment.pop("source", None)
            for key in ("then", "children"):
                walk(node.get(key) or [])

    walk(out.get("flow") or [])
    return out


def scripted_answers(user_text: str) -> dict[str, Any]:
    """Deterministic replies that exercise the answer rules."""
    marker = "Questions JSON:\n"
    start = user_text.rindex(marker) + len(marker)
    end = user_text.find("\n\nRespond using JSON schema", start)
    if end < 0:
        end = user_text.find("\n\nYour previous answers", start)
    questions = json.loads(user_text[start:end])
    answers = []
    for q in questions:
        h = int(hashlib.sha256(q["id"].encode()).hexdigest()[:8], 16)
        if h % 11 == 0:
            continue
        options = q.get("options") or []
        if options:
            pick = options[h % len(options)]
            if h % 5 == 1:
                pick = f"{(h % len(options)) + 1} - {pick}"
            elif h % 13 == 3:
                pick = "Something not on the list"
            value = pick
        elif q.get("numeric_max") not in (None, ""):
            value = str(float(q.get("numeric_max")) + (5 if h % 3 == 0 else -0.37))
        elif q.get("type") == "numeric":
            value = str(h % 100)
        else:
            value = f"text {h % 97}"
        answers.append({"question_id": q["id"], "answer_value": value, "answer_label": value, "answer_raw": value})
    return {"answers": answers}


class ScriptedCaller:
    enabled = True

    async def chat_json(self, *, system_prompt, user_prompt, temperature, max_tokens):
        return scripted_answers(user_prompt)

    async def responses_input_text(self, *, input_items, max_tokens):
        user = "\n\n".join(p.get("text", "") for p in input_items[1]["content"] if p.get("type") == "input_text")
        return json.dumps(scripted_answers(user))

    async def close(self):
        return None


def engine_view(definition: dict[str, Any], *, twins: int, seed: int) -> dict[str, Any]:
    """What the engine makes of a survey definition: its analysis, prompts and a scripted run."""
    from surveytwin.answers.simulate import simulate_staged, simulate_static
    from surveytwin.plan import build_call

    pids = [f"pid_{i}" for i in range(twins)]
    persona = {pid: f"Persona {pid}: 41 years old, lives in Ohio, works as a nurse." for pid in pids}
    view: dict[str, Any] = {}
    try:
        instrument = build_survey_instrument(copy.deepcopy(definition), twins, seed=seed, respondent_ids=pids)
    except Exception as exc:  # noqa: BLE001
        return {"error": f"{type(exc).__name__}: {exc}"}
    view["mode"] = instrument["execution_mode"]
    view["analysis"] = instrument.get("analysis")
    twin_rows = [{"persona_id": pid, "persona_text": persona[pid]} for pid in pids]
    if instrument["execution_mode"] == "static":
        instances = instrument["instances"]
        view["instances"] = [{k: v for k, v in inst.items() if k not in {"elements", "blocks"}} for inst in instances]
        view["calls"] = []
        for index, pid in enumerate(pids):
            inst = instances[index % len(instances)]
            view["calls"].append(
                build_call(
                    persona[pid],
                    inst["questions"],
                    survey_sequence_text=inst["prompt_sequence_text"] or None,
                    survey_sequence_media=inst["prompt_sequence_media"] or None,
                )
            )
        rows, warnings, _ = asyncio.run(simulate_static(caller=ScriptedCaller(), twins=twin_rows, instances=instances))
    else:
        rows, warnings, audits = asyncio.run(
            simulate_staged(caller=ScriptedCaller(), twins=twin_rows, definition=instrument["definition"], seed=seed)
        )
        view["audits"] = audits
    view["rows"] = rows
    view["warnings"] = warnings
    return _drop_unread(json.loads(json.dumps(view, default=str)))


def check(path: Path, twins: int, seed: int, limit: int = 10) -> bool:
    qsf = json.loads(path.read_text(encoding="utf-8-sig"))
    survey = survey_from_template(parse_template(qsf), qsf=qsf)
    expected = engine_view(survey, twins=twins, seed=seed)
    got = engine_view(json.loads(json.dumps(without_provenance(survey))), twins=twins, seed=seed)
    diffs = list(differences(expected, got))
    rows = len(expected.get("rows") or [])
    print(f"{'same' if not diffs else 'DIFFERS':16} {path}  [{expected.get('mode', expected.get('error'))}, {rows} rows]  {len(diffs)} differences")
    for diff in diffs[:limit]:
        print(f"    {diff}")
    return not diffs


def main(argv: list[str]) -> int:
    twins, seed = 4, 11
    targets = [Path(a) for a in argv] or [ROOT / "tests" / "fixtures" / "qsf"]
    files: list[Path] = []
    for target in targets:
        files += sorted(target.rglob("*.qsf")) if target.is_dir() else [target]
    results = [check(f, twins, seed) for f in files]
    print(f"\n{sum(results)}/{len(results)} run the same from the standard fields alone")
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
