import pytest
from sentier_importers.matching.compartments import Placement
from sentier_importers.matching.ef_index import EfFlowIndex
from sentier_importers.matching.matchers import Alias, Candidate
from sentier_importers.matching.pipeline import Match, MatchPipeline, Unmatched, default_pipeline
from sentier_importers.sources.eaternity.bridge import BafuFlow

from tests.matching.ef_fixtures import (
    AIR_RURAL,
    AIR_UNSPEC,
    AIR_URBAN,
    LAND_OCC,
    RES_GROUND,
    RES_WATER,
    WATER_FRESH,
    WATER_UNSPEC,
    cf_row,
    vocab_row,
    write_ef_inputs,
)

LT = "Emissions / Emissions to water / Emissions to water, unspecified (long-term)"
SOIL_INDUSTRIAL = "Emissions / Emissions to soil / Emissions to non-agricultural soil"
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
    # same identity, one CAS, one none: the source CAS names neither -- still not silent
    cf_row("mo-soil-ion", "molybdenum", SOIL_INDUSTRIAL, value=1.0),
    cf_row("mo-soil-dup", "molybdenum", SOIL_INDUSTRIAL, value=1.0),
    cf_row("industrial-area", "industrial area", LAND_OCC, method="ef-3.1:land-use", value=1.0),
    cf_row(
        "traffic-rail",
        "traffic area, rail network",
        LAND_OCC,
        method="ef-3.1:land-use",
        value=1.0,
    ),
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
    vocab_row("mo-soil-ion", "Molybdenum", cas="16065-87-5"),
    vocab_row("mo-soil-dup", "Molybdenum"),
    vocab_row("industrial-area", "Industrial Area"),
    vocab_row("traffic-rail", "Traffic Area, Rail Network"),
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
    # a name that reaches CasMatcher unresolved (no exact/synonym/qualifier/carbon-
    # oxide/ion route of its own) still hits CAS 630-08-0's genuine ambiguity between
    # the biogenic/fossil carbon monoxide pair -- unlike the BARE "Carbon monoxide"
    # name itself, which decision (e), 2026-09-13 now resolves before CAS is ever
    # consulted (see test_carbon_oxide_matcher_beats_cas below).
    got = pipeline.match(air("Carbon monoxide, unspecified isomer"), "630-08-0")
    assert got == Unmatched(
        reason="ambiguous_substances",
        detail="CAS 630-08-0 finds 2 EF flows with different factors for "
        "carbon monoxide (biogenic), carbon monoxide (fossil); "
        "the source CAS 630-08-0 does not single one out",
    )


def test_carbon_oxide_matcher_beats_cas(pipeline):
    # decision (e), 2026-09-13: a bare "Carbon monoxide" resolves onto EF's fossil
    # spelling before CasMatcher ever runs, so the CAS 630-08-0 collision between the
    # biogenic/fossil pair (test above, pre-decision-(e) behaviour) never surfaces.
    got = pipeline.match(air("Carbon monoxide"), "630-08-0")
    assert got.code == "co-fos" and got.tier == "carbon-oxide" and got.placement == "exact"
    assert got.caveats == ("unqualified carbon oxide taken as fossil (decision 2026-09-13)",)


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


def test_identity_tie_source_cas_matching_none_still_gets_a_caveat(pipeline):
    # mo-soil-ion/mo-soil-dup are the same substance (identical factor); the source
    # CAS "7439-98-7" names neither, but mo-soil-ion still carries a different CAS,
    # so the free choice between them still needs the "never silent" treatment --
    # this also covers the cas_pool-empty branch of the free-choice code path.
    got = pipeline.match(
        BafuFlow("Molybdenum", "emissions to soil", "industrial", "kg"), "7439-98-7"
    )
    assert got.code == "mo-soil-dup"  # ties break by code once the CAS pool is empty
    assert got.caveats == (
        "2 EF flows with identical factors; source CAS 7439-98-7 matches none, chose "
        "Molybdenum (CAS none) over Molybdenum (CAS 16065-87-5)",
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
        detail="no EF 3.1 flow with a factor matches by name, synonym, qualifier, "
        "carbon-oxide, ion-strip, land-use class, ore composite, alias, "
        "region-stripped name or CAS in the water compartment",
    )


def test_no_ef_flow_detail_says_with_or_without_a_factor_for_an_inclusive_index(tmp_path):
    # same fixture and pipeline shape as the plain (characterised-only) case above, but
    # built with include_uncharacterised=True: the "no_ef_flow" detail must say the
    # search covered uncharacterised flows too, not just factor-bearing ones.
    index = EfFlowIndex.from_files(
        *write_ef_inputs(tmp_path, CF, VOCAB), include_uncharacterised=True
    )
    pipe = default_pipeline(index, ALIASES)
    got = pipe.match(water("Chromium", "river"), "7440-47-3")
    assert got == Unmatched(
        reason="no_ef_flow",
        detail="no EF 3.1 flow, with or without a factor, matches by name, synonym, "
        "qualifier, carbon-oxide, ion-strip, land-use class, ore composite, alias, "
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


def test_resource_flow_in_unspecified_sub_compartment_falls_back_to_the_resource_branch(pipeline):
    # decision (b): "unspecified" carries no extraction-medium information, and the
    # alias singles out exactly one EF resource leaf (river-res), so the flow now
    # resolves through the resource-branch fallback instead of staying absent.
    got = pipeline.match(BafuFlow("Water, river", "resources", "unspecified", "m3"), None)
    assert got.code == "river-res" and got.placement == "resource_branch_fallback"
    assert got.caveats == (
        "BAFU files this resource under unspecified; EF has river water only as "
        "renewable material resources from water",
    )


def test_resource_flow_in_unspecified_sub_compartment_stays_absent_when_resource_fallback_disabled(
    index,
):
    # the bucket-level Placement.UNSPECIFIED fallback never applies to resources
    # (unchanged); with the new resource-branch fallback also disabled, the flow is
    # reported sub_compartment_absent exactly as before decision (b).
    pipe = default_pipeline(index, ALIASES, resource_fallback=False)
    got = pipe.match(BafuFlow("Water, river", "resources", "unspecified", "m3"), None)
    assert got.reason == "sub_compartment_absent"


class _StubOverrideMatcher:
    """A stub matcher carrying a fixed ``subcategory_override`` on every candidate."""

    tier = "stub"

    def __init__(self, override):
        self._override = override

    def candidates(self, flow, cas, index):
        return [
            Candidate(f, tier=self.tier, subcategory_override=self._override)
            for f in index.by_name("Industrial Area", "resource")
        ]


def test_subcategory_override_changes_where_a_candidate_places(index):
    flow = BafuFlow("Occupation, industrial area", "resources", "unspecified", "m2a")
    with_override = MatchPipeline([_StubOverrideMatcher("land")], index)
    got = with_override.match(flow, None)
    assert got.code == "industrial-area" and got.placement == "exact"

    # decision (b) would otherwise recover this very candidate set through the
    # resource-branch fallback ("unspecified" is itself uninformative, single leaf) --
    # disabled here so this test isolates what the override alone is responsible for.
    without_override = MatchPipeline([_StubOverrideMatcher(None)], index, resource_fallback=False)
    got = without_override.match(flow, None)
    assert got.reason == "sub_compartment_absent"


def test_land_use_matcher_runs_inside_region_strip(pipeline):
    # "Occupation, traffic area, rail network" is filed by BAFU under the "land"
    # sub-compartment, but the region token "CH" trails the name -- RegionStripMatcher
    # strips it and re-applies LandUseMatcher (part of its own ``named`` list) to the
    # clean stem, which then matches directly.
    got = pipeline.match(
        BafuFlow("Occupation, traffic area, rail network, CH", "resources", "land", "m2a"), None
    )
    assert got.code == "traffic-rail"
    assert got.tier == "region/landuse"
    assert got.location == "CH"
    assert got.placement == "exact"
    assert got.caveats == ()


def test_ore_composite_match_at_pipeline_level(tmp_path_factory):
    # a zinc ore composite in resources / in ground decomposes onto plain "Zinc",
    # tier "ore", exact placement, the composite caveat, and EF's default kilogram
    # reference unit (no special-cased method is in play).
    cf = [cf_row("zinc", "zinc", RES_GROUND, value=1.0)]
    vocab = [vocab_row("zinc", "Zinc")]
    idx = EfFlowIndex.from_files(*write_ef_inputs(tmp_path_factory.mktemp("ore-pipe"), cf, vocab))
    ore_pipeline = default_pipeline(idx, {})
    flow = BafuFlow(
        "Zinc, Zn 0.63%, Au 9.7E-4%, Ag 9.7E-4%, Cu 0.38%, Pb 0.014%, in ore",
        "resources",
        "in ground",
        "kg",
    )
    got = ore_pipeline.match(flow, None)
    assert got == Match(
        code="zinc",
        tier="ore",
        placement="exact",
        location=None,
        candidates=1,
        caveats=("ore composite of the BAFU-2026 source nomenclature; the amount is kg of Zinc",),
    )
    assert idx.reference_unit(got.code) == "kilogram"


#: A second index dedicated to decision (b) (resource-branch fallback) scenarios, so
#: they do not have to share leaf/candidate shapes with the rest of this module's CF.
_RB_CF = [
    cf_row("ground-water", "ground water", RES_WATER, method="ef-3.1:water-use", value=37.8),
    cf_row("uranium", "uranium", RES_GROUND, method="ef-3.1:resource-use-fossils", value=1.0),
    cf_row("bromine", "bromine", RES_GROUND, value=1.0),
    # a name spread over two distinct resource leafs: neither UNSPECIFIED placement
    # (never valid for resources) nor the resource-branch fallback (which requires
    # exactly one leaf) can pick between them.
    cf_row("aluminium-ground", "aluminium", RES_GROUND, value=1.0),
    cf_row("aluminium-water", "aluminium", RES_WATER, value=1.0),
    # a name that lands on exactly one leaf, but two flows with different factors and
    # no CAS on either: the resource-branch fallback commits to this candidate set,
    # then _pick's ordinary disambiguation must still be free to report ambiguity.
    cf_row("silver-a", "silver", RES_GROUND, value=1.0),
    cf_row("silver-b", "silver", RES_GROUND, value=2.0),
]
_RB_VOCAB = [
    vocab_row("ground-water", "Ground Water"),
    vocab_row("uranium", "Uranium"),
    vocab_row("bromine", "Bromine"),
    vocab_row("aluminium-ground", "Aluminium"),
    vocab_row("aluminium-water", "Aluminium"),
    vocab_row("silver-a", "Silver"),
    vocab_row("silver-b", "Silver"),
]
_RB_ALIASES = {"water, well": "Ground Water"}


@pytest.fixture(scope="module")
def rb_index(tmp_path_factory):
    return EfFlowIndex.from_files(
        *write_ef_inputs(tmp_path_factory.mktemp("resource-branch"), _RB_CF, _RB_VOCAB)
    )


@pytest.fixture(scope="module")
def rb_pipeline(rb_index):
    return default_pipeline(rb_index, _RB_ALIASES)


def test_uninformative_water_alias_falls_back_to_the_resource_branch(rb_pipeline):
    # "Water, well" is filed by BAFU under "unspecified" (no medium information); the
    # curated alias singles out exactly one EF leaf (Ground Water), so it resolves.
    got = rb_pipeline.match(BafuFlow("Water, well", "resources", "unspecified", "m3"), None)
    assert got.code == "ground-water"
    assert got.tier == "alias"
    assert got.placement == "resource_branch_fallback"
    assert got.caveats == (
        "BAFU files this resource under unspecified; EF has ground water only as "
        "renewable material resources from water",
    )


def test_uninformative_land_subcategory_falls_back_to_the_resource_branch(rb_pipeline):
    # "Uranium" filed under "land" (BAFU's land-family token, not a real land-use
    # class here) carries no extraction-medium information either; it decomposes to
    # exactly one EF leaf by plain name match.
    got = rb_pipeline.match(BafuFlow("Uranium", "resources", "land", "kg"), None)
    assert got.code == "uranium"
    assert got.placement == "resource_branch_fallback"


def test_in_water_is_not_uninformative_stays_absent(rb_pipeline):
    # "in water" is never uninformative for a non-water name (Bromine/Iodine/
    # Magnesium: EF only characterises these from ground, and a sea-water extraction
    # is not the same medium) -- the resource-branch fallback must not paper over it.
    got = rb_pipeline.match(BafuFlow("Bromine", "resources", "in water", "kg"), None)
    assert got == Unmatched(
        reason="sub_compartment_absent",
        detail="EF has bromine only in: non-renewable element resources from ground",
    )


def test_candidates_spread_over_two_resource_leafs_stay_absent(rb_pipeline):
    # "land" is uninformative, but the candidate set spans two distinct EF leafs
    # (ground and water): the resource-branch fallback requires exactly one.
    got = rb_pipeline.match(BafuFlow("Aluminium", "resources", "land", "kg"), None)
    assert got.reason == "sub_compartment_absent"


def test_resource_branch_fallback_can_be_ambiguous(rb_pipeline):
    # "Silver" reaches the resource-branch fallback (single leaf, "unspecified" is
    # uninformative), but its two candidates carry different factors and neither
    # carries a CAS to single one out; _pick reports ambiguous_substances rather than
    # silently picking one, exactly as it would for any other tier.
    got = rb_pipeline.match(BafuFlow("Silver", "resources", "unspecified", "kg"), None)
    assert got == Unmatched(
        reason="ambiguous_substances",
        detail="name match finds 2 EF flows with different factors for silver; no source CAS",
    )


def test_resource_fallback_can_be_disabled(rb_index):
    pipe = default_pipeline(rb_index, _RB_ALIASES, resource_fallback=False)
    got = pipe.match(BafuFlow("Uranium", "resources", "land", "kg"), None)
    assert got == Unmatched(
        reason="sub_compartment_absent",
        detail="EF has uranium only in: non-renewable element resources from ground",
    )


def test_emissions_unspecified_fallback_is_unaffected_by_resource_fallback(pipeline):
    # a plain emissions-bucket flow never even reaches the resource-branch check
    # (bucket_of_bafu_category != "resource"); the pre-existing bucket-level
    # UNSPECIFIED fallback still behaves exactly as it always has.
    got = pipeline.match(water("Zinc", "groundwater"), None)
    assert got.placement == "unspecified_fallback"


# --- rank-8-only relaxed nomenclature placement (decision (f)(1), 2026-09-13) ------

_WIDGET_UNCHAR_VOCAB = [
    # bw="envi-air-hist15me" -> "Emissions to non-urban air or from high stacks"
    vocab_row("widget-a", "Widget", bw="envi-air-hist15me"),
]


def test_relaxed_placement_uses_the_sole_leaf_when_only_one_exists(tmp_path):
    # "indoor" is not "non-urban air or from high stacks", and neither EXACT,
    # UNSPECIFIED nor the resource-branch fallback apply (this is the "air" bucket) --
    # the single uncharacterised candidate's own leaf is used, with a caveat
    # disclosing the relaxed placement carries no factor.
    index = EfFlowIndex.from_files(
        *write_ef_inputs(tmp_path, [], _WIDGET_UNCHAR_VOCAB, shards=1),
        include_uncharacterised=True,
    )
    pipe = default_pipeline(index, {})
    got = pipe.match(BafuFlow("Widget", "emissions to air", "indoor", "kg"), None)
    assert got == Match(
        code="widget-a",
        tier="name",
        placement=Placement.NOMENCLATURE.value,
        location=None,
        candidates=1,
        caveats=(
            "EF has this name only in emissions to non-urban air or from high "
            "stacks; placed there for nomenclature alignment (no factor)",
        ),
    )


_TWO_LEAF_UNCHAR_VOCAB = [
    # bw="envi-air-unkn" -> "Emissions to air, unspecified"
    vocab_row("widget2-a", "Widget2", bw="envi-air-unkn"),
    # bw="envi-air-indr-unkn" -> "Emissions to air, indoor"
    vocab_row("widget2-b", "Widget2", bw="envi-air-indr-unkn"),
]


def test_relaxed_placement_prefers_the_unspecified_leaf_among_several(tmp_path):
    # two uncharacterised candidates on different leafs; "high. pop." itself matches
    # neither leaf's own family, but one candidate's leaf happens to be exactly the
    # bucket-level "air, unspecified" leaf -- with unspecified_fallback disabled (so
    # the ordinary UNSPECIFIED branch never intercepts it first), the relaxed
    # placement still prefers that leaf over the alphabetically-first one. Since more
    # than one leaf exists, the caveat lists every leaf found and names which one was
    # picked, rather than falsely claiming the name exists "only" on that one leaf.
    index = EfFlowIndex.from_files(
        *write_ef_inputs(tmp_path, [], _TWO_LEAF_UNCHAR_VOCAB, shards=1),
        include_uncharacterised=True,
    )
    pipe = default_pipeline(index, {}, unspecified_fallback=False)
    got = pipe.match(BafuFlow("Widget2", "emissions to air", "high. pop.", "kg"), None)
    assert got.code == "widget2-a"  # the "air, unspecified" leaf, not "air, indoor"
    assert got.placement == Placement.NOMENCLATURE.value
    assert got.caveats == (
        "EF has this name only in 'emissions to air, indoor', 'emissions to air, "
        "unspecified'; placed on 'emissions to air, unspecified' for nomenclature "
        "alignment (no factor)",
    )


def test_relaxed_placement_prefers_the_non_long_term_unspecified_leaf_too(tmp_path):
    # a "*, long-term" source whose candidates do NOT include the long-term
    # unspecified leaf, but DO include the plain (non-long-term) one: the plain
    # unspecified leaf must still be preferred over the alphabetically-first leaf --
    # the long-term unspecified leaf almost never actually holds an uncharacterised
    # candidate in practice, so falling through only to alphabetical order here would
    # be wrong far more often than the "prefer the long-term leaf first" rule helps.
    # This is the real BAFU-2026 shape: Sulfate/Potassium/Scandium/... to
    # "groundwater, long-term" candidate onto fresh water / sea water / water
    # unspecified -- must land on "water, unspecified", not "fresh water"
    # (alphabetically first).
    vocab = [
        vocab_row("water-fresh", "Widget5", bw="envi-wate-suwa"),  # fresh water
        vocab_row("water-sea", "Widget5", bw="envi-wate-ocea"),  # sea water
        vocab_row("water-unspec", "Widget5", bw="envi-wate-unkn"),  # water, unspecified
    ]
    index = EfFlowIndex.from_files(
        *write_ef_inputs(tmp_path, [], vocab, shards=1), include_uncharacterised=True
    )
    pipe = default_pipeline(index, {})
    got = pipe.match(
        BafuFlow("Widget5", "emissions to water", "groundwater, long-term", "kg"), None
    )
    assert got.code == "water-unspec"
    assert got.placement == Placement.NOMENCLATURE.value
    assert got.caveats == (
        "EF has this name only in 'emissions to fresh water', 'emissions to sea water', "
        "'emissions to water, unspecified'; placed on 'emissions to water, unspecified' "
        "for nomenclature alignment (no factor)",
    )


_THREE_LEAF_NO_UNSPECIFIED_VOCAB = [
    # neither leaf here is a bucket-level "unspecified" leaf at all
    vocab_row("widget3-a", "Widget3", bw="envi-air-indr-unkn"),  # air, indoor
    vocab_row("widget3-b", "Widget3", bw="envi-air-hist15me"),  # non-urban air/high stacks
]


def test_relaxed_placement_falls_back_to_the_alphabetically_first_leaf(tmp_path):
    # two candidates on leafs, neither of which is a bucket-level "unspecified" leaf
    # (long-term or not) -- the preference list never matches either, so the
    # alphabetically first leaf ("air, indoor" sorts before "non-urban air or from
    # high stacks") wins.
    index = EfFlowIndex.from_files(
        *write_ef_inputs(tmp_path, [], _THREE_LEAF_NO_UNSPECIFIED_VOCAB, shards=1),
        include_uncharacterised=True,
    )
    pipe = default_pipeline(index, {})
    got = pipe.match(BafuFlow("Widget3", "emissions to air", "low. pop., long-term", "kg"), None)
    assert got.code == "widget3-a"  # "air, indoor" -- alphabetically first
    assert got.placement == Placement.NOMENCLATURE.value
    assert got.caveats == (
        "EF has this name only in 'emissions to air, indoor', 'emissions to "
        "non-urban air or from high stacks'; placed on 'emissions to air, indoor' "
        "for nomenclature alignment (no factor)",
    )


def test_relaxed_placement_never_fires_on_the_characterised_only_index(tmp_path):
    # a real gate, not a vacuous one: two CHARACTERISED candidates spread over two
    # leafs that satisfy neither EXACT (subcategory "indoor" matches neither leaf's
    # family) nor UNSPECIFIED (disabled here, and neither leaf is the bucket-level
    # unspecified leaf anyway) nor the resource-branch fallback (bucket is "air", not
    # "resource"). On the inclusive index this exact shape would qualify for the
    # relaxed nomenclature placement -- but ``index.includes_uncharacterised`` is
    # False here (a plain characterised-only, rank-7-shaped index), so the pipeline
    # must still report ``sub_compartment_absent``, never Placement.NOMENCLATURE.
    cf = [
        cf_row("widget-c-urban", "widget6", AIR_URBAN, value=1.0),
        cf_row("widget-c-rural", "widget6", AIR_RURAL, value=1.0),
    ]
    vocab = [vocab_row("widget-c-urban", "Widget6"), vocab_row("widget-c-rural", "Widget6")]
    index = EfFlowIndex.from_files(*write_ef_inputs(tmp_path, cf, vocab))
    assert index.includes_uncharacterised is False
    pipe = default_pipeline(index, {}, unspecified_fallback=False)
    got = pipe.match(BafuFlow("Widget6", "emissions to air", "indoor", "kg"), None)
    assert got == Unmatched(
        reason="sub_compartment_absent",
        detail="EF has widget6 only in: emissions to non-urban air or from high "
        "stacks, emissions to urban air close to ground",
    )
