"""Persona banks: the default Twin-2K-500 package and any Croissant Twins bank.

A bank folder or zip is recognized by its ``croissant.json``, which must name
the Croissant Twins 0.1 profile in ``conformsTo``. It is checked and ingested
with the website's pipeline into a local SQLite file (cached), then served by
the website's bank runtime, so persona text, representation labels, filters
and sampling are the same as on the website.
"""

from __future__ import annotations

import difflib
import hashlib
import json
import os
import shutil
import zipfile
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

from .._display import Listing, plural, short_list, table
from ..errors import BankError, FilterError
from .ct import catalog, ingest
from .ct._db import db_path_override
from .ct.iris import CT_PROFILE_IRIS
from .ct.manifest import CTManifest
from .ct.payload_store import LocalDirBlobStore
from .ct.runtime import BankNotFoundError, CroissantTwinsBank
from .wrapping import EMPTY_PERSONA_PROMPT, wrap_untrusted_persona_block

DEFAULT_BANK = "twin-2k-500"
# Where the default package comes from. Open question 18 (where the package is
# published) is not settled; until it is, point this at a local copy.
DEFAULT_BANK_ENV = "SURVEYTWIN_TWIN2K_BANK"
DEFAULT_BANK_HF_REPO_ENV = "SURVEYTWIN_TWIN2K_HF_REPO"
EMPTY_REPRESENTATION = "empty"
_ARTIFACT_FORMAT = 1


def cache_dir() -> Path:
    root = os.environ.get("SURVEYTWIN_CACHE_DIR", "").strip()
    return Path(root) if root else Path.home() / ".cache" / "surveytwin"


@dataclass
class Representation:
    id: str
    label: str
    kind: str
    description: str
    is_default: bool
    coverage: int
    media_type: str


@dataclass
class FilterField:
    name: str
    label: str
    type: str
    values: list[dict[str, Any]] = field(default_factory=list)
    min: float | None = None
    max: float | None = None


@dataclass
class FilterOptions:
    fields: list[FilterField]

    def __iter__(self):
        return iter(self.fields)

    def names(self) -> list[str]:
        return [f.name for f in self.fields]

    def __str__(self) -> str:
        if not self.fields:
            return "This bank declares no filterable fields."
        rows = []
        for f in self.fields:
            if f.type == "numeric":
                span = "no values" if f.min is None else f"{f.min:g} to {f.max:g}"
                rows.append([f.name, f.label, "numeric", span])
            else:
                values = [f"{v['label']} ({v['count']:,})" for v in f.values]
                rows.append([f.name, f.label, "categorical", short_list(values)])
        return "\n".join([f"{plural(len(self.fields), 'filterable field')}:"] + table(rows))


@dataclass
class AudiencePreview:
    matching: int
    total: int
    share: float
    by_value: dict[str, dict[str, int]]
    missing_representation: int
    covers_sample_size: bool | None
    representation: str

    def __str__(self) -> str:
        lines = [f"{self.matching:,} of {self.total:,} personas match ({self.share:.1%}), with representation {self.representation}"]
        for field_name, counts in self.by_value.items():
            lines.append(f"  {field_name}: " + ", ".join(f"{v} {n:,}" for v, n in counts.items()))
        if self.missing_representation:
            lines.append(f"  {self.missing_representation:,} more match the filters but have no text in this representation")
        if self.covers_sample_size is not None:
            lines.append("  enough for the sample size: " + ("yes" if self.covers_sample_size else "no"))
        return "\n".join(lines)


@dataclass
class Twin:
    id: str
    attributes: dict[str, Any]
    representation: str
    text: str
    source: dict[str, Any]

    def __repr__(self) -> str:
        # The persona text can be long; print(twin.text) shows it.
        attrs = ", ".join(f"{k}={v!r}" for k, v in list(self.attributes.items())[:6])
        more = f", +{len(self.attributes) - 6} more" if len(self.attributes) > 6 else ""
        return f"Twin({self.id!r}, {attrs}{more}; {len(self.text):,} characters of {self.representation})"


# ---------------------------------------------------------------------------
# locating and recognizing a bank
# ---------------------------------------------------------------------------


def _safe_extract_zip(zip_path: Path, dest: Path) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path) as archive:
        for info in archive.infolist():
            name = PurePosixPath(info.filename)
            if name.is_absolute() or ".." in name.parts:
                raise BankError(f"{zip_path.name}: unsafe path inside the zip: {info.filename}")
        archive.extractall(dest)


def _find_bank_dir(root: Path) -> Path:
    """The folder holding ``croissant.json``: the root, ``root/bank``, or one below."""
    if (root / "croissant.json").is_file():
        return root
    if (root / "bank" / "croissant.json").is_file():
        return root / "bank"
    candidates = sorted(p.parent for p in root.rglob("croissant.json") if len(p.relative_to(root).parts) <= 3)
    if not candidates:
        raise BankError(
            f"{root}: not a Croissant Twins bank: no croissant.json manifest found. "
            "A bank is a folder (or zip) whose croissant.json names the Croissant Twins 0.1 profile."
        )
    if len(candidates) > 1:
        raise BankError(f"{root}: more than one croissant.json found: " + ", ".join(str(c) for c in candidates))
    return candidates[0]


def _default_bank_source() -> Path:
    local = os.environ.get(DEFAULT_BANK_ENV, "").strip()
    if local:
        return Path(local).expanduser()
    repo = os.environ.get(DEFAULT_BANK_HF_REPO_ENV, "").strip()
    if repo:
        try:
            from huggingface_hub import snapshot_download
        except ImportError as exc:  # pragma: no cover - a required dependency
            raise BankError("huggingface-hub is needed to download the default bank") from exc
        path = snapshot_download(repo_id=repo, repo_type="dataset", cache_dir=str(cache_dir() / "hf"))
        return Path(path)
    raise BankError(
        "The default Twin-2K-500 bank is not available yet: its Croissant Twins package "
        "(twin-2k-500-ct) has not been published for download. "
        f"Point {DEFAULT_BANK_ENV} at a local copy of the package folder or zip, "
        f"or {DEFAULT_BANK_HF_REPO_ENV} at the HuggingFace dataset that holds it, "
        "or pass the folder to st.bank(...)."
    )


def recognize(bank_dir: Path) -> CTManifest:
    """Read the manifest and require the Croissant Twins profile (CT001)."""
    try:
        manifest = CTManifest.from_path(bank_dir)
    except Exception as exc:  # noqa: BLE001
        raise BankError(f"{bank_dir}/croissant.json could not be read: {exc}") from exc
    if not any(c in CT_PROFILE_IRIS for c in manifest.conforms_to):
        raise BankError(
            f"{bank_dir}/croissant.json is not a Croissant Twins bank: [CT001] its conformsTo "
            "does not name the Croissant Twins 0.1 profile"
        )
    return manifest


def _fingerprint(bank_dir: Path) -> str:
    """A hash of the bank's file names and contents: the same bank gives the same
    fingerprint on any machine. Remembered by exact file stats so an unchanged
    bank is not re-read on every open."""
    files = sorted(p for p in bank_dir.rglob("*") if p.is_file())
    stats = hashlib.sha256()
    for path in files:
        stat = path.stat()
        stats.update(f"{path.relative_to(bank_dir).as_posix()}\0{stat.st_size}\0{stat.st_mtime_ns}\0".encode())
    stat_key = f"{bank_dir.resolve()}::{stats.hexdigest()}"
    memo_path = cache_dir() / "banks" / "fingerprints.json"
    try:
        memo = json.loads(memo_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        memo = {}
    if isinstance(memo, dict) and isinstance(memo.get(stat_key), str):
        return memo[stat_key]
    digest = hashlib.sha256()
    for path in files:
        digest.update(path.relative_to(bank_dir).as_posix().encode() + b"\0")
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1 << 20), b""):
                digest.update(chunk)
        digest.update(b"\0")
    fingerprint = digest.hexdigest()[:16]
    memo = memo if isinstance(memo, dict) else {}
    memo[stat_key] = fingerprint
    try:
        memo_path.parent.mkdir(parents=True, exist_ok=True)
        memo_path.write_text(json.dumps(memo, indent=0), encoding="utf-8")
    except OSError:
        pass
    return fingerprint


def _bank_id(bank_dir: Path, root: Path, source: Path) -> str:
    """The package folder's name: the folder above ``bank/`` in the website's
    layout (``twin-2k-500-ct/bank/croissant.json``), else the folder holding
    ``croissant.json``; for a zip whose files sit at its top level, the zip's name."""
    package = bank_dir.parent if bank_dir.name == "bank" else bank_dir
    if package.resolve() == root.resolve() and source.is_file():
        name = source.stem
    elif package.resolve() == root.resolve().parent and source.is_file():
        name = source.stem
    else:
        name = package.name
    return ingest._slugify(name)


def _build(bank_dir: Path, target: Path, bank_id: str) -> dict[str, Any]:
    """The website's ``artifact.build_artifact`` into ``target``, logs captured."""
    artifact_path = target / "artifact.sqlite3"
    payload_root = target / "payloads"
    if target.exists():
        shutil.rmtree(target)
    target.mkdir(parents=True)
    log_lines: list[str] = []
    try:
        with db_path_override(str(artifact_path), readonly=False):
            catalog.ensure_schema(force=True)
            result = ingest.ingest_bank(
                bank_dir,
                bank_id=bank_id,
                force=True,
                approve=True,
                verify_checksums=True,
                payload_store="object",
                blob_store=LocalDirBlobStore(payload_root),
                log=log_lines.append,
            )
        catalog.finalize_artifact_file(str(artifact_path))
    except SystemExit as exc:
        shutil.rmtree(target, ignore_errors=True)
        errors = [line.strip() for line in log_lines if "[ERROR" in line]
        raise BankError(f"{bank_dir}: {exc}" + ("\n" + "\n".join(errors) if errors else "")) from None
    except Exception as exc:  # noqa: BLE001
        shutil.rmtree(target, ignore_errors=True)
        raise BankError(f"{bank_dir}: the bank could not be read: {exc}") from exc
    record = {
        "format": _ARTIFACT_FORMAT,
        "bank_id": result.get("bank_id"),
        "version_id": result.get("version_id"),
        "log": log_lines,
    }
    (target / "built.json").write_text(json.dumps(record, indent=2), encoding="utf-8")
    return record


# ---------------------------------------------------------------------------
# the public Bank
# ---------------------------------------------------------------------------


def _render_representations(reps: list[Representation]) -> list[str]:
    rows = [[r.label, "default" if r.is_default else "", plural(r.coverage, "persona"), r.id] for r in reps]
    lines = table(rows)
    return lines + [f"  {r.label}: {r.description}" for r in reps if r.description]


class Bank:
    """A persona bank, read locally. Build one with ``st.bank(...)``."""

    def __init__(self, runtime: CroissantTwinsBank, *, source: Path, bank_dir: Path, warnings: list[str], fingerprint: str, is_default: bool) -> None:
        self._runtime = runtime
        self.source = source
        self.bank_dir = bank_dir
        self.warnings = warnings
        self.fingerprint = fingerprint
        self.is_default = is_default
        desc = runtime.describe_bank()
        self._desc = desc
        self.id = desc.bank_id
        self.name = desc.name
        self.version = desc.version
        self.persona_basis = desc.persona_basis
        self.size = desc.persona_count
        self.format = "Twin-2K-500" if is_default else "Croissant Twins"
        self.default_representation = desc.default_view_id

    def __repr__(self) -> str:
        return f"Bank({self.id!r} v{self.version}, {self.size} personas, {self.format})"

    def __str__(self) -> str:
        reps = [f"{r.label}{' (default)' if r.is_default else ''}" for r in self.representations()]
        lines = [
            f"{self.name} ({self.id} v{self.version}), {self.size:,} personas",
            f"Representations: {short_list(reps)}",
            f"Filterable fields: {short_list(self.filter_options().names(), 12) or 'none'}",
        ]
        return "\n".join(lines + [f"note: {w}" for w in self.warnings])

    # -- description ------------------------------------------------------------

    @property
    def description(self) -> dict[str, Any]:
        d = self._desc
        return {
            "bank_id": d.bank_id,
            "version": d.version,
            "name": d.name,
            "publisher": d.publisher,
            "license": d.license,
            "citation": d.citation,
            "persona_basis": d.persona_basis,
            "persona_count": d.persona_count,
            "conditions_of_access": d.conditions_of_access,
        }

    def representations(self) -> list[Representation]:
        return Listing([
            Representation(
                id=v.view_id,
                label=v.label,
                kind=v.kind,
                description=v.description,
                is_default=v.is_default,
                coverage=v.coverage_count,
                media_type=v.media_type,
            )
            for v in self._runtime.list_representation_views()
        ], title="Representations:", render=_render_representations)

    def resolve_representation(self, representation: str | None) -> str:
        """A view id; ``None`` means the bank's default view. ``empty`` stays empty."""
        if representation == EMPTY_REPRESENTATION:
            return EMPTY_REPRESENTATION
        views = self._runtime.list_representation_views()
        if not views:
            raise BankError(f"Bank {self.id} declares no representations")
        if representation is None:
            if len(views) == 1:
                return views[0].view_id
            default = next((v for v in views if v.is_default), None)
            if default is None:
                raise BankError(
                    f"Bank {self.id} has {len(views)} representations and no default; name one of: "
                    + ", ".join(v.view_id for v in views)
                )
            return default.view_id
        wanted = str(representation)
        for v in views:
            if wanted in {v.view_id, v.kind, v.label}:
                return v.view_id
        lowered = wanted.lower()
        matches = [v for v in views if lowered in {v.view_id.lower(), v.kind.lower(), v.label.lower()} or v.kind.lower().endswith(lowered)]
        if len(matches) == 1:
            return matches[0].view_id
        raise BankError(
            f"Bank {self.id} has no representation {wanted!r}; choose one of: "
            + ", ".join(f"{v.view_id} ({v.label})" for v in views)
            + f", or {EMPTY_REPRESENTATION!r} for no persona"
        )

    def representation_label(self, view_id: str) -> str:
        if view_id == EMPTY_REPRESENTATION:
            return "Empty persona"
        return self._runtime.view_label(view_id)

    # -- filters ------------------------------------------------------------------

    def filter_options(self) -> FilterOptions:
        fields: list[FilterField] = []
        for f in self._runtime.describe_filters():
            if f.is_numeric:
                with catalog.bank_db(self._runtime._resolve_data_path()):
                    values = catalog.distinct_filter_values(self._runtime.version_id, f.field_name, limit=100000)
                numbers = []
                for value, _count in values:
                    try:
                        numbers.append(float(value))
                    except (TypeError, ValueError):
                        continue
                fields.append(
                    FilterField(
                        name=f.field_name,
                        label=f.label,
                        type="numeric",
                        min=min(numbers) if numbers else None,
                        max=max(numbers) if numbers else None,
                    )
                )
                continue
            counts = dict(self._runtime.distinct_filter_values(f.field_name, limit=1000))
            labels = {str(v.get("code")): str(v.get("label") or v.get("code")) for v in (f.values or [])}
            codes = list(labels) or sorted(counts)
            for code in counts:
                if code not in labels:
                    codes.append(code)
            fields.append(
                FilterField(
                    name=f.field_name,
                    label=f.label,
                    type="categorical",
                    values=[{"value": c, "label": labels.get(c, c), "count": int(counts.get(c, 0))} for c in dict.fromkeys(codes)],
                )
            )
        return FilterOptions(fields)

    def normalize_filters(self, filters: dict[str, Any] | None) -> dict[str, Any]:
        """The design's dict format -> the bank catalog's clauses, refusing unknown names."""
        if not filters:
            return {}
        if not isinstance(filters, dict):
            raise FilterError("filters must be a dict from a field name to the values to keep")
        options = {f.name: f for f in self.filter_options().fields}
        out: dict[str, Any] = {}
        for field_name, clause in filters.items():
            if field_name not in options:
                close = difflib.get_close_matches(str(field_name), list(options), n=3, cutoff=0.3)
                hint = f" Did you mean: {', '.join(close)}?" if close else f" Filterable fields: {', '.join(options)}."
                raise FilterError(f"Bank {self.id} has no filterable field {field_name!r}.{hint}")
            spec = options[field_name]
            if isinstance(clause, dict):
                if clause.get("missing"):
                    out[field_name] = {"op": "is_missing", "value": True}
                    continue
                if "min" in clause or "max" in clause:
                    if spec.type != "numeric":
                        raise FilterError(f"{field_name!r} is not numeric; list the values to keep instead of a range")
                    out[field_name] = {"op": "range", "gte": clause.get("min"), "lte": clause.get("max")}
                    continue
                raise FilterError(f"{field_name!r}: a dict filter takes 'min'/'max' or 'missing': True")
            values = [clause] if isinstance(clause, (str, int, float)) else list(clause or [])
            if spec.type == "numeric":
                out[field_name] = {"op": "in", "values": [str(v) for v in values]}
                continue
            by_code = {str(v["value"]): str(v["value"]) for v in spec.values}
            by_label = {str(v["label"]).lower(): str(v["value"]) for v in spec.values}
            codes: list[str] = []
            for value in values:
                text = str(value)
                if text in by_code:
                    codes.append(by_code[text])
                elif text.lower() in by_label:
                    codes.append(by_label[text.lower()])
                else:
                    known = [str(v["label"]) for v in spec.values]
                    close = difflib.get_close_matches(text, known, n=3, cutoff=0.3)
                    hint = f" Did you mean: {', '.join(close)}?" if close else f" Values: {', '.join(known[:20])}."
                    raise FilterError(f"Field {field_name!r} has no value {text!r}.{hint}")
            out[field_name] = {"op": "in", "values": codes}
        return out

    def preview_filter(self, filters: dict[str, Any] | None = None, *, representation: str | None = None, sample_size: int | None = None) -> AudiencePreview:
        clauses = self.normalize_filters(filters)
        view_id = self.resolve_representation(representation)
        count_view = None if view_id == EMPTY_REPRESENTATION else view_id
        counts = self._runtime.count_eligible_personas(count_view, clauses)
        matching = int(counts.get("matching", 0))
        total_personas = int(self.size or 0)
        # personas matching the filters regardless of representation
        with catalog.bank_db(self._runtime._resolve_data_path()):
            sql, params = catalog._matching_persona_sql(self._runtime.version_id, clauses)
            from .ct._db import db_connection

            with db_connection() as conn:
                row = conn.execute(f"SELECT COUNT(*) AS c FROM ({sql}) AS m", params).fetchone()
                matching_any = int(dict(row)["c"])
                by_value: dict[str, dict[str, int]] = {}
                for field_name, clause in clauses.items():
                    if clause.get("op") != "in":
                        continue
                    placeholders = ",".join("?" for _ in clause["values"])
                    rows = conn.execute(
                        f"SELECT v.value_text AS value, COUNT(DISTINCT v.persona_id) AS c FROM ct_bank_persona_values v "
                        f"WHERE v.version_id = ? AND v.field = ? AND v.value_text IN ({placeholders}) "
                        f"AND v.persona_id IN ({sql}) GROUP BY v.value_text",
                        [self._runtime.version_id, field_name, *clause["values"], *params],
                    ).fetchall()
                    by_value[field_name] = {str(dict(r)["value"]): int(dict(r)["c"]) for r in rows}
        if view_id == EMPTY_REPRESENTATION:
            matching = matching_any
        return AudiencePreview(
            matching=matching,
            total=total_personas,
            share=round(matching / total_personas, 6) if total_personas else 0.0,
            by_value=by_value,
            missing_representation=max(matching_any - matching, 0),
            covers_sample_size=None if sample_size is None else matching >= int(sample_size),
            representation=view_id,
        )

    # -- sampling and persona text -------------------------------------------------

    def sample(self, *, representation: str, filters: dict[str, Any] | None, n: int, seed: int) -> list[str]:
        """Twin ids, seeded; the website's reservoir sample over the eligible set."""
        clauses = self.normalize_filters(filters)
        view = None if representation == EMPTY_REPRESENTATION else representation
        if view is None:
            # every persona is eligible for the empty baseline; sample over any view
            view = self.resolve_representation(None)
        return self._runtime.sample_personas(view, clauses, int(n), seed=int(seed))

    def _field_order(self) -> list[str]:
        if not hasattr(self, "_fields_in_order"):
            self._fields_in_order = [f.field_name for f in self._runtime.describe_filters()]
        return self._fields_in_order

    def twin(self, persona_id: str, representation: str) -> Twin:
        raw = self._runtime.persona_attrs(persona_id)
        # The ingest stores attributes in an order that depends on the Python
        # process; give them the bank's field order, then any others by name.
        order = [k for k in self._field_order() if k in raw] + sorted(k for k in raw if k not in self._field_order())
        attrs = {k: raw[k] for k in order}
        if representation == EMPTY_REPRESENTATION:
            return Twin(
                id=str(persona_id),
                attributes=attrs,
                representation=EMPTY_REPRESENTATION,
                text=EMPTY_PERSONA_PROMPT,
                source={"bank_id": self.id, "version": self.version, "representation": EMPTY_REPRESENTATION},
            )
        payload = self._runtime.load_prompt_payload(persona_id, representation)
        if payload is None:
            text = "[Persona representation unavailable for this view]"
            meta: dict[str, Any] = {}
        else:
            meta = dict(payload.metadata)
            meta["view_label"] = self._runtime.view_label(representation)
            text = wrap_untrusted_persona_block(payload.text, meta)
        return Twin(
            id=str(persona_id),
            attributes=attrs,
            representation=representation,
            text=text,
            source={
                "bank_id": self.id,
                "version": self.version,
                "representation": representation,
                "representation_id": meta.get("representation_id", ""),
            },
        )

    def payload_chars(self, persona_id: str, representation: str) -> int:
        if representation == EMPTY_REPRESENTATION:
            return 0
        rep = self._runtime.resolve_representation(persona_id, representation)
        return len(rep.payload or "") if rep else 0

    def max_payload_chars(self, representation: str) -> int:
        if representation == EMPTY_REPRESENTATION:
            return 0
        return int(self._runtime.max_payload_chars(representation) or 0)


def open_bank(name_or_path: str | os.PathLike[str] = DEFAULT_BANK) -> Bank:
    """``"twin-2k-500"`` (the default) or a Croissant Twins folder or zip."""
    is_default = str(name_or_path) == DEFAULT_BANK
    source = _default_bank_source() if is_default else Path(name_or_path).expanduser()
    if not source.exists():
        raise BankError(f"{source}: no such file or folder")
    work = cache_dir() / "banks"
    if source.is_file() and source.suffix.lower() == ".zip":
        digest = hashlib.sha256(source.read_bytes()).hexdigest()[:16]
        extracted = work / "extracted" / digest
        if not (extracted / ".complete").exists():
            if extracted.exists():
                shutil.rmtree(extracted)
            _safe_extract_zip(source, extracted)
            (extracted / ".complete").write_text("ok", encoding="utf-8")
        root = extracted
    elif source.is_dir():
        root = source
    else:
        raise BankError(f"{source}: a bank must be a folder or a .zip file")
    bank_dir = _find_bank_dir(root)
    manifest = recognize(bank_dir)
    issues = manifest.validate()
    errors = [i for i in issues if i.severity == "error"]
    if errors:
        raise BankError(
            f"{bank_dir}: the bank fails {len(errors)} Croissant Twins check(s):\n"
            + "\n".join(f"  [{i.code}] {i.message}" for i in errors)
        )
    warnings = [f"[{i.code}] {i.message}" for i in issues if i.severity != "error"]
    fingerprint = _fingerprint(bank_dir)
    bank_id = _bank_id(bank_dir, root, source)
    target = work / "built" / f"{fingerprint}-{bank_id}"
    built = target / "built.json"
    if built.exists():
        try:
            record = json.loads(built.read_text(encoding="utf-8"))
        except ValueError:
            record = {}
        if record.get("format") != _ARTIFACT_FORMAT:
            record = _build(bank_dir, target, bank_id)
    else:
        record = _build(bank_dir, target, bank_id)
    try:
        runtime = CroissantTwinsBank.load_from_artifact(str(target / "artifact.sqlite3"), payload_root=str(target / "payloads"))
    except BankNotFoundError as exc:
        raise BankError(str(exc)) from exc
    return Bank(runtime, source=source, bank_dir=bank_dir, warnings=warnings, fingerprint=fingerprint, is_default=is_default)
