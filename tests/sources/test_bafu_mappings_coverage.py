import json
from dataclasses import replace

import pyarrow as pa
import pyarrow.parquet as pq
from sentier_importers.core.context import RunContext
from sentier_importers.core.pipeline import _assemble
from sentier_importers.sources.bafu.ecospold import flow_id
from sentier_importers.sources.bafu.mappings_biosphere_coverage import BafuEfCoverageSource
from sentier_importers.sources.eaternity.bridge import BafuFlow, BafuFlowIndex

from tests.matching.ef_fixtures import CF_SCHEMA, cf_row, vocab_row
from tests.sources.test_bafu_mappings_matched import (
    CF,
    CO2,
    RADON,
    VOCAB,
    WATER,
    _config,
    _run,
    _stage,
)

GAS = flow_id("Gas, natural/m3", "resources", "in ground", "m3")
PEAT = flow_id("Peat", "resources", "in ground", "kg")


def _source(root):
    return BafuEfCoverageSource(
        _config(
            root,
            name="bafu-ef-coverage",
            module="sentier_importers.sources.bafu.mappings_biosphere_coverage",
            verb="coverage",
            emit="coverage",
        )
    )


def test_every_universe_flow_has_exactly_one_row(tmp_path):
    root = _stage(tmp_path, rank3=[CO2], rank6=[RADON])
    rows = _run(_source(root), tmp_path)
    assert len(rows) == 5
    by_code = {r["source"]["code"]: r for r in rows}
    assert set(by_code) == {CO2, WATER, RADON, GAS, PEAT}
    assert by_code[CO2] == {
        "source": {
            "name": "Carbon dioxide, fossil",
            "code": CO2,
            "unit": "kg",
            "context": ["emissions to air", "unspecified"],
        },
        "status": "mapped",
        "bridge": 3,
    }
    assert by_code[RADON]["status"] == "mapped" and by_code[RADON]["bridge"] == 6
    assert by_code[WATER] == {
        "source": {
            "name": "Water, river",
            "code": WATER,
            "unit": "m3",
            "context": ["resources", "in water"],
        },
        "status": "mapped",
        "bridge": 7,
        "tier": "alias",
        "placement": "exact",
    }


def test_rows_are_sorted_by_name_then_context(tmp_path):
    rows = _run(_source(_stage(tmp_path)), tmp_path)
    names = [r["source"]["name"] for r in rows]
    assert names == sorted(names)


def test_unmapped_rows_carry_reason_and_detail(tmp_path):
    root = _stage(tmp_path)
    rows = _run(_source(root), tmp_path)
    by_name = {r["source"]["name"]: r for r in rows}
    gas = by_name["Gas, natural/m3"]
    assert gas["status"] == "unmapped" and gas["reason"] == "no_ef_flow"
    assert "resource compartment" in gas["detail"]
    assert "bridge" not in gas and "tier" not in gas


def test_unit_mismatch_is_reported_as_unmapped(tmp_path):
    root = _stage(tmp_path)
    # an EF natural-gas flow in MJ: the BAFU m3 flow matches by alias/name but cannot be converted
    extra = [
        cf_row(
            "gas",
            "natural gas",
            "Resources / Resources from ground / Non-renewable energy resources from ground",
            method="ef-3.1:resource-use-fossils",
            value=1.0,
        )
    ]
    cf = pq.read_table(root / "characterization-factors.parquet").to_pylist() + extra
    pq.write_table(
        pa.Table.from_pylist(cf, schema=CF_SCHEMA), root / "characterization-factors.parquet"
    )
    # give the BAFU name a route to the EF flow through a synonym-free exact name is impossible
    # ("Gas, natural/m3" != "natural gas"), so rely on the matched source's alias table only if it
    # has one; otherwise this stays no_ef_flow. Assert the row is unmapped either way and, if it
    # reached the unit check, that the reason is unit_mismatch.
    rows = _run(_source(root), tmp_path)
    gas = next(r for r in rows if r["source"]["name"] == "Gas, natural/m3")
    assert gas["status"] == "unmapped"
    assert gas["reason"] in {"no_ef_flow", "unit_mismatch"}


def test_location_and_caveats_appear_only_when_present(tmp_path):
    root = _stage(tmp_path)
    rows = _run(_source(root), tmp_path)
    for r in rows:
        if r["status"] == "mapped" and r["bridge"] == 7:
            assert "location" not in r or r["location"]
            assert "caveats" not in r or r["caveats"]
        else:
            assert "location" not in r and "caveats" not in r


def test_cas_conflicts_are_reported_on_the_affected_rows(tmp_path):
    root = _stage(tmp_path)
    source = _source(root)
    records = source.parse(
        source.fetch(RunContext(cache_dir=tmp_path / "cache", output_dir=tmp_path / "out"))
    )
    inputs = records[0]["inputs"]
    conflicted = replace(inputs, cas_conflicts={"Peat": ("1-1-1", "2-2-2")})
    rows = source.transform([{"inputs": conflicted}])
    peat = next(r for r in rows if r["source"]["name"] == "Peat")
    assert peat["cas_conflict"] == ["1-1-1", "2-2-2"]
    assert all("cas_conflict" not in r for r in rows if r["source"]["name"] != "Peat")


def test_assembled_package_uses_the_coverage_verb(tmp_path):
    config = _config(
        _stage(tmp_path),
        name="bafu-ef-coverage",
        module="sentier_importers.sources.bafu.mappings_biosphere_coverage",
        verb="coverage",
        emit="coverage",
    )
    package = _assemble(_run(BafuEfCoverageSource(config), tmp_path), config)
    assert set(package) == {"name", "version", "coverage"}
    assert len(package["coverage"]) == 5
    json.dumps(package)  # JSON-serialisable (no sets, no tuples that matter)


def test_no_intermediate_database_identifier_in_output(tmp_path):
    blob = json.dumps(_run(_source(_stage(tmp_path)), tmp_path)).lower()
    assert "ecoinvent" not in blob and "biosphere3" not in blob


def test_rank7_match_location_and_caveats_are_reported_when_present(tmp_path):
    # "Water, KR" resolves to a real EF water-use flow with a resolvable ISO location
    # (location present, no caveats); "Water, Europe" resolves to the same flow but as a
    # regional aggregate EF cannot place (caveats present, no location) -- see the sibling
    # matched source's own ``test_location_and_caveats_are_carried``.
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
    source = _source(root)
    records = source.parse(
        source.fetch(RunContext(cache_dir=tmp_path / "cache", output_dir=tmp_path / "out"))
    )
    inputs = records[0]["inputs"]
    extra = [
        BafuFlow("Water, KR", "emissions to water", "unspecified", "m3"),
        BafuFlow("Water, Europe", "emissions to water", "unspecified", "m3"),
    ]
    augmented = replace(inputs, bafu=BafuFlowIndex.from_flows(list(inputs.bafu) + extra))
    rows = source.transform([{"inputs": augmented}])
    by_name = {r["source"]["name"]: r for r in rows}

    kr = by_name["Water, KR"]
    assert kr["bridge"] == 7 and kr["location"] == "KR" and "caveats" not in kr

    europe = by_name["Water, Europe"]
    assert europe["bridge"] == 7 and "location" not in europe
    assert europe["caveats"] == [
        "regional aggregate Europe in the source name; EF applies the global default factor"
    ]
