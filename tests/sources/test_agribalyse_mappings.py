import io
import json
from pathlib import Path

import jsonschema
import pyarrow as pa
import pyarrow.parquet as pq
from sentier_importers.core.pipeline import _assemble
from sentier_importers.core.source import SourceConfig
from sentier_importers.core.types import RawData
from sentier_importers.sources.agribalyse.mappings_biosphere import (
    AgribalyseBiosphereMappingsSource,
)

# Vendored copy of sentier-mappings/schema/randonneur-package.schema.json so tests are
# self-contained (no dependency on a sibling checkout path — CI has no such path).
_SCHEMA = Path(__file__).parent / "fixtures" / "randonneur-package.schema.json"

_COLS = [
    "source_name",
    "source_unit",
    "source_context",
    "source_cas",
    "target_db",
    "target_code",
    "target_name",
    "target_unit",
    "unit_conversion",
    "priority_tier",
    "provenance",
]


def _raw() -> RawData:
    rows = [
        # keep: curated EF target
        {
            "source_name": "Nitrogen, total",
            "source_unit": "",
            "source_context": [],
            "source_cas": None,
            "target_db": "ef",
            "target_code": "uuid-1",
            "target_name": "Nitrogen, Total (excluding N2)",
            "target_unit": "kg",
            "unit_conversion": 1.0,
            "priority_tier": 1,
            "provenance": "curated_overrides",
        },
        # keep: conversion factor != 1
        {
            "source_name": "Water m3",
            "source_unit": "m3",
            "source_context": ["water"],
            "source_cas": None,
            "target_db": "ef",
            "target_code": "uuid-2",
            "target_name": "Water",
            "target_unit": "kg",
            "unit_conversion": 1000.0,
            "priority_tier": 2,
            "provenance": "llm.biosphere-residuals-reviewed",
        },
        # drop: ecoinvent provenance
        {
            "source_name": "X",
            "source_unit": "kg",
            "source_context": [],
            "source_cas": None,
            "target_db": "ef",
            "target_code": "uuid-3",
            "target_name": "Y",
            "target_unit": "kg",
            "unit_conversion": 1.0,
            "priority_tier": 1,
            "provenance": "agribalyse.ecoinvent-3.10-biosphere-flowmapper",
        },
        # drop: harmonised-flows bulk
        {
            "source_name": "Z",
            "source_unit": "kg",
            "source_context": [],
            "source_cas": None,
            "target_db": "ef",
            "target_code": "uuid-4",
            "target_name": "Zt",
            "target_unit": "kg",
            "unit_conversion": 1.0,
            "priority_tier": 5,
            "provenance": "harmonised-flows-simple",
        },
        # drop: empty target_db
        {
            "source_name": "Q",
            "source_unit": "kg",
            "source_context": [],
            "source_cas": None,
            "target_db": "",
            "target_code": None,
            "target_name": "",
            "target_unit": "",
            "unit_conversion": 1.0,
            "priority_tier": 13,
            "provenance": "unmatchable",
        },
    ]
    cols = {
        c: pa.array(
            [r[c] for r in rows],
            (
                pa.list_(pa.string())
                if c == "source_context"
                else (
                    pa.float64()
                    if c == "unit_conversion"
                    else (pa.int64() if c == "priority_tier" else pa.string())
                )
            ),
        )
        for c in _COLS
    }
    buf = io.BytesIO()
    pq.write_table(pa.table(cols), buf)
    return RawData(content=buf.getvalue(), source_url="file://m.parquet")


def _config():
    return SourceConfig(
        name="biosphere",
        module="sentier_importers.sources.agribalyse.mappings_biosphere",
        target="sentier_mappings",
        category="02-agribalyse-3.2__ef-3.1",
        fetch_url="unused://",
        fetch_format="parquet",
        output_format="json",
        package_name="agribalyse-3.2__ef-3.1-biosphere",
        package_version="0.1.0",
        package_verb="replace",
    )


def test_transform_keeps_only_non_ecoinvent_authored_rows():
    src = AgribalyseBiosphereMappingsSource(_config())
    rows = src.transform(src.parse(_raw()))
    assert [r["source"]["name"] for r in rows] == ["Nitrogen, total", "Water m3"]
    water = rows[1]
    assert water["conversion_factor"] == 1000.0
    assert water["target"]["code"] == "uuid-2"
    assert "curated_overrides" in rows[0]["comment"]
    assert "conversion_factor" not in rows[0]  # unit_conversion == 1.0 omitted


def test_assembled_package_matches_randonneur_schema():
    src = AgribalyseBiosphereMappingsSource(_config())
    rows = src.transform(src.parse(_raw()))
    package = _assemble(rows, _config())
    assert package["name"] == "agribalyse-3.2__ef-3.1-biosphere"
    assert package["version"] == "0.1.0"
    assert len(package["replace"]) == 2

    schema = json.loads(_SCHEMA.read_text())
    jsonschema.validate(package, schema)  # raises if invalid
