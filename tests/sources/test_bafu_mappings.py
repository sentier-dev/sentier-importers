import io
import json
from pathlib import Path

import jsonschema
import pyarrow as pa
import pyarrow.parquet as pq
from sentier_importers.core.pipeline import _assemble
from sentier_importers.core.source import SourceConfig
from sentier_importers.core.types import RawData
from sentier_importers.sources.bafu.ecospold import flow_id
from sentier_importers.sources.bafu.mappings_biosphere import BafuBiosphereMappingsSource

# Vendored copy of sentier-mappings/schema/randonneur-package.schema.json so tests are
# self-contained (no dependency on a sibling checkout path — CI has no such path).
_SCHEMA = Path(__file__).parent / "fixtures" / "randonneur-package.schema.json"


def _row(**overrides):
    row = {
        "source_name": "Carbon dioxide, fossil",
        "source_category": "emissions to air",
        "source_subcategory": "unspecified",
        "source_unit": "kg",
        "target_code": "08a91e70-3ddc-11dd-91be-0050c2490048",
        "target_name": "carbon dioxide, fossil",
        "target_unit": "kilogram",
        "target_categories": ["Emissions", "Emissions to air"],
        "unit_conversion": 1.0,
        "tier": "T2",
        "candidate_count": 1,
        "cf_equivalent": True,
        "target_db": "ef",
    }
    row.update(overrides)
    return row


def _raw(rows) -> RawData:
    table = pa.Table.from_pylist(
        rows,
        schema=pa.schema(
            [
                ("source_name", pa.string()),
                ("source_category", pa.string()),
                ("source_subcategory", pa.string()),
                ("source_unit", pa.string()),
                ("target_code", pa.string()),
                ("target_name", pa.string()),
                ("target_unit", pa.string()),
                ("target_categories", pa.list_(pa.string())),
                ("unit_conversion", pa.float64()),
                ("tier", pa.string()),
                ("candidate_count", pa.int64()),
                ("cf_equivalent", pa.bool_()),
                ("target_db", pa.string()),
            ]
        ),
    )
    buffer = io.BytesIO()
    pq.write_table(table, buffer)
    return RawData(content=buffer.getvalue(), source_url="file://test")


def _transform(rows):
    source = BafuBiosphereMappingsSource(
        config=SourceConfig(
            name="biosphere",
            module="sentier_importers.sources.bafu.mappings_biosphere",
            target="sentier_mappings",
            category="03-bafu-2026-v1__ef-3.1",
            fetch_url="file://test",
            fetch_format="parquet",
            output_format="json",
            package_name="bafu-2026-v1__ef-3.1-biosphere",
            package_version="0.1.0",
            package_verb="replace",
        )
    )
    return source.transform(source.parse(_raw(rows)))


def test_entry_carries_the_ef_code_as_the_payload():
    entry = _transform([_row()])[0]
    assert entry["target"]["code"] == "08a91e70-3ddc-11dd-91be-0050c2490048"
    assert entry["target"]["unit"] == "kilogram"
    assert entry["target"]["context"] == ["Emissions", "Emissions to air"]


def test_source_code_matches_the_vocab_flow_id():
    """The bridge must join to the IRI the BAFU vocab importer mints."""
    entry = _transform([_row()])[0]
    assert entry["source"]["code"] == flow_id(
        "Carbon dioxide, fossil", "emissions to air", "unspecified"
    )


def test_source_context_is_category_then_subcategory():
    entry = _transform([_row()])[0]
    assert entry["source"]["context"] == ["emissions to air", "unspecified"]


def test_conversion_factor_only_when_units_differ():
    assert "conversion_factor" not in _transform([_row()])[0]
    entry = _transform([_row(unit_conversion=1000.0)])[0]
    assert entry["conversion_factor"] == 1000.0


def test_comment_records_the_tier_and_candidate_count():
    entry = _transform([_row(tier="T3", candidate_count=2)])[0]
    assert "T3" in entry["comment"]
    assert "2 CF-equivalent candidates" in entry["comment"]


def test_stringified_nan_never_reaches_an_entry():
    """Regression against the agribalyse package's 1,093 `"unit": "nan"` entries."""
    rows = [_row(source_unit="nan", source_subcategory="nan", target_unit="nan")]
    entry = _transform(rows)[0]
    assert "unit" not in entry["source"]
    assert entry["source"]["context"] == ["emissions to air"]
    assert "unit" not in entry["target"]
    assert not any(
        str(v).lower() == "nan"
        for part in (entry["source"], entry["target"])
        for v in part.values()
    )


def test_row_without_a_target_code_is_dropped():
    assert _transform([_row(target_code="")]) == []


def test_non_ef_target_is_dropped():
    assert _transform([_row(target_db="ecoinvent-3.9.1-biosphere")]) == []


def test_assembled_package_validates_against_the_randonneur_schema():
    rows = _transform([_row(), _row(source_name="Methane, fossil", tier="T3")])
    package = _assemble(
        rows,
        SourceConfig(
            name="biosphere",
            module="sentier_importers.sources.bafu.mappings_biosphere",
            target="sentier_mappings",
            category="03-bafu-2026-v1__ef-3.1",
            fetch_url="file://test",
            fetch_format="parquet",
            output_format="json",
            package_name="bafu-2026-v1__ef-3.1-biosphere",
            package_version="0.1.0",
            package_verb="replace",
        ),
    )
    assert package["name"] == "bafu-2026-v1__ef-3.1-biosphere"
    assert len(package["replace"]) == 2
    jsonschema.validate(package, json.loads(_SCHEMA.read_text()))
