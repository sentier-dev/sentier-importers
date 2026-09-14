import os
from pathlib import Path

import pytest
from sentier_importers.core.errors import ParseError
from sentier_importers.matching.ef_index import EfFlow, EfFlowIndex
from sentier_importers.matching.matchers import (
    Alias,
    AliasMatcher,
    Candidate,
    CarbonOxideMatcher,
    CasMatcher,
    ExactNameMatcher,
    IonStripMatcher,
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

#: Fallback guess only, never authoritative -- same layout assumption as
#: test_bw_context.py's own real-data constants. Set ``SENTIER_METHODS_CF``/
#: ``SENTIER_VOCAB_FLOWS`` to override.
_CHECKOUTS_ROOT = Path(__file__).parents[3]
_DEFAULT_CF = _CHECKOUTS_ROOT / "sentier-methods/data/01-ef-3.1/characterization-factors.parquet"
_DEFAULT_VOCAB_DIR = _CHECKOUTS_ROOT / "sentier-vocab/data/elementary-flows"
_REAL_CF = Path(os.environ.get("SENTIER_METHODS_CF", str(_DEFAULT_CF)))
_REAL_VOCAB_DIR = Path(os.environ.get("SENTIER_VOCAB_FLOWS", str(_DEFAULT_VOCAB_DIR)))
_REAL_INPUTS_AVAILABLE = _REAL_CF.exists() and _REAL_VOCAB_DIR.exists()


@pytest.fixture(scope="module")
def real_index():
    """The real, inclusive EF index, shared by every real-data test in this module --
    expensive enough (tens of seconds: ~90k flows from every sentier-vocab shard plus
    the full CF table) that each test building its own would multiply the whole
    suite's runtime for no benefit.
    """
    return EfFlowIndex.from_files(_REAL_CF, _REAL_VOCAB_DIR, include_uncharacterised=True)


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


#: Round 6, decision 2026-09-14: "Water Vapour" lists "Water" among its own many
#: generic synonyms (real EF vocab rows do exactly this), but is a DIFFERENT
#: substance from plain "Water" -- no EF flow named "Water" (by name OR by its own
#: vocab label) exists in this fixture's soil bucket at all.
_SOIL_INDUSTRIAL = "Emissions / Emissions to soil / Emissions to non-agricultural soil"
_WATER_VAPOUR_CF = [cf_row("water-vapour", "water vapour", _SOIL_INDUSTRIAL)]
_WATER_VAPOUR_VOCAB = [
    vocab_row("water-vapour", "Water Vapour", alt=["Water", "H2O", "Steam"], cas="7732-18-6"),
]


@pytest.fixture(scope="module")
def water_vapour_index(tmp_path_factory):
    return EfFlowIndex.from_files(
        *write_ef_inputs(
            tmp_path_factory.mktemp("water-vapour"), _WATER_VAPOUR_CF, _WATER_VAPOUR_VOCAB
        )
    )


def test_alias_synonym_fallback_does_not_fall_onto_an_unrelated_namesake(water_vapour_index):
    # "Water" (the alias target) is not the vocab label of ANY flow in the soil
    # bucket -- it merely appears deep in "Water Vapour"'s own synonym list. The
    # synonym fallback must require the matched flow's OWN label to equal the
    # target, not just any synonym hit, so this must resolve to nothing at all.
    m = AliasMatcher({"h2o vapor source": "Water"})
    got = m.candidates(
        BafuFlow("H2O vapor source", "emissions to soil", "industrial", "kg"),
        None,
        water_vapour_index,
    )
    assert got == []


#: A flow whose vocab pref_label genuinely differs from its JRC name (content, not
#: just case) and is kept as a synonym (no defect, no collision) -- the shape the
#: synonym fallback exists for: by_name finds nothing (the JRC name differs), but
#: the kept vocab label (now a synonym, and EfFlow.label) resolves it unambiguously.
_SILANE_CF = [cf_row("silane", "chlorosilane, trimethyl-", AIR_UNSPEC)]
_SILANE_VOCAB = [vocab_row("silane", "Chlorotrimethylsilane", cas="75-77-4")]


@pytest.fixture(scope="module")
def silane_index(tmp_path_factory):
    return EfFlowIndex.from_files(
        *write_ef_inputs(tmp_path_factory.mktemp("silane"), _SILANE_CF, _SILANE_VOCAB)
    )


def test_alias_synonym_fallback_resolves_an_unambiguous_kept_label(silane_index):
    m = AliasMatcher({"trimethylchlorosilane": "Chlorotrimethylsilane"})
    assert silane_index.by_name("Chlorotrimethylsilane", "air") == []  # JRC name differs
    got = m.candidates(_air("Trimethylchlorosilane"), None, silane_index)
    assert [c.flow.code for c in got] == ["silane"]


def test_shipped_alias_file_loads_lowercased_and_has_the_seed_entries():
    aliases = load_aliases()
    assert aliases["particulates, < 2.5 um"].target == "Particles (PM2.5)"
    assert aliases["water, river"].target == "River water"
    assert all(k == k.lower() for k in aliases)
    assert all(isinstance(v, Alias) and v.target for v in aliases.values())
    assert aliases["particulates, < 10 um"].caveat is not None


def test_shipped_alias_file_has_all_thirteen_entries_added_by_this_task():
    # decisions (c) (2026-09-13): fossil water taken as groundwater, and BAFU
    # "Nitrogen" taken as total nitrogen; both carry a caveat naming the decision. The
    # other ten are plain spelling aliases (no caveat), plus the three oxygen-demand
    # targets that carry no EF 3.1 factor today. The thirteenth (decision (b), Task 2)
    # is the /kg twin of the existing "water, process, unspecified natural origin/m3"
    # alias -- without it, the /kg flows' single EF leaf holds several
    # differently-factored candidates and the resource-branch fallback reports them
    # ambiguous_substances instead.
    aliases = load_aliases()
    expected = {
        "water, fossil": Alias(
            target="Ground Water",
            caveat=(
                "fossil water taken as groundwater (decision 2026-09-13; EF files all "
                "water resources under renewable material resources from water)"
            ),
        ),
        "nitrogen": Alias(
            target="Nitrogen, Total (excluding N2)",
            caveat=(
                "BAFU 'Nitrogen' emitted to water taken as total nitrogen (decision 2026-09-13)"
            ),
        ),
        "coal, brown": Alias(target="Brown Coal"),
        "coal, hard": Alias(target="Hard Coal"),
        "oil, crude": Alias(target="Crude Oil"),
        "1-butanol": Alias(target="Butanol"),
        "azadirachtin a+b": Alias(target="Azadirachtin"),
        "benzo(g,h,i)perylene": Alias(target="Benzo(ghi)perylene"),
        "propylene glycol methyl ether acetate": Alias(
            target="Propylene Glycol Monomethyl Ether Acetate"
        ),
        "bod5, biological oxygen demand": Alias(target="Biological Oxygen Demand"),
        "cod, chemical oxygen demand": Alias(target="Chemical Oxygen Demand"),
        "toc, total organic carbon": Alias(target="Total Organic Carbon"),
        "water, process, unspecified natural origin/kg": Alias(target="Water"),
    }
    assert len(expected) == 13
    for key, alias in expected.items():
        assert aliases[key] == alias


def test_shipped_alias_file_has_the_seven_wood_and_water_entries_added_in_round_3():
    # decision (f)(3), 2026-09-13: five standing-wood spellings onto EF's "Wood"
    # resource flow (uncharacterised in EF 3.1, so these fire only on the nomenclature
    # package), plus two more "Water" spellings ("water, unspecified" yields no entry today -- no
    # BAFU flow currently carries that exact name -- kept anyway, same as the
    # pre-existing benzo(a)anthracene alias).
    aliases = load_aliases()
    expected = {
        "wood, soft, standing": Alias(target="Wood"),
        "wood, hard, standing": Alias(target="Wood"),
        "wood, primary forest, standing": Alias(target="Wood"),
        "wood, unspecified, standing/kg": Alias(target="Wood"),
        "wood, unspecified, standing/m3": Alias(target="Wood"),
        "water/m3": Alias(target="Water"),
        "water, unspecified": Alias(target="Water"),
    }
    assert len(expected) == 7
    for key, alias in expected.items():
        assert aliases[key] == alias


@pytest.mark.skipif(not _REAL_INPUTS_AVAILABLE, reason="real EF inputs not available")
def test_round_3_alias_targets_exist_in_the_inclusive_index(real_index):
    # "Wood" and "Water" (the only two distinct targets among the seven new round-3
    # aliases) must both actually resolve in the inclusive (include_uncharacterised)
    # index, in whichever bucket their BAFU names place in -- "Wood" and "water/m3"
    # are uncharacterised (nomenclature package only); "water, unspecified" targets the
    # same characterised resource "Water" flow the pre-existing water aliases already use.
    assert real_index.by_name("Wood", "resource")
    assert real_index.by_name("Water", "resource")
    assert real_index.by_name("Water", "air")


def test_shipped_alias_file_has_the_nineteen_entries_added_in_round_4():
    # round 4, decision 2026-09-13: nine plain-spelling aliases (no caveat), four
    # decision-carrying spellings, the two TiO2 -> Titanium ore-composite-shaped
    # names OreCompositeMatcher itself does not decompose (see ef_units.STOICHIOMETRIC
    # for the mass-fraction factor), and four land-use-class aliases onto the nearest
    # EF land-use flow. "benzo(a)anthracene" replaces the earlier, dead
    # "benzo[a]anthracene" (square brackets) target -- EF 3.1 has no such flow at all;
    # "BENZ(a)ANTHRACENE" is the real, characterised pref_label.
    aliases = load_aliases()
    expected = {
        "propane, 1,1,1,3,3-pentafluoro-, hfc-245fa": Alias(target="1,1,1,3,3-pentafluoropropane"),
        "chlorosilane, trimethyl-": Alias(target="Chlorotrimethylsilane"),
        "toluene, 2-chloro-": Alias(target="O-chlorotoluene"),
        "thiazole, 2-(thiocyanatemethylthio)benzo-": Alias(
            target="2-(Thiocyanomethylthio)benzothiazole"
        ),
        "disodium acid methane arsenate": Alias(target="Disodium Methylarsonate"),
        "benzo(a)anthracene": Alias(target="BENZ(a)ANTHRACENE"),
        "heat, waste": Alias(target="Waste Heat"),
        "particulates, > 10 um": Alias(target="Particles (> PM10)"),
        "energy, from hydro power": Alias(target="Primary Energy From Hydro Power"),
        "chlormequat": Alias(
            target="Chlormequat Chloride",
            caveat="commercial chloride form of the same active ingredient (decision 2026-09-13)",
        ),
        "primisulfuron": Alias(
            target="Primisulfuron-methyl",
            caveat="methyl ester is the active ingredient as sold (decision 2026-09-13)",
        ),
        "tributylstannane": Alias(
            target="Tributyltin",
            caveat="tributyltin hydride taken as the tributyltin cation (decision 2026-09-13)",
        ),
        "benzene (as btex)": Alias(
            target="Benzene",
            caveat="BTEX reported as benzene taken as benzene (decision 2026-09-13)",
        ),
        "tio2, 54% in ilmenite, 2.6% in crude ore": Alias(target="Titanium"),
        "tio2, 95% in rutile, 0.40% in crude ore": Alias(target="Titanium"),
        "occupation, water courses, artificial": Alias(
            target="Inland Water Bodies",
            caveat="nearest EF land-use class (decision 2026-09-13)",
        ),
        "transformation, to water courses, artificial": Alias(
            target="To Inland Water Bodies",
            caveat="nearest EF land-use class (decision 2026-09-13)",
        ),
        "transformation, from sea and ocean": Alias(
            target="From Seabed", caveat="nearest EF land-use class (decision 2026-09-13)"
        ),
        "transformation, to sea and ocean": Alias(
            target="To Seabed", caveat="nearest EF land-use class (decision 2026-09-13)"
        ),
    }
    assert len(expected) == 19
    for key, alias in expected.items():
        assert aliases[key] == alias


@pytest.mark.skipif(not _REAL_INPUTS_AVAILABLE, reason="real EF inputs not available")
def test_round_4_alias_targets_exist_in_the_inclusive_index(real_index):
    # every distinct target among the nineteen new round-4 aliases must resolve in
    # the inclusive (include_uncharacterised) index, in whichever bucket its BAFU name
    # places in. Only "Waste Heat", "Particles (> PM10)", "Primary Energy From Hydro
    # Power" and the four land-use targets are uncharacterised (nomenclature package
    # only); the other eleven carry factors and fire from the matched package.
    #
    # round 6, decision 2026-09-14 (third cut): a characterised flow's matching
    # key is now the CF table's own JRC spelling, not the vocab pref_label these
    # targets were curated against; AliasMatcher itself falls back to a synonym
    # hit only when it can do so unambiguously (every hit's OWN label equals the
    # target, and every hit is the same (name, cas) identity), so "resolves" here
    # mirrors that exact rule rather than a loose by_name-or-by_synonym truthiness
    # check -- and asserts the resolved identity (CAS or code), not just that
    # *something* came back.
    index = real_index

    def resolves(target: str, bucket: str) -> list[EfFlow]:
        by_name = index.by_name(target, bucket)
        if by_name:
            return by_name
        target_lower = target.strip().lower()
        hits = [
            f for f in index.by_synonym(target, bucket) if f.label.strip().lower() == target_lower
        ]
        return hits if len({(f.name.strip().lower(), f.cas) for f in hits}) == 1 else []

    for target, bucket in [
        ("1,1,1,3,3-pentafluoropropane", "air"),
        ("Chlorotrimethylsilane", "air"),
        ("O-chlorotoluene", "air"),
        ("2-(Thiocyanomethylthio)benzothiazole", "air"),
        ("Disodium Methylarsonate", "air"),
        ("BENZ(a)ANTHRACENE", "air"),
        ("Waste Heat", "air"),
        ("Particles (> PM10)", "air"),
        ("Primary Energy From Hydro Power", "resource"),
        ("Chlormequat Chloride", "air"),
        ("Primisulfuron-methyl", "air"),
        ("Tributyltin", "air"),
        ("Benzene", "air"),
        ("Titanium", "resource"),
        ("Inland Water Bodies", "resource"),
        ("To Inland Water Bodies", "resource"),
        ("From Seabed", "resource"),
        ("To Seabed", "resource"),
    ]:
        hits = resolves(target, bucket)
        assert hits, f"{target!r} in {bucket!r} did not resolve"
        # every hit is the same identity: a real resolution, not several substances
        assert len({(f.name.strip().lower(), f.cas) for f in hits}) == 1
        assert all(f.code for f in hits)


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
        "aliases:\n  Foo: Bar\n  Baz:\n    target: Qux\n    caveat: some caveat\n",
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
    # round 6, decision 2026-09-14 (third cut): the EF class name embedded in the
    # caveat is the matching key, which agrees with the vocab pref_label here
    # except for case -- _pick_jrc_name prefers the vocab casing ("Industrial
    # Area"), same as before this round.
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
    assert (
        got[0].caveat
        == "ore composite of the BAFU-2026 source nomenclature; the amount is kg of Zinc"
    )


def test_ore_composite_matcher_decomposes_a_copper_ore_with_the_crude_ore_spelling(ore_index):
    flow = _ground_resource(
        "Copper, 0.99% in sulfide, Cu 0.36% and Mo 8.2E-3% in crude ore, in ground"
    )
    got = OreCompositeMatcher().candidates(flow, None, ore_index)
    assert [c.flow.code for c in got] == ["copper"]
    assert (
        got[0].caveat
        == "ore composite of the BAFU-2026 source nomenclature; the amount is kg of Copper"
    )


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


def test_synonym_matcher_does_not_steal_an_ore_composite_name(tmp_path_factory):
    # a hypothetical EF resource flow whose alt_label happens to collide with the ore
    # composite's own name: SynonymMatcher must not resolve it before
    # OreCompositeMatcher gets a chance. OreCompositeMatcher is ordered right after
    # LandUseMatcher (before SynonymMatcher) in default_pipeline's ``named`` list for
    # exactly this reason -- it would return tier "synonym" (onto the wrong,
    # non-decomposed flow) instead of "ore" (onto the bare element) otherwise.
    name = "Zinc, Zn 0.63%, Au 9.7E-4%, Ag 9.7E-4%, Cu 0.38%, Pb 0.014%, in ore"
    cf = [
        cf_row("zinc", "zinc", RES_GROUND, value=1.0),
        cf_row("hijack", "hijack resource", RES_GROUND, value=1.0),
    ]
    vocab = [
        vocab_row("zinc", "Zinc"),
        vocab_row("hijack", "Hijack Resource", alt=[name]),
    ]
    idx = EfFlowIndex.from_files(*write_ef_inputs(tmp_path_factory.mktemp("ore-order"), cf, vocab))
    pipeline = default_pipeline(idx, {})
    got = pipeline.match(BafuFlow(name, "resources", "in ground", "kg"), None)
    assert isinstance(got, Match)
    assert got.tier == "ore" and got.code == "zinc"


def test_ore_composite_matcher_restricts_to_the_element_resources_leaf(tmp_path_factory):
    # two EF flows named "Zinc" in the resource bucket on different leafs: the
    # decomposition must resolve onto the element-resources leaf only, never onto a
    # same-named flow filed under an unrelated resource leaf (e.g. energy resources).
    cf = [
        cf_row("zinc-element", "zinc", RES_GROUND, value=1.0),
        cf_row(
            "zinc-energy",
            "zinc",
            "Resources / Resources from ground / Non-renewable energy resources from ground",
            value=1.0,
        ),
    ]
    vocab = [vocab_row("zinc-element", "Zinc"), vocab_row("zinc-energy", "Zinc")]
    idx = EfFlowIndex.from_files(*write_ef_inputs(tmp_path_factory.mktemp("ore-leaf"), cf, vocab))
    flow = _ground_resource("Zinc, Zn 0.63%, Au 9.7E-4%, Ag 9.7E-4%, Cu 0.38%, Pb 0.014%, in ore")
    got = OreCompositeMatcher().candidates(flow, None, idx)
    assert [c.flow.code for c in got] == ["zinc-element"]


# --- IonStripMatcher ---------------------------------------------------------------

ION_CF = [
    cf_row("arsenic", "arsenic", WATER_FRESH, value=1.0),
    cf_row("calcium", "calcium", WATER_FRESH, value=1.0),
    cf_row("copper", "copper", WATER_FRESH, value=1.0),
    cf_row("perchlorate", "perchlorate", WATER_FRESH, value=1.0),
    cf_row("cr6", "chromium(6+)", WATER_FRESH, value=50.0),
]
ION_VOCAB = [
    vocab_row("arsenic", "Arsenic"),
    vocab_row("calcium", "Calcium"),
    vocab_row("copper", "Copper"),
    vocab_row("perchlorate", "Perchlorate"),
    vocab_row("cr6", "Chromium(6+)", alt=["Chromium VI"], cas="18540-29-9"),
]


@pytest.fixture(scope="module")
def ion_index(tmp_path_factory):
    """A tiny EF index of bare-element/anion water flows for IonStripMatcher."""
    return EfFlowIndex.from_files(
        *write_ef_inputs(tmp_path_factory.mktemp("ion"), ION_CF, ION_VOCAB)
    )


def _water(name, sub="river", unit="kg"):
    return BafuFlow(name, "emissions to water", sub, unit)


def test_ion_strip_matcher_tier():
    assert IonStripMatcher.tier == "ion"


@pytest.mark.parametrize(
    "name,code",
    [
        ("Arsenic, ion", "arsenic"),
        ("Calcium II", "calcium"),
        ("Copper ion", "copper"),
        ("Perchlorate, ion", "perchlorate"),
    ],
)
def test_ion_strip_matcher_strips_the_marker_and_matches_the_bare_stem(ion_index, name, code):
    got = IonStripMatcher().candidates(_water(name), None, ion_index)
    assert [c.flow.code for c in got] == [code]
    assert got[0].tier == "ion"
    assert got[0].caveat == (
        "ion-form source name mapped onto the EF element flow (decision 2026-09-13)"
    )


def test_ion_strip_matcher_yields_nothing_for_a_name_with_no_marker(ion_index):
    assert IonStripMatcher().candidates(_water("Copper"), None, ion_index) == []


def test_ion_strip_matcher_yields_nothing_when_ef_has_no_matching_stem(ion_index):
    assert IonStripMatcher().candidates(_water("Silver, ion"), None, ion_index) == []


def test_ion_strip_matcher_defers_when_the_source_cas_names_a_species_specific_flow(ion_index):
    # "Chromium VI" strips to "Chromium" just like any other ion-shaped name, but its
    # CAS (18540-29-9, shared with EF's own "Chromium(6+)") already names a
    # species-shaped EF flow in this bucket -- IonStripMatcher must defer (``[]``)
    # rather than collapse onto plain "Chromium" and steal the better match away from
    # CasMatcher, which runs later in the pipeline. This is the real BAFU-2026 shape:
    # "Chromium VI" carries no synonym of its own onto "Chromium(6+)" in sentier-vocab,
    # only a shared CAS.
    got = IonStripMatcher().candidates(_water("Chromium VI"), "18540-29-9", ion_index)
    assert got == []


def test_ion_strip_matcher_does_not_defer_without_a_species_specific_cas_match(ion_index):
    # "Copper ion"'s CAS (7440-50-8) names only the bare "Copper" flow (no species
    # marker) -- nothing to defer to, so the collapse still happens.
    got = IonStripMatcher().candidates(_water("Copper ion"), "7440-50-8", ion_index)
    assert [c.flow.code for c in got] == ["copper"]


#: The real BAFU-2026 shape: "Chromium VI" carries no synonym of its own in
#: sentier-vocab onto "Chromium(6+)" -- only a shared CAS. A separate index (no
#: ``alt=["Chromium VI"]``) so this scenario is not accidentally rescued by the
#: synonym tier instead of the CAS-aware defer under test.
_CR_CAS_ONLY_CF = [
    cf_row("chromium", "chromium", WATER_FRESH, value=1.0),
    cf_row("cr6-cas-only", "chromium(6+)", WATER_FRESH, value=50.0),
]
_CR_CAS_ONLY_VOCAB = [
    vocab_row("chromium", "Chromium", cas="7440-47-3"),
    vocab_row("cr6-cas-only", "Chromium(6+)", cas="18540-29-9"),
]


@pytest.fixture(scope="module")
def cr_cas_only_index(tmp_path_factory):
    return EfFlowIndex.from_files(
        *write_ef_inputs(
            tmp_path_factory.mktemp("cr-cas-only"), _CR_CAS_ONLY_CF, _CR_CAS_ONLY_VOCAB
        )
    )


def test_chromium_vi_resolves_via_cas_not_ion_strip(cr_cas_only_index):
    # decision (d)'s own worked example: at the full-pipeline level, "Chromium VI"
    # (no exact/synonym route, only a shared CAS) must still resolve onto EF's
    # species-specific "Chromium(6+)" via CasMatcher, not collapse onto plain
    # "Chromium" via IonStripMatcher -- the CAS-aware defer above is what keeps this
    # true now that ion-strip runs ahead of CasMatcher in tier order.
    pipeline = default_pipeline(cr_cas_only_index, {})
    got = pipeline.match(_water("Chromium VI"), "18540-29-9")
    assert got.code == "cr6-cas-only" and got.tier == "cas"


def test_chromium_vi_resolves_via_synonym_before_ion_strip_ever_runs(ion_index):
    # when EF *does* carry "Chromium VI" as a synonym of a species-specific flow
    # (unlike the real BAFU-2026 shape, tested above via CAS), the earlier
    # SynonymMatcher tier wins even before CasMatcher, let alone ion-strip.
    pipeline = default_pipeline(ion_index, {})
    got = pipeline.match(_water("Chromium VI"), None)
    assert got.code == "cr6" and got.tier == "synonym"


# --- CarbonOxideMatcher --------------------------------------------------------------

CO_CF = [
    cf_row("co2-fos-air", "carbon dioxide (fossil)", AIR_UNSPEC, value=1.0),
    cf_row("co-fos-air", "carbon monoxide (fossil)", AIR_UNSPEC, value=1.0),
    cf_row("co2-bio-res", "carbon dioxide (biogenic)", RES_WATER, value=1.0),
]
CO_VOCAB = [
    vocab_row("co2-fos-air", "Carbon dioxide (fossil)", cas="124-38-9"),
    vocab_row("co-fos-air", "Carbon monoxide (fossil)", cas="630-08-0"),
    vocab_row("co2-bio-res", "carbon dioxide (biogenic)", cas="124-38-9"),
]


@pytest.fixture(scope="module")
def co_index(tmp_path_factory):
    """A tiny EF index of qualified carbon-oxide flows for CarbonOxideMatcher."""
    return EfFlowIndex.from_files(*write_ef_inputs(tmp_path_factory.mktemp("co"), CO_CF, CO_VOCAB))


def test_carbon_oxide_matcher_tier():
    assert CarbonOxideMatcher.tier == "carbon-oxide"


def test_carbon_oxide_matcher_rewrites_bare_carbon_dioxide_emission_to_fossil(co_index):
    flow = BafuFlow("Carbon dioxide", "emissions to air", "unspecified", "kg")
    got = CarbonOxideMatcher().candidates(flow, None, co_index)
    assert [c.flow.code for c in got] == ["co2-fos-air"]
    assert got[0].caveat == "unqualified carbon oxide taken as fossil (decision 2026-09-13)"


def test_carbon_oxide_matcher_rewrites_bare_carbon_monoxide_emission_to_fossil(co_index):
    flow = BafuFlow("Carbon monoxide", "emissions to air", "high. pop.", "kg")
    got = CarbonOxideMatcher().candidates(flow, None, co_index)
    assert [c.flow.code for c in got] == ["co-fos-air"]
    assert got[0].caveat == "unqualified carbon oxide taken as fossil (decision 2026-09-13)"


def test_carbon_oxide_matcher_rewrites_co2_uptake_resource_flow_to_biogenic(co_index):
    flow = BafuFlow("Carbon dioxide, in air", "resources", "unspecified", "kg")
    got = CarbonOxideMatcher().candidates(flow, None, co_index)
    assert [c.flow.code for c in got] == ["co2-bio-res"]
    assert got[0].caveat == "CO2 uptake from air taken as biogenic (decision 2026-09-13)"


def test_carbon_oxide_matcher_ignores_a_bare_carbon_dioxide_outside_the_air_bucket(co_index):
    # a bare "Carbon dioxide" emission is only ever taken as fossil in the air bucket
    # (decision (e)); the same bare name to water or soil must not be rewritten here.
    flow = BafuFlow("Carbon dioxide", "emissions to water", "river", "kg")
    assert CarbonOxideMatcher().candidates(flow, None, co_index) == []


def test_carbon_oxide_matcher_ignores_an_already_qualified_name(co_index):
    flow = BafuFlow("Carbon dioxide, fossil", "emissions to air", "unspecified", "kg")
    assert CarbonOxideMatcher().candidates(flow, None, co_index) == []


def test_carbon_oxide_matcher_ignores_co2_uptake_outside_the_resource_bucket(co_index):
    flow = BafuFlow("Carbon dioxide, in air", "emissions to air", "unspecified", "kg")
    assert CarbonOxideMatcher().candidates(flow, None, co_index) == []


def test_carbon_oxide_matcher_ignores_unrelated_names(co_index):
    flow = BafuFlow("Carbon tetrachloride", "emissions to air", "unspecified", "kg")
    assert CarbonOxideMatcher().candidates(flow, None, co_index) == []
