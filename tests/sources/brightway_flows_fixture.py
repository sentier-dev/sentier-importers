"""Builders for a tiny Brightway Flows ``harmonised-flows-simple`` document."""

import gzip
import json

FLOW_IRI = "https://vocab.brightway.dev/elementary-flows/"
CONTEXT_IRI = "https://vocab.brightway.one/flow-contexts/"
UNIT_IRI = "https://vocab.brightway.one/units/unit/"


FORMULA_KEY = "https://w3id.org/chemrof/molecular_formula"


def flow(
    identifier,
    label,
    context_code="envi-air-unkn",
    unit="kg",
    source="EF 3.1",
    *,
    alt_labels=None,
    cas_numbers=None,
    ec_numbers=None,
    formula=None,
):
    record = {
        "identifier": identifier,
        "source": source,
        "prefLabel": label,
        "context_iri": CONTEXT_IRI + context_code,
        "unit": unit,
    }
    if alt_labels is not None:
        record["altLabel"] = alt_labels
    if cas_numbers is not None:
        record["cas_numbers"] = cas_numbers
    if ec_numbers is not None:
        record["ec_numbers"] = ec_numbers
    if formula is not None:
        record["properties"] = {FORMULA_KEY: formula}
    return record


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


def excluded_concept(list_slug, source_id, label="x", context="Final waste flows"):
    return {
        "@id": f"https://vocab.brightway.one/{list_slug}/flow/{source_id}",
        "skos:prefLabel": label,
        "context": context,
        "qudt:hasUnit": {"@id": UNIT_IRI + "KiloGM"},
        "brightway:exclusionReason": "not an exchange with the environment",
    }


def document(flows, links_by_list, redirects=(), excluded_by_list=None):
    """``excluded_by_list``: ``{list: [excluded_concept(...), ...]}``, the source flows
    a list's correspondence refuses to link."""
    excluded_by_list = excluded_by_list or {}
    return {
        "schema_version": 7,
        "flows": list(flows),
        "redirects": [
            {"identifier": old, "replaced_by_identifier": new} for old, new in redirects
        ],
        "correspondences": [
            {
                "@id": f"https://vocab.brightway.dev/correspondences/{slug}",
                "xkos:madeOf": assocs,
                **(
                    {"brightway:excludedSourceConcept": excluded_by_list[slug]}
                    if slug in excluded_by_list
                    else {}
                ),
            }
            for slug, assocs in links_by_list.items()
        ],
    }


def gz(doc) -> bytes:
    return gzip.compress(json.dumps(doc).encode())
