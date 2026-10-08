"""Manifest-driven parser for Croissant Twins (CT 0.1) banks.

This module turns a ``croissant.json`` into a normalized, typed description of a
persona bank. It is deliberately generic: it discovers everything from the
manifest's expanded vocabulary IRIs and never checks for specific dataset names,
column names, file names, or directory layouts (per the implementation plan).

It parses metadata only — it does not read persona/representation *data*. That
keeps it cheap enough to run against the 1M-persona fixtures and makes it the
shared contract used by both the offline ingester and the serve-time runtime.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field as dc_field
from pathlib import Path
from typing import Any

from . import iris
from .iris import expand_iri, local_name, normalize_iri


class CTManifestError(ValueError):
    """Raised when a manifest cannot be parsed as a CT bank."""


@dataclass(frozen=True)
class CTIssue:
    code: str
    severity: str  # "error" | "warning"
    message: str
    pointer: str | None = None


@dataclass
class CTSource:
    """Where a field's values come from in a distributed resource."""

    file_id: str | None = None
    column: str | None = None
    json_path: str | None = None

    def read(self, record: dict) -> Any:
        """Extract this field's value from a parsed record (dict)."""
        if self.column is not None:
            return record.get(self.column)
        if self.json_path:
            return _eval_json_path(self.json_path, record)
        return None


@dataclass
class CTField:
    id: str
    name: str
    data_types: list[str]  # normalized IRIs
    equivalent_property: str | None  # normalized IRI
    source: CTSource
    filterable: bool = False
    sensitive: bool = False  # legacy ct:sensitive (retired in spec rev 2026-07-09; still honored)
    selection_dimension: bool = False  # legacy ct:selectionDimension (retired; still honored)
    is_array: bool = False
    codebook_id: str | None = None  # legacy ct:codebook (retired; still honored)
    references_id: str | None = None
    missing_values: list[Any] = dc_field(default_factory=list)
    description: str | None = None
    values: list["CTCodebookEntry"] = dc_field(default_factory=list)  # inline ct:values; array order = display order
    raw: dict = dc_field(default_factory=dict)

    @property
    def is_numeric(self) -> bool:
        return any(local_name(dt).lower() in {"integer", "float", "number", "long", "decimal"} for dt in self.data_types)


@dataclass
class CTCodebookEntry:
    code: str
    label: str | None = None
    description: str | None = None
    ordinal: int | None = None
    count: int | None = None


@dataclass
class CTCodebook:
    id: str
    name: str | None
    entries: list[CTCodebookEntry]

    def ordered_codes(self) -> list[str]:
        ents = sorted(self.entries, key=lambda e: (e.ordinal if e.ordinal is not None else 1_000_000, e.code))
        return [e.code for e in ents]


@dataclass
class CTResource:
    id: str
    type: str  # "FileObject" | "FileSet"
    content_url: str | None = None
    encoding_format: str | None = None
    sha256: str | None = None
    md5: str | None = None
    content_size: str | None = None
    includes: str | None = None  # FileSet glob
    contained_in: list[str] = dc_field(default_factory=list)


# payload modes
PAYLOAD_TEXT = "text"
PAYLOAD_STRUCTURED = "structured"
PAYLOAD_EXTERNAL = "external"


@dataclass
class CTRecordSet:
    id: str
    name: str | None
    data_types: list[str]  # normalized IRIs
    key_field_id: str | None
    fields: list[CTField]
    description: str | None = None
    derivable: bool = False
    complete_coverage: bool = False

    def field_by_id(self, fid: str) -> CTField | None:
        return next((f for f in self.fields if f.id == fid), None)

    def field_by_property(self, prop_iri: str) -> CTField | None:
        return next((f for f in self.fields if f.equivalent_property == prop_iri), None)

    @property
    def is_persona(self) -> bool:
        return iris.CT_PERSONA in self.data_types

    @property
    def is_representation(self) -> bool:
        return iris.CT_REPRESENTATION in self.data_types

    @property
    def is_enumeration(self) -> bool:
        return iris.SC_ENUMERATION in self.data_types


@dataclass
class CTRepresentationSet:
    """A representation RecordSet with its resolved CT roles."""

    record_set: CTRecordSet
    id_field: CTField | None
    persona_fk_field: CTField | None
    kind_field: CTField | None
    payload_mode: str | None
    payload_field: CTField | None
    media_type_field: CTField | None
    language_field: CTField | None
    default_field: CTField | None
    selection_dimensions: list[CTField]  # fields marked ct:selectionDimension


@dataclass
class CTManifest:
    raw: dict
    context: dict
    name: str
    description: str | None
    version: str | None
    license: str | None
    url: str | None
    same_as: list[str]
    creators: list[str]
    publisher: str | None
    citation: str | None
    date_published: str | None
    languages: list[str]
    conforms_to: list[str]  # normalized
    persona_basis: str | None  # normalized IRI
    default_representation_kind: str | None  # normalized IRI
    conditions_of_access: str | None
    usage_info: str | None
    human_data_statement: str | None  # legacy term (retired; folded into rai:dataCollection) — still read
    data_collection: str | None  # rai:dataCollection — how the data came to be (collection / generation / mix)
    target_population: dict | str | None  # plain text (spec rev 2026-07-09) or legacy structured object
    data_limitations: str | None
    data_biases: str | None
    data_use_cases: str | None
    capabilities: list[str]
    keywords: list[str]
    domain: str | None  # storefront shelf, e.g. "politics-policy" (ct:domain)
    region: str | None  # storefront display region, e.g. "United States" (ct:region)
    resources: dict[str, CTResource]
    record_sets: list[CTRecordSet]
    codebooks: dict[str, CTCodebook]
    persona_set: CTRecordSet
    representation_sets: list[CTRepresentationSet]

    # ---- convenience ----
    @property
    def persona_id_field(self) -> CTField | None:
        return self.persona_set.field_by_property(iris.CT_PERSONA_ID) or (
            self.persona_set.field_by_id(self.persona_set.key_field_id) if self.persona_set.key_field_id else None
        )

    @property
    def filter_fields(self) -> list[CTField]:
        return [f for f in self.persona_set.fields if f.filterable]

    def resource(self, rid: str | None) -> CTResource | None:
        return self.resources.get(rid) if rid else None

    # ---- factory ----
    @classmethod
    def from_path(cls, path: str | Path) -> "CTManifest":
        p = Path(path)
        if p.is_dir():
            p = p / "croissant.json"
        try:
            raw = json.loads(p.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise CTManifestError(f"croissant.json not found at {p}") from exc
        except json.JSONDecodeError as exc:
            raise CTManifestError(f"croissant.json is not valid JSON: {exc}") from exc
        return cls.from_dict(raw)

    @classmethod
    def from_dict(cls, raw: dict) -> "CTManifest":
        if not isinstance(raw, dict):
            raise CTManifestError("Manifest must be a JSON object")
        context = raw.get("@context") or {}
        if not isinstance(context, dict):
            context = {}

        def norm(v: Any) -> Any:
            return expand_iri(v, context)

        def norm_list(v: Any) -> list[str]:
            if v is None:
                return []
            items = v if isinstance(v, list) else [v]
            return [norm(i) for i in items if isinstance(i, str)]

        # --- resources ---
        resources: dict[str, CTResource] = {}
        for d in _as_list(raw.get("distribution")):
            if not isinstance(d, dict):
                continue
            rid = d.get("@id") or d.get("name")
            if not rid:
                continue
            rtype = local_name(d.get("@type", "")) or "FileObject"
            resources[rid] = CTResource(
                id=rid,
                type=rtype,
                content_url=d.get("contentUrl"),
                encoding_format=d.get("encodingFormat"),
                sha256=d.get("sha256"),
                md5=d.get("md5"),
                content_size=_as_str(d.get("contentSize")),
                includes=_first(d.get("includes")),
                contained_in=[
                    (c.get("@id") if isinstance(c, dict) else c)
                    for c in _as_list(d.get("containedIn"))
                ],
            )

        # --- record sets + codebooks ---
        record_sets: list[CTRecordSet] = []
        codebooks: dict[str, CTCodebook] = {}
        for rs in _as_list(raw.get("recordSet") or raw.get("recordset")):
            if not isinstance(rs, dict):
                continue
            parsed = _parse_record_set(rs, context)
            record_sets.append(parsed)
            if parsed.is_enumeration:
                codebooks[parsed.id] = _parse_codebook(rs, parsed)

        persona_candidates = [r for r in record_sets if r.is_persona]
        if len(persona_candidates) == 0:
            raise CTManifestError("No authoritative Persona RecordSet (dataType ct:Persona) found")
        if len(persona_candidates) > 1:
            raise CTManifestError(
                f"Multiple authoritative Persona RecordSets found: {[r.id for r in persona_candidates]}"
            )
        persona_set = persona_candidates[0]

        rep_candidates = [r for r in record_sets if r.is_representation]
        if not rep_candidates:
            raise CTManifestError("No Representation RecordSet (dataType ct:Representation) found")
        representation_sets = [_resolve_representation_set(r) for r in rep_candidates]

        return cls(
            raw=raw,
            context=context,
            name=_as_str(raw.get("name")) or "(unnamed bank)",
            description=_as_str(raw.get("description")),
            version=_as_str(raw.get("version")),
            license=_first_id_or_str(raw.get("license")),
            url=_as_str(raw.get("url")),
            same_as=[s for s in (_first_id_or_str(x) for x in _as_list(raw.get("sameAs"))) if s],
            creators=_names(raw.get("creator")),
            publisher=_first_name(raw.get("publisher")),
            citation=_as_str(raw.get("citeAs")),
            date_published=_as_str(raw.get("datePublished")),
            languages=[s for s in (_as_str(x) for x in _as_list(raw.get("inLanguage"))) if s],
            conforms_to=norm_list(raw.get("conformsTo")),
            persona_basis=_first_id_or_str_norm(raw.get("ct:personaBasis"), norm),
            default_representation_kind=_first_id_or_str_norm(raw.get("ct:defaultRepresentationKind"), norm),
            conditions_of_access=_as_str(raw.get("conditionsOfAccess") or raw.get("sc:conditionsOfAccess")),
            usage_info=_as_str(raw.get("usageInfo") or raw.get("sc:usageInfo")),
            human_data_statement=_as_str(raw.get("ct:humanDataStatement")),
            data_collection=_as_str(raw.get("rai:dataCollection")),
            target_population=raw.get("ct:targetPopulation")
            if isinstance(raw.get("ct:targetPopulation"), (dict, str)) else None,
            data_limitations=_as_str(raw.get("rai:dataLimitations")),
            data_biases=_as_str(raw.get("rai:dataBiases")),
            data_use_cases=_as_str(raw.get("rai:dataUseCases")),
            capabilities=[local_name(c) for c in norm_list(raw.get("ct:capability"))],
            keywords=[s for s in (_as_str(x) for x in _as_list(raw.get("keywords"))) if s],
            domain=_as_str(raw.get("ct:domain")),
            region=_as_str(raw.get("ct:region")),
            resources=resources,
            record_sets=record_sets,
            codebooks=codebooks,
            persona_set=persona_set,
            representation_sets=representation_sets,
        )

    # ---- validation (schema-level; data-level checks happen at ingest) ----
    def validate(self) -> list[CTIssue]:
        issues: list[CTIssue] = []

        if not any(c in iris.CROISSANT_PROFILE_IRIS for c in self.conforms_to):
            issues.append(CTIssue("CT001", "warning", "Manifest does not declare Croissant 1.1 conformance"))
        if not any(c in iris.CT_PROFILE_IRIS for c in self.conforms_to):
            issues.append(CTIssue("CT001", "error", "Manifest does not declare the CT 0.1 profile IRI"))
        if not self.persona_basis:
            issues.append(CTIssue("CT010", "warning", "Dataset-level ct:personaBasis is missing"))
        if not self.conditions_of_access:
            issues.append(CTIssue("CT011", "warning", "sc:conditionsOfAccess is missing"))
        if not self.domain or not self.region:
            # Required by the spec (CT016); warning here so pre-revision banks keep ingesting.
            issues.append(CTIssue("CT016", "warning", "ct:domain and/or ct:region is missing"))
        if self.persona_id_field is None:
            issues.append(CTIssue("CT004", "error", "Persona RecordSet has no ct:personaId key field"))

        for rep in self.representation_sets:
            ptr = rep.record_set.id
            if rep.id_field is None:
                issues.append(CTIssue("CT005", "error", "Representation set missing ct:representationId", ptr))
            if rep.persona_fk_field is None:
                issues.append(CTIssue("CT006", "error", "Representation set missing ct:persona foreign key", ptr))
            if rep.kind_field is None:
                issues.append(CTIssue("CT009", "error", "Representation set missing ct:representationKind", ptr))
            if rep.payload_mode is None:
                issues.append(CTIssue("CT008", "error", "Representation set has no resolvable payload mode", ptr))
            if not self._has_kind_descriptions(rep):
                # Spec CT014 (kind usage descriptions); warning so pre-revision banks keep ingesting.
                issues.append(CTIssue("CT014", "warning", "No kind usage description (ct:values / RecordSet description / codebook)", ptr))
        return issues

    def _has_kind_descriptions(self, rep: "CTRepresentationSet") -> bool:
        if rep.record_set.description:
            return True
        if rep.kind_field and any(e.description for e in rep.kind_field.values):
            return True
        for f in [*rep.selection_dimensions, rep.kind_field]:
            if f is None or not f.codebook_id:
                continue
            cb = self.codebooks.get(f.codebook_id)
            if cb and any(e.description for e in cb.entries):
                return True
        return False


# ---------------------------------------------------------------------------
# parsing helpers
# ---------------------------------------------------------------------------


def _parse_record_set(rs: dict, context: dict) -> CTRecordSet:
    rid = rs.get("@id") or rs.get("name") or "(recordset)"
    key = rs.get("key")
    key_id = key.get("@id") if isinstance(key, dict) else (key if isinstance(key, str) else None)
    fields = [_parse_field(f, context) for f in _as_list(rs.get("field")) if isinstance(f, dict)]
    return CTRecordSet(
        id=rid,
        name=_as_str(rs.get("name")),
        data_types=[expand_iri(dt, context) for dt in _as_list(rs.get("dataType"))],
        key_field_id=key_id,
        fields=fields,
        description=_as_str(rs.get("description")),
        derivable=bool(_ct_value(rs, context, "derivable")),
        complete_coverage=bool(_ct_value(rs, context, "completeCoverage")),
    )


def _parse_field(f: dict, context: dict) -> CTField:
    source = f.get("source") or {}
    extract = source.get("extract") or {}
    file_ref = source.get("fileObject") or source.get("fileSet") or {}
    file_id = file_ref.get("@id") if isinstance(file_ref, dict) else (file_ref if isinstance(file_ref, str) else None)
    json_path = extract.get("jsonPath") or extract.get("jsonpath")
    references = f.get("references")
    ref_id = None
    if isinstance(references, dict):
        # supports both {"@id": ...} and {"field": {"@id": ...}}
        if "@id" in references:
            ref_id = references["@id"]
        elif isinstance(references.get("field"), dict):
            ref_id = references["field"].get("@id")
    codebook = _ct_value(f, context, "codebook")
    codebook_id = codebook.get("@id") if isinstance(codebook, dict) else codebook
    return CTField(
        id=f.get("@id") or f.get("name") or "(field)",
        name=_as_str(f.get("name")) or local_name(f.get("@id", "")),
        data_types=[expand_iri(dt, context) for dt in _as_list(f.get("dataType"))],
        equivalent_property=expand_iri(f["equivalentProperty"], context) if f.get("equivalentProperty") else None,
        source=CTSource(file_id=file_id, column=extract.get("column"), json_path=json_path),
        filterable=bool(_ct_value(f, context, "filterable")),
        sensitive=bool(_ct_value(f, context, "sensitive")),
        selection_dimension=bool(_ct_value(f, context, "selectionDimension")),
        is_array=bool(f.get("isArray")),
        codebook_id=codebook_id,
        references_id=ref_id,
        missing_values=_missing_values(_ct_value(f, context, "missingValue")),
        description=_as_str(f.get("description")),
        values=_parse_inline_values(_ct_value(f, context, "values")),
        raw=f,
    )


def _parse_inline_values(v: Any) -> list[CTCodebookEntry]:
    """Parse inline ``ct:values`` — array order is display order (ordinal = index).

    Entries are strings (code = label) or objects ``{code, label?, description?}``.
    """
    entries: list[CTCodebookEntry] = []
    for i, item in enumerate(_as_list(v)):
        if isinstance(item, dict):
            code = item.get("code")
            if code is None:
                continue
            entries.append(
                CTCodebookEntry(
                    code=str(code),
                    label=_as_str(item.get("label")),
                    description=_as_str(item.get("description")),
                    ordinal=i,
                )
            )
        elif isinstance(item, (str, int, float)):
            entries.append(CTCodebookEntry(code=str(item), label=str(item), ordinal=i))
    return entries


def _resolve_representation_set(rs: CTRecordSet) -> CTRepresentationSet:
    id_field = rs.field_by_property(iris.CT_REPRESENTATION_ID)
    persona_fk = rs.field_by_property(iris.CT_PERSONA_FK) or next(
        (f for f in rs.fields if f.references_id), None
    )
    kind_field = rs.field_by_property(iris.CT_REPRESENTATION_KIND)
    media_field = rs.field_by_property(iris.SC_ENCODING_FORMAT)
    language_field = rs.field_by_property(iris.SC_IN_LANGUAGE)
    default_field = rs.field_by_property(iris.CT_IS_DEFAULT)
    selection_dims = [f for f in rs.fields if f.selection_dimension]

    # payload mode: exactly one of text / structured / external
    payload_field = None
    payload_mode = None
    if rs.field_by_property(iris.SC_TEXT):
        payload_mode, payload_field = PAYLOAD_TEXT, rs.field_by_property(iris.SC_TEXT)
    elif rs.field_by_property(iris.CT_PAYLOAD):
        payload_mode, payload_field = PAYLOAD_STRUCTURED, rs.field_by_property(iris.CT_PAYLOAD)
    elif rs.field_by_property(iris.SC_CONTENT_URL):
        payload_mode, payload_field = PAYLOAD_EXTERNAL, rs.field_by_property(iris.SC_CONTENT_URL)

    return CTRepresentationSet(
        record_set=rs,
        id_field=id_field,
        persona_fk_field=persona_fk,
        kind_field=kind_field,
        payload_mode=payload_mode,
        payload_field=payload_field,
        media_type_field=media_field,
        language_field=language_field,
        default_field=default_field,
        selection_dimensions=selection_dims,
    )


def _parse_codebook(rs_raw: dict, parsed: CTRecordSet) -> CTCodebook:
    # the inline `data` array uses field @ids as keys
    field_ids = {f.id: f for f in parsed.fields}

    def col(suffix: str) -> str | None:
        for fid in field_ids:
            if fid.endswith("/" + suffix) or local_name(fid) == suffix:
                return fid
        return None

    code_col = col("code") or (parsed.key_field_id if parsed.key_field_id else None)
    label_col = col("label")
    desc_col = col("description")
    ord_col = col("ordinal")
    count_col = col("count")
    entries: list[CTCodebookEntry] = []
    for row in _as_list(rs_raw.get("data")):
        if not isinstance(row, dict):
            continue
        code = row.get(code_col) if code_col else None
        if code is None:
            continue
        entries.append(
            CTCodebookEntry(
                code=str(code),
                label=_as_str(row.get(label_col)) if label_col else None,
                description=_as_str(row.get(desc_col)) if desc_col else None,
                ordinal=_as_int(row.get(ord_col)) if ord_col else None,
                count=_as_int(row.get(count_col)) if count_col else None,
            )
        )
    return CTCodebook(id=parsed.id, name=parsed.name, entries=entries)


# ---------------------------------------------------------------------------
# small utilities
# ---------------------------------------------------------------------------


def _as_list(v: Any) -> list:
    if v is None:
        return []
    return v if isinstance(v, list) else [v]


def _first(v: Any) -> Any:
    items = _as_list(v)
    return items[0] if items else None


def _as_str(v: Any) -> str | None:
    if v is None:
        return None
    if isinstance(v, str):
        return v
    if isinstance(v, (int, float, bool)):
        return str(v)
    return None


def _as_int(v: Any) -> int | None:
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _ct_value(d: dict, context: dict, suffix: str) -> Any:
    """Read a ``ct:<suffix>`` annotation, expanding keys so prefix choice is irrelevant."""
    target = iris.CT + suffix
    direct = d.get("ct:" + suffix)
    if direct is not None:
        return direct
    for k, v in d.items():
        if isinstance(k, str) and not k.startswith("@") and expand_iri(k, context) == target:
            return v
    return None


def _missing_values(v: Any) -> list[Any]:
    out: list[Any] = []
    for item in _as_list(v):
        if isinstance(item, dict):
            out.append(item.get("value"))
        else:
            out.append(item)
    return out


def _names(v: Any) -> list[str]:
    out: list[str] = []
    for item in _as_list(v):
        if isinstance(item, dict):
            n = item.get("name")
            if n:
                out.append(str(n))
        elif isinstance(item, str):
            out.append(item)
    return out


def _first_name(v: Any) -> str | None:
    names = _names(v)
    return names[0] if names else None


def _first_id_or_str(v: Any) -> str | None:
    item = _first(v)
    if isinstance(item, dict):
        return item.get("@id") or item.get("name")
    return _as_str(item)


def _first_id_or_str_norm(v: Any, norm) -> str | None:
    item = _first(v)
    if isinstance(item, dict):
        val = item.get("@id") or item.get("name")
    else:
        val = item
    return norm(val) if isinstance(val, str) else None


_JSON_PATH_RE = re.compile(r"^\$(\.[A-Za-z_][\w-]*|\[\d+\]|\['[^']*'\])*$")


def _eval_json_path(path: str, record: Any) -> Any:
    """Evaluate a restricted JSONPath (``$.a.b``, ``$['a']``, ``$.a[0]``)."""
    if not path or not path.startswith("$"):
        return None
    cur = record
    # tokenize
    tokens = re.findall(r"\.([A-Za-z_][\w-]*)|\['([^']*)'\]|\[(\d+)\]", path)
    for dot, brk, idx in tokens:
        key = dot or brk
        if key:
            if isinstance(cur, dict):
                cur = cur.get(key)
            else:
                return None
        elif idx:
            i = int(idx)
            if isinstance(cur, list) and 0 <= i < len(cur):
                cur = cur[i]
            else:
                return None
        if cur is None:
            return None
    return cur


def humanize_label(value: str) -> str:
    """Turn ``StructuredProfile`` / ``behavioral_history`` / ``overall`` into a label."""
    s = local_name(value)
    s = s.replace("_", " ").replace("-", " ")
    s = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s[:1].upper() + s[1:] if s else value
