"""End-to-end: rank-4 (biosphere3 -> Eaternity) x CF identity -> bafu-2026-v1 -> ef-3.1."""

import copy
import io
import json
from pathlib import Path

import jsonschema
import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from sentier_importers.core.context import RunContext
from sentier_importers.core.pipeline import _assemble
from sentier_importers.core.source import SourceConfig
from sentier_importers.core.types import RawData
from sentier_importers.sources.bafu.ecospold import flow_id
from sentier_importers.sources.eaternity.bridge import BafuFlow
from sentier_importers.sources.eaternity.cf_identity import EfLabels, Twin
from sentier_importers.sources.eaternity.inference import entry_for
from sentier_importers.sources.eaternity.inference_review import EaternityInferenceReviewSource
from sentier_importers.sources.eaternity.mappings_biosphere import EaternityInferredBafuEfSource

from tests.sources.bafu_fixture import fixture_zip

_SCHEMA = Path(__file__).parent / "fixtures" / "randonneur-package.schema.json"
ECO = "ecoinvent-3.9.1-biosphere"
B3_CO2 = "349b29d1-3e58-4c66-98b9-9d1a076efd2e"
B3_WATER = "b3-water-river"
B3_GHOST = "b3-ghost"
EF_CO2 = "08a91e70-3ddc-11dd-91be-0050c2490048"
EF_CO2_URBAN = "ef-co2-urban"
EF_WATER = "ef-water-river"
EAT_CO2 = "eaternity-co2"
EAT_WATER = "eaternity-water"
EAT_GHOST = "eaternity-ghost"


def _rank4(entries):
    return {
        "name": "ecoinvent-biosphere3__eaternity-bafu-ext-biosphere",
        "version": "0.2.0",
        "replace": entries,
    }


def _b3(code, name, unit, context, target_code, target_name, target_unit, target_root):
    return {
        "source": {"name": name, "code": code, "unit": unit, "context": context},
        "target": {
            "name": target_name,
            "code": target_code,
            "unit": target_unit,
            "context": [target_root],
        },
    }


RANK4 = _rank4(
    [
        _b3(
            B3_CO2,
            "Carbon dioxide, fossil",
            "kilogram",
            ["air"],
            EAT_CO2,
            "Carbon dioxide, fossil",
            "kg",
            "air",
        ),
        _b3(
            B3_WATER,
            "Water, river",
            "cubic meter",
            ["natural resource", "in water"],
            EAT_WATER,
            "Water, river",
            "m3",
            "natural resource",
        ),
        _b3(
            B3_GHOST,
            "Hexythiazox",
            "kilogram",
            ["soil", "agricultural"],
            EAT_GHOST,
            "Hexythiazox",
            "kg",
            "soil",
        ),
    ]
)
RANK3_EMPTY = {"name": "bafu-2026-v1__ef-3.1-biosphere", "version": "0.5.0", "replace": []}
RANK3_WITH_CO2 = {
    **RANK3_EMPTY,
    "replace": [
        {
            "source": {
                "name": "Carbon dioxide, fossil",
                "code": flow_id("Carbon dioxide, fossil", "emissions to air", "unspecified", "kg"),
                "unit": "kg",
                "context": ["emissions to air", "unspecified"],
            },
            "target": {"name": "carbon dioxide, fossil", "code": EF_CO2, "unit": "kilogram"},
        }
    ],
}
CF_TABLES = {
    "climate": [
        {"database": ECO, "code": B3_CO2, "amount": 1.0},
        {"database": "ef", "code": EF_CO2, "amount": 1.0},
        {"database": "ef", "code": EF_CO2_URBAN, "amount": 1.0},
    ],
}
EF_ROWS = [
    {
        "flow": f"https://vocab.sentier.dev/flows/{EF_CO2}",
        "flow_name": "carbon dioxide, fossil",
        "flow_context": "Emissions / Emissions to air / Emissions to air, unspecified",
    },
    {
        "flow": EF_CO2_URBAN,
        "flow_name": "carbon dioxide, fossil",
        "flow_context": "Emissions / Emissions to air / Emissions to urban air close to ground",
    },
    {
        "flow": EF_WATER,
        "flow_name": "water, river",
        "flow_context": "Resources / Resources from water",
    },
]


def _parquet(rows, schema):
    buffer = io.BytesIO()
    pq.write_table(pa.Table.from_pylist(rows, schema=schema), buffer)
    return buffer.getvalue()


def _stage(tmp_path, rank3=RANK3_EMPTY):
    (tmp_path / "rank4.json").write_text(json.dumps(RANK4))
    (tmp_path / "rank3.json").write_text(json.dumps(rank3))
    (tmp_path / "bafu.zip").write_bytes(fixture_zip().content)
    (tmp_path / "ef.parquet").write_bytes(
        _parquet(
            EF_ROWS,
            pa.schema(
                [("flow", pa.string()), ("flow_name", pa.string()), ("flow_context", pa.string())]
            ),
        )
    )
    cfs = tmp_path / "method_cfs"
    for method, rows in CF_TABLES.items():
        (cfs / method).mkdir(parents=True)
        (cfs / method / "cfs.parquet").write_bytes(
            _parquet(
                rows,
                pa.schema(
                    [("database", pa.string()), ("code", pa.string()), ("amount", pa.float64())]
                ),
            )
        )
    return tmp_path


def _config(
    root, module, name="eaternity-inferred-bafu-ef-biosphere", verb="replace", emit="biosphere"
):
    return SourceConfig(
        name=name,
        module=module,
        target="sentier_mappings",
        category="06-bafu-2026-v1__ef-3.1",
        fetch_url=f"file://{root}/rank4.json",
        fetch_format="json",
        output_format="json",
        emit_filename=emit,
        package_name="bafu-2026-v1__ef-3.1-biosphere-inferred",
        package_version="0.1.0",
        package_verb=verb,
        inputs={
            "rank3": f"file://{root}/rank3.json",
            "ecospold": f"file://{root}/bafu.zip",
            "ef_flows": f"file://{root}/ef.parquet",
            "method_cfs": f"file://{root}/method_cfs",
        },
    )


def _run(source, tmp_path):
    ctx = RunContext(cache_dir=tmp_path / "cache", output_dir=tmp_path / "out")
    return source.transform(source.parse(source.fetch(ctx)))


def test_infers_a_bafu_ef_entry_from_the_eaternity_pair(tmp_path):
    root = _stage(tmp_path)
    source = EaternityInferredBafuEfSource(
        _config(root, "sentier_importers.sources.eaternity.mappings_biosphere")
    )
    rows = _run(source, tmp_path)
    assert len(rows) == 1
    entry = rows[0]
    assert entry["source"] == {
        "name": "Carbon dioxide, fossil",
        "code": flow_id("Carbon dioxide, fossil", "emissions to air", "unspecified", "kg"),
        "unit": "kg",
        "context": ["emissions to air", "unspecified"],
    }
    assert entry["target"] == {
        "name": "carbon dioxide, fossil",
        "code": EF_CO2,
        "unit": "kilogram",
        "context": ["Emissions", "Emissions to air", "Emissions to air, unspecified"],
    }
    assert "comment" not in entry
    assert "conversion_factor" not in entry


def test_flows_already_in_rank_3_are_not_re_emitted(tmp_path):
    root = _stage(tmp_path, rank3=RANK3_WITH_CO2)
    source = EaternityInferredBafuEfSource(
        _config(root, "sentier_importers.sources.eaternity.mappings_biosphere")
    )
    assert _run(source, tmp_path) == []


def test_review_sidecar_explains_every_withheld_pair(tmp_path):
    root = _stage(tmp_path)
    source = EaternityInferenceReviewSource(
        _config(
            root,
            "sentier_importers.sources.eaternity.inference_review",
            name="eaternity-inferred-bafu-ef-review",
            verb="review",
            emit="inference_review",
        )
    )
    rows = _run(source, tmp_path)
    by_reason = {r["reason"]: r for r in rows}
    # Water, river resolves to a BAFU flow but its biosphere3 partner has no EF factor
    assert by_reason["uncharacterised"]["source"]["name"] == "Water, river"
    assert by_reason["uncharacterised"]["source"]["context"] == ["resources", "in water"]
    # Hexythiazox is an Eaternity extension flow: not in the BAFU-2026 v1 universe
    ghost = by_reason["target_unresolved"]
    assert ghost["source"] == {
        "name": "Hexythiazox",
        "code": EAT_GHOST,
        "unit": "kg",
        "context": ["soil"],
    }
    assert len(rows) == 2


def test_no_entry_ever_names_the_intermediate_database(tmp_path):
    root = _stage(tmp_path)
    source = EaternityInferredBafuEfSource(
        _config(root, "sentier_importers.sources.eaternity.mappings_biosphere")
    )
    blob = json.dumps(_run(source, tmp_path)).lower()
    assert "ecoinvent" not in blob
    assert "biosphere3" not in blob


def test_assembled_package_validates_against_the_randonneur_schema(tmp_path):
    root = _stage(tmp_path)
    config = _config(root, "sentier_importers.sources.eaternity.mappings_biosphere")
    rows = _run(EaternityInferredBafuEfSource(config), tmp_path)
    package = _assemble(rows, config)
    assert package["name"] == "bafu-2026-v1__ef-3.1-biosphere-inferred"
    jsonschema.validate(package, json.loads(_SCHEMA.read_text()))


def test_partners_asserting_different_ef_flows_are_withheld_not_picked(tmp_path):
    """Two biosphere3 flows both routed to the same Eaternity flow, with different CF
    vectors: neither may win by code order."""

    root = _stage(tmp_path)
    rank4 = copy.deepcopy(RANK4)
    rank4["replace"].append(
        _b3(
            "zz-other-co2",
            "Carbon dioxide, land transformation",
            "kilogram",
            ["air"],
            EAT_CO2,
            "Carbon dioxide, fossil",
            "kg",
            "air",
        )
    )
    (root / "rank4.json").write_text(json.dumps(rank4))
    cfs = root / "method_cfs" / "climate" / "cfs.parquet"
    rows = CF_TABLES["climate"] + [
        {"database": ECO, "code": "zz-other-co2", "amount": 0.5},
        {"database": "ef", "code": "ef-co2-lt", "amount": 0.5},
    ]
    cfs.write_bytes(
        _parquet(
            rows,
            pa.schema(
                [("database", pa.string()), ("code", pa.string()), ("amount", pa.float64())]
            ),
        )
    )
    ef = root / "ef.parquet"
    ef.write_bytes(
        _parquet(
            EF_ROWS
            + [
                {
                    "flow": "ef-co2-lt",
                    "flow_name": "carbon dioxide (land use change)",
                    "flow_context": "Emissions / Emissions to air / Emissions to air, unspecified",
                }
            ],
            pa.schema(
                [("flow", pa.string()), ("flow_name", pa.string()), ("flow_context", pa.string())]
            ),
        )
    )
    config = _config(root, "sentier_importers.sources.eaternity.mappings_biosphere")
    source = EaternityInferredBafuEfSource(config)
    inference = source.infer(
        source.parse(
            source.fetch(RunContext(cache_dir=tmp_path / "cache", output_dir=tmp_path / "out"))
        )
    )
    assert inference.entries == []
    disagree = [r for r in inference.review if r["reason"] == "partners_disagree"]
    assert len(disagree) == 1
    assert disagree[0]["source"]["name"] == "Carbon dioxide, fossil"
    assert EF_CO2 in disagree[0]["detail"] and "ef-co2-lt" in disagree[0]["detail"]


def test_rank_3_skips_are_counted(tmp_path):
    root = _stage(tmp_path, rank3=RANK3_WITH_CO2)
    source = EaternityInferredBafuEfSource(
        _config(root, "sentier_importers.sources.eaternity.mappings_biosphere")
    )
    inference = source.infer(
        source.parse(
            source.fetch(RunContext(cache_dir=tmp_path / "cache", output_dir=tmp_path / "out"))
        )
    )
    assert inference.skipped_in_rank3 == 1
    assert inference.entries == []


def test_review_sidecar_never_carries_an_intermediate_database_identifier(tmp_path):
    """Reasons may say "biosphere3 partner" (a public Brightway name); no biosphere3 code
    or ecoinvent database name may reach the sidecar."""
    root = _stage(tmp_path)
    source = EaternityInferenceReviewSource(
        _config(
            root,
            "sentier_importers.sources.eaternity.inference_review",
            name="eaternity-inferred-bafu-ef-review",
            verb="review",
            emit="inference_review",
        )
    )
    blob = json.dumps(_run(source, tmp_path)).lower()
    assert "ecoinvent" not in blob
    for code in (B3_CO2, B3_WATER, B3_GHOST):
        assert code.lower() not in blob


def test_parse_before_fetch_fails_loudly(tmp_path):
    root = _stage(tmp_path)
    source = EaternityInferredBafuEfSource(
        _config(root, "sentier_importers.sources.eaternity.mappings_biosphere")
    )
    with pytest.raises(RuntimeError, match="fetch\\(\\) must run before parse"):
        source.parse(RawData(content=b"{}", source_url="file://x"))


def test_volume_flows_get_the_ef_unit_spelling():
    labels = EfLabels.from_rows(
        [
            {
                "flow": EF_WATER,
                "flow_name": "water, river",
                "flow_context": "Resources / Resources from water",
            }
        ]
    )
    entry = entry_for(
        BafuFlow("Water, river", "resources", "in water", "m3"),
        Twin(EF_WATER, "T2", 1, True, True),
        1.0,
        labels,
    )
    assert entry["source"]["unit"] == "m3"
    assert entry["target"]["unit"] == "cubic meter"
