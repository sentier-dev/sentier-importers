from sentier_importers.matching.ef_index import EfFlowIndex
from sentier_importers.matching.matchers import (
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


def _index(tmp_path):
    return EfFlowIndex.from_files(*write_ef_inputs(tmp_path, CF, VOCAB))


def _air(name, sub="low. pop."):
    return BafuFlow(name, "emissions to air", sub, "kg")


def test_exact_name_matcher(tmp_path):
    flow = BafuFlow("Tin", "emissions to water", "river", "kg")
    got = ExactNameMatcher().candidates(flow, None, _index(tmp_path))
    assert got == [Candidate(_index(tmp_path).get("tin"))]
    assert got[0].location is None and got[0].region is None
    assert ExactNameMatcher.tier == "name"


def test_exact_name_matcher_is_bucket_scoped(tmp_path):
    assert ExactNameMatcher().candidates(_air("Tin"), None, _index(tmp_path)) == []


def test_synonym_matcher(tmp_path):
    flow = BafuFlow("tin ATOM", "emissions to water", "river", "kg")
    assert [c.flow.code for c in SynonymMatcher().candidates(flow, None, _index(tmp_path))] == [
        "tin"
    ]
    assert SynonymMatcher.tier == "synonym"


def test_cas_matcher_needs_a_cas_and_returns_every_flow_sharing_it(tmp_path):
    flow = BafuFlow("Stannum", "emissions to water", "river", "kg")
    index = _index(tmp_path)
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


def test_qualifier_matcher_rewrites_comma_qualifiers(tmp_path):
    index = _index(tmp_path)
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


def test_alias_matcher_uses_the_curated_table_case_insensitively(tmp_path):
    aliases = {"Particulates, < 2.5 um": "Particles (PM2.5)", "water, river": "River water"}
    m = AliasMatcher(aliases)
    assert [
        c.flow.code for c in m.candidates(_air("particulates, < 2.5 UM"), None, _index(tmp_path))
    ] == ["pm25"]
    assert m.candidates(_air("Particulates, > 10 um"), None, _index(tmp_path)) == []
    assert AliasMatcher.tier == "alias"


def test_shipped_alias_file_loads_lowercased_and_has_the_seed_entries():
    aliases = load_aliases()
    assert aliases["particulates, < 2.5 um"] == "Particles (PM2.5)"
    assert aliases["water, river"] == "River water"
    assert all(k == k.lower() for k in aliases)
    assert all(isinstance(v, str) and v for v in aliases.values())


def test_region_strip_matcher_records_the_iso_location(tmp_path):
    index = _index(tmp_path)
    inner = [ExactNameMatcher(), AliasMatcher({"water, river": "River water"})]
    m = RegionStripMatcher(inner)
    got = m.candidates(BafuFlow("Water, KR", "emissions to water", "river", "m3"), None, index)
    assert [(c.flow.code, c.location, c.region) for c in got] == [("water-em", "KR", None)]
    got = m.candidates(BafuFlow("Water, river, CH", "resources", "in water", "m3"), None, index)
    assert [(c.flow.code, c.location, c.region) for c in got] == [("river-res", "CH", None)]
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


def test_region_strip_does_not_treat_chemical_suffixes_as_regions(tmp_path):
    index = _index(tmp_path)
    m = RegionStripMatcher([ExactNameMatcher()])
    # "Carbon tetrachloride" has no region token; a two-capital abbreviation is not an ISO code
    assert m.candidates(_air("Carbon tetrachloride"), None, index) == []
    assert m.candidates(_air("Methane, tetrachloro-, CFC-10"), None, index) == []
