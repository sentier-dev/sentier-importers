import json
from pathlib import Path

import jsonschema
import pytest
from sentier_importers.core.context import RunContext
from sentier_importers.core.pipeline import _assemble
from sentier_importers.core.source import SourceConfig
from sentier_importers.core.types import RawData
from sentier_importers.matching.ef_index import EfFlowIndex
from sentier_importers.matching.pipeline import Match, Unmatched, default_pipeline
from sentier_importers.sources.bafu.ecospold import flow_id
from sentier_importers.sources.bafu.mappings_biosphere_matched import (
    BafuEfMatchedSource,
    _decide,
    substance_cas,
    unit_conversion,
)
from sentier_importers.sources.eaternity.bridge import BafuFlow

from tests.matching.ef_fixtures import (
    AIR_RURAL,
    AIR_UNSPEC,
    RES_GROUND,
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
#                              Gas, natural/m3        | resources        | in ground   | m3
#                              Peat                   | resources        | in ground   | kg
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
GAS = flow_id("Gas, natural/m3", "resources", "in ground", "m3")
PEAT = flow_id("Peat", "resources", "in ground", "kg")
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
        r["inputs"].pipeline.match(
            BafuFlow("Water", "emissions to water", "unspecified", "kg"), None
        ),
        r["inputs"].index,
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
    entry = source.entry_for(flow, r["inputs"].pipeline.match(flow, None), r["inputs"].index)
    assert entry["target"]["location"] == "KR" and "conversion_factor" not in entry
    flow = BafuFlow("Water, Europe", "emissions to water", "unspecified", "m3")
    entry = source.entry_for(flow, r["inputs"].pipeline.match(flow, None), r["inputs"].index)
    assert "location" not in entry["target"]
    assert (
        entry["comment"]
        == "regional aggregate Europe in the source name; EF applies the global default factor"
    )


@pytest.mark.parametrize(
    "bafu_unit,ef_unit,expected",
    [
        ("kg", "kilogram", 1.0),
        ("kilogram", "kg", 1.0),
        ("m3", "cubic meter", 1.0),
        ("cubic meter", "m3", 1.0),
        ("kBq", "kBq", 1.0),
        ("m2", "m2", 1.0),
        ("m2*a", "m2*a", 1.0),
        ("m2a", "m2*a", 1.0),  # BAFU spells it without the asterisk; same area-time
        ("MJ", "megajoule", 1.0),
        ("Bq", "kBq", 0.001),
        ("kWh", "megajoule", 3.6),
        ("m3", "megajoule", None),  # volume vs energy: no fixed conversion
        ("kg", "kBq", None),  # mass vs activity: no fixed conversion
        ("Nm3", "kilogram", None),  # volume vs mass: no fixed conversion
        ("m2", "m2*a", None),  # area vs area-time: no fixed conversion
        ("kg", "unknown-unit", None),  # unit outside the dimension table at all
    ],
)
def test_unit_conversion_fixed_factors_and_cross_dimension_mismatches(
    bafu_unit, ef_unit, expected
):
    assert unit_conversion(bafu_unit, ef_unit) == expected


def test_natural_gas_volume_onto_a_fossil_resource_flow_is_withheld_as_unit_mismatch(tmp_path):
    # EF characterises fossil resources in megajoule (resource-use-fossils); a BAFU
    # m3-denominated gas flow has no fixed volume -> energy conversion.
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
    assert isinstance(outcome, Unmatched) and outcome.reason == "unit_mismatch"
    assert "m3" in outcome.detail and "megajoule" in outcome.detail
    rows = source.transform(records)
    assert GAS not in {r["source"]["code"] for r in rows}


def test_peat_mass_onto_a_fossil_resource_flow_is_withheld_as_unit_mismatch(tmp_path):
    # same decision, the other cross-dimension direction: a BAFU kg-denominated peat
    # flow has no fixed mass -> energy conversion onto EF's megajoule reference unit.
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
    assert isinstance(outcome, Unmatched) and outcome.reason == "unit_mismatch"
    rows = source.transform(records)
    assert PEAT not in {r["source"]["code"] for r in rows}


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
    root = _stage(tmp_path, rank3=[CO2])
    source = BafuEfMatchedSource(_config(root))
    records = source.parse(
        source.fetch(RunContext(cache_dir=tmp_path / "cache", output_dir=tmp_path / "out"))
    )
    outcomes = source.outcomes(records)
    assert {flow.code for flow, _ in outcomes} == {WATER, RADON, GAS, PEAT}
    assert all(isinstance(o, (Match, Unmatched)) for _, o in outcomes)
    # Gas and Peat have no matching EF flow in the default fixture CF/VOCAB at all
    by_code = {flow.code: outcome for flow, outcome in outcomes}
    assert by_code[GAS].reason == "no_ef_flow"
    assert by_code[PEAT].reason == "no_ef_flow"


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
    assert (
        _decide(
            BafuFlow("Carbon dioxide", "emissions to air", "unspecified", "kg"), none, _EMPTY_INDEX
        ).reason
        == "qualifier_missing"
    )
    assert (
        _decide(
            BafuFlow("Carbon monoxide", "emissions to air", "high. pop.", "kg"), none, _EMPTY_INDEX
        ).reason
        == "qualifier_missing"
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
    fossil = _decide(
        BafuFlow("Water, fossil", "resources", "in water", "m3"),
        Unmatched("ambiguous_substances", "z"),
        _EMPTY_INDEX,
    )
    assert fossil == Unmatched(
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
    blob = json.dumps(_run(BafuEfMatchedSource(_config(_stage(tmp_path))), tmp_path)).lower()
    assert "ecoinvent" not in blob and "biosphere3" not in blob


def test_parse_before_fetch_fails_loudly(tmp_path):
    source = BafuEfMatchedSource(_config(_stage(tmp_path)))
    with pytest.raises(RuntimeError, match="fetch\\(\\) must run before parse"):
        source.parse(RawData(content=b"", source_url="file://x"))
