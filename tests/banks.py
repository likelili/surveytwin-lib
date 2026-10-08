"""Small Croissant Twins banks for tests, adapted from the website's bank tests."""

from __future__ import annotations

import json
from pathlib import Path


def write_mini_bank(root: Path, derivable: bool = False) -> Path:
    bank = root / "ct-mini-test-bank" / "bank"
    bank.mkdir(parents=True, exist_ok=True)
    manifest = {
        "@context": {
            "@vocab": "https://schema.org/", "sc": "https://schema.org/",
            "cr": "http://mlcommons.org/croissant/", "ct": "https://exploratwin.org/ns/croissant-twins/",
            "dct": "http://purl.org/dc/terms/", "conformsTo": "dct:conformsTo",
            "recordSet": "cr:recordSet", "field": "cr:field", "key": "cr:key",
            "dataType": {"@id": "cr:dataType", "@type": "@vocab"}, "source": "cr:source",
            "fileObject": "cr:fileObject", "extract": "cr:extract", "jsonPath": "cr:jsonPath",
            "references": "cr:references", "equivalentProperty": "cr:equivalentProperty",
        },
        "@type": "sc:Dataset",
        "conformsTo": ["http://mlcommons.org/croissant/1.1", "https://exploratwin.org/spec/croissant-twins/0.1"],
        "name": "CT Mini Test Bank", "version": "0.0.1",
        "license": "https://creativecommons.org/licenses/by/4.0/", "url": "https://example.org/mini",
        "ct:personaBasis": "ct:SyntheticPersona", "conditionsOfAccess": "public",
        "ct:defaultRepresentationKind": "ct:Summary",
        "distribution": [
            {"@id": "personas.jsonl", "@type": "cr:FileObject", "contentUrl": "personas.jsonl", "encodingFormat": "application/x-ndjson"},
            {"@id": "representations.jsonl", "@type": "cr:FileObject", "contentUrl": "representations.jsonl", "encodingFormat": "application/x-ndjson"},
        ],
        "recordSet": [
            {
                "@id": "personas", "@type": "cr:RecordSet", "dataType": "ct:Persona",
                "key": {"@id": "personas/persona_id"},
                "field": [
                    {"@id": "personas/persona_id", "@type": "cr:Field", "name": "persona_id", "dataType": "sc:Text", "equivalentProperty": "ct:personaId", "source": {"fileObject": {"@id": "personas.jsonl"}, "extract": {"jsonPath": "$.persona_id"}}},
                    {"@id": "personas/region", "@type": "cr:Field", "name": "region", "dataType": "sc:Text", "ct:filterable": True, "equivalentProperty": "ct:geographicRegion", "source": {"fileObject": {"@id": "personas.jsonl"}, "extract": {"jsonPath": "$.region"}}},
                    {"@id": "personas/age", "@type": "cr:Field", "name": "age", "dataType": "sc:Integer", "ct:filterable": True, "equivalentProperty": "ct:age", "source": {"fileObject": {"@id": "personas.jsonl"}, "extract": {"jsonPath": "$.age"}}},
                ],
            },
            {
                "@id": "representations", "@type": "cr:RecordSet",
                "dataType": ["ct:Representation", "sc:CreativeWork"],
                "key": {"@id": "representations/representation_id"},
                "field": [
                    {"@id": "representations/representation_id", "@type": "cr:Field", "name": "representation_id", "dataType": "sc:Text", "equivalentProperty": "ct:representationId", "source": {"fileObject": {"@id": "representations.jsonl"}, "extract": {"jsonPath": "$.representation_id"}}},
                    {"@id": "representations/persona_id", "@type": "cr:Field", "name": "persona_id", "dataType": "sc:Text", "equivalentProperty": "ct:persona", "references": {"field": {"@id": "personas/persona_id"}}, "source": {"fileObject": {"@id": "representations.jsonl"}, "extract": {"jsonPath": "$.persona_id"}}},
                    {"@id": "representations/representation_kind", "@type": "cr:Field", "name": "representation_kind", "dataType": ["sc:Text", "ct:RepresentationKind"], "equivalentProperty": "ct:representationKind", "source": {"fileObject": {"@id": "representations.jsonl"}, "extract": {"jsonPath": "$.representation_kind"}}},
                    {"@id": "representations/text", "@type": "cr:Field", "name": "text", "dataType": "sc:Text", "equivalentProperty": "sc:text", "source": {"fileObject": {"@id": "representations.jsonl"}, "extract": {"jsonPath": "$.text"}}},
                    {"@id": "representations/media_type", "@type": "cr:Field", "name": "media_type", "dataType": "sc:Text", "equivalentProperty": "sc:encodingFormat", "source": {"fileObject": {"@id": "representations.jsonl"}, "extract": {"jsonPath": "$.media_type"}}},
                    {"@id": "representations/is_default", "@type": "cr:Field", "name": "is_default", "dataType": "sc:Boolean", "equivalentProperty": "ct:isDefaultRepresentation", "source": {"fileObject": {"@id": "representations.jsonl"}, "extract": {"jsonPath": "$.is_default"}}},
                ],
            },
        ],
    }
    if derivable:
        for rs in manifest["recordSet"]:
            if rs.get("@id") == "representations":
                rs["ct:derivable"] = True
                rs["ct:completeCoverage"] = True
    (bank / "croissant.json").write_text(json.dumps(manifest), encoding="utf-8")
    personas = [
        {"persona_id": "p1", "region": "West", "age": 25},
        {"persona_id": "p2", "region": "West", "age": 45},
        {"persona_id": "p3", "region": "East", "age": 65},
        {"persona_id": "p4", "region": "East", "age": 30},
    ]
    (bank / "personas.jsonl").write_text("\n".join(json.dumps(p) for p in personas), encoding="utf-8")
    reps = []
    for p in personas:
        pid = p["persona_id"]
        reps.append({"representation_id": f"{pid}:summary", "persona_id": pid, "representation_kind": "ct:Summary", "text": f"{pid} summary text.", "media_type": "text/plain", "is_default": True})
        reps.append({"representation_id": f"{pid}:demo", "persona_id": pid, "representation_kind": "ct:DemographicsOnly", "text": f"{pid} demographics.", "media_type": "text/plain", "is_default": False})
    (bank / "representations.jsonl").write_text("\n".join(json.dumps(r) for r in reps), encoding="utf-8")
    return bank


def write_v2_bank(root: Path) -> Path:
    """Spec-rev-2026-07-09 bank: inline ct:values on kind + filter fields, no codebooks,
    no selection dimensions, rai:dataCollection, plain-text targetPopulation, domain/region."""
    bank = root / "ct-v2-test-bank" / "bank"
    bank.mkdir(parents=True, exist_ok=True)
    manifest = {
        "@context": {
            "@vocab": "https://schema.org/", "sc": "https://schema.org/",
            "cr": "http://mlcommons.org/croissant/", "ct": "https://exploratwin.org/ns/croissant-twins/",
            "rai": "http://mlcommons.org/croissant/RAI/",
            "dct": "http://purl.org/dc/terms/", "conformsTo": "dct:conformsTo",
            "recordSet": "cr:recordSet", "field": "cr:field", "key": "cr:key",
            "dataType": {"@id": "cr:dataType", "@type": "@vocab"}, "source": "cr:source",
            "fileObject": "cr:fileObject", "extract": "cr:extract", "jsonPath": "cr:jsonPath",
            "references": "cr:references", "equivalentProperty": "cr:equivalentProperty",
        },
        "@type": "sc:Dataset",
        "conformsTo": ["http://mlcommons.org/croissant/1.1", "https://exploratwin.org/spec/croissant-twins/0.1"],
        "name": "CT V2 Test Bank", "version": "0.0.1",
        "license": "https://creativecommons.org/licenses/by/4.0/", "url": "https://example.org/v2",
        "ct:personaBasis": "ct:SyntheticPersona", "conditionsOfAccess": "public",
        "ct:domain": "consumer", "ct:region": "United States",
        "ct:targetPopulation": "Adult online shoppers in the United States, 2025. Not a probability sample.",
        "rai:dataCollection": "Generated by example-gen 2.1 conditioned on aggregate retail statistics.",
        "distribution": [
            {"@id": "personas.jsonl", "@type": "cr:FileObject", "contentUrl": "personas.jsonl", "encodingFormat": "application/x-ndjson"},
            {"@id": "representations.jsonl", "@type": "cr:FileObject", "contentUrl": "representations.jsonl", "encodingFormat": "application/x-ndjson"},
        ],
        "recordSet": [
            {
                "@id": "personas", "@type": "cr:RecordSet", "dataType": "ct:Persona",
                "key": {"@id": "personas/persona_id"},
                "field": [
                    {"@id": "personas/persona_id", "@type": "cr:Field", "name": "persona_id", "dataType": "sc:Text", "equivalentProperty": "ct:personaId", "source": {"fileObject": {"@id": "personas.jsonl"}, "extract": {"jsonPath": "$.persona_id"}}},
                    # inline values with a deliberately non-alphabetical declared order + code!=label entry
                    {"@id": "personas/plan", "@type": "cr:Field", "name": "plan", "dataType": "sc:Text", "ct:filterable": True,
                     "ct:values": ["small", "medium", {"code": "lg", "label": "Large"}],
                     "ct:missingValue": [None, ""],
                     "source": {"fileObject": {"@id": "personas.jsonl"}, "extract": {"jsonPath": "$.plan"}}},
                ],
            },
            {
                "@id": "representations", "@type": "cr:RecordSet",
                "dataType": ["ct:Representation", "sc:CreativeWork"],
                "key": {"@id": "representations/representation_id"},
                "field": [
                    {"@id": "representations/representation_id", "@type": "cr:Field", "name": "representation_id", "dataType": "sc:Text", "equivalentProperty": "ct:representationId", "source": {"fileObject": {"@id": "representations.jsonl"}, "extract": {"jsonPath": "$.representation_id"}}},
                    {"@id": "representations/persona_id", "@type": "cr:Field", "name": "persona_id", "dataType": "sc:Text", "equivalentProperty": "ct:persona", "references": {"field": {"@id": "personas/persona_id"}}, "source": {"fileObject": {"@id": "representations.jsonl"}, "extract": {"jsonPath": "$.persona_id"}}},
                    # bank-defined kind codes (the pick), with required per-kind descriptions
                    {"@id": "representations/representation_kind", "@type": "cr:Field", "name": "representation_kind", "dataType": "sc:Text", "equivalentProperty": "ct:representationKind",
                     "ct:values": [
                         {"code": "summarised", "label": "Summarised", "description": "One-paragraph profile. Use for large panels; omits purchase detail."},
                         {"code": "full", "label": "Full profile", "description": "Complete profile with purchase history. Highest fidelity."},
                     ],
                     "source": {"fileObject": {"@id": "representations.jsonl"}, "extract": {"jsonPath": "$.representation_kind"}}},
                    {"@id": "representations/text", "@type": "cr:Field", "name": "text", "dataType": "sc:Text", "equivalentProperty": "sc:text", "source": {"fileObject": {"@id": "representations.jsonl"}, "extract": {"jsonPath": "$.text"}}},
                    {"@id": "representations/media_type", "@type": "cr:Field", "name": "media_type", "dataType": "sc:Text", "equivalentProperty": "sc:encodingFormat", "source": {"fileObject": {"@id": "representations.jsonl"}, "extract": {"jsonPath": "$.media_type"}}},
                    {"@id": "representations/is_default", "@type": "cr:Field", "name": "is_default", "dataType": "sc:Boolean", "equivalentProperty": "ct:isDefaultRepresentation", "source": {"fileObject": {"@id": "representations.jsonl"}, "extract": {"jsonPath": "$.is_default"}}},
                ],
            },
        ],
    }
    (bank / "croissant.json").write_text(json.dumps(manifest), encoding="utf-8")
    personas = [
        {"persona_id": "v1", "plan": "lg"},
        {"persona_id": "v2", "plan": "small"},
        {"persona_id": "v3", "plan": "small"},
        {"persona_id": "v4", "plan": "medium"},
    ]
    (bank / "personas.jsonl").write_text("\n".join(json.dumps(p) for p in personas), encoding="utf-8")
    reps = []
    for p in personas:
        pid = p["persona_id"]
        reps.append({"representation_id": f"{pid}/summarised", "persona_id": pid, "representation_kind": "summarised", "text": f"{pid} summarised.", "media_type": "text/markdown", "is_default": False})
        reps.append({"representation_id": f"{pid}/full", "persona_id": pid, "representation_kind": "full", "text": f"{pid} full profile.", "media_type": "text/markdown", "is_default": True})
    (bank / "representations.jsonl").write_text("\n".join(json.dumps(r) for r in reps), encoding="utf-8")
    return bank
