import pytest
from sentier_importers.core.errors import ParseError
from sentier_importers.matching.ef_index import EfFlowIndex
from sentier_importers.matching.matchers import (
    Alias,
    AliasMatcher,
    Candidate,
    CasMatcher,
    ExactNameMatcher,
    LandUseMatcher,
    OreCompositeMatcher,
    QualifierMatcher,
    RegionStripMatcher,
    SynonymMatcher,
    _normalise_land_class,
    load_aliases,
)
from sentier_importers.matching.pipeline import Match, default_pipeline
from sentier_importers.sources.eaternity.bridge import BafuFlow

from tests.matching.ef_fixtures import (
    AIR_RURAL,
    AIR_UNSPEC,
    LAND_OCC,
    LAND_TRANS,
    RES_GROUND,
    RES_WATER,
    WATER_FRESH,
    cf_row,
    vocab_row,
    write_ef_inputs,
)

CF = [
    cf_row("co2-bio", "carbon dioxide (biogenic)", AIR_RURAL),
    cf_row("co2-fos", "carbon dioxide (fossil)", AIR_RURAL),
    cf_row("co2-luc", "carbon dioxide (land use change)", AIR_RURAL),
    cf_row("pm25", "particles (pm2.5)", AIR_RURAL),
    cf_row("tin", "tin", WATER_FRESH),
    cf_row("water-res", "water", RES_WATER, method="ef-3.1:water-use", location=None),
    cf_row("river-res", "river water", RES_WATER, method="ef-3.1:water-use"),
    cf_row("water-em", "water", WATER_FRESH, method="ef-3.1:water-use"),
    cf_row("ccl4", "carbon tetrachloride", AIR_UNSPEC),
]
VOCAB = [
    vocab_row("co2-bio", "Carbon dioxide (biogenic)", cas="124-38-9"),
    vocab_row("co2-fos", "Carbon dioxide (fossil)", cas="124-38-9"),
    vocab_row("co2-luc", "Carbon dioxide (land use change)", cas="124-38-9"),
    vocab_row("pm25", "Particles (PM2.5)"),
    vocab_row("tin", "Tin", alt=["Tin atom"], cas="7440-31-5"),
    vocab_row("water-res", "Water", cas="7732-18-5"),
    vocab_row("river-res", "River water", cas="7732-18-5"),
    vocab_row("water-em", "Water", cas="7732-18-5"),
    vocab_row("ccl4", "Carbon tetrachloride", cas="56-23-5"),
]


@pytest.fixture(scope="module")
def index(tmp_path_factory):
    """The tiny EF index shared read-only by every test in this module."""
    return EfFlowIndex.from_files(*write_ef_inputs(tmp_path_factory.mktemp("ef"), CF, VOCAB))


def _air(name, sub="low. pop."):
    return BafuFlow(name, "emissions to air", sub, "kg")


LAND_CF = [
    cf_row("industrial-area", "industrial area", LAND_OCC, method="ef-3.1:land-use", value=1.0),
    cf_row(
        "to-industrial-area", "to industrial area", LAND_TRANS, method="ef-3.1:land-use", value=1.0
    ),
    cf_row("arable-irrigated", "arable, irrigated", LAND_OCC, method="ef-3.1:land-use", value=1.0),
    cf_row(
        "from-unspecified", "from unspecified", LAND_TRANS, method="ef-3.1:land-use", value=1.0
    ),
    cf_row("dump-site", "dump site", LAND_OCC, method="ef-3.1:land-use", value=1.0),
    cf_row(
        "traffic-rail",
        "traffic area, rail network",
        LAND_OCC,
        method="ef-3.1:land-use",
        value=1.0,
    ),
    cf_row(
        "unspecified-natural",
        "unspecified, natural",
        LAND_OCC,
        method="ef-3.1:land-use",
        value=1.0,
    ),
    cf_row("natural-cls", "natural", LAND_OCC, method="ef-3.1:land-use", value=1.0),
]
LAND_VOCAB = [
    vocab_row("industrial-area", "Industrial Area"),
    vocab_row("to-industrial-area", "To Industrial Area"),
    vocab_row("arable-irrigated", "Arable, Irrigated"),
    vocab_row("from-unspecified", "From Unspecified"),
    vocab_row("dump-site", "Dump Site"),
    vocab_row("traffic-rail", "Traffic Area, Rail Network"),
    vocab_row("unspecified-natural", "Unspecified, Natural"),
    vocab_row("natural-cls", "Natural"),
]


@pytest.fixture(scope="module")
def land_index(tmp_path_factory):
    """A tiny EF land-use index (occupation + transformation leafs) for LandUseMatcher."""
    return EfFlowIndex.from_files(
        *write_ef_inputs(tmp_path_factory.mktemp("land"), LAND_CF, LAND_VOCAB)
    )


def _resource(name, sub="unspecified", unit="m2a"):
    return BafuFlow(name, "resources", sub, unit)


def test_exact_name_matcher(index):
    flow = BafuFlow("Tin", "emissions to water", "river", "kg")
    got = ExactNameMatcher().candidates(flow, None, index)
    assert got == [Candidate(index.get("tin"), tier="name")]
    assert got[0].location is None and got[0].region is None
    assert got[0].tier == "name"
    assert ExactNameMatcher.tier == "name"


def test_exact_name_matcher_is_bucket_scoped(index):
    assert ExactNameMatcher().candidates(_air("Tin"), None, index) == []


def test_synonym_matcher(index):
    flow = BafuFlow("tin ATOM", "emissions to water", "river", "kg")
    assert [c.flow.code for c in SynonymMatcher().candidates(flow, None, index)] == ["tin"]
    assert SynonymMatcher.tier == "synonym"


def test_cas_matcher_needs_a_cas_and_returns_every_flow_sharing_it(index):
    flow = BafuFlow("Stannum", "emissions to water", "river", "kg")
    assert CasMatcher().candidates(flow, None, index) == []
    assert [c.flow.code for c in CasMatcher().candidates(flow, "007440-31-5", index)] == ["tin"]
    codes = [
        c.flow.code for c in CasMatcher().candidates(_air("Carbon dioxide"), "124-38-9", index)
    ]
    assert codes == [
        "co2-bio",
        "co2-fos",
        "co2-luc",
    ]  # sorted by code, disambiguation is not our job
    assert CasMatcher.tier == "cas"


def test_qualifier_matcher_rewrites_comma_qualifiers(index):
    m = QualifierMatcher()
    assert [c.flow.code for c in m.candidates(_air("Carbon dioxide, biogenic"), None, index)] == [
        "co2-bio"
    ]
    assert [
        c.flow.code for c in m.candidates(_air("Carbon dioxide, non-fossil"), None, index)
    ] == ["co2-bio"]
    assert [c.flow.code for c in m.candidates(_air("Carbon dioxide, fossil"), None, index)] == [
        "co2-fos"
    ]
    assert [
        c.flow.code for c in m.candidates(_air("Carbon dioxide, land transformation"), None, index)
    ] == ["co2-luc"]
    assert m.candidates(_air("Carbon dioxide"), None, index) == []
    assert m.candidates(_air("Carbon dioxide, weird"), None, index) == []
    assert QualifierMatcher.tier == "qualifier"


def test_qualifier_matcher_strips_the_name_before_matching(index):
    # a trailing space must not defeat the anchored regex, same as every other matcher
    assert [
        c.flow.code
        for c in QualifierMatcher().candidates(_air("Carbon dioxide, biogenic "), None, index)
    ] == ["co2-bio"]


def test_alias_matcher_uses_the_curated_table_case_insensitively(index):
    aliases = {"Particulates, < 2.5 um": "Particles (PM2.5)", "water, river": "River water"}
    m = AliasMatcher(aliases)
    assert [c.flow.code for c in m.candidates(_air("particulates, < 2.5 UM"), None, index)] == [
        "pm25"
    ]
    assert m.candidates(_air("Particulates, > 10 um"), None, index) == []
    assert AliasMatcher.tier == "alias"


def test_alias_matcher_accepts_plain_strings_and_alias_values(index):
    m = AliasMatcher({"foo": "Particles (PM2.5)", "bar": Alias(target="Particles (PM2.5)")})
    assert [c.flow.code for c in m.candidates(_air("foo"), None, index)] == ["pm25"]
    assert [c.flow.code for c in m.candidates(_air("bar"), None, index)] == ["pm25"]


def test_alias_matcher_puts_the_caveat_on_every_candidate(index):
    m = AliasMatcher({"foo": Alias(target="Particles (PM2.5)", caveat="a caveat")})
    got = m.candidates(_air("foo"), None, index)
    assert [c.caveat for c in got] == ["a caveat"]
    assert got[0].tier == "alias"
    no_caveat = AliasMatcher({"foo": "Particles (PM2.5)"})
    assert no_caveat.candidates(_air("foo"), None, index)[0].caveat is None


def test_shipped_alias_file_loads_lowercased_and_has_the_seed_entries():
    aliases = load_aliases()
    assert aliases["particulates, < 2.5 um"].target == "Particles (PM2.5)"
    assert aliases["water, river"].target == "River water"
    assert all(k == k.lower() for k in aliases)
    assert all(isinstance(v, Alias) and v.target for v in aliases.values())
    assert aliases["particulates, < 10 um"].caveat is not None


def test_shipped_alias_file_has_the_decision_a_and_c_entries():
    # decisions (a) and (c) (Laurenz, 2026-09-13): fossil water taken as non-renewable
    # groundwater, and BAFU "Nitrogen" taken as total nitrogen; both carry a caveat
    # naming the decision. "coal, hard" is a plain spelling alias (no caveat needed).
    aliases = load_aliases()
    assert aliases["water, fossil"] == Alias(
        target="Ground Water",
        caveat="fossil water taken as non-renewable groundwater (decision 2026-09-13)",
    )
    assert aliases["nitrogen"] == Alias(
        target="Nitrogen, Total (excluding N2)",
        caveat="BAFU 'Nitrogen' emitted to water taken as total nitrogen (decision 2026-09-13)",
    )
    assert aliases["coal, hard"] == Alias(target="Hard Coal")


def test_load_aliases_raises_on_missing_aliases_key(tmp_path):
    path = tmp_path / "aliases.yaml"
    path.write_text("not_aliases: {}\n", encoding="utf-8")
    with pytest.raises(ParseError):
        load_aliases(path)


def test_load_aliases_raises_on_empty_file(tmp_path):
    path = tmp_path / "aliases.yaml"
    path.write_text("", encoding="utf-8")
    with pytest.raises(ParseError):
        load_aliases(path)


def test_load_aliases_raises_on_null_value(tmp_path):
    path = tmp_path / "aliases.yaml"
    path.write_text("aliases:\n  water, well:\n", encoding="utf-8")
    with pytest.raises(ParseError):
        load_aliases(path)


def test_load_aliases_raises_on_non_string_value(tmp_path):
    path = tmp_path / "aliases.yaml"
    path.write_text("aliases:\n  water, well: 42\n", encoding="utf-8")
    with pytest.raises(ParseError):
        load_aliases(path)


def test_load_aliases_raises_on_unknown_mapping_key(tmp_path):
    # a typo like "cavaet:" must not be silently dropped
    path = tmp_path / "aliases.yaml"
    path.write_text(
        "aliases:\n  water, well:\n    target: Water\n    cavaet: typo\n", encoding="utf-8"
    )
    with pytest.raises(ParseError):
        load_aliases(path)


def test_load_aliases_happy_path_with_string_and_mapping_values(tmp_path):
    path = tmp_path / "aliases.yaml"
    path.write_text(
        "aliases:\n" "  Foo: Bar\n" "  Baz:\n" "    target: Qux\n" "    caveat: some caveat\n",
        encoding="utf-8",
    )
    aliases = load_aliases(path)
    assert aliases["foo"] == Alias(target="Bar")
    assert aliases["baz"] == Alias(target="Qux", caveat="some caveat")


def test_region_strip_matcher_records_the_iso_location(index):
    inner = [ExactNameMatcher(), AliasMatcher({"water, river": Alias("River water", "note")})]
    m = RegionStripMatcher(inner)
    got = m.candidates(BafuFlow("Water, KR", "emissions to water", "river", "m3"), None, index)
    assert [(c.flow.code, c.location, c.region, c.tier) for c in got] == [
        ("water-em", "KR", None, "region/name")
    ]
    got = m.candidates(BafuFlow("Water, river, CH", "resources", "in water", "m3"), None, index)
    assert [(c.flow.code, c.location, c.region, c.tier) for c in got] == [
        ("river-res", "CH", None, "region/alias")
    ]
    # a caveated inner alias hit keeps its caveat after the region wrap
    assert [c.caveat for c in got] == ["note"]
    got = m.candidates(BafuFlow("Water, Europe", "emissions to water", "river", "m3"), None, index)
    assert [(c.flow.code, c.location, c.region) for c in got] == [("water-em", None, "Europe")]
    got = m.candidates(BafuFlow("Water, RER", "emissions to water", "river", "m3"), None, index)
    assert [(c.flow.code, c.location, c.region) for c in got] == [("water-em", None, "RER")]
    assert m.candidates(BafuFlow("Water", "emissions to water", "river", "kg"), None, index) == []
    assert [
        (c.flow.code, c.location)
        for c in m.candidates(
            BafuFlow("Water, XX", "emissions to water", "river", "kg"), None, index
        )
    ] == [("water-em", "XX")]
    assert RegionStripMatcher.tier == "region"


def test_region_strip_does_not_treat_chemical_suffixes_as_regions(index):
    m = RegionStripMatcher([ExactNameMatcher()])
    # "Carbon tetrachloride" has no region token; a two-capital abbreviation is not an ISO code
    assert m.candidates(_air("Carbon tetrachloride"), None, index) == []
    assert m.candidates(_air("Methane, tetrachloro-, CFC-10"), None, index) == []


def test_region_strip_matcher_yields_nothing_when_no_inner_matcher_hits(index):
    # a region token is stripped ("KR"), but the stem "Water" does not exist in the air
    # bucket (bucket scoping on the stem carries through the retry) -- the loop over
    # inner matchers exhausts without a hit.
    m = RegionStripMatcher([ExactNameMatcher()])
    flow = BafuFlow("Water, KR", "emissions to air", "unspecified", "kg")
    assert m.candidates(flow, None, index) == []


def test_land_use_matcher_tier():
    assert LandUseMatcher.tier == "landuse"


def test_land_use_matcher_occupation_carries_filing_caveat(land_index):
    got = LandUseMatcher().candidates(_resource("Occupation, industrial area"), None, land_index)
    assert [c.flow.code for c in got] == ["industrial-area"]
    assert got[0].tier == "landuse"
    assert got[0].subcategory_override == "land"
    assert got[0].caveat == (
        "BAFU files this land flow under resources / unspecified; placed on EF land use"
    )


def test_land_use_matcher_keyword_is_case_insensitive(land_index):
    got = LandUseMatcher().candidates(_resource("occupation, industrial area"), None, land_index)
    assert [c.flow.code for c in got] == ["industrial-area"]


def test_land_use_matcher_no_filing_caveat_when_already_filed_under_land(land_index):
    got = LandUseMatcher().candidates(
        _resource("Occupation, dump site", sub="land"), None, land_index
    )
    assert [c.flow.code for c in got] == ["dump-site"]
    assert got[0].caveat is None


def test_land_use_matcher_collapses_a_missing_sub_class(land_index):
    got = LandUseMatcher().candidates(
        _resource("Occupation, industrial area, built up"), None, land_index
    )
    assert [c.flow.code for c in got] == ["industrial-area"]
    assert got[0].caveat == (
        "BAFU files this land flow under resources / unspecified; placed on EF land use; "
        "sub-class 'industrial area, built up' collapsed onto EF class 'Industrial Area'; "
        "EF has no flow for the sub-class"
    )


def test_land_use_matcher_collapse_caveat_only_when_already_under_land(land_index):
    got = LandUseMatcher().candidates(
        _resource("Occupation, dump site, hazardous", sub="land"), None, land_index
    )
    assert [c.flow.code for c in got] == ["dump-site"]
    assert got[0].caveat == (
        "sub-class 'dump site, hazardous' collapsed onto EF class 'Dump Site'; "
        "EF has no flow for the sub-class"
    )


def test_land_use_matcher_transformation_to(land_index):
    got = LandUseMatcher().candidates(
        _resource("Transformation, to industrial area"), None, land_index
    )
    assert [c.flow.code for c in got] == ["to-industrial-area"]


def test_land_use_matcher_class_synonym_annual_crop(land_index):
    got = LandUseMatcher().candidates(
        _resource("Occupation, annual crop, irrigated"), None, land_index
    )
    assert [c.flow.code for c in got] == ["arable-irrigated"]


def test_land_use_matcher_class_synonym_unknown(land_index):
    got = LandUseMatcher().candidates(_resource("Transformation, from unknown"), None, land_index)
    assert [c.flow.code for c in got] == ["from-unspecified"]


def test_land_use_matcher_class_synonym_natural_non_use(land_index):
    got = LandUseMatcher().candidates(
        _resource("Occupation, unspecified, natural (non-use)"), None, land_index
    )
    assert [c.flow.code for c in got] == ["unspecified-natural"]


def test_land_use_matcher_class_synonym_bare_non_use(land_index):
    got = LandUseMatcher().candidates(_resource("Occupation, non-use"), None, land_index)
    assert [c.flow.code for c in got] == ["natural-cls"]


def test_land_use_matcher_no_class_and_no_parent_is_empty(land_index):
    got = LandUseMatcher().candidates(
        _resource("Occupation, water bodies, artificial"), None, land_index
    )
    assert got == []


def test_land_use_matcher_only_applies_to_the_resource_bucket(land_index):
    flow = BafuFlow("Occupation, industrial area", "emissions to air", "unspecified", "m2a")
    assert LandUseMatcher().candidates(flow, None, land_index) == []


def test_land_use_matcher_ignores_non_land_names(land_index):
    assert LandUseMatcher().candidates(_resource("Zinc"), None, land_index) == []


def test_land_use_matcher_enforces_the_leaf_family(land_index):
    # "To Industrial Area" exists only on the transformation leaf; an occupation-shaped
    # name must not match it even though a bare by_name lookup would find it.
    got = LandUseMatcher().candidates(
        _resource("Occupation, to industrial area"), None, land_index
    )
    assert got == []


def test_land_use_matcher_refuses_a_name_with_an_unstripped_region_token(land_index):
    # the one-level collapse would otherwise mistake "CH" for a droppable sub-class
    # segment and match "Traffic Area, Rail Network" without ever recording a location;
    # region-stripping is RegionStripMatcher's job, so this matcher refuses outright.
    got = LandUseMatcher().candidates(
        _resource("Occupation, traffic area, rail network, CH", sub="land"), None, land_index
    )
    assert got == []


# --- _normalise_land_class -------------------------------------------------------


def test_normalise_land_class_maps_each_synonym_individually():
    assert _normalise_land_class("annual crop") == "arable"
    assert _normalise_land_class("unknown") == "unspecified"
    assert _normalise_land_class("natural (non-use)") == "natural"
    assert _normalise_land_class("non-use") == "natural"


def test_normalise_land_class_applies_synonyms_per_comma_segment():
    assert _normalise_land_class("annual crop, irrigated") == "arable, irrigated"
    assert _normalise_land_class("unspecified, natural (non-use)") == "unspecified, natural"


def test_normalise_land_class_leaves_non_synonym_segments_untouched():
    assert _normalise_land_class("industrial area, built up") == "industrial area, built up"


def test_normalise_land_class_strips_whitespace_and_lowercases():
    assert _normalise_land_class(" Industrial Area , Built Up ") == "industrial area, built up"


# --- LandUseMatcher._by_class -----------------------------------------------------


def test_by_class_occupation_looks_up_the_bare_class_name(land_index):
    found = LandUseMatcher._by_class(
        land_index, "occupation", "industrial area", "land occupation"
    )
    assert [f.code for f in found] == ["industrial-area"]


def test_by_class_from_and_to_prefix_the_class_name(land_index):
    found_from = LandUseMatcher._by_class(land_index, "from", "unspecified", "land transformation")
    assert [f.code for f in found_from] == ["from-unspecified"]
    found_to = LandUseMatcher._by_class(land_index, "to", "industrial area", "land transformation")
    assert [f.code for f in found_to] == ["to-industrial-area"]


def test_by_class_filters_out_the_wrong_leaf(land_index):
    # "To Industrial Area" exists only on the transformation leaf; asking for it on
    # the occupation leaf must come back empty even though the name itself resolves.
    assert (
        LandUseMatcher._by_class(land_index, "occupation", "to industrial area", "land occupation")
        == []
    )


def test_by_class_returns_a_code_sorted_list(land_index):
    # "Industrial Area" and "Arable, Irrigated" both exist; sortedness only really
    # matters once more than one flow shares a class, but the contract (see the
    # method's own docstring) is that _by_class always hands back a code-sorted list.
    found = LandUseMatcher._by_class(
        land_index, "occupation", "industrial area", "land occupation"
    )
    assert found == sorted(found, key=lambda f: f.code)


# --- OreCompositeMatcher ----------------------------------------------------------

ORE_CF = [
    cf_row("zinc", "zinc", RES_GROUND, value=1.0),
    cf_row("copper", "copper", RES_GROUND, value=1.0),
]
ORE_VOCAB = [
    vocab_row("zinc", "Zinc"),
    vocab_row("copper", "Copper"),
]


@pytest.fixture(scope="module")
def ore_index(tmp_path_factory):
    """A tiny EF index of bare-element resource flows for OreCompositeMatcher."""
    return EfFlowIndex.from_files(
        *write_ef_inputs(tmp_path_factory.mktemp("ore"), ORE_CF, ORE_VOCAB)
    )


def _ground_resource(name, unit="kg"):
    return BafuFlow(name, "resources", "in ground", unit)


def test_ore_composite_matcher_tier():
    assert OreCompositeMatcher.tier == "ore"


def test_ore_composite_matcher_decomposes_a_zinc_ore(ore_index):
    flow = _ground_resource("Zinc, Zn 0.63%, Au 9.7E-4%, Ag 9.7E-4%, Cu 0.38%, Pb 0.014%, in ore")
    got = OreCompositeMatcher().candidates(flow, None, ore_index)
    assert [c.flow.code for c in got] == ["zinc"]
    assert got[0].tier == "ore"
    assert got[0].caveat == "ecoinvent v2 ore composite; the amount is kg of Zinc"


def test_ore_composite_matcher_decomposes_a_copper_ore_with_the_crude_ore_spelling(ore_index):
    flow = _ground_resource(
        "Copper, 0.99% in sulfide, Cu 0.36% and Mo 8.2E-3% in crude ore, in ground"
    )
    got = OreCompositeMatcher().candidates(flow, None, ore_index)
    assert [c.flow.code for c in got] == ["copper"]
    assert got[0].caveat == "ecoinvent v2 ore composite; the amount is kg of Copper"


def test_ore_composite_matcher_refuses_a_compound_name(ore_index):
    # "TiO2" is not a plain capitalised element word: the leading segment names a
    # compound, and collapsing it onto an element would assert the wrong substance.
    flow = _ground_resource("TiO2, 54% in ilmenite, 2.6% in crude ore")
    assert OreCompositeMatcher().candidates(flow, None, ore_index) == []


def test_ore_composite_matcher_refuses_a_bare_element_name(ore_index):
    assert OreCompositeMatcher().candidates(_ground_resource("Copper"), None, ore_index) == []


def test_ore_composite_matcher_is_resource_bucket_only(ore_index):
    flow = BafuFlow(
        "Zinc, Zn 0.63%, Au 9.7E-4%, Ag 9.7E-4%, Cu 0.38%, Pb 0.014%, in ore",
        "emissions to air",
        "unspecified",
        "kg",
    )
    assert OreCompositeMatcher().candidates(flow, None, ore_index) == []


# --- tier-ordering invariant: land names never resolve via name/synonym/cas ------


def test_land_names_in_the_fixture_index_never_resolve_outside_the_land_tier(land_index):
    """Every land EF flow in ``land_index``, reconstructed back into its BAFU
    ``Occupation``/``Transformation`` name, must resolve through ``landuse`` or
    ``region/landuse`` (or fail outright) -- never through ``name``, ``synonym`` or
    ``cas``, which would silently place it without ever going through the land-use
    placement-override/collapse/leaf-filter rules.
    """
    pipeline = default_pipeline(land_index, {})
    land_flows = [f for f in land_index if f.leaf in ("land occupation", "land transformation")]
    assert land_flows  # sanity: the fixture actually carries land flows
    for f in land_flows:
        name = (
            f"Occupation, {f.name}" if f.leaf == "land occupation" else f"Transformation, {f.name}"
        )
        got = pipeline.match(BafuFlow(name, "resources", "unspecified", "m2a"), None)
        if isinstance(got, Match):
            assert got.tier in ("landuse", "region/landuse"), (name, got.tier)


def test_synonym_matcher_does_not_steal_a_land_use_name(tmp_path_factory):
    # a hypothetical EF resource flow whose alt_label happens to collide with a BAFU
    # land-use name ("Occupation, dump site"): SynonymMatcher must not resolve it
    # before LandUseMatcher gets a chance. LandUseMatcher is ordered right after
    # ExactNameMatcher (before SynonymMatcher) in default_pipeline's ``named`` list
    # for exactly this reason.
    cf = [
        cf_row("dump-site", "dump site", LAND_OCC, method="ef-3.1:land-use", value=1.0),
        cf_row("hijack", "hijack resource", RES_GROUND, method="ef-3.1:resource-use-fossils"),
    ]
    vocab = [
        vocab_row("dump-site", "Dump Site"),
        vocab_row("hijack", "Hijack Resource", alt=["Occupation, dump site"]),
    ]
    idx = EfFlowIndex.from_files(*write_ef_inputs(tmp_path_factory.mktemp("hijack"), cf, vocab))
    pipeline = default_pipeline(idx, {})
    got = pipeline.match(BafuFlow("Occupation, dump site", "resources", "land", "m2a"), None)
    assert isinstance(got, Match)
    assert got.tier == "landuse" and got.code == "dump-site"
