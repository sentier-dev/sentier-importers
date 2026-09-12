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
    cf_row("meoh-a", "methanol", AIR_UNSPEC, method="ef-3.1:human-toxicity-cancer", value=2.29e-7),
    cf_row(
        "meoh-b",
        "methanol",
        AIR_UNSPEC,
        method="ef-3.1:photochemical-ozone-formation-human-health",
        value=0.236,
    ),
    # same identity (same factor), distinct CAS: the source CAS should single one out
    cf_row("mo-a", "molybdenum", WATER_FRESH, value=1.0),
    cf_row("mo-b", "molybdenum", WATER_FRESH, value=1.0),
    # same name, different factors, no CAS on either flow: ambiguous under a region wrap
    cf_row("riv-a", "river water", WATER_FRESH, value=1.0),
    cf_row("riv-b", "river water", WATER_FRESH, value=5.0),
    # same identity, same name, same CAS: a pure code tie-break, no caveat
    cf_row("dup-a", "dupenium", WATER_FRESH, value=9.0),
    cf_row("dup-b", "dupenium", WATER_FRESH, value=9.0),
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
    vocab_row("meoh-a", "Methanol", cas="636-21-5"),
    vocab_row("meoh-b", "Methanol", cas="67-56-1"),
    vocab_row("mo-a", "Molybdenum", cas="16065-87-5"),
    vocab_row("mo-b", "Molybdenum", cas="7439-98-7"),
    vocab_row("riv-a", "River water"),
    vocab_row("riv-b", "River water"),
    vocab_row("dup-a", "Dupenium", cas="99-99-9"),
    vocab_row("dup-b", "Dupenium", cas="99-99-9"),
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
        detail="CAS 630-08-0 finds 2 EF flows with different factors for "
        "carbon monoxide (biogenic), carbon monoxide (fossil); "
        "the source CAS 630-08-0 does not single one out",
    )


def test_qualifier_beats_cas(pipeline):
    # CAS 630-08-0 is shared by both carbon monoxide substances and would be
    # ambiguous; the qualifier spelling built for exactly this case must run first.
    got = pipeline.match(air("Carbon monoxide, fossil"), "630-08-0")
    assert got.code == "co-fos" and got.tier == "qualifier"


def test_alias_beats_cas(pipeline):
    # "Water" shares CAS 7732-18-5 across several EF water flows; the curated alias
    # for "water, river" must resolve it before CAS gets a chance to be ambiguous.
    got = pipeline.match(BafuFlow("Water, river", "resources", "in water", "m3"), "7732-18-5")
    assert got.code == "river-res" and got.tier == "alias"


def test_region_beats_cas(pipeline):
    # a region-stripped name is still name evidence; it must run before CAS, which
    # would otherwise resolve (or, in the real EF water families, find ambiguous)
    # "Water, KR" by its shared CAS before the region token is ever stripped.
    got = pipeline.match(water("Water, KR", "river", "m3"), "7732-18-5")
    assert got.code == "water-em" and got.tier == "region/name" and got.location == "KR"


def test_same_name_different_factors_resolved_by_source_cas(pipeline):
    got = pipeline.match(air("Methanol"), "000067-56-1")
    assert got.code == "meoh-b" and got.tier == "name"
    assert got.candidates == 2 and got.caveats == ()


def test_same_name_different_factors_without_cas_is_ambiguous(pipeline):
    got = pipeline.match(air("Methanol"), None)
    assert got == Unmatched(
        reason="ambiguous_substances",
        detail="name match finds 2 EF flows with different factors for methanol; no source CAS",
    )


def test_same_name_different_factors_with_foreign_cas_is_ambiguous(pipeline):
    got = pipeline.match(air("Methanol"), "1-2-3")
    assert got.reason == "ambiguous_substances"


def test_identity_tie_prefers_source_cas_with_a_caveat(pipeline):
    # mo-a/mo-b are the same substance (identical factor) under two different CAS
    # numbers; the source CAS singles one out, but the choice is never silent.
    got = pipeline.match(water("Molybdenum", "river"), "7439-98-7")
    assert got.code == "mo-b"
    assert got.caveats == (
        "2 EF flows with identical factors; chose Molybdenum (CAS 7439-98-7) over "
        "Molybdenum (CAS 16065-87-5)",
    )


def test_identity_tie_without_cas_falls_back_to_code_with_a_caveat(pipeline):
    got = pipeline.match(water("Molybdenum", "river"), None)
    assert got.code == "mo-a"
    assert got.caveats != ()


def test_identity_tie_with_one_shared_cas_has_no_caveat(pipeline):
    # ccl4/cfc10 already share a single CAS between them: confirms the caveat is
    # genuinely conditioned on more than one distinct CAS among the tied candidates
    got = pipeline.match(air("Methane, tetrachloro-, CFC-10"), "56-23-5")
    assert got.code == "cfc10" and got.caveats == ()


def test_identical_name_and_cas_ties_break_by_code(pipeline):
    got = pipeline.match(water("Dupenium", "river"), None)
    assert got.code == "dup-a" and got.caveats == ()


def test_ambiguity_under_region_wrap_uses_the_inner_tier(pipeline):
    got = pipeline.match(water("River water, KR", "river", "m3"), None)
    assert got == Unmatched(
        reason="ambiguous_substances",
        detail="region/name match finds 2 EF flows with different factors for "
        "river water; no source CAS",
    )


def test_sub_compartment_absent_lists_every_distinct_name(index):
    # unspecified_fallback=False turns an UNSPECIFIED-only candidate set (both ccl4
    # and cfc10 exist only on the bucket-level unspecified leaf) into a
    # sub_compartment_absent naming every distinct candidate name and just that leaf.
    pipe = default_pipeline(index, ALIASES, unspecified_fallback=False)
    got = pipe.match(air("Methane, tetrachloro-, CFC-10", "indoor"), "56-23-5")
    assert got == Unmatched(
        reason="sub_compartment_absent",
        detail="EF has carbon tetrachloride, cfc-10 only in: emissions to air, unspecified",
    )


def test_elemental_cas_does_not_collapse_speciation(pipeline):
    got = pipeline.match(water("Chromium", "river"), "7440-47-3")
    assert got == Unmatched(
        reason="no_ef_flow",
        detail="no EF 3.1 flow with a factor matches by name, synonym, qualifier, alias, "
        "region-stripped name or CAS in the water compartment",
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
