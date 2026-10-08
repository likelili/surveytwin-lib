"""The generic ``PersonaBankRuntime`` contract and its implementations.

``CroissantTwinsBank`` serves any ingested CT bank from the catalog.
``LegacyTwin2KBank`` delegates to the existing built-in loader so the two
selection paths share one interface. The built-in Twin-2K production run path
is left untouched; the legacy wrapper exists for interface symmetry and tests.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from . import catalog
from .iris import local_name


class BankNotFoundError(LookupError):
    pass


def _fetch_object_payload(uri: str, offset: int, length: int, store: Any = None) -> str:
    """Byte-range fetch of an object-backed representation payload."""
    from . import payload_store

    try:
        active = store if store is not None else payload_store.get_active_store()
        data = active.fetch_range(uri, offset, length)
        return data.decode("utf-8", errors="replace")
    except Exception:
        return ""


# ---------------------------------------------------------------------------
# public contract objects
# ---------------------------------------------------------------------------


@dataclass
class RepresentationView:
    view_id: str
    label: str
    kind: str
    kind_label: str
    selection_dimensions: dict[str, str]
    description: str
    is_default: bool
    coverage_count: int
    media_type: str
    payload_mode: str

    def to_public(self) -> dict[str, Any]:
        return {
            "view_id": self.view_id,
            "label": self.label,
            "kind": self.kind,
            "selection_dimensions": self.selection_dimensions,
            "description": self.description,
            "is_default": self.is_default,
            "coverage_count": self.coverage_count,
            "media_type": self.media_type,
        }


@dataclass
class FilterDef:
    field_name: str
    label: str
    data_type: str
    is_numeric: bool
    sensitive: bool
    semantic_iri: str
    values: list[dict[str, Any]]

    def to_public(self) -> dict[str, Any]:
        return {
            "field": self.field_name,
            "label": self.label,
            "data_type": self.data_type,
            "is_numeric": self.is_numeric,
            "sensitive": self.sensitive,
            "values": [v.get("code") for v in self.values] if self.values else [],
            "value_labels": {v.get("code"): (v.get("label") or v.get("code")) for v in self.values} if self.values else {},
        }


@dataclass
class BankDescription:
    bank_id: str
    version_id: str
    version: str
    name: str
    publisher: str
    description: str
    license: str
    citation: str
    url: str
    same_as: list[str]
    creators: list[str]
    languages: list[str]
    keywords: list[str]
    persona_basis: str
    persona_count: int
    representation_count: int
    conditions_of_access: str
    usage_info: str
    human_data_statement: str
    data_limitations: str
    data_biases: str
    data_use_cases: str
    capabilities: list[str]
    target_population: dict | None
    default_view_id: str | None
    card_md: str = ""
    domain: str = ""
    region: str = ""
    license_usage: dict[str, Any] | None = None

    def to_card(self) -> dict[str, Any]:
        """Storefront card shape (no README body)."""
        return {
            "bank_id": self.bank_id,
            "version_id": self.version_id,
            "version": self.version,
            "name": self.name,
            "publisher": self.publisher,
            "description": self.description,
            "license": self.license,
            "citation": self.citation,
            "url": self.url,
            "persona_basis": self.persona_basis,
            "persona_count": self.persona_count,
            "representation_count": self.representation_count,
            "conditions_of_access": self.conditions_of_access,
            "data_limitations": self.data_limitations,
            "capabilities": self.capabilities,
            "domain": self.domain,
            "region": self.region,
        }


@dataclass
class ResolvedRepresentation:
    representation_id: str
    persona_id: str
    kind: str
    media_type: str
    payload_mode: str
    payload: str
    char_count: int


@dataclass
class PromptPayload:
    text: str
    media_type: str
    representation_id: str
    kind: str
    bank_id: str
    version: str
    view_label: str
    trusted: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class ExecutionAccess:
    allowed: bool
    reason: str
    conditions_of_access: str


# ---------------------------------------------------------------------------
# runtime protocol
# ---------------------------------------------------------------------------


@runtime_checkable
class PersonaBankRuntime(Protocol):
    def describe_bank(self) -> BankDescription: ...
    def list_representation_views(self) -> list[RepresentationView]: ...
    def describe_filters(self) -> list[FilterDef]: ...
    def count_eligible_personas(self, view_id: str | None, filters: dict[str, Any]) -> dict[str, int]: ...
    def filter_personas(self, view_id: str | None, filters: dict[str, Any], limit: int) -> list[str]: ...
    def sample_personas(self, view_id: str | None, filters: dict[str, Any], n: int, seed: int) -> list[str]: ...
    def resolve_representation(self, persona_id: str, view_id: str | None) -> ResolvedRepresentation | None: ...
    def load_prompt_payload(self, persona_id: str, view_id: str | None) -> PromptPayload | None: ...
    def evaluate_execution_access(self, user: Any) -> ExecutionAccess: ...


# ---------------------------------------------------------------------------
# Croissant Twins bank (catalog-backed, serve time)
# ---------------------------------------------------------------------------


class CroissantTwinsBank:
    """Serves a CT bank version.

    Three storage generations share this one class:
    - legacy rows: all data in the durable catalog (no artifact fields set);
    - artifact-backed store banks: metadata durable, data in a blob-store
      artifact downloaded to the local cache on first data access;
    - ephemeral banks: metadata *and* data in a local artifact file, payload
      packs in a local directory (nothing durable).
    """

    def __init__(
        self,
        version_row: dict[str, Any],
        *,
        data_db_path: str | None = None,
        meta_db_path: str | None = None,
        blob_store: Any = None,
    ) -> None:
        self._v = version_row
        self.version_id = version_row["id"]
        self.bank_id = version_row["bank_id"]
        import json

        self._summary = json.loads(version_row.get("summary_json") or "{}")
        self._derivable = bool(version_row.get("derivable"))
        self._views_cache: list[dict] | None = None
        self._explicit_data_path = data_db_path
        self._meta_db_path = meta_db_path
        self._blob_store = blob_store
        # Interactive request paths set this so a cold multi-GB artifact fails
        # fast ("warming up") instead of hanging into a proxy timeout.
        self.fast_fail_downloads = False

    # -- factories --
    @classmethod
    def load(cls, *, bank_id: str | None = None, version_id: str | None = None) -> "CroissantTwinsBank":
        if version_id:
            row = catalog.get_version(version_id)
            if not row:
                raise BankNotFoundError(f"version {version_id} not found")
            return cls(row)
        if bank_id:
            row = catalog.latest_approved_version(bank_id)
            if not row:
                raise BankNotFoundError(f"no approved version for bank {bank_id}")
            return cls(row)
        raise BankNotFoundError("bank_id or version_id required")

    @classmethod
    def load_from_artifact(cls, artifact_path: str, *, payload_root: str | None = None) -> "CroissantTwinsBank":
        """Open a self-contained bank artifact (ephemeral uploads)."""
        with catalog.bank_db(artifact_path):
            bundle = catalog.read_artifact_bundle()
        row = dict(bundle["version"])
        bank = bundle.get("bank") or {}
        row.setdefault("bank_name", bank.get("name") or "")
        row.setdefault("bank_publisher", bank.get("publisher") or "")
        store = None
        if payload_root:
            from .payload_store import LocalDirBlobStore

            store = LocalDirBlobStore(payload_root)
        return cls(row, data_db_path=artifact_path, meta_db_path=artifact_path, blob_store=store)

    # -- storage routing --
    def _resolve_data_path(self) -> str | None:
        if self._explicit_data_path:
            return self._explicit_data_path
        if self._v.get("artifact_uri"):
            from . import artifact

            return artifact.ensure_local_artifact(self._v, fast_fail=self.fast_fail_downloads)
        return None

    def _data_db(self):
        """Context manager routing per-persona data queries to the artifact."""
        return catalog.bank_db(self._resolve_data_path())

    def _meta_db(self):
        """Views/filters/version metadata: durable for store banks, artifact for ephemeral."""
        return catalog.bank_db(self._meta_db_path)

    # -- helpers --
    def _views(self) -> list[dict]:
        if self._views_cache is None:
            with self._meta_db():
                self._views_cache = catalog.get_views(self.version_id)
        return self._views_cache

    def resolve_view_id(self, view_id: str | None) -> str | None:
        views = self._views()
        if not views:
            return None
        if view_id:
            match = next((v for v in views if v["view_key"] == view_id), None)
            if match:
                return match["view_key"]
            raise BankNotFoundError(f"representation view {view_id} not found in bank {self.bank_id}")
        default = next((v for v in views if v.get("is_default")), views[0])
        return default["view_key"]

    def view_label(self, view_id: str | None) -> str:
        vid = self.resolve_view_id(view_id)
        v = next((x for x in self._views() if x["view_key"] == vid), None)
        return v["label"] if v else ""

    # -- contract --
    def describe_bank(self) -> BankDescription:
        s = self._summary
        default = next((v for v in self._views() if v.get("is_default")), None)
        return BankDescription(
            bank_id=self.bank_id,
            version_id=self.version_id,
            version=self._v.get("version", ""),
            name=s.get("name") or self._v.get("bank_name") or self.bank_id,
            publisher=s.get("publisher") or self._v.get("bank_publisher") or "",
            description=s.get("description") or "",
            license=s.get("license") or self._v.get("license") or "",
            citation=s.get("citation") or "",
            url=s.get("url") or "",
            same_as=s.get("same_as") or [],
            creators=s.get("creators") or [],
            languages=s.get("languages") or [],
            keywords=s.get("keywords") or [],
            persona_basis=s.get("persona_basis") or self._v.get("persona_basis") or "",
            persona_count=int(self._v.get("persona_count") or 0),
            representation_count=int(self._v.get("representation_count") or 0),
            conditions_of_access=s.get("conditions_of_access") or self._v.get("conditions_of_access") or "",
            usage_info=s.get("usage_info") or "",
            human_data_statement=s.get("human_data_statement") or "",
            data_limitations=s.get("data_limitations") or "",
            data_biases=s.get("data_biases") or "",
            data_use_cases=s.get("data_use_cases") or "",
            capabilities=s.get("capabilities") or [],
            target_population=s.get("target_population"),
            default_view_id=default["view_key"] if default else None,
            card_md=self._v.get("card_md") or "",
            domain=s.get("domain") or "",
            region=s.get("region") or "",
            license_usage=s.get("license_usage"),
        )

    def list_representation_views(self) -> list[RepresentationView]:
        out = []
        import json

        for v in self._views():
            out.append(
                RepresentationView(
                    view_id=v["view_key"],
                    label=v["label"],
                    kind=v["kind"],
                    kind_label=v["kind_label"],
                    selection_dimensions=json.loads(v.get("selection_dims_json") or "{}"),
                    description=v.get("description") or "",
                    is_default=bool(v.get("is_default")),
                    coverage_count=int(v.get("coverage_count") or 0),
                    media_type=v.get("media_type") or "",
                    payload_mode=v.get("payload_mode") or "",
                )
            )
        return out

    def describe_filters(self) -> list[FilterDef]:
        out = []
        with self._meta_db():
            filter_rows = catalog.get_filters(self.version_id)
        for f in filter_rows:
            out.append(
                FilterDef(
                    field_name=f["field_name"],
                    label=f.get("label") or f["field_name"],
                    data_type=f.get("data_type") or "Text",
                    is_numeric=bool(f.get("is_numeric")),
                    sensitive=bool(f.get("sensitive")),
                    semantic_iri=f.get("semantic_iri") or "",
                    values=f.get("codebook") or [],
                )
            )
        return out

    def count_eligible_personas(self, view_id: str | None, filters: dict[str, Any]) -> dict[str, int]:
        vid = self.resolve_view_id(view_id)
        if not vid:
            return {"total": 0, "matching": 0}
        with self._data_db():
            total = catalog.count_total_for_view(self.version_id, vid, derivable=self._derivable)
            matching = total if not filters else catalog.count_matching(self.version_id, vid, filters, derivable=self._derivable)
        return {"total": total, "matching": matching}

    def filter_personas(self, view_id: str | None, filters: dict[str, Any], limit: int = 50) -> list[str]:
        # capped preview via deterministic sampling over the eligible set
        return self.sample_personas(view_id, filters, limit, seed=0)

    def sample_personas(self, view_id: str | None, filters: dict[str, Any], n: int, seed: int = 42) -> list[str]:
        vid = self.resolve_view_id(view_id)
        if not vid:
            return []
        with self._data_db():
            return catalog.sample_persona_ids(self.version_id, vid, filters or {}, n, seed=seed, derivable=self._derivable)

    def list_personas_page(self, view_id: str | None, filters: dict[str, Any], offset: int, limit: int) -> list[str]:
        """Stable, persona_id-ordered page of eligible personas (panel-builder browsing)."""
        vid = self.resolve_view_id(view_id)
        if not vid:
            return []
        with self._data_db():
            return catalog.list_matching_persona_ids(
                self.version_id, vid, filters or {}, offset=offset, limit=limit, derivable=self._derivable
            )

    def persona_attrs(self, persona_id: str) -> dict[str, Any]:
        with self._data_db():
            return catalog.get_persona_attrs(self.version_id, persona_id)

    def personas_present(self, persona_ids: list[str]) -> set[str]:
        with self._data_db():
            return catalog.persona_ids_present(self.version_id, persona_ids)

    def distinct_filter_values(self, field_name: str, limit: int = 100) -> list[tuple[str, int]]:
        with self._data_db():
            return catalog.distinct_filter_values(self.version_id, field_name, limit=limit)

    def view_meta(self, view_id: str | None) -> dict[str, Any]:
        vid = self.resolve_view_id(view_id)
        if not vid:
            return {}
        with self._meta_db():
            return catalog.get_view(self.version_id, vid) or {}

    def max_payload_chars(self, view_id: str | None) -> int:
        """Largest payload for the view (context-budget preflight)."""
        meta = self.view_meta(view_id)
        max_chars = int(meta.get("max_char") or 0)
        if max_chars <= 0:  # pre-migration rows: fall back to a scan
            vid = self.resolve_view_id(view_id)
            if not vid:
                return 0
            with self._data_db():
                max_chars = catalog.max_char_count(self.version_id, vid)
        return max_chars

    def resolve_representation(self, persona_id: str, view_id: str | None) -> ResolvedRepresentation | None:
        vid = self.resolve_view_id(view_id)
        if not vid:
            return None
        with self._data_db():
            rep = catalog.get_representation(self.version_id, persona_id, vid, derivable=self._derivable)
        if not rep:
            return None
        payload_text = rep.get("payload") or ""
        if not payload_text and rep.get("payload_uri") and int(rep.get("payload_len") or 0) > 0:
            payload_text = _fetch_object_payload(
                rep["payload_uri"], int(rep.get("payload_offset") or 0), int(rep.get("payload_len") or 0),
                store=self._blob_store,
            )
        return ResolvedRepresentation(
            representation_id=rep.get("representation_id") or "",
            persona_id=persona_id,
            kind=rep.get("kind") or "",
            media_type=rep.get("media_type") or "",
            payload_mode=rep.get("payload_mode") or "",
            payload=payload_text,
            char_count=int(rep.get("char_count") or 0),
        )

    def load_prompt_payload(self, persona_id: str, view_id: str | None) -> PromptPayload | None:
        rep = self.resolve_representation(persona_id, view_id)
        if rep is None or not (rep.payload or "").strip():
            return None
        return PromptPayload(
            text=rep.payload,
            media_type=rep.media_type or "text/plain",
            representation_id=rep.representation_id,
            kind=rep.kind,
            bank_id=self.bank_id,
            version=self._v.get("version", ""),
            view_label=self.view_label(view_id),
            trusted=False,
            metadata={
                "bank_id": self.bank_id,
                "version": self._v.get("version", ""),
                "representation_kind": rep.kind,
                "representation_id": rep.representation_id,
                "media_type": rep.media_type,
            },
        )

    def evaluate_execution_access(self, user: Any = None) -> ExecutionAccess:
        coa = (self._summary.get("conditions_of_access") or self._v.get("conditions_of_access") or "").strip()
        is_public = coa.lower().startswith("public") or coa == ""
        return ExecutionAccess(
            allowed=is_public,
            reason="public access" if is_public else "non-public conditions of access; gated execution",
            conditions_of_access=coa,
        )
