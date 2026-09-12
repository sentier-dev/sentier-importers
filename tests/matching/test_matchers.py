import pytest
from sentier_importers.core.errors import ParseError
from sentier_importers.matching.ef_index import EfFlowIndex
from sentier_importers.matching.matchers import (
    Alias,
    AliasMatcher,
    Candidate,
    CasMatcher,
    ExactNameMatcher,
    QualifierMatcher,
    RegionStripMatcher,
    SynonymMatcher,
    load_aliases,
)
from sentier_importers.sources.eaternity.bridge import BafuFlow

from tests.matching.ef_fixtures import (
    AIR_RURAL,
    AIR_UNSPEC,
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
    inner = [ExactNameMatcher(), AliasMatcher({"water, river": "River water"})]
    m = RegionStripMatcher(inner)
    got = m.candidates(BafuFlow("Water, KR", "emissions to water", "river", "m3"), None, index)
    assert [(c.flow.code, c.location, c.region, c.tier) for c in got] == [
        ("water-em", "KR", None, "region/name")
    ]
    got = m.candidates(BafuFlow("Water, river, CH", "resources", "in water", "m3"), None, index)
    assert [(c.flow.code, c.location, c.region, c.tier) for c in got] == [
        ("river-res", "CH", None, "region/alias")
    ]
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
