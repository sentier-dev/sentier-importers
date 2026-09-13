import json
from dataclasses import replace

import pyarrow as pa
import pyarrow.parquet as pq
from sentier_importers.core.context import RunContext
from sentier_importers.core.pipeline import _assemble
from sentier_importers.sources.bafu.mappings_biosphere_coverage import BafuEfCoverageSource
from sentier_importers.sources.eaternity.bridge import BafuFlow, BafuFlowIndex

from tests.matching.ef_fixtures import CF_SCHEMA, RES_GROUND, VOCAB_SCHEMA, cf_row, vocab_row
from tests.sources.test_bafu_mappings_matched import (
    CF,
    CO2,
    GAS,
    LAND,
    MINE_GAS,
    PEAT,
    RADON,
    VOCAB,
    WATER,
    _config,
    _run,
    _stage,
)


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
    assert len(rows) == 7
    by_code = {r["source"]["code"]: r for r in rows}
    assert set(by_code) == {CO2, WATER, RADON, GAS, PEAT, MINE_GAS, LAND}
    assert by_code[LAND]["status"] == "mapped" and by_code[LAND]["bridge"] == 7
    assert by_code[LAND]["tier"] == "landuse"
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


def test_rank_3_wins_when_a_flow_is_named_by_both_rank_3_and_rank_6(tmp_path):
    root = _stage(tmp_path, rank3=[CO2], rank6=[CO2])
    rows = _run(_source(root), tmp_path)
    co2 = next(r for r in rows if r["source"]["code"] == CO2)
    assert co2["status"] == "mapped" and co2["bridge"] == 3


def test_rows_are_sorted_by_name_then_context(tmp_path):
    rows = _run(_source(_stage(tmp_path)), tmp_path)
    keys = [
        (r["source"]["name"], tuple(r["source"]["context"]), r["source"]["unit"]) for r in rows
    ]
    assert keys == sorted(keys)


def test_rows_sharing_a_name_are_ordered_by_subcategory(tmp_path):
    # two Zinc flows differing only in sub-compartment, built out of order, must come back
    # agricultural before industrial: the tiebreak the fixture's five distinct names cannot
    # exercise on their own.
    root = _stage(tmp_path)
    source = _source(root)
    records = source.parse(
        source.fetch(RunContext(cache_dir=tmp_path / "cache", output_dir=tmp_path / "out"))
    )
    inputs = records[0]["inputs"]
    zn_industrial = BafuFlow("Zinc", "emissions to soil", "industrial", "kg")
    zn_agricultural = BafuFlow("Zinc", "emissions to soil", "agricultural", "kg")
    augmented = replace(inputs, bafu=BafuFlowIndex.from_flows([zn_industrial, zn_agricultural]))
    rows = source.transform([{"inputs": augmented}])
    assert [r["source"]["context"] for r in rows] == [
        ["emissions to soil", "agricultural"],
        ["emissions to soil", "industrial"],
    ]


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
    # an EF natural-gas flow in MJ, reachable from both BAFU gas flows by alias/synonym,
    # so the pipeline resolves a real Match for each. "Gas, natural/m3" has a fixed
    # energy-content factor (decision 2026-09-13) and converts; the mine off-gas flow's
    # name is not in that table, so it is still withheld as unit_mismatch.
    extra_cf = [
        cf_row("gas", "natural gas", RES_GROUND, method="ef-3.1:resource-use-fossils", value=1.0)
    ]
    cf = pq.read_table(root / "characterization-factors.parquet").to_pylist() + extra_cf
    pq.write_table(
        pa.Table.from_pylist(cf, schema=CF_SCHEMA), root / "characterization-factors.parquet"
    )
    extra_vocab = [
        vocab_row(
            "gas",
            "Natural gas",
            alt=["Gas, natural/m3", "Gas, mine, off-gas, process, coal mining/m3"],
        )
    ]
    vocab_path = root / "elementary-flows" / "air-01.parquet"
    vocab = pq.read_table(vocab_path).to_pylist() + extra_vocab
    pq.write_table(pa.Table.from_pylist(vocab, schema=VOCAB_SCHEMA), vocab_path)
    rows = _run(_source(root), tmp_path)
    gas = next(r for r in rows if r["source"]["name"] == "Gas, natural/m3")
    assert gas["status"] == "mapped" and gas["bridge"] == 7
    mine_gas = next(
        r for r in rows if r["source"]["name"] == "Gas, mine, off-gas, process, coal mining/m3"
    )
    assert mine_gas["status"] == "unmapped" and mine_gas["reason"] == "unit_mismatch"
    assert "megajoule" in mine_gas["detail"]


def test_location_and_caveats_are_absent_outside_a_rank7_match(tmp_path):
    # the fixture's own rank-7 match (WATER, via alias) carries neither key; the positive
    # case (location and/or caveats present on a real rank-7 match) is pinned by
    # ``test_rank7_match_location_and_caveats_are_reported_when_present`` below.
    root = _stage(tmp_path)
    rows = _run(_source(root), tmp_path)
    for r in rows:
        if not (r["status"] == "mapped" and r["bridge"] == 7):
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
    assert len(package["coverage"]) == 7
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
