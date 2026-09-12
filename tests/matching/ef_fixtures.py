"""Tiny EF CF table + vocab shard builders, written as parquet into a tmp dir."""

from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

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
VOCAB_SCHEMA = pa.schema(
    [
        ("iri", pa.string()),
        ("pref_label", pa.string()),
        ("alt_labels", pa.list_(pa.string())),
        ("cas_number", pa.string()),
        ("source", pa.string()),
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


def vocab_row(code, label, alt=(), cas=None, source=EF_SOURCE):
    # alt=None models the ~4,051 real EF vocab rows whose alt_labels is null,
    # as distinct from alt=() (an explicit empty list).
    return {
        "iri": IRI + code,
        "pref_label": label,
        "alt_labels": list(alt) if alt is not None else None,
        "cas_number": cas,
        "source": source,
    }


def write_ef_inputs(root: Path, cf_rows, vocab_rows, shards: int = 1) -> tuple[Path, Path]:
    """Write one CF parquet and split ``vocab_rows`` round-robin across ``shards`` vocab
    parquet files (``air-01.parquet``, ``air-02.parquet``, ...); return (cf_path, vocab_dir).
    """
    cf = root / "characterization-factors.parquet"
    pq.write_table(pa.Table.from_pylist(cf_rows, schema=CF_SCHEMA), cf)
    vocab_dir = root / "elementary-flows"
    vocab_dir.mkdir(exist_ok=True)
    rows = list(vocab_rows)
    groups: list[list[dict]] = [[] for _ in range(shards)]
    for i, row in enumerate(rows):
        groups[i % shards].append(row)
    for i, group in enumerate(groups, start=1):
        pq.write_table(
            pa.Table.from_pylist(group, schema=VOCAB_SCHEMA), vocab_dir / f"air-{i:02d}.parquet"
        )
    return cf, vocab_dir
