import json
from dataclasses import replace
from pathlib import Path

import jsonschema
import pytest
from sentier_importers.core.context import RunContext
from sentier_importers.core.pipeline import _assemble
from sentier_importers.core.source import SourceConfig
from sentier_importers.core.types import RawData
from sentier_importers.matching.ef_index import EfFlow, EfFlowIndex
from sentier_importers.matching.matchers import load_aliases
from sentier_importers.matching.pipeline import Match, Unmatched, default_pipeline
from sentier_importers.sources.bafu.ecospold import flow_id
from sentier_importers.sources.bafu.mappings_biosphere_coverage import BafuEfCoverageSource
from sentier_importers.sources.bafu.mappings_biosphere_matched import (
    BafuEfMatchedSource,
    _decide,
    _name_only_match,
    _sits_on_own_unspecified_leaf,
    substance_cas,
)
from sentier_importers.sources.eaternity.bridge import BafuFlow, BafuFlowIndex

from tests.matching.ef_fixtures import (
    AIR_RURAL,
    AIR_UNSPEC,
    LAND_OCC,
    RES_GROUND,
    RES_WATER,
    WATER_FRESH,
    cf_row,
    vocab_row,
    write_ef_inputs,
)
from tests.sources.bafu_fixture import fixture_zip

_SCHEMA = Path(__file__).parent / "fixtures" / "randonneur-package.schema.json"
# fixture zip biosphere flows: Carbon dioxide, fossil       | emissions to air | unspecified | kg
#                                                                              (CAS 124-38-9)
#                              Water, river                 | resources        | in water    | m3
#                              Radon-222                    | emissions to air | low. pop.   | Bq
#                                                                              (CAS 14859-67-7)
#                              Gas, natural/m3               | resources        | in ground   | m3
#                              Peat                          | resources        | in ground   | kg
#                              Gas, mine, off-gas, process,   resources          in ground     Nm3
#                                coal mining/m3
#                              Occupation, industrial area   | resources        | unspecified | m2a
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
    cf_row("industrial-area", "industrial area", LAND_OCC, method="ef-3.1:land-use", value=1.0),
]
VOCAB = [
    vocab_row("co2-fos", "Carbon dioxide (fossil)", cas="124-38-9"),
    vocab_row("river", "River water", cas="7732-18-5"),
    vocab_row("rn222", "Radon-222", cas="14859-67-7"),
    vocab_row("industrial-area", "Industrial Area"),
]
CO2 = flow_id("Carbon dioxide, fossil", "emissions to air", "unspecified", "kg")
WATER = flow_id("Water, river", "resources", "in water", "m3")
RADON = flow_id("Radon-222", "emissions to air", "low. pop.", "Bq")
GAS = flow_id("Gas, natural/m3", "resources", "in ground", "m3")
PEAT = flow_id("Peat", "resources", "in ground", "kg")
MINE_GAS = flow_id("Gas, mine, off-gas, process, coal mining/m3", "resources", "in ground", "Nm3")
LAND = flow_id("Occupation, industrial area", "resources", "unspecified", "m2a")
URANIUM = flow_id("Uranium", "resources", "land", "kg")
#: _decide never touches its ``index`` argument for a pure Unmatched-in/Unmatched-out
#: call (the freshwater check and _refine_unmatched are both index-free); an empty
#: index documents that rather than passing None.
_EMPTY_INDEX = EfFlowIndex.from_tables([], [])


def _payload(codes):
    return {
        "name": "x",
        "version": "0",
        "replace": [{"source": {"code": c}, "target": {"code": "t"}} for c in codes],
    }


def _stage(tmp_path, curated=(), inferred=(), cf=CF, vocab=VOCAB):
    (tmp_path / "bafu.zip").write_bytes(fixture_zip().content)
    write_ef_inputs(tmp_path, cf, vocab)
    (tmp_path / "curated.json").write_text(json.dumps(_payload(curated)))
    (tmp_path / "inferred.json").write_text(json.dumps(_payload(inferred)))
    return tmp_path


def _config(
    root,
    name="bafu-ef-biosphere-matched",
    module="sentier_importers.sources.bafu.mappings_biosphere_matched",
    verb="replace",
    emit="biosphere-3-matched",
):
    return SourceConfig(
        name=name,
        module=module,
        target="sentier_mappings",
        category="bafu-2026-v1__ef-3.1",
        fetch_url=f"file://{root}/bafu.zip",
        fetch_format="zip",
        output_format="json",
        emit_filename=emit,
        package_name="bafu-2026-v1__ef-3.1-biosphere-matched",
        package_version="0.1.0",
        package_verb=verb,
        inputs={
            "curated": f"file://{root}/curated.json",
            "inferred": f"file://{root}/inferred.json",
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
    assert set(by_code) == {CO2, WATER, RADON, LAND}
    co2 = by_code[CO2]
    assert co2["source"] == {
        "name": "Carbon dioxide, fossil",
        "code": CO2,
        "unit": "kg",
        "context": ["emissions to air", "unspecified"],
    }
    # round 6, decision 2026-09-14 (third cut): the matching key is the CF table's
    # own JRC spelling, but it agrees with the vocab pref_label here except for
    # case, so _pick_jrc_name prefers the vocab casing; entry_for's emitted name
    # (EfFlow.label) is unaffected either way -- unchanged from before this round.
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


def test_land_use_flow_emits_with_m2_star_a_unit_and_a_placement_comment(tmp_path):
    rows = _run(BafuEfMatchedSource(_config(_stage(tmp_path))), tmp_path)
    land = next(r for r in rows if r["source"]["code"] == LAND)
    assert land["source"] == {
        "name": "Occupation, industrial area",
        "code": LAND,
        "unit": "m2a",
        "context": ["resources", "unspecified"],
    }
    assert land["target"]["code"] == "industrial-area"
    assert land["target"]["unit"] == "m2*a"
    assert "conversion_factor" not in land
    assert "placed on EF land use" in land["comment"]


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
        r["inputs"].pipeline.match(
            BafuFlow("Water", "emissions to water", "unspecified", "kg"), None
        ),
        r["inputs"].index,
    )
    assert from_kg["target"]["unit"] == "cubic meter" and from_kg["conversion_factor"] == 0.001


def test_flows_already_in_curated_or_inferred_are_skipped(tmp_path):
    rows = _run(
        BafuEfMatchedSource(_config(_stage(tmp_path, curated=[CO2], inferred=[RADON]))), tmp_path
    )
    assert {r["source"]["code"] for r in rows} == {WATER, LAND}


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
    entry = source.entry_for(flow, r["inputs"].pipeline.match(flow, None), r["inputs"].index)
    assert entry["target"]["location"] == "KR" and "conversion_factor" not in entry
    flow = BafuFlow("Water, Europe", "emissions to water", "unspecified", "m3")
    entry = source.entry_for(flow, r["inputs"].pipeline.match(flow, None), r["inputs"].index)
    assert "location" not in entry["target"]
    assert (
        entry["comment"]
        == "regional aggregate Europe in the source name; EF applies the global default factor"
    )


class _ForcedInclusiveMatchedSource(BafuEfMatchedSource):
    """Test-only subclass forcing ``include_uncharacterised=True`` on the default,
    characterised-only matched source -- pins that ``transform`` never emits a match
    onto an uncharacterised target even if the flag were ever flipped by accident.
    """

    include_uncharacterised = True


def test_matched_source_never_emits_an_uncharacterised_target_even_if_forced(tmp_path):
    # "Mine gas" carries no CF-table factor at all but a synonym that exactly names the
    # otherwise-unmapped mine off-gas BAFU flow, placed via the reso-grou bw-context
    # crosswalk. With include_uncharacterised forced True, the pipeline itself would
    # happily match onto it -- transform()'s own characterised guard must still
    # withhold it (belt-and-braces alongside include_uncharacterised's normal default).
    vocab = VOCAB + [
        vocab_row(
            "mine-gas-unchar",
            "Mine gas",
            alt=["Gas, mine, off-gas, process, coal mining/m3"],
            bw="reso-grou",
        )
    ]
    root = _stage(tmp_path, vocab=vocab)
    source = _ForcedInclusiveMatchedSource(_config(root))
    records = source.parse(
        source.fetch(RunContext(cache_dir=tmp_path / "cache", output_dir=tmp_path / "out"))
    )
    outcomes = dict(source.outcomes(records))
    flow = next(f for f in outcomes if f.code == MINE_GAS)
    outcome = outcomes[flow]
    # sanity: the inclusive index really does resolve a match onto the uncharacterised target
    assert isinstance(outcome, Match) and outcome.code == "mine-gas-unchar"
    assert not records[0]["inputs"].index.get(outcome.code).characterised
    rows = source.transform(records)
    assert MINE_GAS not in {r["source"]["code"] for r in rows}


def test_natural_gas_volume_converts_via_energy_content_onto_the_fossil_resource_flow(tmp_path):
    # EF characterises fossil resources in megajoule (resource-use-fossils); a BAFU
    # m3-denominated natural-gas flow has a fixed net calorific value (35.98 MJ/m3,
    # round 7, decision 2026-09-14), so it converts rather than being withheld.
    cf = CF + [
        cf_row(
            "gas-mj", "natural gas", RES_GROUND, method="ef-3.1:resource-use-fossils", value=1.0
        )
    ]
    vocab = VOCAB + [vocab_row("gas-mj", "Natural gas", alt=["Gas, natural/m3"])]
    root = _stage(tmp_path, cf=cf, vocab=vocab)
    source = BafuEfMatchedSource(_config(root))
    records = source.parse(
        source.fetch(RunContext(cache_dir=tmp_path / "cache", output_dir=tmp_path / "out"))
    )
    outcomes = dict(source.outcomes(records))
    flow = next(f for f in outcomes if f.code == GAS)
    outcome = outcomes[flow]
    assert isinstance(outcome, Match)
    entry = source.entry_for(flow, outcome, records[0]["inputs"].index)
    assert entry["conversion_factor"] == 35.98
    assert "35.98 MJ/m3" in entry["comment"]
    rows = source.transform(records)
    assert GAS in {r["source"]["code"] for r in rows}


def test_gas_mine_off_gas_volume_converts_via_the_approximated_energy_content(
    tmp_path,
):
    # decision (g), 2026-09-13: "Gas, mine, off-gas, process, coal mining/m3" (unit
    # Nm3) candidates onto "Natural gas" (as the real BAFU inventory's shared CAS
    # 8006-14-2 also does); the energy-content table now carries a dedicated key for
    # this exact (name, unit) pair (35.98 MJ/Nm3, the natural-gas value, round 7,
    # decision 2026-09-14), so it converts instead of being withheld, and the caveat
    # discloses the approximation.
    cf = CF + [
        cf_row(
            "gas-mj", "natural gas", RES_GROUND, method="ef-3.1:resource-use-fossils", value=1.0
        )
    ]
    vocab = VOCAB + [
        vocab_row(
            "gas-mj",
            "Natural gas",
            alt=["Gas, natural/m3", "Gas, mine, off-gas, process, coal mining/m3"],
        )
    ]
    root = _stage(tmp_path, cf=cf, vocab=vocab)
    source = BafuEfMatchedSource(_config(root))
    records = source.parse(
        source.fetch(RunContext(cache_dir=tmp_path / "cache", output_dir=tmp_path / "out"))
    )
    outcomes = dict(source.outcomes(records))
    flow = next(f for f in outcomes if f.code == MINE_GAS)
    outcome = outcomes[flow]
    assert isinstance(outcome, Match)
    entry = source.entry_for(flow, outcome, records[0]["inputs"].index)
    assert entry["conversion_factor"] == 35.98
    assert "35.98 MJ/Nm3" in entry["comment"]
    assert (
        "coal-mine off-gas approximated as natural gas (decision 2026-09-13)" in entry["comment"]
    )
    rows = source.transform(records)
    assert MINE_GAS in {r["source"]["code"] for r in rows}


def test_uncharacterised_ef_flow_is_never_used_by_the_default_matched_source(tmp_path):
    # Guard against phase 2 task 3's ``include_uncharacterised`` flag ever flipping to
    # True by accident on this source (task 4 makes it a class attribute this source can
    # opt into deliberately -- this test pins today's default, False). Stage an EF vocab
    # row with no CF-table factor at all ("Mine gas") but a synonym that exactly names
    # the otherwise-unmapped "Gas, mine, off-gas, process, coal mining/m3" BAFU flow, and
    # a bw-context crosswalk (reso-grou) that places it in the same bucket ("resource")
    # BAFU's flow is in. If the source's index ever included uncharacterised flows, this
    # synonym would resolve a real match; with the flag at its default it must not.
    vocab = VOCAB + [
        vocab_row(
            "mine-gas-unchar",
            "Mine gas",
            alt=["Gas, mine, off-gas, process, coal mining/m3"],
            bw="reso-grou",
        )
    ]
    root = _stage(tmp_path, vocab=vocab)
    source = BafuEfMatchedSource(_config(root))
    records = source.parse(
        source.fetch(RunContext(cache_dir=tmp_path / "cache", output_dir=tmp_path / "out"))
    )
    outcomes = dict(source.outcomes(records))
    flow = next(f for f in outcomes if f.code == MINE_GAS)
    outcome = outcomes[flow]
    assert isinstance(outcome, Unmatched) and outcome.reason == "no_ef_flow"
    rows = source.transform(records)
    assert MINE_GAS not in {r["source"]["code"] for r in rows}

    # the coverage sidecar's own second pass runs over the inclusive index, so it
    # correctly reports this exact scenario as biosphere-4-nomenclature (mapped,
    # uncharacterised), never as biosphere-3-matched -- the matched source above never
    # emits it.
    coverage = BafuEfCoverageSource(
        _config(
            root,
            name="bafu-ef-biosphere-coverage",
            module="sentier_importers.sources.bafu.mappings_biosphere_coverage",
            verb="coverage",
            emit="coverage",
        )
    )
    coverage_rows = _run(coverage, tmp_path)
    mine_gas_row = next(r for r in coverage_rows if r["source"]["code"] == MINE_GAS)
    assert mine_gas_row["status"] == "mapped"
    assert mine_gas_row["package"] == "biosphere-4-nomenclature"
    assert mine_gas_row["characterised"] is False


def test_peat_energy_content_converts_onto_the_fossil_resource_flow(tmp_path):
    # same decision, the other cross-dimension direction: a BAFU kg-denominated peat
    # flow has a fixed ecoinvent v2 net calorific value (9.9 MJ/kg), so it converts
    # onto EF's megajoule reference unit instead of being withheld.
    cf = CF + [cf_row("peat", "peat", RES_GROUND, method="ef-3.1:resource-use-fossils", value=1.0)]
    vocab = VOCAB + [vocab_row("peat", "Peat")]
    root = _stage(tmp_path, cf=cf, vocab=vocab)
    source = BafuEfMatchedSource(_config(root))
    records = source.parse(
        source.fetch(RunContext(cache_dir=tmp_path / "cache", output_dir=tmp_path / "out"))
    )
    outcomes = dict(source.outcomes(records))
    flow = next(f for f in outcomes if f.code == PEAT)
    outcome = outcomes[flow]
    assert isinstance(outcome, Match)
    entry = source.entry_for(flow, outcome, records[0]["inputs"].index)
    assert entry["conversion_factor"] == 9.9
    assert "9.9 MJ/kg" in entry["comment"]
    rows = source.transform(records)
    assert PEAT in {r["source"]["code"] for r in rows}


def test_uranium_resource_branch_fallback_converts_via_energy_content(tmp_path):
    # decision (b): "Uranium" is filed by BAFU under "land" (no extraction-medium
    # information); with exactly one EF leaf reachable by plain name, it resolves
    # through the resource-branch fallback, and Task 1's energy-content factor
    # (560000 MJ/kg) still applies onto EF's megajoule reference unit.
    cf = CF + [
        cf_row("uranium", "uranium", RES_GROUND, method="ef-3.1:resource-use-fossils", value=1.0)
    ]
    vocab = VOCAB + [vocab_row("uranium", "Uranium")]
    root = _stage(tmp_path, cf=cf, vocab=vocab)
    source = BafuEfMatchedSource(_config(root))
    records = source.parse(
        source.fetch(RunContext(cache_dir=tmp_path / "cache", output_dir=tmp_path / "out"))
    )
    outcomes = dict(source.outcomes(records))
    flow = next(f for f in outcomes if f.code == URANIUM)
    outcome = outcomes[flow]
    assert isinstance(outcome, Match) and outcome.placement == "resource_branch_fallback"
    entry = source.entry_for(flow, outcome, records[0]["inputs"].index)
    assert entry["target"]["unit"] == "megajoule"
    assert entry["conversion_factor"] == 560_000
    assert "BAFU files this resource under land" in entry["comment"]
    assert "energy content 560000 MJ/kg" in entry["comment"]
    rows = source.transform(records)
    assert URANIUM in {r["source"]["code"] for r in rows}


def test_resource_correction_flow_carries_its_own_caveat_onto_a_characterised_target(
    tmp_path,
):
    # a "..., resource correction" flow is a correction entry against a substance's own
    # extraction, not a distinct resource -- entry_for appends a caveat saying so,
    # regardless of which tier/placement actually resolved the match.
    cf = CF + [cf_row("iron", "iron", RES_GROUND, value=1.0)]
    vocab = VOCAB + [vocab_row("iron", "Iron")]
    root = _stage(tmp_path, cf=cf, vocab=vocab)
    source = BafuEfMatchedSource(_config(root))
    (r,) = source.parse(
        source.fetch(RunContext(cache_dir=tmp_path / "cache", output_dir=tmp_path / "out"))
    )
    flow = BafuFlow("Iron, resource correction", "resources", "in ground", "kg")
    match = Match(
        code="iron", tier="name", placement="exact", location=None, candidates=1, caveats=()
    )
    entry = source.entry_for(flow, match, r["inputs"].index)
    assert (
        "source is a resource-correction flow, mapped to the extraction of the same "
        "element" in entry["comment"]
    )


def test_coal_hard_alias_converts_via_energy_content(tmp_path):
    # "Coal, hard" -> "Hard Coal" via the shipped alias; energy content 17.73 MJ/kg
    # (round 7, decision 2026-09-14).
    cf = CF + [
        cf_row("coal", "hard coal", RES_GROUND, method="ef-3.1:resource-use-fossils", value=1.0)
    ]
    vocab = VOCAB + [vocab_row("coal", "Hard Coal")]
    root = _stage(tmp_path, cf=cf, vocab=vocab)
    source = BafuEfMatchedSource(_config(root))
    (r,) = source.parse(
        source.fetch(RunContext(cache_dir=tmp_path / "cache", output_dir=tmp_path / "out"))
    )
    flow = BafuFlow("Coal, hard", "resources", "in ground", "kg")
    match = r["inputs"].pipeline.match(flow, None)
    assert isinstance(match, Match) and match.tier == "alias"
    entry = source.entry_for(flow, match, r["inputs"].index)
    assert entry["conversion_factor"] == 17.73
    assert "17.73 MJ/kg" in entry["comment"]


def test_uranium_mass_onto_an_ionising_radiation_flow_is_withheld_as_unit_mismatch(tmp_path):
    # EF characterises ionising radiation in kBq; a BAFU kg-denominated Uranium flow
    # has no fixed mass -> activity conversion, even though the pipeline itself matches.
    cf = CF + [
        cf_row(
            "u238",
            "uranium-238",
            AIR_UNSPEC,
            method="ef-3.1:ionising-radiation-human-health",
            value=1.0,
        )
    ]
    vocab = VOCAB + [vocab_row("u238", "Uranium-238", alt=["Uranium"])]
    root = _stage(tmp_path, cf=cf, vocab=vocab)
    source = BafuEfMatchedSource(_config(root))
    (r,) = source.parse(
        source.fetch(RunContext(cache_dir=tmp_path / "cache", output_dir=tmp_path / "out"))
    )
    flow = BafuFlow("Uranium", "emissions to air", "unspecified", "kg")
    match = r["inputs"].pipeline.match(flow, None)
    assert isinstance(match, Match)  # sanity: the pipeline itself matches by synonym
    outcome = _decide(flow, match, r["inputs"].index)
    assert isinstance(outcome, Unmatched) and outcome.reason == "unit_mismatch"
    assert "kg" in outcome.detail and "kBq" in outcome.detail


def test_ocean_discharge_does_not_take_the_water_use_unspecified_fallback(tmp_path):
    cf = CF + [
        cf_row(
            "water-unspec",
            "water",
            "Emissions / Emissions to water / Emissions to water, unspecified",
            method="ef-3.1:water-use",
            value=-10.0,
        )
    ]
    vocab = VOCAB + [vocab_row("water-unspec", "Water", cas="7732-18-5")]
    root = _stage(tmp_path, cf=cf, vocab=vocab)
    source = BafuEfMatchedSource(_config(root))
    (r,) = source.parse(
        source.fetch(RunContext(cache_dir=tmp_path / "cache", output_dir=tmp_path / "out"))
    )
    flow = BafuFlow("Water", "emissions to water", "ocean", "kg")
    match = r["inputs"].pipeline.match(flow, None)
    # sanity: the pipeline itself would take the unspecified fallback onto "Water"
    assert isinstance(match, Match)
    outcome = _decide(flow, match, r["inputs"].index)
    assert outcome == Unmatched("no_ef_flow", "EF water use has no sea-water discharge flow")


def test_outcomes_returns_one_tuple_per_non_excluded_fixture_flow(tmp_path):
    root = _stage(tmp_path, curated=[CO2])
    source = BafuEfMatchedSource(_config(root))
    records = source.parse(
        source.fetch(RunContext(cache_dir=tmp_path / "cache", output_dir=tmp_path / "out"))
    )
    outcomes = source.outcomes(records)
    assert {flow.code for flow, _ in outcomes} == {
        WATER,
        RADON,
        GAS,
        PEAT,
        MINE_GAS,
        LAND,
        URANIUM,
    }
    assert all(isinstance(o, (Match, Unmatched)) for _, o in outcomes)
    # Gas, Peat, the mine off-gas and Uranium have no matching EF flow in the default
    # fixture CF/VOCAB at all
    by_code = {flow.code: outcome for flow, outcome in outcomes}
    assert by_code[GAS].reason == "no_ef_flow"
    assert by_code[PEAT].reason == "no_ef_flow"
    assert by_code[MINE_GAS].reason == "no_ef_flow"
    assert by_code[URANIUM].reason == "no_ef_flow"
    assert isinstance(by_code[LAND], Match) and by_code[LAND].tier == "landuse"


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
                    "name": "Methanol",
                    "category": "emissions to soil",
                    "subcategory": "industrial",
                    "unit": "kg",
                    "cas": "67-56-1",  # same substance, no leading zeros: must agree
                    "group_code": 4,
                },
                {
                    "name": "Methanol",
                    "category": "emissions to soil",
                    "subcategory": "agricultural",
                    "unit": "kg",
                    "cas": "",  # blank: ignored, not a third distinct value
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
    assert cas == {"Methanol": "67-56-1"}
    assert conflicts == {"Mixed": ("1-1-1", "2-2-2")}


def test_decide_refines_unmatched_outcomes():
    none = Unmatched("no_ef_flow", "x")
    assert (
        _decide(
            BafuFlow("Arsenic, ion", "emissions to water", "river", "kg"), none, _EMPTY_INDEX
        ).reason
        == "speciation"
    )
    assert (
        _decide(
            BafuFlow("Copper ion", "emissions to water", "river", "kg"), none, _EMPTY_INDEX
        ).reason
        == "speciation"
    )
    assert (
        _decide(
            BafuFlow("Calcium II", "emissions to water", "river", "kg"), none, _EMPTY_INDEX
        ).reason
        == "speciation"
    )
    # decision (e), 2026-09-13 removes the ``qualifier_missing`` refinement entirely:
    # ``matching.matchers.CarbonOxideMatcher`` now resolves a bare carbon oxide before
    # the pipeline could ever return a plain "no_ef_flow" for one (see
    # mappings_biosphere_matched.py's ``_refine_unmatched`` docstring), so a synthetic
    # ``no_ef_flow`` input for one of these names -- as this test constructs directly,
    # bypassing the pipeline -- is simply left alone.
    assert (
        _decide(
            BafuFlow("Carbon dioxide", "emissions to air", "unspecified", "kg"), none, _EMPTY_INDEX
        ).reason
        == "no_ef_flow"
    )
    assert (
        _decide(
            BafuFlow("Carbon monoxide", "emissions to air", "high. pop.", "kg"), none, _EMPTY_INDEX
        ).reason
        == "no_ef_flow"
    )
    assert (
        _decide(
            BafuFlow("Carbon dioxide", "emissions to water", "river", "kg"), none, _EMPTY_INDEX
        ).reason
        == "no_ef_flow"
    )
    other = Unmatched("sub_compartment_absent", "y")
    assert (
        _decide(BafuFlow("Arsenic, ion", "emissions to water", "river", "kg"), other, _EMPTY_INDEX)
        is other
    )


def test_decide_reports_non_freshwater_for_both_match_and_unmatched_inputs(tmp_path):
    # Decision 5 fires for an Unmatched outcome regardless of its original reason...
    salt = _decide(
        BafuFlow("Water, salt, ocean", "resources", "in water", "m3"),
        Unmatched("no_ef_flow", "x"),
        _EMPTY_INDEX,
    )
    assert salt == Unmatched(
        "non_freshwater", "EF water use characterises freshwater deprivation only"
    )
    # ...and it must override a real Match too: build a tiny index + pipeline where
    # "Water, salt, ocean" resolves (via a curated alias) to a real water-use flow.
    cf, vocab = write_ef_inputs(
        tmp_path,
        [cf_row("sea", "water", RES_WATER, method="ef-3.1:water-use", value=1.0)],
        [vocab_row("sea", "Water", cas="7732-18-5")],
    )
    index = EfFlowIndex.from_files(cf, vocab)
    pipeline = default_pipeline(index, {"water, salt, ocean": "Water"})
    flow = BafuFlow("Water, salt, ocean", "resources", "in water", "m3")
    match = pipeline.match(flow, None)
    assert isinstance(match, Match)  # sanity: the pipeline itself would happily match
    outcome = _decide(flow, match, index)
    assert outcome == Unmatched(
        "non_freshwater", "EF water use characterises freshwater deprivation only"
    )


def test_water_fossil_no_longer_non_freshwater_resolves_via_alias_instead(tmp_path):
    # Decision 2026-09-13 withdraws "Water, fossil" from _NON_FRESHWATER: it is taken
    # as groundwater and resolved through the shipped "water, fossil" alias onto
    # "Ground Water" instead, carrying that decision as a caveat.
    cf, vocab = write_ef_inputs(
        tmp_path,
        [cf_row("gw", "ground water", RES_WATER, method="ef-3.1:water-use", value=1.0)],
        [vocab_row("gw", "Ground Water", cas="7732-18-5")],
    )
    index = EfFlowIndex.from_files(cf, vocab)
    pipeline = default_pipeline(index, load_aliases())
    flow = BafuFlow("Water, fossil", "resources", "in water", "m3")
    match = pipeline.match(flow, None)
    assert isinstance(match, Match) and match.tier == "alias"  # sanity: the alias fires
    outcome = _decide(flow, match, index)
    assert isinstance(outcome, Match) and outcome.code == "gw"
    assert outcome.caveats == (
        "fossil water taken as groundwater (decision 2026-09-13; EF files all water "
        "resources under renewable material resources from water)",
    )


def test_decide_no_longer_withholds_an_ion_shaped_match():
    # decision (d), 2026-09-13 drops the speciation guard entirely on the Match side:
    # even a hand-built Match onto a bare element for an ion-shaped BAFU name (as a
    # pre-decision-(d) CAS match would have been) now passes through _decide
    # untouched -- the withholding logic that used to live here is gone; the real
    # emission path is matching.matchers.IonStripMatcher (see test_matchers.py /
    # test_pipeline.py), not this fallback.
    cf = [cf_row("cu", "copper", AIR_UNSPEC, method="ef-3.1:human-toxicity-cancer", value=1.0)]
    vocab = [vocab_row("cu", "Copper", cas="7440-50-8")]
    index = EfFlowIndex.from_tables(cf, vocab)
    match = Match(
        code="cu", tier="cas", placement="exact", location=None, candidates=1, caveats=()
    )
    outcome = _decide(
        BafuFlow("Copper ion", "emissions to air", "unspecified", "kg"), match, index
    )
    assert outcome is match


def test_name_only_match_recovers_a_factorless_namesake_in_another_bucket():
    # round 5, decision 2026-09-14: "Basalt" is a BAFU resource extraction; EF carries
    # no "Basalt" flow in the resource bucket at all, only a factorless one placed on
    # a soil-emission leaf via the bw-context crosswalk (envi-grou-indu is always
    # uncharacterised -- see bw_context.NEVER_CHARACTERISED_CODES) -- exactly the
    # shape the ordinary bucket-scoped matchers can never reach.
    cf: list = []
    vocab = [vocab_row("basalt-soil", "Basalt", bw="envi-grou-indu")]
    index = EfFlowIndex.from_tables(cf, vocab, include_uncharacterised=True)
    flow = BafuFlow("Basalt", "resources", "in ground", "kg")
    match = _name_only_match(flow, index)
    assert match == Match(
        code="basalt-soil",
        tier="name-only",
        placement="name_only",
        location=None,
        candidates=1,
        caveats=(
            "EF has this name only as emissions to non-agricultural soil; the source "
            "is a resource extraction, so the EF context is omitted (name alignment "
            "only, decision 2026-09-14)",
        ),
    )


def test_name_only_match_prefers_the_namesake_on_its_own_unspecified_leaf():
    # two factorless "Shale" namesakes: one on a specific (non-unspecified) soil leaf,
    # one on the air bucket's own unspecified leaf -- the latter must be chosen,
    # deterministically, over the code-sorted-first tiebreak.
    vocab = [
        vocab_row("shale-soil", "Shale", bw="envi-grou-indu"),
        vocab_row("shale-air", "Shale", bw="envi-air-unkn"),
    ]
    index = EfFlowIndex.from_tables([], vocab, include_uncharacterised=True)
    flow = BafuFlow("Shale", "resources", "in ground", "kg")
    match = _name_only_match(flow, index)
    assert match.code == "shale-air"
    assert match.caveats == (
        "EF has this name only as emissions to air, unspecified, emissions to "
        "non-agricultural soil; the source is a resource extraction, so the EF "
        "context is omitted (name alignment only, decision 2026-09-14)",
    )


def test_name_only_match_refuses_when_any_namesake_is_characterised():
    # "Talc" carries a real factor in the air bucket (a different bucket than the
    # source's own "resource" category, so the ordinary pipeline never finds it
    # either) and a second, factorless "Talc" elsewhere -- ANY characterised
    # namesake vetoes the whole name, protecting BAFU's salts, Talc and Uranium from
    # being aligned next to a live impact number for the same substance name.
    cf = [cf_row("talc-air", "talc", AIR_UNSPEC, method="ef-3.1:human-toxicity-cancer", value=1.0)]
    vocab = [
        vocab_row("talc-air", "Talc"),
        vocab_row("talc-soil", "Talc", bw="envi-grou-indu"),
    ]
    index = EfFlowIndex.from_tables(cf, vocab, include_uncharacterised=True)
    flow = BafuFlow("Talc", "resources", "in ground", "kg")
    assert _name_only_match(flow, index) is None


def test_sits_on_own_unspecified_leaf_is_false_for_a_context_with_no_bucket():
    # defensive branch: a real bw-context-crosswalked EfFlow always maps to a bucket,
    # but _sits_on_own_unspecified_leaf must not assume that -- a flow whose context
    # cannot be mapped to any bucket at all is never "on" any unspecified leaf.
    flow = EfFlow(code="x", name="Ghost", context=("Nonsense",), characterised=False)
    assert flow.bucket is None
    assert _sits_on_own_unspecified_leaf(flow) is False


def test_name_only_match_returns_none_when_ef_has_no_namesake_at_all():
    index = EfFlowIndex.from_tables([], [], include_uncharacterised=True)
    flow = BafuFlow("Gypsum", "resources", "in ground", "kg")
    assert _name_only_match(flow, index) is None


def test_name_only_alignment_never_fires_on_a_characterised_only_index():
    # contrived: an uncharacterised EfFlow that WOULD resolve the name is placed
    # directly into the index's own lookup tables even though the index itself was
    # not built with include_uncharacterised (impossible via the real from_tables()
    # constructor) -- this pins the guard specifically on
    # EfFlowIndex.includes_uncharacterised, the same flag the matched package's own
    # (real, from_tables()-built) index always carries False, rather than on "no
    # uncharacterised flow happens to be present".
    namesake = EfFlow(
        code="widget-soil",
        name="Widget",
        context=("Emissions", "Emissions to soil", "Emissions to non-agricultural soil"),
        characterised=False,
    )
    index = EfFlowIndex([namesake], {}, includes_uncharacterised=False)
    flow = BafuFlow("Widget", "emissions to air", "unspecified", "kg")
    outcome = _decide(flow, Unmatched("no_ef_flow", "x"), index)
    assert outcome == Unmatched("no_ef_flow", "x")


def test_decide_applies_name_only_alignment_ahead_of_speciation_refinement():
    # round 5 runs before _refine_unmatched: a name-only alignment, when one applies,
    # is more informative than narrowing "no_ef_flow" into "speciation" -- though no
    # real BAFU name in the round-5 recovery list is ion-shaped, this pins the order
    # directly so a future _decide reordering cannot silently swap the two.
    vocab = [vocab_row("arsenic-ion-air", "Arsenic, ion", bw="envi-air-unkn")]
    index = EfFlowIndex.from_tables([], vocab, include_uncharacterised=True)
    flow = BafuFlow("Arsenic, ion", "emissions to water", "river", "kg")
    outcome = _decide(flow, Unmatched("no_ef_flow", "x"), index)
    assert isinstance(outcome, Match) and outcome.code == "arsenic-ion-air"
    assert outcome.placement == "name_only"


def test_decide_withholds_a_wood_flow_reported_by_volume_as_unit_mismatch():
    # round 5, decision 2026-09-14: "Wood, standing" resolves through a curated alias
    # onto EF's uncharacterised "Wood" (mirroring the real aliases.yaml wood entries)
    # -- a real pipeline Match, not a name-only one. EF counts Wood by mass; a
    # cubic-meter source has no density conversion anywhere in this codebase (unlike
    # water), so it must be withheld rather than guessed.
    vocab = [vocab_row("wood-unchar", "Wood", bw="reso-biot")]
    index = EfFlowIndex.from_tables([], vocab, include_uncharacterised=True)
    pipeline = default_pipeline(index, {"wood, standing": "Wood"})
    flow = BafuFlow("Wood, standing", "resources", "biotic", "m3")
    match = pipeline.match(flow, None)
    assert isinstance(match, Match)  # sanity: the alias resolves a real Match
    outcome = _decide(flow, match, index)
    assert outcome == Unmatched(
        "unit_mismatch",
        "EF Wood is counted by mass; no density convention converts m3 (decision 2026-09-14)",
    )


def test_decide_allows_a_wood_flow_reported_by_mass():
    # the other half of the same alias: a kilogram source agrees with EF's own
    # dimension for Wood, so it passes through untouched.
    vocab = [vocab_row("wood-unchar", "Wood", bw="reso-biot")]
    index = EfFlowIndex.from_tables([], vocab, include_uncharacterised=True)
    pipeline = default_pipeline(index, {"wood, standing": "Wood"})
    flow = BafuFlow("Wood, standing", "resources", "biotic", "kg")
    match = pipeline.match(flow, None)
    outcome = _decide(flow, match, index)
    assert isinstance(outcome, Match) and outcome.code == "wood-unchar"


def test_chromium_vi_pipeline_match_lands_on_the_species_specific_target():
    # pipeline-level (not a _decide round-trip -- _decide is a no-op for any Match now
    # that decision (d) dropped the speciation guard, see
    # test_decide_no_longer_withholds_an_ion_shaped_match above; the real-data,
    # CAS-only shape of this scenario is covered directly in test_matchers.py):
    # "Chromium VI" lands on the flow whose EF target name itself carries the
    # oxidation state, the right species, not a collapse.
    #
    # round 6, decision 2026-09-14: the target's name is now the CF table's own JRC
    # spelling ("chromium vi"), which equals the BAFU name outright, so this now
    # matches at the name tier -- it no longer needs "Chromium VI" as an alt_label
    # synonym of the (now superseded) vocab pref_label "Chromium(6+)" to be found.
    cf = [
        cf_row("cr6", "chromium vi", AIR_UNSPEC, method="ef-3.1:human-toxicity-cancer", value=1.0)
    ]
    vocab = [vocab_row("cr6", "Chromium(6+)", alt=["Chromium VI"], cas="18540-29-9")]
    index = EfFlowIndex.from_tables(cf, vocab)
    pipeline = default_pipeline(index, {})
    flow = BafuFlow("Chromium VI", "emissions to air", "unspecified", "kg")
    match = pipeline.match(flow, None)
    assert isinstance(match, Match) and match.code == "cr6" and match.tier == "name"


def test_ammonium_plus_pipeline_match_lands_on_the_curated_alias_target():
    # pipeline-level: "Ammonium+" -> "Ammonium" via a curated alias -- a trailing "+"
    # is not one of IonStripMatcher's own markers (comma/bare "ion", or a roman
    # numeral II-VI), so only the curated alias resolves it.
    cf = [cf_row("nh4", "ammonium", WATER_FRESH, method="ef-3.1:eutrophication-marine", value=1.0)]
    vocab = [vocab_row("nh4", "Ammonium", cas="14798-03-9")]
    index = EfFlowIndex.from_tables(cf, vocab)
    pipeline = default_pipeline(index, {"ammonium+": "Ammonium"})
    flow = BafuFlow("Ammonium+", "emissions to water", "river", "kg")
    match = pipeline.match(flow, None)
    assert isinstance(match, Match) and match.code == "nh4" and match.tier == "alias"


def test_entry_for_raises_when_no_fixed_conversion_exists(tmp_path):
    cf = CF + [
        cf_row(
            "u238",
            "uranium-238",
            AIR_UNSPEC,
            method="ef-3.1:ionising-radiation-human-health",
            value=1.0,
        )
    ]
    vocab = VOCAB + [vocab_row("u238", "Uranium-238", alt=["Uranium"])]
    root = _stage(tmp_path, cf=cf, vocab=vocab)
    source = BafuEfMatchedSource(_config(root))
    (r,) = source.parse(
        source.fetch(RunContext(cache_dir=tmp_path / "cache", output_dir=tmp_path / "out"))
    )
    flow = BafuFlow("Uranium", "emissions to air", "unspecified", "kg")
    match = r["inputs"].pipeline.match(flow, None)
    assert isinstance(match, Match)  # sanity: the pipeline itself matches by synonym
    with pytest.raises(ValueError, match="no fixed conversion"):
        source.entry_for(flow, match, r["inputs"].index)


def test_assembled_package_validates_against_the_randonneur_schema(tmp_path):
    config = _config(_stage(tmp_path))
    package = _assemble(_run(BafuEfMatchedSource(config), tmp_path), config)
    jsonschema.validate(package, json.loads(_SCHEMA.read_text()))


def test_no_intermediate_database_identifier_in_output(tmp_path):
    # exercises both caveat-producing paths that used to name the intermediate database
    # verbatim: an energy-content conversion (Peat, already in the fixture zip -- see
    # test_peat_energy_content_converts_onto_the_fossil_resource_flow) and an
    # ore-composite decomposition (injected the same way other tests in this module add
    # a flow the fixture zip itself doesn't carry).
    cf = CF + [
        cf_row("peat", "peat", RES_GROUND, method="ef-3.1:resource-use-fossils", value=1.0),
        cf_row("zinc", "zinc", RES_GROUND, value=1.0),
    ]
    vocab = VOCAB + [vocab_row("peat", "Peat"), vocab_row("zinc", "Zinc")]
    root = _stage(tmp_path, cf=cf, vocab=vocab)
    source = BafuEfMatchedSource(_config(root))
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

    # sanity: both caveat-producing paths actually fired in this fixture
    assert any("net calorific value" in r.get("comment", "") for r in rows)
    assert any("ore composite" in r.get("comment", "") for r in rows)

    blob = json.dumps(rows).lower()
    assert "ecoinvent" not in blob and "biosphere3" not in blob


def test_parse_before_fetch_fails_loudly(tmp_path):
    source = BafuEfMatchedSource(_config(_stage(tmp_path)))
    with pytest.raises(RuntimeError, match="fetch\\(\\) must run before parse"):
        source.parse(RawData(content=b"", source_url="file://x"))
