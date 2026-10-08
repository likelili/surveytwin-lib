"""The ``surveytwin`` command.

    surveytwin check study.qsf [--network]
    surveytwin prompts study.qsf --twins 3 --seed 7
    surveytwin model-check mymodels.py:claude
    surveytwin run study.qsf --twins 500 --seed 7 --model gpt-5.6-luna-medium --out runs/study
    surveytwin rerun runs/study
    surveytwin export runs/study
    surveytwin report runs/study
    surveytwin batch submit|status|collect ...

Every command that needs personas takes --bank (default: the Twin-2K-500
package), --representation and --filter FIELD=VALUE[,VALUE] (repeatable).

--model takes a preset name (gpt-5.6-luna-medium, or any OpenAI model name) or
FILE.py:NAME, a model defined in Python in that file:

    # mymodels.py
    import surveytwin as st
    claude = st.Model.http("claude-sonnet", "https://api.anthropic.com/v1/messages",
                           layout="anthropic", body={"model": "claude-sonnet-5"},
                           headers={"x-api-key": st.env("ANTHROPIC_API_KEY"),
                                    "anthropic-version": "2023-06-01"})

NAME may also be a function that takes no arguments and returns the model.
The file is run as Python, like any script you run yourself.
"""

from __future__ import annotations

import argparse
import dataclasses
import hashlib
import importlib.util
import inspect
import json
import shutil
import sys
from pathlib import Path
from typing import Any

from .errors import ConfigError, FilterError, SurveytwinError


def _filters(pairs: list[str] | None) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for pair in pairs or []:
        if "=" not in pair:
            raise FilterError(f"--filter needs FIELD=VALUE[,VALUE]; got {pair!r}")
        field, values = pair.split("=", 1)
        if ".." in values:
            low, high = values.split("..", 1)
            try:
                out[field.strip()] = {"min": float(low) if low else None, "max": float(high) if high else None}
            except ValueError:
                raise FilterError(f"--filter {pair!r}: a range needs numbers, as in age=18..29") from None
        elif values.strip().lower() == ":missing":
            out[field.strip()] = {"missing": True}
        else:
            out[field.strip()] = [v.strip() for v in values.split(",") if v.strip()]
    return out


def is_model_file(value: str | None) -> bool:
    """True for FILE.py:NAME. A name that merely contains colons (a fine-tuned
    OpenAI model such as ft:gpt-4.1:org::id) stays a preset name."""
    head, sep, _name = str(value or "").rpartition(":")
    return bool(sep) and head.endswith(".py")


def load_model_file(value: str):
    """The Model called NAME in FILE.py (NAME may be a function returning one)."""
    from .providers.models import Model

    path_text, _, name = value.rpartition(":")
    path = Path(path_text).expanduser()
    if not path.is_file():
        raise ConfigError(f"--model {value}: no such file {path_text}")
    if not name.isidentifier():
        raise ConfigError(f"--model {value}: {name!r} is not a Python name; use FILE.py:NAME")
    content = path.read_bytes()
    digest = hashlib.sha256(content).hexdigest()
    module_name = f"_surveytwin_models_{digest[:16]}"
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise ConfigError(f"--model {value}: cannot load {path.name} as Python")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    folder = str(path.resolve().parent)
    sys.path.insert(0, folder)  # so the file can import its neighbours
    try:
        spec.loader.exec_module(module)
    except Exception as exc:  # noqa: BLE001 - any error in the user's file
        sys.modules.pop(module_name, None)
        raise ConfigError(f"--model {value}: {path.name} failed to load: {type(exc).__name__}: {exc}") from None
    finally:
        try:
            sys.path.remove(folder)
        except ValueError:
            pass
    if not hasattr(module, name):
        found = sorted(k for k, v in vars(module).items() if isinstance(v, Model))
        hint = f" It defines: {', '.join(found)}." if found else " It defines no Model."
        raise ConfigError(f"--model {value}: {path.name} has no {name!r}.{hint}")
    obj = getattr(module, name)
    if inspect.isfunction(obj):
        try:
            obj = obj()
        except Exception as exc:  # noqa: BLE001
            raise ConfigError(f"--model {value}: calling {name}() failed: {type(exc).__name__}: {exc}") from None
    if not isinstance(obj, Model):
        raise ConfigError(
            f"--model {value}: {name!r} is a {type(obj).__name__}, not a surveytwin Model; build it with "
            "st.Model.http, st.Model.openai_compatible, st.Model.from_function or st.Model.preset"
        )
    return dataclasses.replace(obj, source=f"{path.name}:{name} (sha256 {digest[:12]})")


def model_value(value: str | None, provider: str | None = None):
    """--model and --provider as configure() takes them: a preset name or a Model."""
    if value and is_model_file(value):
        if provider:
            raise ConfigError("--provider applies to preset names only; a model from a file sets its own address")
        return load_model_file(value)
    return value


def _survey_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("qsf", help="the .qsf file, a survey in the standard format (.json), or a survey draft (.json)")
    p.add_argument("--drop", action="append", default=[], metavar="BLOCK_ID", help="drop a block (repeatable)")
    # Removed: still read, in every spelling argparse accepts, so a batch submitted
    # with it can be collected; anywhere else it is refused with the reason.
    p.add_argument("--assume", action="append", default=[], help=argparse.SUPPRESS)


def _config_args(p: argparse.ArgumentParser, *, needs_model: bool) -> None:
    p.add_argument("--bank", default="twin-2k-500", help="'twin-2k-500' (default) or a Croissant Twins folder or zip")
    p.add_argument("--representation", default=None, help="the bank's representation (default: the bank's default)")
    p.add_argument("--filter", action="append", default=[], metavar="FIELD=VALUES", help="e.g. region=West,East or age=18..29")
    p.add_argument("--twins", type=int, default=3, help="sample size")
    p.add_argument("--seed", type=int, default=42)
    p.add_argument(
        "--model",
        required=needs_model,
        default=None if needs_model else "gpt-5.6-luna-medium",
        help="a preset name such as gpt-5.6-luna-medium, or FILE.py:NAME for a model defined in Python",
    )
    p.add_argument("--provider", default=None, choices=["openai", "openrouter"], help="for preset names (default: openai)")
    p.add_argument("--system-instruction", default=None, help="replace the behavioral instruction (text, or @file)")
    p.add_argument("--embedded-data", action="append", default=[], metavar="FIELD=VALUE", help="a field the study filled from outside the QSF")
    p.add_argument("--max-stages", type=int, default=25)
    p.add_argument("--carry-stage-images", action="store_true")
    p.add_argument("--accept-missing-images", action="store_true")
    p.add_argument("--mode", default="auto", choices=["auto", "static", "staged"])


def _read_survey(args):
    """A .qsf export, a survey in the standard format (.json with "schema_version"),
    or a survey draft (.json with "title" and "questions")."""
    from . import read_qsf, read_survey

    if getattr(args, "assume", None):
        raise SurveytwinError(
            "--assume was removed: a dropped question no longer exists, and logic that reads it "
            "counts as never shown (see 'surveytwin check ... --drop BLOCK_ID')"
        )
    if not args.qsf:
        raise SurveytwinError("Give the survey: a .qsf file or a survey draft (.json)")
    path = Path(args.qsf)
    if path.suffix.lower() == ".json":
        from .builder import build_survey
        from .errors import QSFReadError

        try:
            data = json.loads(path.read_bytes().decode("utf-8-sig"))
        except (OSError, ValueError) as exc:
            raise QSFReadError(f"{path}: cannot read the draft ({exc})") from None
        if isinstance(data, dict) and "schema_version" in data and "SurveyElements" not in data:
            survey = read_survey(path)
            for warning in survey.read_warnings:
                print(f"warning: {warning.path}: {warning.message}", file=sys.stderr)
        elif isinstance(data, dict) and "questions" in data and "SurveyElements" not in data:
            from .builder import validate_draft

            for issue in validate_draft(data):
                if issue.severity == "warning":
                    print(f"warning: {issue}", file=sys.stderr)
            survey = build_survey(data)
        else:
            survey = read_qsf(path)
    else:
        survey = read_qsf(args.qsf)
    if args.drop:
        survey = survey.drop_blocks(args.drop)
    return survey


def _config(args, *, batch: bool = False):
    from . import configure

    instruction = args.system_instruction
    if instruction and instruction.startswith("@"):
        try:
            instruction = Path(instruction[1:]).read_text(encoding="utf-8")
        except OSError as exc:
            raise ConfigError(f"--system-instruction {instruction}: cannot read the file ({exc.strerror})") from None
    embedded = dict(e.split("=", 1) for e in args.embedded_data if "=" in e)
    return configure(
        bank=args.bank,
        representation=args.representation,
        filters=_filters(args.filter),
        sample_size=args.twins,
        seed=args.seed,
        model=model_value(args.model, args.provider),
        provider=args.provider or "openai",
        batch=batch,
        system_instruction=instruction,
        embedded_data=embedded or None,
        carry_stage_images=args.carry_stage_images,
        max_stages=args.max_stages,
        accept_missing_images=args.accept_missing_images,
    )


def cmd_check(args) -> int:
    survey = _read_survey(args)
    print(survey)
    print(survey.preview_blocks())
    if survey.exclude_blocks:
        print(survey.drop_report)
    embedded = dict(e.split("=", 1) for e in args.embedded_data if "=" in e)
    print(survey.check(network=args.network, embedded_data=embedded or None))
    return 0


def cmd_convert(args) -> int:
    """Write the survey in the standard format (a QSF, a draft, or a standard survey as it runs)."""
    survey = _read_survey(args)
    text = json.dumps(survey.standard, indent=2, ensure_ascii=False) + "\n"
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
        print(f"Saved {survey.name!r} in the standard format to {args.out}")
    else:
        sys.stdout.write(text)
    return 0


def cmd_schema(args) -> int:
    """Print the JSON Schema of the standard format."""
    from .spec.model import json_schema

    sys.stdout.write(json.dumps(json_schema(), indent=2, ensure_ascii=False) + "\n")
    return 0


def cmd_model_check(args) -> int:
    from .providers.models import Model

    model = model_value(args.model, args.provider)
    if isinstance(model, str):
        model = Model.preset(model, provider=args.provider or "openai")
    check = model.check()
    print(f"model: {model.name} ({model.kind})")
    print(f"reply text found: {check.text_found}")
    print(f"token counts found: {check.usage_found}")
    print(f"stop reason found: {check.stop_reason_found}")
    print(f"seconds: {check.latency_s}")
    if not check.ok:
        print(f"reply: {str(check.response)[:500]}")
    return 0 if check.ok else 1


def cmd_prompts(args) -> int:
    from . import plan

    p = plan(_read_survey(args), _config(args), mode=args.mode)
    # Planned for --twins as the run will be (randomizers balanced over them), so
    # these are the prompts that run sends; --show prints only the first few.
    for twin in p.twins[: args.show or args.twins]:
        preview = p.preview_prompt(twin.id)
        print(f"##### twin {twin.id} ({preview.kind}, ~{preview.input_tokens:,} input tokens) #####")
        print(preview.text)
        print()
    return 0


def _progress(done: int, total: int) -> None:
    print(f"\r{done}/{total} twins", end="", file=sys.stderr, flush=True)
    if done == total:
        print(file=sys.stderr)


def cmd_run(args) -> int:
    from . import plan, run

    p = plan(_read_survey(args), _config(args), mode=args.mode)
    results = run(p, max_concurrency=args.concurrency, keep_prompts=args.keep_prompts, on_progress=_progress)
    out = results.save(args.out)
    if p.survey.kind == "qsf":
        (Path(out) / "survey.qsf").write_bytes(p.survey.raw_bytes)
    elif p.survey.kind == "standard":
        # (survey.json is the run's own record of the survey, written by save)
        (Path(out) / "survey_standard.json").write_bytes(p.survey.raw_bytes)
    else:
        target = Path(out) / "survey_draft.json"
        if Path(args.qsf).resolve() != target.resolve():
            shutil.copyfile(args.qsf, target)
    print(results)
    print(f"Saved to {out}")
    for warning in results.warnings[:20]:
        print(f"warning: {warning}")
    return 0


def cmd_rerun(args) -> int:
    from .results.results import Results

    results = Results.load(args.folder)
    print(results.rerun_counts())
    fixed = results.rerun_flagged(model=model_value(args.model))
    fixed.save(args.folder)
    print(fixed)
    return 0


def cmd_export(args) -> int:
    from .results.results import Results

    from .results.results import update_checksums

    results = Results.load(args.folder, verify=False)
    paths = results.write_exports(args.folder, report=False, text_files=True, bundle=args.bundle)
    update_checksums(args.folder, paths.values())  # the rebuilt files, so the folder still loads
    for kind, path in paths.items():
        print(f"{kind}: {path}")
    return 0


def cmd_report(args) -> int:
    from .results.results import Results

    from .results.results import update_checksums

    results = Results.load(args.folder, verify=False)
    target = Path(args.folder) / ("report.json" if args.json else "report.html")
    results.report(target, format="json" if args.json else "html")
    update_checksums(args.folder, [target])  # the rebuilt report, so the folder still loads and reruns
    print(target)
    return 0


def cmd_batch(args) -> int:
    from . import batch, plan

    state_path = Path(args.out) / "batch_state.json"
    if args.action == "submit":
        p = plan(_read_survey(args), _config(args, batch=True), mode="static")
        batch_id = batch.submit(p)
        Path(args.out).mkdir(parents=True, exist_ok=True)
        state = {"batch_id": batch_id, "argv": args.argv}
        state_path.write_text(json.dumps(state, indent=2), encoding="utf-8")
        print(batch_id)
        return 0
    if not state_path.is_file():
        raise SurveytwinError(f"{state_path}: no batch here; run 'surveytwin batch submit ... --out {args.out}' first")
    state = json.loads(state_path.read_text(encoding="utf-8"))
    if args.action == "status":
        print(batch.status(state["batch_id"]))
        return 0
    saved = build_parser().parse_args(list(state["argv"]))
    if getattr(saved, "assume", None):  # an option older versions had
        print("warning: --assume from the submitted command is ignored; dropped questions no longer exist", file=sys.stderr)
        saved.assume = []
    p = plan(_read_survey(saved), _config(saved, batch=True), mode="static")
    results = batch.collect(state["batch_id"], p)
    results.save(args.out)
    print(results)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="surveytwin", description="Run Qualtrics surveys on digital twins.")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("check", help="execution mode, findings and support levels")
    _survey_args(p)
    p.add_argument("--network", action="store_true", help="test image links over the network")
    p.add_argument("--embedded-data", action="append", default=[], metavar="FIELD=VALUE", help="a value you will supply to the run (counted as set)")
    p.set_defaults(func=cmd_check)

    p = sub.add_parser("convert", help="write the survey in the standard format (JSON)")
    _survey_args(p)
    p.add_argument("--out", "-o", default="", help="the file to write (default: print it)")
    p.set_defaults(func=cmd_convert)

    p = sub.add_parser("schema", help="print the JSON Schema of the standard format")
    p.set_defaults(func=cmd_schema)

    p = sub.add_parser("model-check", help="one tiny call: does the model reply, with token counts and a stop reason?")
    p.add_argument("model", help="a preset name, or FILE.py:NAME")
    p.add_argument("--provider", default=None, choices=["openai", "openrouter"], help="for preset names (default: openai)")
    p.set_defaults(func=cmd_model_check)

    p = sub.add_parser("prompts", help="print the exact prompts for a few twins, with no API call")
    _survey_args(p)
    _config_args(p, needs_model=False)
    p.add_argument("--show", type=int, default=0, metavar="K", help="print only the first K twins of the --twins plan (default: all)")
    p.set_defaults(func=cmd_prompts)

    p = sub.add_parser("run", help="run and write the run folder")
    _survey_args(p)
    _config_args(p, needs_model=True)
    p.add_argument("--out", required=True)
    p.add_argument("--concurrency", type=int, default=8)
    p.add_argument("--keep-prompts", action="store_true")
    p.set_defaults(func=cmd_run)

    p = sub.add_parser("rerun", help="re-ask the flagged answers of a saved run")
    p.add_argument("folder")
    p.add_argument("--model", default=None, help="the run's own model by default; a preset name or FILE.py:NAME")
    p.set_defaults(func=cmd_rerun)

    p = sub.add_parser("export", help="rebuild the export files of a saved run")
    p.add_argument("folder")
    p.add_argument("--bundle", action="store_true", help="also write export_bundle.zip")
    p.set_defaults(func=cmd_export)

    p = sub.add_parser("report", help="rebuild the validation report of a saved run")
    p.add_argument("folder")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_report)

    p = sub.add_parser("batch", help="the OpenAI Batch API path for static surveys")
    p.add_argument("action", choices=["submit", "status", "collect"])
    p.add_argument("--out", required=True, help="the run folder (holds batch_state.json)")
    p.add_argument("qsf", nargs="?", help="the .qsf file or a survey draft (submit only)")
    p.add_argument("--drop", action="append", default=[])
    p.add_argument("--assume", action="append", default=[], help=argparse.SUPPRESS)  # removed; see _survey_args
    _config_args(p, needs_model=False)
    p.set_defaults(func=cmd_batch)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    # The arguments as given (kept by 'batch submit' so 'collect' rebuilds the same plan).
    args.argv = list(argv) if argv is not None else sys.argv[1:]
    try:
        return int(args.func(args) or 0)
    except SurveytwinError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
