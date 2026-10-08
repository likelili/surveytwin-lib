"""IRI expansion + normalization for Croissant / Croissant Twins manifests.

CT and Croissant manifests mix prefixes (``sc``/``cr``/``ct``/``dct``/``prov``/``rai``)
and both ``http://`` and ``https://`` schema.org. The generic runtime must resolve
fields by *meaning* — the implementation plan forbids matching on filenames or
literal column names — so every compact IRI is expanded against the manifest
``@context`` and normalized so that, e.g., ``http://schema.org/text`` and
``https://schema.org/text`` compare equal.
"""

from __future__ import annotations

from typing import Any


def normalize_iri(iri: Any) -> Any:
    """Return a scheme-insensitive canonical form of an absolute IRI.

    Strips ``http(s)://`` and a leading ``www.`` so comparisons are scheme
    agnostic. Non-strings pass through unchanged.
    """
    if not isinstance(iri, str):
        return iri
    s = iri.strip()
    for scheme in ("https://", "http://"):
        if s.startswith(scheme):
            s = s[len(scheme):]
            break
    if s.startswith("www."):
        s = s[4:]
    return s


def expand_iri(term: Any, context: dict | None) -> Any:
    """Expand a compact term (``ct:Persona``) to a normalized absolute IRI.

    - Absolute IRIs are normalized.
    - ``prefix:local`` is resolved against ``@context`` (supporting both the
      string and ``{"@id": ...}`` context forms).
    - Bare terms are expanded against ``@vocab`` when present.
    - Unknown prefixes and non-strings are returned unchanged.
    """
    if not isinstance(term, str) or not term:
        return term
    if term.startswith("http://") or term.startswith("https://"):
        return normalize_iri(term)
    if term.startswith("_:") or term.startswith("@"):
        return term
    ctx = context or {}
    if ":" in term:
        prefix, local = term.split(":", 1)
        if local.startswith("//"):
            return normalize_iri(term)
        base = ctx.get(prefix)
        if isinstance(base, dict):
            base = base.get("@id")
        if isinstance(base, str):
            return normalize_iri(base + local)
        return term  # unknown prefix; leave as-is for diagnostics
    vocab = ctx.get("@vocab")
    if isinstance(vocab, str):
        return normalize_iri(vocab + term)
    return term


def local_name(iri: Any) -> str:
    """Best-effort local name of an IRI/compact term (after ``/``, ``#`` or ``:``)."""
    if not isinstance(iri, str) or not iri:
        return ""
    s = iri
    for sep in ("#", "/"):
        if sep in s:
            s = s.rsplit(sep, 1)[-1]
    if ":" in s:
        s = s.rsplit(":", 1)[-1]
    return s


# --- canonical namespaces (normalized, scheme-stripped) ---
SC = "schema.org/"
CR = "mlcommons.org/croissant/"
CT = "exploratwin.org/ns/croissant-twins/"

CT_PROFILE_IRIS = (
    "exploratwin.org/spec/croissant-twins/0.1",
    "exploratwin.org/spec/croissant-twins/",
)
CROISSANT_PROFILE_IRIS = (
    "mlcommons.org/croissant/1.1",
    "mlcommons.org/croissant/1.0",
)

# schema.org terms
SC_TEXT = SC + "text"
SC_CONTENT_URL = SC + "contentUrl"
SC_ENCODING_FORMAT = SC + "encodingFormat"
SC_IN_LANGUAGE = SC + "inLanguage"
SC_ENUMERATION = SC + "Enumeration"
SC_DATASET = SC + "Dataset"
SC_NAME = SC + "name"

# Croissant Twins classes
CT_PERSONA = CT + "Persona"
CT_REPRESENTATION = CT + "Representation"
CT_SOURCE_RECORD = CT + "SourceRecord"

# Croissant Twins properties
CT_PERSONA_ID = CT + "personaId"
CT_REPRESENTATION_ID = CT + "representationId"
CT_PERSONA_FK = CT + "persona"
CT_REPRESENTATION_KIND = CT + "representationKind"
CT_PAYLOAD = CT + "payload"
CT_FILTERABLE = CT + "filterable"
CT_SELECTION_DIMENSION = CT + "selectionDimension"
CT_IS_DEFAULT = CT + "isDefaultRepresentation"
CT_CODEBOOK = CT + "codebook"
CT_SENSITIVE = CT + "sensitive"
CT_MISSING_VALUE = CT + "missingValue"
CT_DEFAULT_REP_KIND = CT + "defaultRepresentationKind"
CT_PERSONA_BASIS = CT + "personaBasis"
