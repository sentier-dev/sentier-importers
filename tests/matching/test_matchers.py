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
    QualifierMatcher,
    RegionStripMatcher,
    SynonymMatcher,
    load_aliases,
)
from sentier_importers.sources.eaternity.bridge import BafuFlow

from tests.matching.ef_fixtures import (
    AIR_RURAL,
    AIR_UNSPEC,
    LAND_OCC,
    LAND_TRANS,
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
