"""``Results``: every answer, every twin's audit, usage and provenance, and the export files."""

from __future__ import annotations

import csv
import hashlib
import json
import shutil
import tempfile
import zipfile
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..errors import ResultsError
from . import exports as ex
from .stats import compute_summary_stats

FLAGGED_STATUSES = {"skipped", "out_of_range", "needs_validation"}


@dataclass
class Answer:
    twin_id: str
    question_id: str
    question_text: str
    question_type: str
    value: str
    label: str
    raw: str
    status: str | None
    adjustments: list[dict[str, Any]] = field(default_factory=list)
    repair: dict[str, Any] | None = None
    embedded_data: dict[str, Any] = field(default_factory=dict)
    # The chosen option's code in the values file (its QSF ID, recoded), recorded
    # from the options the twin was shown; blank for an answer without one.
    code: str = ""
    # Answers repaired since that decided this question's text, options or
    # visibility: it was answered as the original, flagged answer showed it.
    shaped_by_repaired: list[str] = field(default_factory=list)


def _answer_from_row(row: dict[str, Any], marks: dict[tuple[str, str], list[str]] | None = None) -> Answer:
    status = str(row.get("answer_status") or "") or None
    if status is None and row.get("not_shown"):
        status = "not_shown"
    if status == "needs_validation":
        status = "skipped"
    adjustments: list[dict[str, Any]] = []
    if isinstance(row.get("choice_count_violation"), dict):
        adjustments.append({"rule": "choice_count_violation", **row["choice_count_violation"]})
    repair = None
    if str(row.get("answer_repaired") or "").strip():
        repair = {
            "value": row.get("answer_repaired"),
            "method": row.get("repair_method", ""),
            "by": row.get("repaired_by", ""),
            "at": row.get("repaired_at", ""),
        }
    return Answer(
        twin_id=str(row.get("persona_id", "")),
        question_id=str(row.get("question_id", "")),
        question_text=str(row.get("question_text", "")),
        question_type=str(row.get("question_type", "")),
        value=str(row.get("answer_value", "")),
        label=str(row.get("answer_label", "")),
        raw=str(row.get("answer_raw", "")),
        status=status,
        adjustments=adjustments,
        repair=repair,
        embedded_data=dict(row.get("embedded_data") or {}),
        code=str(row.get("answer_code") or ""),
        shaped_by_repaired=list((marks or {}).get((str(row.get("persona_id", "")), str(row.get("question_id", ""))), [])),
    )


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


class Results:
    """What a run returns. Save it with ``save(folder)``; reopen with ``Results.load``."""

    def __init__(
        self,
        *,
        rows: list[dict[str, Any]],
        twins: list[dict[str, Any]],
        audit: list[dict[str, Any]],
        usage: list[dict[str, Any]],
        warnings: list[str],
        provenance: dict[str, Any],
        run: dict[str, Any],
        prompts: list[dict[str, Any]] | None = None,
    ) -> None:
        self.rows = rows
        self.twins = twins
        self.audit = audit
        self.usage = usage
        self.warnings = warnings
        self.provenance = provenance
        self.run = run
        self.prompts = prompts or []
        # The survey this run asked (its whole definition): saved as survey.json,
        # read when first asked for.
        self._survey: Any = None
        self._survey_file: Path | None = None
        self._survey_problem = ""

    @property
    def survey(self) -> Any:
        """The Survey this run asked, as it was (rules and expressions, drops and
        edits), to check again or plan again: ``st.plan(results.survey, config)``."""
        if self._survey is None and self._survey_file is not None:
            self._survey = self._read_survey_file(self._survey_file)
        if self._survey is None:
            raise ResultsError(self._survey_problem or "This run was saved without its survey (survey.json); it was made before the library kept it")
        return self._survey

    def _read_survey_file(self, path: Path) -> Any:
        from ..survey import Survey

        self._survey_file = None
        try:
            survey = Survey.from_dict(json.loads(path.read_text(encoding="utf-8")))
        except Exception as exc:  # noqa: BLE001 - a damaged file must not stop the rest of the run loading
            self._survey_problem = f"{path}: the saved survey cannot be read ({exc})"
            return None
        expected = str((self.provenance or {}).get("survey_checksum_sha256") or "")
        if expected and survey.checksum != expected:
            self._survey_problem = f"{path}: survey.json is not the survey this run asked (its checksum differs from the run's)"
            return None
        return survey

    def __repr__(self) -> str:
        return (
            f"Results({len(self.twins)} twins, {len(self.rows)} answers, "
            f"{len(self.flagged())} flagged, mode={self.run.get('mode')})"
        )

    # -- data -----------------------------------------------------------------------

    @property
    def answers(self) -> list[Answer]:
        marks = self._repair_marks()
        return [_answer_from_row(r, marks) for r in self.rows]

    def _repair_marks(self) -> dict[tuple[str, str], list[str]]:
        """Answers given to text, options or visibility from an answer repaired
        since, worked out from the repairs stored."""
        from ..answers.repair_marks import shaped_by_repaired

        return shaped_by_repaired(self.rows, self._definition())

    def _expanded(self, value: Any) -> Any:
        """``value`` with each stored image key replaced by the image (``run["media"]``)."""
        media = self.run.get("media") or {}
        if not media:
            return value
        if isinstance(value, str):
            return media.get(value, value)
        if isinstance(value, list):
            return [self._expanded(item) for item in value]
        if isinstance(value, dict):
            return {key: self._expanded(item) for key, item in value.items()}
        return value

    def _definition(self) -> dict[str, Any] | None:
        """The survey as this run asked it, in the standard format. A run saved before
        the engine ran on the standard format stored its template: it is read into
        the standard format here."""
        if "_definition_cache" not in self.__dict__:
            definition = self.run.get("definition")
            if definition is None and isinstance(self.run.get("logic_template"), dict):
                from ..spec.read_template import survey_from_template

                definition = survey_from_template(self.run["logic_template"])
            self._definition_cache = self._expanded(definition) if definition is not None else None
        return self._definition_cache

    def _question_logic(self) -> dict[str, Any] | None:
        """question_logic.json: the survey as the website's export writes it (its
        template shape), from the definition; a run saved before kept its own."""
        if isinstance(self.run.get("logic_template"), dict):
            return self.run["logic_template"]
        if isinstance(self.run.get("definition"), dict):
            from ..spec.compile import compile_survey

            return json.loads(json.dumps(compile_survey(self.run["definition"]), indent=2, ensure_ascii=False))
        return None

    def _copies(self) -> list[list[dict[str, Any]]]:
        """Each static twin's copy, as its shown questions in order (a run saved
        before stored template copies: they are read here)."""
        if "copies" in self.run:
            blocks_per_twin = self._expanded(self.run.get("copies") or [])
            return [[q for block in blocks for q in block.get("questions") or []] for blocks in blocks_per_twin]
        from ..engine.legacy import shown_questions

        return [shown_questions(p.get("Elements")) for p in self._expanded(self.run.get("participants") or []) if isinstance(p, dict)]

    @property
    def exposures(self) -> dict[str, list[dict[str, Any]]]:
        """What each twin was shown in conjoint and MaxDiff tasks, joined with its
        answers: ``{"conjoint": [...], "maxdiff": [...]}``, one row per twin, task
        and option (or item) shown. Empty lists when the survey has no such block."""
        from ..experiments.exposure import exposure_tables

        return exposure_tables(self.audit, self.rows)

    def flagged(self) -> list[Answer]:
        """Answers that came back unusable: skipped or out of range, and not yet repaired."""
        return [
            a
            for a in self.answers
            if a.status in FLAGGED_STATUSES and not a.repair
        ]

    @property
    def usage_totals(self) -> dict[str, Any]:
        """Calls and the token counts the providers reported. There are no prices."""
        totals = {"calls": 0, "input_tokens": 0, "output_tokens": 0, "reasoning_tokens": 0, "total_tokens": 0}
        for u in self.usage:
            totals["calls"] += 1
            for key in ("input_tokens", "output_tokens", "reasoning_tokens", "total_tokens"):
                totals[key] += int(u.get(key) or 0)
        return totals

    def summary_stats(self) -> list[dict[str, Any]]:
        """Per-question distributions (the website's summary statistics)."""
        return compute_summary_stats(self._export_rows())

    def to_pandas(self):
        try:
            import pandas as pd
        except ImportError as exc:  # pragma: no cover
            raise ResultsError("pandas is not installed: pip install 'surveytwin[pandas]'") from exc
        return pd.DataFrame([asdict(a) for a in self.answers])

    # -- the export files ----------------------------------------------------------------

    def _export_rows(self) -> list[dict[str, Any]]:
        """The rows as the website's database returns them: sorted by twin, then question
        id, with embedded data as a JSON string and an empty status as None."""
        out: list[dict[str, Any]] = []
        marks = self._repair_marks()
        for row in sorted(self.rows, key=lambda r: (str(r.get("persona_id", "")), str(r.get("question_id", "")))):
            db_row = {
                "persona_id": row.get("persona_id"),
                "question_id": row.get("question_id"),
                "question_text": row.get("question_text"),
                "question_type": row.get("question_type"),
                "answer_raw": row.get("answer_raw", ""),
                "answer_value": row.get("answer_value"),
                "answer_label": row.get("answer_label"),
                "answer_code": row.get("answer_code") or None,
                "embedded_data_json": json.dumps(row.get("embedded_data") or {}, ensure_ascii=False),
                "answer_status": row.get("answer_status") or None,
                "answer_repaired": row.get("answer_repaired") or None,
                "repair_method": row.get("repair_method") or None,
                "repaired_by": row.get("repaired_by") or None,
                "repaired_at": row.get("repaired_at") or None,
                "shaped_by_repaired": ",".join(marks.get((str(row.get("persona_id", "")), str(row.get("question_id", ""))), [])) or None,
            }
            out.append(db_row)
        return out

    def _manifest_config(self) -> dict[str, Any]:
        cfg = self.run.get("config", {})
        excluded = self.run.get("excluded_blocks") or {}
        bank = cfg.get("bank", {})
        return {
            "model": (cfg.get("model") or {}).get("name"),
            "model_settings": cfg.get("model"),
            "persona_type": cfg.get("representation_label") or cfg.get("representation"),
            "persona_count": cfg.get("sample_size"),
            "seed": cfg.get("seed"),
            "filters": cfg.get("filters") or {},
            "persona_bank": {"bank_id": bank.get("bank_id"), "name": bank.get("name"), "version": bank.get("version")},
            "report_mode": "none",
            "system_prompt": self.run.get("system_prompt"),
            "system_instruction": cfg.get("system_instruction"),
            "use_batch_api": bool(cfg.get("batch")),
            # the website's shape ({survey: [block IDs]}), with each dropped block's name beside it
            "qsf_block_exclusions": {"survey": list(excluded)} if isinstance(excluded, dict) and excluded else {},
            "qsf_block_names": dict(excluded) if isinstance(excluded, dict) else {},
            "embedded_data": cfg.get("embedded_data") or {},
            "carry_stage_images": cfg.get("carry_stage_images"),
            "max_stages": cfg.get("max_stages"),
        }

    def write_exports(self, folder: str | Path, *, report: bool = True, text_files: bool = True, bundle: bool = False) -> dict[str, Path]:
        """Write the website's export files into ``folder``; returns them by kind."""
        out = Path(folder)
        out.mkdir(parents=True, exist_ok=True)
        long_rows = self._export_rows()
        persona_demographics = {str(t["id"]): dict(t.get("attributes") or {}) for t in self.twins}
        embedded_data_by_persona, embedded_data_cols = ex._collect_embedded_data_by_persona(long_rows)
        paths = {
            "csv_labels": out / "responses_labels.csv",
            "csv_values": out / "responses_values.csv",
            "csv_repaired": out / "responses_repaired.csv",
            "csv_labels_raw": out / "responses_labels_raw.csv",
            "csv_values_raw": out / "responses_values_raw.csv",
            "wide_csv": out / "wide.csv",
            "long_csv": out / "long.csv",
            "manifest": out / "manifest.json",
            "question_logic": out / "question_logic.json",
        }
        from .layout import Layout

        layout = Layout(self._definition(), self._copies())
        template = self._question_logic()
        value_maps = layout.value_maps()
        header, description_row, import_id_row, label_rows, persona_attr_cols, embedded_data_cols, question_cols = ex._build_surveytwin_csv(
            long_rows, persona_demographics, embedded_data_by_persona, embedded_data_cols, answer_resolver=ex._resolve_label_answer
        )

        def public(resolver):
            return ex._build_public_surveytwin_csv(
                long_rows,
                persona_demographics,
                embedded_data_by_persona,
                embedded_data_cols,
                answer_resolver=resolver,
                layout=layout,
            )

        pub_header, pub_desc, pub_import, pub_labels = public(ex._resolve_label_answer)
        _, _, _, pub_values = public(lambda row: ex._resolve_value_answer(row, question_value_maps=value_maps))
        _, _, _, pub_repaired = public(ex._resolve_repaired_answer)

        def write_csv(path: Path, rows: list[list[str]]) -> None:
            with path.open("w", encoding="utf-8", newline="") as f:
                writer = csv.writer(f)
                writer.writerow(pub_header)
                writer.writerow(pub_desc)
                writer.writerow(pub_import)
                writer.writerows(rows)

        write_csv(paths["csv_labels"], pub_labels)
        write_csv(paths["csv_values"], pub_values)
        write_csv(paths["csv_repaired"], pub_repaired)
        emit_raw = any(str(r.get("answer_repaired") or "").strip() for r in long_rows)
        if emit_raw:
            _, _, _, raw_labels = public(lambda row: ex._resolve_label_answer(row, use_repaired=False))
            _, _, _, raw_values = public(
                lambda row: ex._resolve_value_answer(row, question_value_maps=value_maps, use_repaired=False)
            )
            write_csv(paths["csv_labels_raw"], raw_labels)
            write_csv(paths["csv_values_raw"], raw_values)
        else:
            for key in ("csv_labels_raw", "csv_values_raw"):
                if paths[key].exists():
                    paths[key].unlink()
                paths.pop(key)
        wide_header, wide_rows = ex._build_legacy_wide_rows(
            data_rows=label_rows,
            persona_attr_cols=persona_attr_cols,
            embedded_data_cols=embedded_data_cols,
            question_cols=question_cols,
        )
        with paths["wide_csv"].open("w", encoding="utf-8", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=wide_header)
            writer.writeheader()
            for row in wide_rows:
                writer.writerow(row)
        long_header, legacy_long = ex._build_legacy_long_rows(
            long_rows=long_rows,
            persona_demographics=persona_demographics,
            embedded_data_by_persona=embedded_data_by_persona,
            persona_attr_cols=persona_attr_cols,
            embedded_data_cols=embedded_data_cols,
        )
        with paths["long_csv"].open("w", encoding="utf-8", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=long_header)
            writer.writeheader()
            for row in legacy_long:
                writer.writerow(row)
        if template is not None:
            paths["question_logic"].write_text(json.dumps(template, ensure_ascii=False, indent=2), encoding="utf-8")
        else:
            paths.pop("question_logic")
        # A survey written in the standard format also gets its definition as the run
        # asked it (question_logic.json, which the report reads, is its template form).
        definition_path = out / "survey_definition.json"
        if self.provenance.get("survey_kind") == "standard" and isinstance(self.run.get("definition"), dict):
            definition_path.write_text(json.dumps(self.run["definition"], ensure_ascii=False, indent=2), encoding="utf-8")
            paths["survey_definition"] = definition_path
        elif definition_path.exists():
            definition_path.unlink()

        # What each twin was shown in conjoint and MaxDiff tasks (only when asked)
        from ..experiments.exposure import columns as exposure_columns

        for kind, rows in self.exposures.items():
            path = out / f"{kind}_exposures.csv"
            if rows:
                with path.open("w", encoding="utf-8", newline="") as f:
                    writer = csv.DictWriter(f, fieldnames=exposure_columns(rows))
                    writer.writeheader()
                    writer.writerows(rows)
                paths[f"{kind}_exposures"] = path
            elif path.exists():
                path.unlink()

        question_metadata: list[dict[str, Any]] = []
        seen: set[str] = set()
        for row in long_rows:
            qid = str(row.get("question_id", ""))
            if qid and qid not in seen:
                seen.add(qid)
                question_metadata.append({"question_id": qid, "question_text": row.get("question_text", ""), "question_type": row.get("question_type", "")})
        summary_rows = compute_summary_stats(long_rows)
        totals = self.usage_totals
        manifest: dict[str, Any] = {
            "export_manifest_version": ex.EXPORT_MANIFEST_VERSION,
            "generated_at": self.provenance.get("finished_at") or ex.utcnow_iso(),
            "run_id": self.run.get("run_id", ""),
            "thread_id": "",
            "message_id": "",
            "research_question": "",
            "config": self._manifest_config(),
            "usage": {k: totals.get(k, 0) for k in ("calls", "input_tokens", "output_tokens", "reasoning_tokens", "total_tokens")},
            "run_status": {"status": "completed", "batch_phase": self.run.get("batch_phase", ""), "error_text": ""},
            "simulation_warnings": list(dict.fromkeys(str(w) for w in self.warnings if str(w).strip())),
            "row_counts": {
                "data_rows": len(label_rows),
                "questions": len(question_metadata),
                "persona_attributes": len(persona_attr_cols),
                "embedded_data_fields": len(embedded_data_cols),
                "summary_metrics": len(summary_rows),
            },
            "question_metadata": question_metadata,
            "question_columns": question_cols,
            "persona_attribute_columns": persona_attr_cols,
            "embedded_data_columns": embedded_data_cols,
            "source_artifact_ids": [],
            "source_artifacts": [],
            "report": {"mode": "none", "generated": False, "source": "none"},
            "provenance": self.provenance,
            "files": [],
        }
        for kind in ("csv_labels", "csv_values", "csv_repaired", "wide_csv", "long_csv", "csv_labels_raw", "csv_values_raw", "question_logic", "survey_definition", "conjoint_exposures", "maxdiff_exposures"):
            path = paths.get(kind)
            if path is not None and path.exists():
                name = {"question_logic": "question_logic_json", "survey_definition": "survey_definition_json"}.get(kind, kind)
                manifest["files"].append({"kind": name, "file_name": path.name, "sha256": _sha256(path), "bytes": path.stat().st_size})
        if self.run.get("mode") == "staged":
            manifest["qsf_staged_runtime"] = {
                "artifact_id": "",
                "execution_mode": "staged",
                "persona_count": len(self.twins),
                "response_rows": len(self.rows),
                "stage_calls": sum(int(a.get("stage_count", 0) or 0) for a in self.audit),
                "analysis": self.run.get("analysis", {}),
            }
        paths["manifest"].write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        if report:
            from .report import write_report

            try:
                write_report(out, run_id=str(self.run.get("run_id", "run")))
                paths["report_html"] = out / "report.html"
                paths["report_json"] = out / "report.json"
            except Exception as exc:  # the website treats the report as best effort, too
                self.warnings.append(f"The validation report could not be built: {exc}")
        if text_files:
            paths.update(self._write_text_files(out, long_rows))
        if bundle:
            bundle_path = out / "export_bundle.zip"
            with zipfile.ZipFile(bundle_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
                archive.writestr("questions/", b"")
                archive.writestr("answers/", b"")
                for kind in ("csv_labels", "csv_values", "csv_repaired", "long_csv", "csv_labels_raw", "csv_values_raw", "report_html"):
                    path = paths.get(kind)
                    if path is None and kind == "report_html" and (out / "report.html").exists():
                        path = out / "report.html"  # the report the folder already holds
                    if path is not None and path.exists():
                        archive.write(path, arcname=path.name)
                by_persona = ex._group_rows_by_persona(long_rows)
                for persona_id in sorted(by_persona):
                    archive.writestr(f"questions/{persona_id}.txt", ex._build_questions_text(persona_id, by_persona[persona_id]).encode("utf-8"))
                    archive.writestr(f"answers/{persona_id}.txt", ex._build_answers_text(persona_id, by_persona[persona_id]).encode("utf-8"))
            paths["bundle_zip"] = bundle_path
        return paths

    def _write_text_files(self, out: Path, long_rows: list[dict[str, Any]]) -> dict[str, Path]:
        by_persona = ex._group_rows_by_persona(long_rows)
        qdir = out / "questions"
        adir = out / "answers"
        qdir.mkdir(parents=True, exist_ok=True)
        adir.mkdir(parents=True, exist_ok=True)
        for persona_id in sorted(by_persona):
            rows = by_persona[persona_id]
            (qdir / f"{persona_id}.txt").write_bytes(ex._build_questions_text(persona_id, rows).encode("utf-8"))
            (adir / f"{persona_id}.txt").write_bytes(ex._build_answers_text(persona_id, rows).encode("utf-8"))
        return {"questions_dir": qdir, "answers_dir": adir}

    def _export_one(self, kind: str, path: str | Path) -> Path:
        target = Path(path)
        with tempfile.TemporaryDirectory(prefix="surveytwin-export-") as tmp:
            files = self.write_exports(tmp, report=False, text_files=False)
            source = files.get(kind)
            if source is None:
                raise ResultsError(f"No {kind} file for this run")
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)
        return target

    def to_qualtrics_csv(self, path: str | Path, *, values: bool = False, repaired: bool = False) -> Path:
        """The Qualtrics-layout CSV: labels (default), coded values, or labels with repairs applied."""
        if values and repaired:
            raise ResultsError("Choose values=True or repaired=True, not both")
        kind = "csv_values" if values else ("csv_repaired" if repaired else "csv_labels")
        return self._export_one(kind, path)

    def to_long_csv(self, path: str | Path) -> Path:
        return self._export_one("long_csv", path)

    def to_wide_csv(self, path: str | Path) -> Path:
        return self._export_one("wide_csv", path)

    def to_text(self, folder: str | Path) -> Path:
        """``questions/<twin_id>.txt`` and ``answers/<twin_id>.txt``, as in the website's download."""
        out = Path(folder)
        self._write_text_files(out, self._export_rows())
        return out

    def report(self, path: str | Path | None = None, *, format: str = "html") -> dict[str, Any]:
        """The validation report; writes ``report.html`` or ``report.json`` when a path is given."""
        if format not in {"html", "json"}:
            raise ResultsError("format must be 'html' or 'json'")
        with tempfile.TemporaryDirectory(prefix="surveytwin-report-") as tmp:
            files = self.write_exports(tmp, report=True, text_files=False)
            if "report_json" not in files:
                raise ResultsError("The validation report could not be built; see results.warnings")
            data = json.loads(Path(files["report_json"]).read_text(encoding="utf-8"))
            if path is not None:
                target = Path(path)
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(files["report_html" if format == "html" else "report_json"], target)
        return data

    # -- reruns ----------------------------------------------------------------------

    def rerun_counts(self) -> dict[str, Any]:
        """How many twins and answers a rerun would re-ask."""
        from ..answers.rerun import flagged_twins

        counts = flagged_twins(self.rows)
        return {"twins": len(counts), "flagged_answers": sum(counts.values())}

    def rerun_flagged(self, *, api_key: str | None = None, model: Any = None, max_concurrency: int = 8, on_usage: Any = None) -> "Results":
        """Re-ask only the flagged answers; returns a new Results with repairs added.

        Originals never change: a repair is stored beside the answer. The model is
        the run's own unless given; a defined model (HTTP or function) cannot be
        restored from a saved run, so pass it again after ``Results.load``.
        """
        import asyncio
        import copy
        import os

        from ..answers.rerun import rerun_all
        from ..plan import system_instruction
        from ..providers.caller import make_caller
        from ..providers.models import Model

        the_model = model if isinstance(model, Model) else None
        if the_model is None and isinstance(model, str):
            the_model = Model.preset(model)
        if the_model is None:
            the_model = getattr(self, "_model", None)
        if the_model is None:
            # a built-in model is made again from its recorded settings
            the_model = Model.from_settings((self.run.get("config") or {}).get("model") or {})
            if the_model is None:
                raise ResultsError(
                    "This run used a defined model; pass it again: rerun_flagged(model=...), "
                    "or on the command line: surveytwin rerun FOLDER --model FILE.py:NAME"
                )
        if the_model.kind == "preset":
            if not (api_key or os.environ.get(str(the_model.api_key_env or ""), "").strip()):
                raise ResultsError(f"No key: pass api_key= or set {the_model.api_key_env}")
        else:
            from ..run import require_model_env

            require_model_env(the_model, api_key=api_key)
        new_usage: list[dict[str, Any]] = []

        def record(item: Any) -> None:
            new_usage.append(item.to_dict())
            if on_usage is not None:
                on_usage(item)

        rows = copy.deepcopy(self.rows)
        instruction = (self.run.get("config") or {}).get("system_instruction")
        # Images the run left out because they could not be loaded stay out; when
        # the run's model took no images, they were left out for that reason only.
        run_model = (self.run.get("config") or {}).get("model") or {}
        left_out = list(self.provenance.get("images_left_out") or []) if run_model.get("images", True) else []

        async def go() -> dict[str, Any]:
            caller = make_caller(the_model, api_key=api_key, on_usage=record)
            try:
                with system_instruction(instruction):
                    return await rerun_all(
                        caller=caller,
                        rows=rows,
                        persona_texts=dict(self.run.get("rerun_personas") or {}),
                        definition=self._definition(),
                        concurrency=max_concurrency,
                        shown_by_twin={str(a.get("persona_id")): a.get("shown") or {} for a in self.audit if isinstance(a, dict)},
                        blocked_image_urls=left_out,
                        images=the_model.images,
                    )
            finally:
                await caller.close()

        outcome = asyncio.run(go())
        warnings = list(self.warnings)
        if outcome.get("missing_persona_text"):
            warnings.append(f"{len(outcome['missing_persona_text'])} flagged twin(s) could not be re-asked: their persona text was not kept.")
        for err in outcome.get("errors") or []:
            warnings.append(f"Rerun failed for {err.get('persona_id')}: {err.get('error')}")
        if outcome.get("not_asked"):
            reason = "the model takes no image input" if not the_model.images else "their image cannot be loaded"
            warnings.append(
                f"{outcome['not_asked']} flagged question(s) that show an image were not re-asked ({reason}); "
                "a question is never answered without its picture."
            )
        if outcome.get("same_item"):
            warnings.append(
                f"{outcome['same_item']} best/worst pair(s) named the same item twice when asked again; "
                "those repairs were not stored and the answers stay flagged."
            )
        if outcome.get("broken_rankings"):
            warnings.append(
                f"{outcome['broken_rankings']} ranking(s) were still not a valid ranking when asked again (a rank "
                "given twice or skipped, or fewer items ranked than the question requires); those repairs were not "
                "stored and the answers stay flagged."
            )
        from ..answers.repair_marks import shaped_by_repaired

        marked = len(shaped_by_repaired(rows, self._definition()))
        if marked:
            warnings.append(
                f"{marked} answer(s) were given to text, options or visibility that came from an answer "
                "repaired since; they are marked (Answer.shaped_by_repaired, and the shaped_by_repaired column of long.csv)."
            )
        from .._version import __version__
        from ..prompts import PROMPT_VERSION

        made_with = str(self.provenance.get("library_version") or "")
        if made_with and made_with != __version__:
            # a saved run is re-asked by the library that loads it, never silently
            warnings.append(
                f"This run was made with surveytwin {made_with}; its flagged answers were asked again with {__version__}."
            )
        provenance = dict(self.provenance)
        provenance.setdefault("reruns", [])
        provenance["reruns"] = list(provenance["reruns"]) + [
            {"at": now_iso(), "model": the_model.settings, "twins": outcome["twins"], "flagged": outcome["flagged"], "repaired": outcome["repaired"],
             "marked": marked, "library_version": __version__, "prompt_version": PROMPT_VERSION}
        ]
        result = Results(
            rows=rows,
            twins=copy.deepcopy(self.twins),
            audit=copy.deepcopy(self.audit),
            usage=list(self.usage) + new_usage,
            warnings=warnings,
            provenance=provenance,
            run=self.run,
            prompts=self.prompts,
        )
        result._model = the_model
        result._survey, result._survey_file, result._survey_problem = self._survey, self._survey_file, self._survey_problem
        return result

    # -- saving and loading ---------------------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        return {
            "format": "surveytwin-results",
            "format_version": 1,
            "rows": self.rows,
            "twins": self.twins,
            "audit": self.audit,
            "usage": self.usage,
            "warnings": self.warnings,
            "provenance": self.provenance,
            "run": self.run,
        }

    def save(self, folder: str | Path) -> Path:
        """The run folder: every export file, the text files, audit, prompts, and results.json.

        Saving into a folder that holds an earlier save removes the files of that
        save this one does not write again (another run's prompts, the text files
        of twins this run did not ask, an old bundle), so the folder holds one run.
        Files the library did not write are left alone and are not checksummed."""
        out = Path(folder)
        out.mkdir(parents=True, exist_ok=True)
        before = set(_run_files(out))
        written = [p for p in self.write_exports(out, report=True, text_files=True).values() if p.is_file()]
        (out / "results.json").write_text(json.dumps(self.to_dict(), ensure_ascii=False, indent=1, default=str), encoding="utf-8")
        with (out / "audit.jsonl").open("w", encoding="utf-8") as f:
            for item in self.audit:
                f.write(json.dumps(item, ensure_ascii=False, default=str) + "\n")
        written += [out / "results.json", out / "audit.jsonl"]
        if self.prompts:
            with (out / "prompts.jsonl").open("w", encoding="utf-8") as f:
                for item in self.prompts:
                    f.write(json.dumps(item, ensure_ascii=False, default=str) + "\n")
            written.append(out / "prompts.jsonl")
        survey = self._survey
        if survey is None and self._survey_file is not None:
            survey = self._read_survey_file(self._survey_file)
            self._survey = survey
        if survey is not None:
            # the survey definition itself, covered by checksums.json below
            (out / "survey.json").write_text(json.dumps(survey.to_dict(), ensure_ascii=False, indent=1), encoding="utf-8")
            written.append(out / "survey.json")
        by_persona = ex._group_rows_by_persona(self._export_rows())
        written += [out / sub / f"{persona_id}.txt" for persona_id in by_persona for sub in ("questions", "answers")]
        for stale in before - set(written) - {out / "checksums.json"}:
            stale.unlink()  # an earlier save's file this run did not write
        _write_checksums(out)
        return out

    @classmethod
    def load(cls, folder: str | Path, *, verify: bool = True) -> "Results":
        root = Path(folder)
        path = root / "results.json"
        if not path.exists():
            raise ResultsError(f"{root}: no results.json; not a saved run folder")
        if verify and (root / "checksums.json").exists():
            expected = json.loads((root / "checksums.json").read_text(encoding="utf-8"))
            for name, digest in expected.items():
                file_path = root / name
                if not file_path.exists():
                    raise ResultsError(f"{root}: {name} is missing")
                if _sha256(file_path) != digest:
                    raise ResultsError(f"{root}: {name} does not match its checksum")
        data = json.loads(path.read_text(encoding="utf-8"))
        if data.get("format") != "surveytwin-results":
            raise ResultsError(f"{path}: not a surveytwin results file")
        prompts: list[dict[str, Any]] = []
        if (root / "prompts.jsonl").exists():
            prompts = [json.loads(line) for line in (root / "prompts.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]
        result = cls(
            rows=data["rows"],
            twins=data["twins"],
            audit=data["audit"],
            usage=data["usage"],
            warnings=data["warnings"],
            provenance=data["provenance"],
            run=data["run"],
            prompts=prompts,
        )
        if (root / "survey.json").exists():
            result._survey_file = root / "survey.json"
        return result


# The files a save writes in a run folder (and questions/*.txt, answers/*.txt).
_RUN_FILE_NAMES = (
    "responses_labels.csv",
    "responses_values.csv",
    "responses_repaired.csv",
    "responses_labels_raw.csv",
    "responses_values_raw.csv",
    "wide.csv",
    "long.csv",
    "manifest.json",
    "question_logic.json",
    "survey_definition.json",
    "conjoint_exposures.csv",
    "maxdiff_exposures.csv",
    "report.html",
    "report.json",
    "results.json",
    "audit.jsonl",
    "prompts.jsonl",
    "survey.json",
    "export_bundle.zip",
    "checksums.json",
)


def _run_files(folder: Path) -> list[Path]:
    """The library's own files in a run folder; nothing else a user put there."""
    found = [folder / name for name in _RUN_FILE_NAMES if (folder / name).is_file()]
    for sub in ("questions", "answers"):
        if (folder / sub).is_dir():
            found += sorted(p for p in (folder / sub).glob("*.txt") if p.is_file())
    return found


def _write_checksums(folder: Path) -> None:
    checksums = {
        str(p.relative_to(folder)): _sha256(p)
        for p in sorted(_run_files(folder))
        if p.name != "checksums.json"
    }
    (folder / "checksums.json").write_text(json.dumps(checksums, indent=1), encoding="utf-8")


def update_checksums(folder: str | Path, rebuilt: Any) -> None:
    """Record the files just rebuilt in a run folder (the report, the export
    files; a folder of text files counts as its files) in its checksums.json, so
    the folder still loads. Every other entry is kept, so a file changed by hand
    is still caught; an entry for a library file that a rebuild removed (the
    files of the original answers, once nothing is repaired) goes. A folder saved
    without checksums is left without them."""
    root = Path(folder)
    target = root / "checksums.json"
    if not target.exists():
        return
    checksums = json.loads(target.read_text(encoding="utf-8"))
    for path in (Path(p) for p in rebuilt):
        for file in sorted(path.glob("*.txt")) if path.is_dir() else [path]:
            if file.is_file():
                checksums[str(file.relative_to(root))] = _sha256(file)
    for name in [n for n in checksums if n in _RUN_FILE_NAMES and not (root / n).exists()]:
        del checksums[name]
    target.write_text(json.dumps(dict(sorted(checksums.items())), indent=1), encoding="utf-8")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
