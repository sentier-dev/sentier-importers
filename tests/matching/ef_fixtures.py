"""Tiny EF CF table + vocab term-file builders, written as parquet into a tmp dir."""

import json
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
from sentier_importers.core.vocab_layers import TERM_SCHEMA

IRI = "https://vocab.sentier.dev/flows/"
AIR_URBAN = "Emissions / Emissions to air / Emissions to urban air close to ground"
AIR_RURAL = "Emissions / Emissions to air / Emissions to non-urban air or from high stacks"
AIR_UNSPEC = "Emissions / Emissions to air / Emissions to air, unspecified"
WATER_FRESH = "Emissions / Emissions to water / Emissions to fresh water"
WATER_UNSPEC = "Emissions / Emissions to water / Emissions to water, unspecified"
RES_WATER = "Resources / Resources from water / Renewable material resources from water"
RES_GROUND = "Resources / Resources from ground / Non-renewable element resources from ground"
LAND_OCC = "Land use / Land occupation"
LAND_TRANS = "Land use / Land transformation"

CF_SCHEMA = pa.schema(
    [
        ("method_id", pa.string()),
        ("impact_category", pa.string()),
        ("flow", pa.string()),
        ("flow_name", pa.string()),
        ("factor_value", pa.float64()),
        ("unit", pa.string()),
        ("flow_context", pa.string()),
        ("location", pa.string()),
    ]
)
EF_SOURCE = "https://vocab.sentier.dev/sources/ef-3.1"


def cf_row(code, name, context, method="ef-3.1:human-toxicity-cancer", value=1.0, location=None):
    return {
        "method_id": method,
        "impact_category": method,
        "flow": IRI + code,
        "flow_name": name,
        "factor_value": value,
        "unit": "CTUh",
        "flow_context": context,
        "location": location,
    }


def vocab_row(code, label, alt=(), cas=None, source=EF_SOURCE, bw=None):
    # alt=None models the ~4,051 real EF vocab rows whose alt_labels is null,
    # as distinct from alt=() (an explicit empty list).
    # bw models a row's one real "bw-context:<code>" additional_notations entry;
    # a row that has no bw-context notation (the common case) gets an empty list.
    return {
        "iri": IRI + code,
        "pref_label": label,
        "alt_labels": list(alt) if alt is not None else None,
        "cas_number": cas,
        "source": source,
        "additional_notations": [f"bw-context:{bw}"] if bw is not None else [],
    }


def term_row(row: dict) -> dict:
    """A flat ``vocab_row`` as a row of sentier-vocab's term layer."""
    term = {k: v for k, v in row.items() if k != "cas_number"}
    term["type"] = "elementary-flow"
    cas = row.get("cas_number")
    term["attributes"] = json.dumps({"cas_number": cas}) if cas is not None else None
    return term


#: A term of another type in the same release: the index must not read it as a flow.
_OTHER_TERM = {
    "iri": "https://vocab.sentier.dev/impact-categories/ef-3.1/acidification",
    "type": "impact-category",
    "pref_label": "Acidification",
    "source": EF_SOURCE,
}


def write_ef_inputs(root: Path, cf_rows, vocab_rows, shards: int = 1) -> tuple[Path, Path]:
    """Write one CF parquet and split ``vocab_rows`` round-robin across ``shards`` vocab
    term files (``ef-3.1-01.parquet``, ``ef-3.1-02.parquet``, ...); return (cf_path, terms_dir).
    """
    cf = root / "characterization-factors.parquet"
    pq.write_table(pa.Table.from_pylist(cf_rows, schema=CF_SCHEMA), cf)
    terms_dir = root / "terms"
    terms_dir.mkdir(exist_ok=True)
    groups: list[list[dict]] = [[_OTHER_TERM] for _ in range(shards)]
    for i, row in enumerate(vocab_rows):
        groups[i % shards].append(term_row(row))
    for i, group in enumerate(groups, start=1):
        pq.write_table(
            pa.Table.from_pylist(group, schema=TERM_SCHEMA), terms_dir / f"ef-3.1-{i:02d}.parquet"
        )
    return cf, terms_dir
