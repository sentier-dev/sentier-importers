import pytest
from sentier_importers.matching.ef_index import EfFlowIndex
from sentier_importers.matching.matchers import Alias
from sentier_importers.matching.pipeline import Match, Unmatched, default_pipeline
from sentier_importers.sources.eaternity.bridge import BafuFlow

from tests.matching.ef_fixtures import (
    AIR_RURAL,
    AIR_UNSPEC,
    RES_WATER,
    WATER_FRESH,
    WATER_UNSPEC,
    cf_row,
    vocab_row,
    write_ef_inputs,
)

LT = "Emissions / Emissions to water / Emissions to water, unspecified (long-term)"
CF = [
    cf_row("zn-fresh", "zinc", WATER_FRESH, value=1.0),
    cf_row("zn-unspec", "zinc", WATER_UNSPEC, value=1.0),
    cf_row("zn-lt", "zinc", LT, value=0.1),
    cf_row("ccl4", "carbon tetrachloride", AIR_UNSPEC, value=3.0),
    cf_row("cfc10", "cfc-10", AIR_UNSPEC, value=3.0),
    cf_row("co-bio", "carbon monoxide (biogenic)", AIR_UNSPEC, value=1.0),
    cf_row("co-fos", "carbon monoxide (fossil)", AIR_UNSPEC, value=1.6),
    cf_row("cr3", "chromium (iii)", WATER_FRESH, value=1.0),
    cf_row("cr6", "chromium (vi)", WATER_FRESH, value=50.0),
    cf_row("pm10", "particles (pm10)", AIR_RURAL, value=5.0),
    cf_row("water-res", "water", RES_WATER, method="ef-3.1:water-use", value=37.8),
    cf_row("river-res", "river water", RES_WATER, method="ef-3.1:water-use", value=42.95),
    cf_row("water-em", "water", WATER_FRESH, method="ef-3.1:water-use", value=-37.8),
]
VOCAB = [
    vocab_row("zn-fresh", "Zinc", cas="7440-66-6"),
    vocab_row("zn-unspec", "Zinc", cas="7440-66-6"),
    vocab_row("zn-lt", "Zinc", cas="7440-66-6"),
    vocab_row("ccl4", "Carbon tetrachloride", cas="56-23-5"),
    vocab_row("cfc10", "CFC-10", cas="56-23-5"),
    vocab_row("co-bio", "Carbon monoxide (biogenic)", cas="630-08-0"),
    vocab_row("co-fos", "Carbon monoxide (fossil)", cas="630-08-0"),
    vocab_row("cr3", "Chromium (III)", cas="16065-83-1"),
    vocab_row("cr6", "Chromium (VI)", cas="18540-29-9"),
    vocab_row("pm10", "Particles (PM10)"),
    vocab_row("water-res", "Water", cas="7732-18-5"),
    vocab_row("river-res", "River water", cas="7732-18-5"),
    vocab_row("water-em", "Water", cas="7732-18-5"),
]
ALIASES = {
    "particulates, < 10 um": Alias("Particles (PM10)", "PM10 includes the fine fraction"),
    "water, river": "River water",
}


@pytest.fixture(scope="module")
def index(tmp_path_factory):
    return EfFlowIndex.from_files(*write_ef_inputs(tmp_path_factory.mktemp("ef"), CF, VOCAB))


@pytest.fixture(scope="module")
def pipeline(index):
    return default_pipeline(index, ALIASES)


def water(name, sub, unit="kg"):
    return BafuFlow(name, "emissions to water", sub, unit)


def air(name, sub="unspecified"):
    return BafuFlow(name, "emissions to air", sub, "kg")


def test_exact_placement_wins(pipeline):
    got = pipeline.match(water("Zinc", "river"), None)
    assert got == Match(
        code="zn-fresh", tier="name", placement="exact", location=None, candidates=1, caveats=()
    )


def test_unspecified_fallback_carries_a_caveat(pipeline):
    got = pipeline.match(water("Zinc", "groundwater"), None)
    assert got.code == "zn-unspec" and got.placement == "unspecified_fallback"
    assert got.caveats == (
        "EF has no ground water flow for this substance; the unspecified context is used",
    )


def test_long_term_is_exact_on_the_long_term_unspecified_leaf(pipeline):
    got = pipeline.match(water("Zinc", "groundwater, long-term"), None)
    assert got.code == "zn-lt" and got.placement == "exact" and got.caveats == ()


def test_fallback_can_be_disabled(index):
    got = default_pipeline(index, {}, unspecified_fallback=False).match(
        water("Zinc", "groundwater"), None
    )
    assert got == Unmatched(
        reason="sub_compartment_absent",
        detail="EF has zinc only in: emissions to fresh water, emissions to water, unspecified, "
        "emissions to water, unspecified (long-term)",
    )


def test_cf_identical_duplicates_are_harmless_pick_closest_name(pipeline):
    got = pipeline.match(air("Methane, tetrachloro-, CFC-10"), "56-23-5")
    assert got.code == "cfc10" and got.tier == "cas" and got.candidates == 2 and got.caveats == ()


def test_substances_with_different_factors_are_ambiguous(pipeline):
    got = pipeline.match(air("Carbon monoxide"), "630-08-0")
    assert got == Unmatched(
        reason="ambiguous_substances",
        detail="CAS 630-08-0 names 2 EF substances with different factors: "
        "carbon monoxide (biogenic), carbon monoxide (fossil)",
    )


def test_elemental_cas_does_not_collapse_speciation(pipeline):
    got = pipeline.match(water("Chromium", "river"), "7440-47-3")
    assert got == Unmatched(
        reason="no_ef_flow",
        detail="no EF 3.1 flow with a factor matches by name, synonym, CAS, qualifier or alias "
        "in the water compartment",
    )


def test_alias_caveat_and_tier_are_carried(pipeline):
    got = pipeline.match(air("Particulates, < 10 um", "low. pop."), None)
    assert got.code == "pm10" and got.tier == "alias" and got.placement == "exact"
    assert got.caveats == ("PM10 includes the fine fraction",)


def test_region_stripped_water_carries_location_and_inner_tier(pipeline):
    got = pipeline.match(BafuFlow("Water, river, CH", "resources", "in water", "m3"), None)
    assert got == Match(
        code="river-res",
        tier="region/alias",
        placement="exact",
        location="CH",
        candidates=1,
        caveats=(),
    )
    got = pipeline.match(water("Water, KR", "river", "m3"), None)
    assert got.code == "water-em" and got.tier == "region/name" and got.location == "KR"


def test_regional_aggregate_has_no_location_and_a_caveat(pipeline):
    got = pipeline.match(water("Water, Europe", "river", "m3"), None)
    assert got.code == "water-em" and got.location is None
    assert got.caveats == (
        "regional aggregate Europe in the source name; EF applies the global default factor",
    )


def test_no_match_reason(pipeline):
    got = pipeline.match(air("Heat, waste"), None)
    assert got.reason == "no_ef_flow"


def test_non_ef_compartment(pipeline):
    got = pipeline.match(BafuFlow("Noise", "non material emissions", "unspecified", "kg"), None)
    assert got == Unmatched(reason="non_ef_compartment", detail="non material emissions")


def test_unknown_bafu_subcategory_is_reported_not_silently_empty(pipeline):
    got = pipeline.match(BafuFlow("Zinc", "emissions to water", "puddle", "kg"), None)
    assert got == Unmatched(reason="unknown_sub_compartment", detail="puddle")


def test_resource_flow_in_unspecified_sub_compartment_is_absent_not_fallback(pipeline):
    got = pipeline.match(BafuFlow("Water, river", "resources", "unspecified", "m3"), None)
    assert got.reason == "sub_compartment_absent"
