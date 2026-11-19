"""Builders for a tiny Brightway Flows ``harmonised-flows-simple`` document."""

import gzip
import json

FLOW_IRI = "https://vocab.brightway.dev/elementary-flows/"
CONTEXT_IRI = "https://vocab.brightway.one/flow-contexts/"
UNIT_IRI = "https://vocab.brightway.one/units/unit/"


def flow(identifier, label, context_code="envi-air-unkn", unit="kg", source="EF 3.1"):
    return {
        "identifier": identifier,
        "source": source,
        "prefLabel": label,
        "context_iri": CONTEXT_IRI + context_code,
        "unit": unit,
    }


def link(
    list_slug,
    source_id,
    target_id,
    *,
    match="closeMatch",
    label="x",
    context="a / b",
    unit="KiloGM",
    multiplier=None,
    basis=None,
):
    source = {
        "@id": f"https://vocab.brightway.one/{list_slug}/flow/{source_id}",
        "skos:prefLabel": label,
        "context": context,
        "qudt:hasUnit": {"@id": UNIT_IRI + unit},
        f"skos:{match}": {"@id": FLOW_IRI + target_id},
    }
    assoc = {"xkos:sourceConcept": source, "xkos:targetConcept": {"@id": FLOW_IRI + target_id}}
    if multiplier is not None:
        assoc["qudt:conversionMultiplier"] = multiplier
        assoc["brightway:conversionBasis"] = basis
    return assoc


def document(flows, links_by_list, redirects=()):
    return {
        "schema_version": 7,
        "flows": list(flows),
        "redirects": [
            {"identifier": old, "replaced_by_identifier": new} for old, new in redirects
        ],
        "correspondences": [
            {"@id": f"https://vocab.brightway.dev/correspondences/{slug}", "xkos:madeOf": assocs}
            for slug, assocs in links_by_list.items()
        ],
    }


def gz(doc) -> bytes:
    return gzip.compress(json.dumps(doc).encode())
