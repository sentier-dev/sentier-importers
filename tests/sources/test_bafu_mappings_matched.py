import json
from pathlib import Path

import jsonschema
import pytest
from sentier_importers.core.context import RunContext
from sentier_importers.core.pipeline import _assemble
from sentier_importers.core.source import SourceConfig
from sentier_importers.core.types import RawData
from sentier_importers.matching.pipeline import Unmatched
from sentier_importers.sources.bafu.ecospold import flow_id
from sentier_importers.sources.bafu.mappings_biosphere_matched import (
    BafuEfMatchedSource,
    classify_unmatched,
    substance_cas,
)
from sentier_importers.sources.eaternity.bridge import BafuFlow

from tests.matching.ef_fixtures import (
    AIR_RURAL,
    AIR_UNSPEC,
    RES_WATER,
    cf_row,
    vocab_row,
    write_ef_inputs,
)
from tests.sources.bafu_fixture import fixture_zip

_SCHEMA = Path(__file__).parent / "fixtures" / "randonneur-package.schema.json"
# fixture zip biosphere flows: Carbon dioxide, fossil | emissions to air | unspecified | kg
#                                                                              (CAS 124-38-9)
#                              Water, river           | resources        | in water    | m3
#                              Radon-222              | emissions to air | low. pop.   | Bq
#                                                                              (CAS 14859-67-7)
CF = [
    cf_row(
        "co2-fos", "carbon dioxide (fossil)", AIR_UNSPEC, method="ef-3.1:climate-change", value=1.0
    ),
    cf_row("river", "river water", RES_WATER, method="ef-3.1:water-use", value=42.95),
    cf_row(
        "rn222",
        "radon-222",
        AIR_RURAL,
        method="ef-3.1:ionising-radiation-human-health",
        value=2.1e-8,
    ),
]
VOCAB = [
    vocab_row("co2-fos", "Carbon dioxide (fossil)", cas="124-38-9"),
    vocab_row("river", "River water", cas="7732-18-5"),
    vocab_row("rn222", "Radon-222", cas="14859-67-7"),
]
CO2 = flow_id("Carbon dioxide, fossil", "emissions to air", "unspecified", "kg")
WATER = flow_id("Water, river", "resources", "in water", "m3")
RADON = flow_id("Radon-222", "emissions to air", "low. pop.", "Bq")


def _payload(codes):
    return {
        "name": "x",
        "version": "0",
        "replace": [{"source": {"code": c}, "target": {"code": "t"}} for c in codes],
    }


def _stage(tmp_path, rank3=(), rank6=(), cf=CF, vocab=VOCAB):
    (tmp_path / "bafu.zip").write_bytes(fixture_zip().content)
    write_ef_inputs(tmp_path, cf, vocab)
    (tmp_path / "rank3.json").write_text(json.dumps(_payload(rank3)))
    (tmp_path / "rank6.json").write_text(json.dumps(_payload(rank6)))
    return tmp_path


def _config(
    root,
    name="bafu-ef-biosphere-matched",
    module="sentier_importers.sources.bafu.mappings_biosphere_matched",
    verb="replace",
    emit="biosphere",
):
    return SourceConfig(
        name=name,
        module=module,
        target="sentier_mappings",
        category="07-bafu-2026-v1__ef-3.1",
        fetch_url=f"file://{root}/bafu.zip",
        fetch_format="zip",
        output_format="json",
        emit_filename=emit,
        package_name="bafu-2026-v1__ef-3.1-biosphere-matched",
        package_version="0.1.0",
        package_verb=verb,
        inputs={
            "rank3": f"file://{root}/rank3.json",
            "rank6": f"file://{root}/rank6.json",
            "ef_cfs": f"file://{root}/characterization-factors.parquet",
            "ef_vocab": f"file://{root}/elementary-flows",
        },
    )


def _run(source, tmp_path):
    ctx = RunContext(cache_dir=tmp_path / "cache", output_dir=tmp_path / "out")
    return source.transform(source.parse(source.fetch(ctx)))


def test_emits_entries_with_flow_ids_ef_units_and_no_comment_for_clean_matches(tmp_path):
    rows = _run(BafuEfMatchedSource(_config(_stage(tmp_path))), tmp_path)
    by_code = {r["source"]["code"]: r for r in rows}
    assert set(by_code) == {CO2, WATER, RADON}
    co2 = by_code[CO2]
    assert co2["source"] == {
        "name": "Carbon dioxide, fossil",
        "code": CO2,
        "unit": "kg",
        "context": ["emissions to air", "unspecified"],
    }
    assert co2["target"] == {
        "code": "co2-fos",
        "name": "Carbon dioxide (fossil)",
        "unit": "kilogram",
        "context": ["Emissions", "Emissions to air", "Emissions to air, unspecified"],
    }
    assert "comment" not in co2 and "conversion_factor" not in co2
    water = by_code[WATER]
    assert water["target"]["code"] == "river" and water["target"]["unit"] == "cubic meter"
    assert "conversion_factor" not in water and "location" not in water["target"]


def test_becquerel_sources_land_on_kilobecquerel_with_a_conversion(tmp_path):
    rows = _run(BafuEfMatchedSource(_config(_stage(tmp_path))), tmp_path)
    radon = next(r for r in rows if r["source"]["code"] == RADON)
    assert radon["source"]["unit"] == "Bq" and radon["target"]["unit"] == "kBq"
    assert radon["conversion_factor"] == 0.001


def test_kilogram_water_emission_onto_an_ef_water_use_flow_converts_to_cubic_meter(tmp_path):
    # a BAFU kg source whose EF target carries only the water-use method: 1 kg = 0.001 m3
    cf = CF + [
        cf_row(
            "water-em",
            "water",
            "Emissions / Emissions to water / Emissions to water, unspecified",
            method="ef-3.1:water-use",
            value=-37.8,
        )
    ]
    vocab = VOCAB + [vocab_row("water-em", "Water", cas="7732-18-5")]
    root = _stage(tmp_path, cf=cf, vocab=vocab)
    source = BafuEfMatchedSource(_config(root))
    records = source.parse(
        source.fetch(RunContext(cache_dir=tmp_path / "cache", output_dir=tmp_path / "out"))
    )
    (r,) = records
    from_kg = source.entry_for(
        BafuFlow("Water", "emissions to water", "unspecified", "kg"),
        r["pipeline"].match(BafuFlow("Water", "emissions to water", "unspecified", "kg"), None),
        r["index"],
    )
    assert from_kg["target"]["unit"] == "cubic meter" and from_kg["conversion_factor"] == 0.001


def test_flows_already_in_rank_3_or_6_are_skipped(tmp_path):
    rows = _run(
        BafuEfMatchedSource(_config(_stage(tmp_path, rank3=[CO2], rank6=[RADON]))), tmp_path
    )
    assert [r["source"]["code"] for r in rows] == [WATER]


def test_location_and_caveats_are_carried(tmp_path):
    cf = CF + [
        cf_row(
            "water-em",
            "water",
            "Emissions / Emissions to water / Emissions to water, unspecified",
            method="ef-3.1:water-use",
            value=-37.8,
            location=None,
        )
    ]
    vocab = VOCAB + [vocab_row("water-em", "Water", cas="7732-18-5")]
    root = _stage(tmp_path, cf=cf, vocab=vocab)
    source = BafuEfMatchedSource(_config(root))
    (r,) = source.parse(
        source.fetch(RunContext(cache_dir=tmp_path / "cache", output_dir=tmp_path / "out"))
    )
    flow = BafuFlow("Water, KR", "emissions to water", "unspecified", "m3")
    entry = source.entry_for(flow, r["pipeline"].match(flow, None), r["index"])
    assert entry["target"]["location"] == "KR" and "conversion_factor" not in entry
    flow = BafuFlow("Water, Europe", "emissions to water", "unspecified", "m3")
    entry = source.entry_for(flow, r["pipeline"].match(flow, None), r["index"])
    assert "location" not in entry["target"]
    assert (
        entry["comment"]
        == "regional aggregate Europe in the source name; EF applies the global default factor"
    )


def test_substance_cas_is_per_name_and_drops_conflicts():
    records = [
        {
            "exchanges": [
                {
                    "name": "Methanol",
                    "category": "emissions to air",
                    "subcategory": "unspecified",
                    "unit": "kg",
                    "cas": "000067-56-1",
                    "group_code": 4,
                },
                {
                    "name": "Methanol",
                    "category": "emissions to water",
                    "subcategory": "river",
                    "unit": "kg",
                    "cas": None,
                    "group_code": 4,
                },
                {
                    "name": "Mixed",
                    "category": "emissions to air",
                    "subcategory": "unspecified",
                    "unit": "kg",
                    "cas": "1-1-1",
                    "group_code": 4,
                },
                {
                    "name": "Mixed",
                    "category": "emissions to water",
                    "subcategory": "river",
                    "unit": "kg",
                    "cas": "2-2-2",
                    "group_code": 4,
                },
                {
                    "name": "Electricity",
                    "category": "electricity",
                    "subcategory": "mix",
                    "unit": "kWh",
                    "cas": "9-9-9",
                    "group_code": 5,
                },
            ]
        }
    ]
    cas, conflicts = substance_cas(records)
    assert cas == {"Methanol": "000067-56-1"}
    assert conflicts == {"Mixed": {"1-1-1", "2-2-2"}}


def test_classify_unmatched_applies_the_plan_decisions():
    none = Unmatched("no_ef_flow", "x")
    assert (
        classify_unmatched(
            BafuFlow("Arsenic, ion", "emissions to water", "river", "kg"), none
        ).reason
        == "speciation"
    )
    assert (
        classify_unmatched(
            BafuFlow("Copper ion", "emissions to water", "river", "kg"), none
        ).reason
        == "speciation"
    )
    assert (
        classify_unmatched(
            BafuFlow("Calcium II", "emissions to water", "river", "kg"), none
        ).reason
        == "speciation"
    )
    assert (
        classify_unmatched(
            BafuFlow("Carbon dioxide", "emissions to air", "unspecified", "kg"), none
        ).reason
        == "qualifier_missing"
    )
    assert (
        classify_unmatched(
            BafuFlow("Carbon monoxide", "emissions to air", "high. pop.", "kg"), none
        ).reason
        == "qualifier_missing"
    )
    assert (
        classify_unmatched(
            BafuFlow("Carbon dioxide", "emissions to water", "river", "kg"), none
        ).reason
        == "no_ef_flow"
    )
    salt = classify_unmatched(BafuFlow("Water, salt, ocean", "resources", "in water", "m3"), none)
    assert salt.reason == "no_ef_flow" and "freshwater deprivation" in salt.detail
    other = Unmatched("sub_compartment_absent", "y")
    assert (
        classify_unmatched(BafuFlow("Arsenic, ion", "emissions to water", "river", "kg"), other)
        is other
    )
    # Decision 5 also covers fossil water even when the pipeline finds candidates
    fossil = classify_unmatched(
        BafuFlow("Water, fossil", "resources", "in water", "m3"),
        Unmatched("ambiguous_substances", "z"),
    )
    assert fossil.reason == "no_ef_flow" and "freshwater deprivation" in fossil.detail


def test_assembled_package_validates_against_the_randonneur_schema(tmp_path):
    config = _config(_stage(tmp_path))
    package = _assemble(_run(BafuEfMatchedSource(config), tmp_path), config)
    jsonschema.validate(package, json.loads(_SCHEMA.read_text()))


def test_no_intermediate_database_identifier_in_output(tmp_path):
    blob = json.dumps(_run(BafuEfMatchedSource(_config(_stage(tmp_path))), tmp_path)).lower()
    assert "ecoinvent" not in blob and "biosphere3" not in blob


def test_parse_before_fetch_fails_loudly(tmp_path):
    source = BafuEfMatchedSource(_config(_stage(tmp_path)))
    with pytest.raises(RuntimeError, match="fetch\\(\\) must run before parse"):
        source.parse(RawData(content=b"", source_url="file://x"))
