import json
from dataclasses import replace

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from sentier_importers.core.context import RunContext
from sentier_importers.core.pipeline import _assemble
from sentier_importers.matching.pipeline import Match
from sentier_importers.sources.bafu import mappings_biosphere_coverage as coverage_mod
from sentier_importers.sources.bafu.mappings_biosphere_coverage import BafuEfCoverageSource
from sentier_importers.sources.bafu.mappings_biosphere_matched import outcome_for
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
    URANIUM,
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
    assert len(rows) == 8
    by_code = {r["source"]["code"]: r for r in rows}
    assert set(by_code) == {CO2, WATER, RADON, GAS, PEAT, MINE_GAS, LAND, URANIUM}
    # Uranium has no matching EF flow in the default fixture CF/VOCAB at all (its
    # resource-branch fallback is exercised with a dedicated CF row in the sibling
    # matched-source test), so it stays unmapped here too.
    assert by_code[URANIUM]["status"] == "unmapped"
    assert by_code[URANIUM]["reason"] == "no_ef_flow"
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
    assert len(package["coverage"]) == 8
    json.dumps(package)  # JSON-serialisable (no sets, no tuples that matter)


def test_no_intermediate_database_identifier_in_output(tmp_path):
    # exercises both caveat-producing paths that used to name the intermediate database
    # verbatim: an energy-content conversion (Peat, already in the fixture zip) and an
    # ore-composite decomposition (injected the same way the sibling matched-source test
    # does, and the way other coverage tests add a flow the fixture zip doesn't carry).
    # The energy-content wording is assembled only in entry_for (the matched/nomenclature
    # sources' own output), never in a coverage row -- Peat is included here anyway so a
    # regression that ever changed that would be caught by the blob-wide assertion below.
    cf = CF + [
        cf_row("peat", "peat", RES_GROUND, method="ef-3.1:resource-use-fossils", value=1.0),
        cf_row("zinc", "zinc", RES_GROUND, value=1.0),
    ]
    vocab = VOCAB + [vocab_row("peat", "Peat"), vocab_row("zinc", "Zinc")]
    root = _stage(tmp_path, cf=cf, vocab=vocab)
    source = _source(root)
    records = source.parse(
        source.fetch(RunContext(cache_dir=tmp_path / "cache", output_dir=tmp_path / "out"))
    )
    inputs = records[0]["inputs"]
    ore_flow = BafuFlow(
        "Zinc, Zn 0.63%, Au 9.7E-4%, Ag 9.7E-4%, Cu 0.38%, Pb 0.014%, in ore",
        "resources",
        "in ground",
        "kg",
    )
    augmented = replace(inputs, bafu=BafuFlowIndex.from_flows(list(inputs.bafu) + [ore_flow]))
    rows = source.transform([{"inputs": augmented}])

    # sanity: the ore-composite match actually landed in this row's caveats
    zinc = next(r for r in rows if r["source"]["name"].startswith("Zinc,"))
    assert zinc["status"] == "mapped" and any("ore composite" in c for c in zinc["caveats"])
    peat = next(r for r in rows if r["source"]["name"] == "Peat")
    assert peat["status"] == "mapped" and peat["bridge"] == 7

    blob = json.dumps(rows).lower()
    assert "ecoinvent" not in blob and "biosphere3" not in blob
    for row in rows:
        detail = row.get("detail", "")
        caveats = " ".join(row.get("caveats", []))
        assert "ecoinvent" not in detail.lower() and "biosphere3" not in detail.lower()
        assert "ecoinvent" not in caveats.lower() and "biosphere3" not in caveats.lower()


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


def test_bridge_8_row_reports_tier_placement_and_characterised_false(tmp_path):
    # "Mine gas" carries no CF-table factor at all but a synonym that exactly names the
    # otherwise-unmapped mine off-gas BAFU flow, placed via the reso-grou bw-context
    # crosswalk (same fixture as the sibling matched-source test).
    vocab = VOCAB + [
        vocab_row(
            "mine-gas-unchar",
            "Mine gas",
            alt=["Gas, mine, off-gas, process, coal mining/m3"],
            bw="reso-grou",
        )
    ]
    root = _stage(tmp_path, vocab=vocab)
    rows = _run(_source(root), tmp_path)
    mine_gas = next(r for r in rows if r["source"]["code"] == MINE_GAS)
    assert mine_gas["status"] == "mapped"
    assert mine_gas["bridge"] == 8
    assert mine_gas["characterised"] is False
    assert mine_gas["tier"] == "synonym"
    assert mine_gas["placement"] == "exact"
    assert "reason" not in mine_gas and "detail" not in mine_gas


def test_a_flow_mapped_in_pass_1_never_also_appears_as_bridge_8(tmp_path):
    # LAND resolves via rank 7 (land-use class tier) in the plain fixture; it must never
    # also be reconsidered by bridge 8's second pass.
    rows = _run(_source(_stage(tmp_path)), tmp_path)
    land = next(r for r in rows if r["source"]["code"] == LAND)
    assert land["bridge"] == 7
    assert "characterised" not in land


def test_unmapped_rows_still_come_from_pass_2_when_nothing_resolves_there_either(tmp_path):
    # GAS has no bw-context candidate at all in the default fixture: pass 2 finds
    # nothing either, and the row is reported unmapped exactly as before.
    root = _stage(tmp_path)
    rows = _run(_source(root), tmp_path)
    gas = next(r for r in rows if r["source"]["code"] == GAS)
    assert gas["status"] == "unmapped" and gas["reason"] == "no_ef_flow"
    assert "bridge" not in gas


def test_bridge_8_location_and_caveats_are_reported_when_present(tmp_path):
    # Same "Water, KR" / "Water, Europe" region-strip scenario as the rank-7 test above,
    # but onto an uncharacterised "Water" vocab row (no CF row at all): bridge 8's
    # location/caveats reporting must work exactly the same way rank 7's does.
    vocab = VOCAB + [vocab_row("water-em-unchar", "Water", cas="7732-18-5", bw="envi-wate-unkn")]
    root = _stage(tmp_path, vocab=vocab)
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
    assert kr["bridge"] == 8 and kr["characterised"] is False
    assert kr["location"] == "KR" and "caveats" not in kr

    europe = by_name["Water, Europe"]
    assert europe["bridge"] == 8 and "location" not in europe
    assert europe["caveats"] == [
        "regional aggregate Europe in the source name; EF applies the global default factor"
    ]


def test_characterised_match_in_pass_2_raises_runtime_error(tmp_path, monkeypatch):
    # Pass 2 must never resolve onto a characterised target -- that would mean the
    # inclusive index changed a characterised rank-7 outcome. Force it via a fake
    # ``outcome_for`` monkeypatched onto the coverage module's own name (the module
    # ``BafuEfMatchedSource.outcomes`` calls its *own* module-level ``outcome_for``
    # unaffected, so pass 1 -- which uses that name -- still runs for real; only the
    # coverage module's pass-2 call sees the fake), returning a match onto "co2-fos"
    # (characterised in the default fixture CF/VOCAB) for GAS, which pass 1 leaves
    # Unmatched in the plain fixture.
    root = _stage(tmp_path)
    source = _source(root)

    def fake_outcome_for(flow, cas, pipeline, index):
        if flow.code == GAS:
            return Match(
                code="co2-fos",
                tier="name",
                placement="exact",
                location=None,
                candidates=1,
                caveats=(),
            )
        return outcome_for(flow, cas, pipeline, index)

    monkeypatch.setattr(coverage_mod, "outcome_for", fake_outcome_for)
    with pytest.raises(RuntimeError, match="characterised match reached bridge 8"):
        _run(source, tmp_path)


def test_energy_carrier_resource_name_is_reported_context_unresolved_not_bridge_8(tmp_path):
    # decision 2026-09-13: an energy-carrier-shaped resource name the bw-context
    # crosswalk can never place correctly is withheld outright, not just uncertain --
    # the coverage sidecar must report it unmapped (context_unresolved), never bridge 8.
    vocab = VOCAB + [
        vocab_row("energy-geo-unchar", "Energy, geothermal, converted", bw="reso-grou")
    ]
    root = _stage(tmp_path, vocab=vocab)
    source = _source(root)
    records = source.parse(
        source.fetch(RunContext(cache_dir=tmp_path / "cache", output_dir=tmp_path / "out"))
    )
    inputs = records[0]["inputs"]
    flow = BafuFlow("Energy, geothermal, converted", "resources", "land", "MJ")
    augmented = replace(inputs, bafu=BafuFlowIndex.from_flows(list(inputs.bafu) + [flow]))
    rows = source.transform([{"inputs": augmented}])
    (row,) = [r for r in rows if r["source"]["name"] == "Energy, geothermal, converted"]
    assert row["status"] == "unmapped"
    assert row["reason"] == "context_unresolved"
    assert "bridge" not in row
