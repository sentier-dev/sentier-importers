import os
from pathlib import Path

import pytest
from sentier_importers.core.errors import ParseError
from sentier_importers.matching.ef_index import (
    EfFlow,
    EfFlowIndex,
    LabelDefect,
    load_label_defects,
    normalise_cas,
)

from tests.matching.ef_fixtures import (
    AIR_RURAL,
    AIR_UNSPEC,
    AIR_URBAN,
    LAND_OCC,
    RES_WATER,
    WATER_FRESH,
    WATER_UNSPEC,
    cf_row,
    vocab_row,
    write_ef_inputs,
)

#: same real-data fallback-guess pattern as test_matchers.py's own constants.
_CHECKOUTS_ROOT = Path(__file__).parents[3]
_DEFAULT_CF = _CHECKOUTS_ROOT / "sentier-methods/data/01-ef-3.1/characterization-factors.parquet"
_DEFAULT_VOCAB_DIR = _CHECKOUTS_ROOT / "sentier-vocab/data/elementary-flows"
_REAL_CF = Path(os.environ.get("SENTIER_METHODS_CF", str(_DEFAULT_CF)))
_REAL_VOCAB_DIR = Path(os.environ.get("SENTIER_VOCAB_FLOWS", str(_DEFAULT_VOCAB_DIR)))
_REAL_INPUTS_AVAILABLE = _REAL_CF.exists() and _REAL_VOCAB_DIR.exists()


@pytest.fixture(scope="module")
def real_index():
    """The real, inclusive EF index -- expensive (tens of seconds: ~90k flows read
    from every sentier-vocab shard plus the full CF table), so every real-data test
    in this module shares this one module-scoped build instead of paying for its
    own.
    """
    return EfFlowIndex.from_files(_REAL_CF, _REAL_VOCAB_DIR, include_uncharacterised=True)


LAND_TRANS = "Land use / Land transformation"

CF = [
    cf_row("cu-urban", "copper", AIR_URBAN, value=2.0),
    cf_row("cu-urban", "copper", AIR_URBAN, method="ef-3.1:ecotoxicity-freshwater", value=5.0),
    cf_row(
        "cu-urban",
        "copper",
        AIR_URBAN,
        method="ef-3.1:ecotoxicity-freshwater",
        value=7.0,
        location="DE",
    ),
    cf_row("cu-urban", "copper", AIR_URBAN, method="ef-3.1:acidification", value=0.1 + 0.2),
    cf_row(
        "cu-urban", "copper", AIR_URBAN, method="ef-3.1:ozone-depletion", value=1.000000000000123
    ),
    cf_row("cu-rural", "copper", AIR_RURAL, value=2.0),
    cf_row("cu-unspec", "copper", AIR_UNSPEC, value=2.0),
    cf_row("ccl4", "carbon tetrachloride", AIR_UNSPEC, value=3.0),
    cf_row("cfc10", "cfc-10", AIR_UNSPEC, value=3.0),
    cf_row("occ", "arable", LAND_OCC, method="ef-3.1:land-use", value=1.0),
    cf_row("nullctx", "ghost", None),
    cf_row("gas-mj", "natural gas", AIR_UNSPEC, method="ef-3.1:resource-use-fossils", value=1.0),
    cf_row(
        "rn-test",
        "radon-222",
        AIR_UNSPEC,
        method="ef-3.1:ionising-radiation-human-health",
        value=1.0,
    ),
    cf_row("water-test", "water", RES_WATER, method="ef-3.1:water-use", value=1.0),
    cf_row(
        "land-trans",
        "from arable",
        LAND_TRANS,
        method="ef-3.1:land-use",
        value=1.0,
    ),
]
VOCAB = [
    vocab_row("cu-urban", "Copper", alt=["Cu"], cas="007440-50-8"),
    vocab_row("cu-rural", "Copper", alt=None, cas="7440-50-8"),
    vocab_row("cu-unspec", "Copper", cas="7440-50-8"),
    vocab_row(
        "ccl4", "Carbon tetrachloride", alt=["Methane, tetrachloro-, CFC-10"], cas="56-23-5"
    ),
    vocab_row("cfc10", "CFC-10", cas="56-23-5"),
    vocab_row("bafu-x", "Copper", source="https://vocab.sentier.dev/sources/bafu-2026"),
    vocab_row("nocf", "Unicorn dust", cas="1-1-1"),
]


def test_normalise_cas():
    assert normalise_cas("007440-50-8") == "7440-50-8"
    assert normalise_cas("7440-50-8") == "7440-50-8"
    assert normalise_cas(" 000056-23-5 ") == "56-23-5"
    assert normalise_cas("") is None
    assert normalise_cas("   ") is None
    assert normalise_cas(None) is None


def test_index_from_files_joins_cf_table_and_vocab(tmp_path):
    cf, vocab = write_ef_inputs(tmp_path, CF, VOCAB)
    index = EfFlowIndex.from_files(cf, vocab)
    flow = index.get("cu-urban")
    assert isinstance(flow, EfFlow)
    # round 6, decision 2026-09-14 (third cut): the matching key is the CF table's
    # own JRC spelling, not the vocab pref_label -- but here they agree in
    # substance and differ only in case, so _pick_jrc_name deliberately prefers
    # the vocab casing ("Copper", not "copper"): a real content difference would
    # still win as the matching key, but this is not one. The vocab label is not
    # added as a synonym either (it is now identical to the name, not just a
    # case-insensitive lookup match), and the displayed label defaults to the
    # (already vocab-cased) name.
    assert flow.name == "Copper"
    assert flow.label == "Copper"
    assert flow.context == (
        "Emissions",
        "Emissions to air",
        "Emissions to urban air close to ground",
    )
    assert flow.context_path == AIR_URBAN
    assert flow.leaf == "emissions to urban air close to ground"
    assert flow.bucket == "air"
    assert flow.synonyms == ("Cu",)
    assert flow.cas == "7440-50-8"


def test_vocab_row_with_null_alt_labels_yields_no_synonyms(tmp_path):
    index = EfFlowIndex.from_files(*write_ef_inputs(tmp_path, CF, VOCAB))
    assert index.get("cu-rural").synonyms == ()


def test_vector_is_the_global_factor_per_method_rounded_to_12_digits(tmp_path):
    index = EfFlowIndex.from_files(*write_ef_inputs(tmp_path, CF, VOCAB))
    # the DE-located row must not overwrite the global (location-less) factor
    vector = index.vector("cu-urban")
    assert vector["ef-3.1:human-toxicity-cancer"] == 2.0
    assert vector["ef-3.1:ecotoxicity-freshwater"] == 5.0
    # 0.1 + 0.2 is not exactly representable; rounding to 12 sig figs makes it compare equal
    assert vector["ef-3.1:acidification"] == 0.3
    # a 15-digit value rounds away its insignificant trailing digits
    assert vector["ef-3.1:ozone-depletion"] == 1.0
    assert index.vector("unknown") == {}


def test_vector_accumulates_location_less_factors_regardless_of_context(tmp_path):
    # method A's row carries the flow's context; method B's row is location-less but has
    # no context of its own -- its factor must still land in the vector once the flow is
    # indexed via method A's row.
    rows = [
        cf_row("dual", "diflow", AIR_UNSPEC, method="ef-3.1:method-a", value=1.0),
        cf_row("dual", "diflow", None, method="ef-3.1:method-b", value=2.0),
    ]
    index = EfFlowIndex.from_files(*write_ef_inputs(tmp_path, rows, []))
    assert index.vector("dual") == {"ef-3.1:method-a": 1.0, "ef-3.1:method-b": 2.0}


def test_from_tables_raises_parse_error_on_bad_factor_value():
    rows = [cf_row("bad", "ghost", AIR_UNSPEC, value="abc")]
    with pytest.raises(ParseError):
        EfFlowIndex.from_tables(rows, [])


def test_only_cf_bearing_ef_flows_are_indexed(tmp_path):
    index = EfFlowIndex.from_files(*write_ef_inputs(tmp_path, CF, VOCAB))
    assert index.get("bafu-x") is None  # a BAFU vocab row, not EF
    assert index.get("nocf") is None  # EF vocab row without any factor
    assert index.get("nullctx") is None  # a CF row with no context cannot be placed
    assert len(index) == 10
    # a context-less flow must not leave an orphan vector/identity behind either
    assert index.vector("nullctx") == {}
    assert index.identity("nullctx") == ()


def test_lookups_are_case_insensitive_and_bucket_scoped(tmp_path):
    index = EfFlowIndex.from_files(*write_ef_inputs(tmp_path, CF, VOCAB))
    assert {f.code for f in index.by_name("COPPER", "air")} == {
        "cu-urban",
        "cu-rural",
        "cu-unspec",
    }
    assert index.by_name("copper", "water") == []
    assert {f.code for f in index.by_synonym("cu", "air")} == {"cu-urban"}
    assert {f.code for f in index.by_cas("000056-23-5", "air")} == {"ccl4", "cfc10"}
    assert index.by_cas(None, "air") == []
    assert {f.code for f in index.by_synonym("methane, tetrachloro-, cfc-10", "air")} == {"ccl4"}


def test_by_name_any_bucket_finds_a_namesake_regardless_of_bucket(tmp_path):
    # round 5, decision 2026-09-14: unlike every other lookup on this index,
    # by_name_any_bucket ignores bucket entirely -- exactly what the nomenclature
    # package's name-only alignment needs (mappings_biosphere_matched._name_only_match).
    cf = [
        cf_row("widget-air", "widget", AIR_UNSPEC, value=1.0),
        cf_row("widget-land", "widget", LAND_OCC, method="ef-3.1:land-use", value=1.0),
    ]
    vocab = [vocab_row("widget-air", "Widget"), vocab_row("widget-land", "Widget")]
    index = EfFlowIndex.from_files(*write_ef_inputs(tmp_path, cf, vocab))
    # case/whitespace-insensitive, like every other lookup, and code-sorted
    assert [f.code for f in index.by_name_any_bucket(" WIDGET ")] == ["widget-air", "widget-land"]
    assert index.by_name_any_bucket("nonexistent") == []
    # the ordinary bucket-scoped lookup still only ever sees its own bucket
    assert {f.code for f in index.by_name("widget", "air")} == {"widget-air"}


def test_names_and_synonyms_are_stripped_of_whitespace(tmp_path):
    rows = [cf_row("lonely2", "ozone\xa0", AIR_UNSPEC)]
    index = EfFlowIndex.from_files(*write_ef_inputs(tmp_path, rows, []))
    assert {f.code for f in index.by_name("Ozone", "air")} == {"lonely2"}


def test_land_context_is_a_resource(tmp_path):
    index = EfFlowIndex.from_files(*write_ef_inputs(tmp_path, CF, VOCAB))
    assert index.get("occ").bucket == "resource"
    assert index.by_name("arable", "resource")[0].code == "occ"


def test_flow_without_vocab_row_keeps_the_cf_table_name(tmp_path):
    index = EfFlowIndex.from_files(
        *write_ef_inputs(tmp_path, [cf_row("lonely", "ozone\xa0", AIR_UNSPEC)], [])
    )
    assert index.get("lonely").name == "ozone"
    assert index.by_name("Ozone", "air")[0].code == "lonely"
    assert index.get("lonely").synonyms == () and index.get("lonely").cas is None


def test_results_are_sorted_by_code_for_determinism(tmp_path):
    index = EfFlowIndex.from_files(*write_ef_inputs(tmp_path, CF, VOCAB))
    assert [f.code for f in index.by_name("copper", "air")] == [
        "cu-rural",
        "cu-unspec",
        "cu-urban",
    ]


def test_identity_is_a_hashable_cf_vector_key(tmp_path):
    rows = [
        cf_row("id-a", "same", AIR_UNSPEC, method="ef-3.1:m1", value=1.0),
        cf_row("id-a", "same", AIR_UNSPEC, method="ef-3.1:m2", value=2.0),
        cf_row("id-b", "same", AIR_UNSPEC, method="ef-3.1:m2", value=2.0),
        cf_row("id-b", "same", AIR_UNSPEC, method="ef-3.1:m1", value=1.0),
    ]
    index = EfFlowIndex.from_files(*write_ef_inputs(tmp_path, rows, []))
    assert index.identity("id-a") == index.identity("id-b")
    assert index.identity("id-a") == (("ef-3.1:m1", 1.0), ("ef-3.1:m2", 2.0))
    assert index.identity("unknown") == ()


def test_iter_yields_all_flows_sorted_by_code(tmp_path):
    index = EfFlowIndex.from_files(*write_ef_inputs(tmp_path, CF, VOCAB))
    codes = [f.code for f in index]
    assert sorted(codes) == codes
    assert len(codes) == len(index)


def test_from_files_reads_all_vocab_shards(tmp_path):
    index = EfFlowIndex.from_files(*write_ef_inputs(tmp_path, CF, VOCAB, shards=2))
    assert index.get("cu-urban").synonyms == ("Cu",)
    assert index.get("ccl4").cas == "56-23-5"
    assert len(index) == 10


def test_from_bytes_reads_the_cf_table_from_memory(tmp_path):
    cf, vocab = write_ef_inputs(tmp_path, CF, VOCAB)
    index = EfFlowIndex.from_bytes(cf.read_bytes(), vocab)
    assert index.get("cu-urban").name == "Copper"  # vocab casing preferred; see above
    assert index.get("cu-urban").synonyms == ("Cu",)
    assert len(index) == len(EfFlowIndex.from_files(cf, vocab))


def test_reference_unit_is_megajoule_for_resource_use_fossils(tmp_path):
    index = EfFlowIndex.from_files(*write_ef_inputs(tmp_path, CF, VOCAB))
    assert index.reference_unit("gas-mj") == "megajoule"


def test_reference_unit_is_kbq_for_ionising_radiation(tmp_path):
    index = EfFlowIndex.from_files(*write_ef_inputs(tmp_path, CF, VOCAB))
    assert index.reference_unit("rn-test") == "kBq"


def test_reference_unit_is_cubic_meter_for_water_use(tmp_path):
    index = EfFlowIndex.from_files(*write_ef_inputs(tmp_path, CF, VOCAB))
    assert index.reference_unit("water-test") == "cubic meter"


def test_reference_unit_is_area_or_area_time_for_land_use(tmp_path):
    # keyed on the EF context leaf, not the flow name: EF names occupation flows
    # "Arable", "Pasture/meadow" etc., never anything starting with "occupation"
    index = EfFlowIndex.from_files(*write_ef_inputs(tmp_path, CF, VOCAB))
    assert index.get("occ").leaf == "land occupation"
    assert index.reference_unit("occ") == "m2*a"  # "arable", leaf "land occupation"
    assert index.get("land-trans").leaf == "land transformation"
    assert index.reference_unit("land-trans") == "m2"  # "from arable", leaf "land transformation"


def test_reference_unit_defaults_to_kilogram(tmp_path):
    index = EfFlowIndex.from_files(*write_ef_inputs(tmp_path, CF, VOCAB))
    assert index.reference_unit("cu-urban") == "kilogram"  # human-toxicity-cancer et al.
    assert index.reference_unit("unknown-code") == "kilogram"  # no vector at all


# --- uncharacterised flows (phase 2 task 3) -------------------------------------

UNCHAR_VOCAB = [
    *VOCAB,
    # unambiguous crosswalk: envi-air-unkn -> air, unspecified
    vocab_row("un-air", "Uncharacterised air thing", cas="1-2-3", bw="envi-air-unkn"),
    # ambiguous crosswalk: envi-grou-unkn -> soil, unspecified (majority), flagged
    vocab_row("un-soil", "Uncharacterised soil thing", bw="envi-grou-unkn"),
    # envi-biot has no EF leaf at all: must be skipped
    vocab_row("un-biot", "Uncharacterised biotic thing", bw="envi-biot"),
    # no bw-context notation at all: must be skipped
    vocab_row("un-none", "No notation thing"),
    # a code from a family not otherwise exercised here, just to broaden coverage
    vocab_row("un-land", "Uncharacterised land thing", alt=["Land alias"], bw="laus-occu"),
]


def test_default_build_is_unaffected_by_uncharacterised_rows(tmp_path):
    # same 10 characterised flows as the plain CF/VOCAB fixture (see
    # test_only_cf_bearing_ef_flows_are_indexed), regardless of the extra
    # uncharacterised vocab rows present in the shard
    cf, vocab = write_ef_inputs(tmp_path, CF, UNCHAR_VOCAB)
    default_index = EfFlowIndex.from_files(cf, vocab)
    assert len(default_index) == 10
    assert default_index.get("un-air") is None
    assert default_index.get("un-soil") is None
    assert default_index.get("un-land") is None


def test_include_uncharacterised_adds_flows_via_bw_context(tmp_path):
    cf, vocab = write_ef_inputs(tmp_path, CF, UNCHAR_VOCAB)
    index = EfFlowIndex.from_files(cf, vocab, include_uncharacterised=True)
    flow = index.get("un-air")
    assert flow is not None
    assert flow.characterised is False
    assert flow.context_uncertain is False
    assert flow.context == (
        "Emissions",
        "Emissions to air",
        "Emissions to air, unspecified",
    )
    assert flow.bucket == "air"
    assert flow.leaf == "emissions to air, unspecified"
    assert flow.cas == "1-2-3"
    assert index.by_name("Uncharacterised air thing", "air") == [flow]
    assert index.by_cas("1-2-3", "air") == [flow]


def test_ambiguous_bw_context_code_marks_flow_uncertain(tmp_path):
    index = EfFlowIndex.from_files(
        *write_ef_inputs(tmp_path, CF, UNCHAR_VOCAB), include_uncharacterised=True
    )
    flow = index.get("un-soil")
    assert flow is not None
    assert flow.characterised is False
    assert flow.context_uncertain is True
    assert flow.bucket == "soil"


def test_envi_biot_code_is_skipped(tmp_path):
    index = EfFlowIndex.from_files(
        *write_ef_inputs(tmp_path, CF, UNCHAR_VOCAB), include_uncharacterised=True
    )
    assert index.get("un-biot") is None


def test_row_without_bw_context_notation_is_skipped(tmp_path):
    index = EfFlowIndex.from_files(
        *write_ef_inputs(tmp_path, CF, UNCHAR_VOCAB), include_uncharacterised=True
    )
    assert index.get("un-none") is None


def test_uncharacterised_flow_has_no_vector_or_identity(tmp_path):
    index = EfFlowIndex.from_files(
        *write_ef_inputs(tmp_path, CF, UNCHAR_VOCAB), include_uncharacterised=True
    )
    assert index.vector("un-air") == {}
    assert index.identity("un-air") == ()


def test_reference_unit_is_none_for_uncharacterised_flow(tmp_path):
    index = EfFlowIndex.from_files(
        *write_ef_inputs(tmp_path, CF, UNCHAR_VOCAB), include_uncharacterised=True
    )
    assert index.reference_unit("un-air") is None
    assert index.reference_unit("un-soil") is None


def test_include_uncharacterised_count_matches_expectation(tmp_path):
    index = EfFlowIndex.from_files(
        *write_ef_inputs(tmp_path, CF, UNCHAR_VOCAB), include_uncharacterised=True
    )
    # 10 characterised (from CF) + 3 uncharacterised placed (un-air, un-soil, un-land);
    # un-biot and un-none are skipped
    assert len(index) == 13
    assert {f.code for f in index if not f.characterised} == {"un-air", "un-soil", "un-land"}


def test_from_bytes_passes_include_uncharacterised_through(tmp_path):
    cf, vocab = write_ef_inputs(tmp_path, CF, UNCHAR_VOCAB)
    index = EfFlowIndex.from_bytes(cf.read_bytes(), vocab, include_uncharacterised=True)
    assert index.get("un-air") is not None
    assert index.get("un-air").characterised is False


def test_from_tables_include_uncharacterised_default_is_false():
    index = EfFlowIndex.from_tables(CF, UNCHAR_VOCAB)
    assert index.get("un-air") is None
    assert len(index) == 10


def test_characterised_row_keeps_cf_context_despite_a_conflicting_bw_notation(tmp_path):
    # Pins the ``if code in contexts: continue`` guard in ``from_tables``: a vocab row
    # for an already-characterised code must never be re-placed via its own bw-context
    # notation, even when that crosswalk disagrees with the CF table's own context.
    # Without the guard, the uncharacterised branch appends a second EfFlow for the
    # same code (built from the same vocab row) *after* the characterised one; since
    # EfFlowIndex.__init__ does ``self._flows[flow.code] = flow`` in iteration order,
    # that second entry would silently win, flipping ``characterised`` to False and
    # swapping in the wrong (water) context in place of the CF table's (air) one.
    rows = [cf_row("dual-ctx", "dual", AIR_URBAN, value=1.0)]
    vocab = [vocab_row("dual-ctx", "Dual", bw="envi-wate-suwa")]
    index = EfFlowIndex.from_files(
        *write_ef_inputs(tmp_path, rows, vocab), include_uncharacterised=True
    )
    flow = index.get("dual-ctx")
    assert flow is not None
    assert flow.characterised is True
    assert flow.context_uncertain is False
    assert flow.context_path == AIR_URBAN
    assert index.vector("dual-ctx") == {"ef-3.1:human-toxicity-cancer": 1.0}


def test_includes_uncharacterised_flag_mirrors_the_build_argument(tmp_path):
    cf, vocab = write_ef_inputs(tmp_path, CF, UNCHAR_VOCAB)
    assert EfFlowIndex.from_files(cf, vocab).includes_uncharacterised is False
    assert EfFlowIndex.from_files(cf, vocab, include_uncharacterised=True).includes_uncharacterised


@pytest.mark.parametrize(
    "name",
    [
        "Energy, geothermal, converted",
        "Energy, kinetic (in wind), converted",
        "Energy, solar, converted",
        "Energy, potential (in hydropower reservoir), converted",
        "Energy, gross calorific value, in biomass, primary forest",
        "Primary Energy From Geothermics",
        "Oil Sand (10% Bitumen)",
        "Pit Methane",
        "energy, tidal, converted",  # case-insensitive
    ],
)
def test_energy_shaped_resource_names_are_forced_uncertain_regardless_of_code(tmp_path, name):
    # reso-wate is NOT in bw_context.AMBIGUOUS_CODES on its own -- an ordinary
    # (non-energy) row placed via it stays context_uncertain=False (see the sibling
    # negative-control test below). The crosswalk simply has no code that reaches EF's
    # energy-resource leaves at all, so any of these names must come back uncertain
    # even via this otherwise-unambiguous code.
    vocab = [vocab_row("un-energy", name, bw="reso-wate")]
    index = EfFlowIndex.from_files(
        *write_ef_inputs(tmp_path, [], vocab), include_uncharacterised=True
    )
    flow = index.get("un-energy")
    assert flow is not None
    assert flow.bucket == "resource"
    assert flow.context_uncertain is True


def test_non_energy_resource_name_on_the_same_code_stays_certain(tmp_path):
    vocab = [vocab_row("un-material", "Wood", bw="reso-wate")]
    index = EfFlowIndex.from_files(
        *write_ef_inputs(tmp_path, [], vocab), include_uncharacterised=True
    )
    flow = index.get("un-material")
    assert flow is not None
    assert flow.bucket == "resource"
    assert flow.context_uncertain is False


def test_energy_shaped_name_outside_the_resource_bucket_is_not_forced_uncertain(tmp_path):
    # the override is scoped to the resource bucket only: an "Energy ..." name placed
    # via an air bw-context code is left alone (envi-air-unkn is unambiguous already).
    vocab = [vocab_row("un-energy-air", "Energy something", bw="envi-air-unkn")]
    index = EfFlowIndex.from_files(
        *write_ef_inputs(tmp_path, [], vocab), include_uncharacterised=True
    )
    flow = index.get("un-energy-air")
    assert flow is not None
    assert flow.bucket == "air"
    assert flow.context_uncertain is False


# --- label defects (round 6: sentier-vocab pref_label defects) -----------------


def test_load_label_defects_happy_path(tmp_path):
    path = tmp_path / "label_defects.yaml"
    path.write_text(
        "label_defects:\n"
        "  - cas: '1120-01-0'\n"
        "    wrong_label: sodium\n"
        "    true_name: Sodium hexadecyl sulphate\n"
        "    note: a note\n",
        encoding="utf-8",
    )
    defects = load_label_defects(path)
    assert defects == {
        "1120-01-0": LabelDefect(
            wrong_label="sodium", true_name="Sodium hexadecyl sulphate", note="a note"
        )
    }


def test_load_label_defects_raises_on_missing_top_level_key(tmp_path):
    path = tmp_path / "label_defects.yaml"
    path.write_text("not_label_defects: []\n", encoding="utf-8")
    with pytest.raises(ParseError):
        load_label_defects(path)


def test_load_label_defects_raises_on_empty_file(tmp_path):
    path = tmp_path / "label_defects.yaml"
    path.write_text("", encoding="utf-8")
    with pytest.raises(ParseError):
        load_label_defects(path)


def test_load_label_defects_raises_on_missing_field(tmp_path):
    path = tmp_path / "label_defects.yaml"
    path.write_text(
        "label_defects:\n  - cas: '1120-01-0'\n    wrong_label: sodium\n    note: a note\n",
        encoding="utf-8",
    )
    with pytest.raises(ParseError):
        load_label_defects(path)


def test_load_label_defects_raises_on_unknown_field(tmp_path):
    path = tmp_path / "label_defects.yaml"
    path.write_text(
        "label_defects:\n"
        "  - cas: '1120-01-0'\n"
        "    wrong_label: sodium\n"
        "    true_name: Sodium hexadecyl sulphate\n"
        "    note: a note\n"
        "    typo: oops\n",
        encoding="utf-8",
    )
    with pytest.raises(ParseError):
        load_label_defects(path)


def test_load_label_defects_raises_on_blank_field(tmp_path):
    path = tmp_path / "label_defects.yaml"
    path.write_text(
        "label_defects:\n"
        "  - cas: '1120-01-0'\n"
        "    wrong_label: sodium\n"
        "    true_name: ''\n"
        "    note: a note\n",
        encoding="utf-8",
    )
    with pytest.raises(ParseError):
        load_label_defects(path)


def test_load_label_defects_raises_on_non_string_field(tmp_path):
    path = tmp_path / "label_defects.yaml"
    path.write_text(
        "label_defects:\n"
        "  - cas: 1120\n"
        "    wrong_label: sodium\n"
        "    true_name: Sodium hexadecyl sulphate\n"
        "    note: a note\n",
        encoding="utf-8",
    )
    with pytest.raises(ParseError):
        load_label_defects(path)


def test_load_label_defects_raises_on_non_list_value(tmp_path):
    path = tmp_path / "label_defects.yaml"
    path.write_text("label_defects: {}\n", encoding="utf-8")
    with pytest.raises(ParseError):
        load_label_defects(path)


def test_shipped_label_defects_file_has_the_four_known_defects():
    defects = load_label_defects()
    assert defects == {
        "1120-01-0": LabelDefect(
            wrong_label="sodium",
            true_name="Sodium hexadecyl sulphate",
            note=defects["1120-01-0"].note,
        ),
        "7647-14-5": LabelDefect(
            wrong_label="sea water",
            true_name="Sodium chloride",
            note=defects["7647-14-5"].note,
        ),
        "86290-81-5": LabelDefect(
            wrong_label="8-(N-Indolyl)-2,6-dimethyl-7-octen-2-ol",
            true_name="Gasoline",
            note=defects["86290-81-5"].note,
        ),
        "29797-40-8": LabelDefect(
            wrong_label="Benzal chloride",
            true_name=(
                "mixture of 2,4-dichloro-1-methylbenzene; 1,4-dichloro-2-methylbenzene; "
                "1,2-dichloro-4-methylbenzene; 1,2-dichloro-3-methylbenzene; "
                "1,3-dichloro-2-methylbenzene"
            ),
            note=defects["29797-40-8"].note,
        ),
    }
    for defect in defects.values():
        assert "reported 2026-09-14" in defect.note


@pytest.mark.skipif(not _REAL_INPUTS_AVAILABLE, reason="real EF inputs not available")
def test_shipped_defects_true_name_matches_the_real_jrc_name(real_index):
    # every defect's true_name must actually be what JRC calls that CAS today (case-
    # insensitively) -- a curated true_name that drifts from the CF table would be
    # exactly as wrong as the vocab label it replaces. When the CAS carries no CF
    # row at all (fully uncharacterised) or JRC's own name still equals the wrong
    # label, there is nothing authoritative to check against, so the curated
    # true_name is trusted as documentation instead.
    defects = load_label_defects()
    for cas, defect in defects.items():
        jrc_names = {
            f.name.strip().lower() for f in real_index if f.cas == cas and f.characterised
        }
        if not jrc_names or jrc_names == {defect.wrong_label.strip().lower()}:
            continue  # nothing authoritative to check against; true_name is documentation
        assert jrc_names == {defect.true_name.strip().lower()}


# --- characterised naming: JRC flow_name is authoritative (round 6, second cut) --

DEFECT = LabelDefect(wrong_label="sodium", true_name="Sodium hexadecyl sulphate", note="n")


def test_characterised_name_comes_from_the_jrc_table_not_the_vocab_label(tmp_path):
    # the CF table's own flow_name ("sodium hexadecyl sulphate") wins outright; the
    # vocab pref_label ("sodium"), which differs, survives only as a synonym.
    rows = [cf_row("na-surf", "sodium hexadecyl sulphate", AIR_UNSPEC)]
    vocab = [vocab_row("na-surf", "sodium", cas="1120-01-0")]
    index = EfFlowIndex.from_tables(rows, vocab, label_defects={})
    flow = index.get("na-surf")
    assert flow.name == "sodium hexadecyl sulphate"
    assert index.by_name("sodium hexadecyl sulphate", "air") == [flow]
    assert index.by_synonym("sodium", "air") == [flow]
    assert index.suppressed_vocab_synonyms == 0


def test_defect_listed_vocab_label_is_dropped_not_kept_as_synonym(tmp_path):
    # same shape as above, but "sodium" is listed in label_defects for this CAS: the
    # wrong label must not resurface via the synonym tier either.
    rows = [cf_row("na-surf", "sodium hexadecyl sulphate", AIR_UNSPEC)]
    vocab = [vocab_row("na-surf", "sodium", cas="1120-01-0")]
    index = EfFlowIndex.from_tables(rows, vocab, label_defects={"1120-01-0": DEFECT})
    flow = index.get("na-surf")
    assert flow.name == "sodium hexadecyl sulphate"
    assert index.by_synonym("sodium", "air") == []
    assert flow.synonyms == ()
    assert index.suppressed_vocab_synonyms == 1


def test_defect_wrong_label_is_stripped_out_of_alt_labels_too(tmp_path):
    rows = [cf_row("na-syn", "sodium hexadecyl sulphate", AIR_UNSPEC)]
    vocab = [vocab_row("na-syn", "sodium", alt=["Sodium", "Other synonym"], cas="1120-01-0")]
    index = EfFlowIndex.from_tables(rows, vocab, label_defects={"1120-01-0": DEFECT})
    flow = index.get("na-syn")
    assert flow.synonyms == ("Other synonym",)


def test_defect_does_not_fire_on_a_different_cas(tmp_path):
    rows = [cf_row("na-other", "sodium hexadecyl sulphate", AIR_UNSPEC)]
    vocab = [vocab_row("na-other", "sodium", cas="9999-99-9")]
    index = EfFlowIndex.from_tables(rows, vocab, label_defects={"1120-01-0": DEFECT})
    flow = index.get("na-other")
    assert flow.name == "sodium hexadecyl sulphate"
    assert flow.synonyms == ("sodium",)
    assert index.suppressed_vocab_synonyms == 0


def test_defect_does_not_fire_when_vocab_label_does_not_match_wrong_label(tmp_path):
    rows = [cf_row("na-real", "sodium", AIR_UNSPEC)]
    vocab = [vocab_row("na-real", "Sodium (inorganic)", cas="1120-01-0")]
    index = EfFlowIndex.from_tables(rows, vocab, label_defects={"1120-01-0": DEFECT})
    flow = index.get("na-real")
    assert flow.name == "sodium"
    assert flow.synonyms == ("Sodium (inorganic)",)
    assert index.suppressed_vocab_synonyms == 0


def test_vocab_label_colliding_with_a_different_flows_jrc_name_in_the_same_bucket_is_dropped(
    tmp_path,
):
    # flow B's own JRC name is "widget b"; flow A's vocab pref_label happens to equal
    # it exactly and both flows share a leaf (so also a bucket) -- keeping "widget b"
    # as A's synonym would let the synonym tier land A's namesakes on B instead,
    # exactly the label-defect collision shape, so it is dropped even with no defect
    # entry at all.
    rows = [
        cf_row("flow-a", "widget a", WATER_FRESH),
        cf_row("flow-b", "widget b", WATER_FRESH),
    ]
    vocab = [
        vocab_row("flow-a", "widget b", cas="111-11-1"),
        vocab_row("flow-b", "widget b", cas="222-22-2"),
    ]
    index = EfFlowIndex.from_tables(rows, vocab, label_defects={})
    flow_a = index.get("flow-a")
    flow_b = index.get("flow-b")
    assert flow_a.name == "widget a"
    assert flow_a.synonyms == ()  # "widget b" dropped: it is flow_b's own JRC name
    assert flow_b.name == "widget b"
    assert index.suppressed_vocab_synonyms == 1
    assert index.by_synonym("widget b", "water") == []
    assert index.by_name("widget b", "water") == [flow_b]


def test_vocab_label_colliding_across_leaves_in_the_same_bucket_is_still_dropped(tmp_path):
    # round 6, decision 2026-09-14 (third cut): flow B sits on a DIFFERENT leaf
    # (water, unspecified) than flow A (fresh water), but the SAME bucket (water) --
    # by_name/by_synonym are bucket-scoped, not leaf-scoped, so a collision on a
    # different leaf in the same bucket is exactly as real as one on the same leaf,
    # and must be caught the same way (a leaf-only check would have missed this).
    rows = [
        cf_row("flow-a", "widget a", WATER_FRESH),
        cf_row("flow-b", "widget b", WATER_UNSPEC),
    ]
    vocab = [
        vocab_row("flow-a", "widget b", cas="111-11-1"),
        vocab_row("flow-b", "widget b", cas="222-22-2"),
    ]
    index = EfFlowIndex.from_tables(rows, vocab, label_defects={})
    flow_a = index.get("flow-a")
    assert flow_a.synonyms == ()
    assert index.suppressed_vocab_synonyms == 1
    assert index.by_synonym("widget b", "water") == []


def test_vocab_label_matching_a_same_named_flow_in_a_different_bucket_is_not_a_collision(
    tmp_path,
):
    # same shared vocab spelling as above, but flow_b now sits in a different
    # BUCKET (air, not water): by_name/by_synonym are bucket-scoped, so there is no
    # actual collision an alias/synonym lookup could ever trip over -- the synonym
    # is kept.
    rows = [
        cf_row("flow-a", "widget a", WATER_FRESH),
        cf_row("flow-b", "widget b", AIR_UNSPEC),
    ]
    vocab = [
        vocab_row("flow-a", "widget b", cas="111-11-1"),
        vocab_row("flow-b", "widget b", cas="222-22-2"),
    ]
    index = EfFlowIndex.from_tables(rows, vocab, label_defects={})
    flow_a = index.get("flow-a")
    assert flow_a.synonyms == ("widget b",)
    assert index.suppressed_vocab_synonyms == 0


def test_vocab_label_colliding_with_an_uncharacterised_flows_name_is_dropped(tmp_path):
    # round 6, decision 2026-09-14 (third cut): the collision index must include
    # uncharacterised flows too -- this is exactly the real Sodium shape before
    # label_defects.yaml named it explicitly: a characterised flow's vocab label
    # colliding with an UNCHARACTERISED flow's own name in the same bucket.
    rows = [cf_row("surfactant", "widget hexadecyl sulphate", WATER_FRESH)]
    vocab = [
        vocab_row("surfactant", "Widget", cas="111-11-1"),
        vocab_row("real-widget", "Widget", cas="222-22-2", bw="envi-wate-suwa"),
    ]
    index = EfFlowIndex.from_tables(rows, vocab, include_uncharacterised=True, label_defects={})
    surfactant = index.get("surfactant")
    assert surfactant.synonyms == ()
    assert index.suppressed_vocab_synonyms == 1
    real_widget = index.get("real-widget")
    assert real_widget is not None
    assert real_widget.characterised is False
    assert real_widget.name == "Widget"
    assert index.by_name("Widget", "water") == [real_widget]


def test_multi_name_code_picks_the_most_frequent_spelling(tmp_path):
    rows = [
        cf_row("multi", "Widget A", AIR_UNSPEC, method="ef-3.1:m1"),
        cf_row("multi", "Widget A", AIR_UNSPEC, method="ef-3.1:m2"),
        cf_row("multi", "Widget B", AIR_UNSPEC, method="ef-3.1:m3"),
    ]
    index = EfFlowIndex.from_tables(rows, [], label_defects={})
    assert index.get("multi").name == "Widget A"
    assert index.multi_name_codes == 1


def test_multi_name_code_tie_breaks_alphabetically(tmp_path):
    rows = [
        cf_row("tie", "Zeta", AIR_UNSPEC, method="ef-3.1:m1"),
        cf_row("tie", "Alpha", AIR_UNSPEC, method="ef-3.1:m2"),
    ]
    index = EfFlowIndex.from_tables(rows, [], label_defects={})
    assert index.get("tie").name == "Alpha"
    assert index.multi_name_codes == 1


def test_multi_name_codes_is_zero_when_every_code_agrees(tmp_path):
    rows = [
        cf_row("agree", "Widget", AIR_UNSPEC, method="ef-3.1:m1"),
        cf_row("agree", "Widget", AIR_UNSPEC, method="ef-3.1:m2"),
    ]
    index = EfFlowIndex.from_tables(rows, [], label_defects={})
    assert index.multi_name_codes == 0


def test_uncharacterised_flow_still_uses_the_vocab_label_and_the_old_relabel_rule(
    tmp_path,
):
    # no CF row exists for an uncharacterised flow, so it keeps the vocab label as
    # its name, still passed through label_defects exactly as before this round.
    vocab = [vocab_row("na-uncertain", "sodium", cas="1120-01-0", bw="envi-air-unkn")]
    index = EfFlowIndex.from_tables(
        [], vocab, include_uncharacterised=True, label_defects={"1120-01-0": DEFECT}
    )
    flow = index.get("na-uncertain")
    assert flow.name == "Sodium hexadecyl sulphate"
    assert flow.characterised is False
    assert index.relabelled_count == 1
    assert index.suppressed_vocab_synonyms == 0


def test_relabelled_count_and_suppressed_vocab_synonyms_are_independent_counters(
    tmp_path,
):
    rows = [cf_row("na-a", "sodium hexadecyl sulphate", AIR_UNSPEC)]
    vocab = [
        vocab_row("na-a", "sodium", cas="1120-01-0"),
        vocab_row("na-b", "sodium", cas="1120-01-0", bw="envi-air-unkn"),
    ]
    index = EfFlowIndex.from_tables(
        rows, vocab, include_uncharacterised=True, label_defects={"1120-01-0": DEFECT}
    )
    # na-a is characterised: its vocab label is suppressed, never "relabelled"
    # na-b is uncharacterised: it is renamed the old way instead
    assert index.relabelled_count == 1
    assert index.suppressed_vocab_synonyms == 1


def test_from_tables_defaults_to_the_shipped_label_defects_file():
    # no label_defects kwarg at all: from_tables loads the packaged file itself.
    rows = [cf_row("na-default", "sodium hexadecyl sulphate", AIR_UNSPEC)]
    vocab = [vocab_row("na-default", "sodium", cas="1120-01-0")]
    index = EfFlowIndex.from_tables(rows, vocab)
    assert index.get("na-default").name == "sodium hexadecyl sulphate"
    assert index.by_synonym("sodium", "air") == []
    assert index.suppressed_vocab_synonyms == 1


def test_from_files_and_from_bytes_forward_label_defects(tmp_path):
    rows = [cf_row("na-ff", "sodium hexadecyl sulphate", AIR_UNSPEC)]
    vocab = [vocab_row("na-ff", "sodium", cas="1120-01-0")]
    cf, vocab_dir = write_ef_inputs(tmp_path, rows, vocab)
    files_index = EfFlowIndex.from_files(cf, vocab_dir, label_defects={"1120-01-0": DEFECT})
    assert files_index.get("na-ff").name == "sodium hexadecyl sulphate"
    assert files_index.by_synonym("sodium", "air") == []
    bytes_index = EfFlowIndex.from_bytes(
        cf.read_bytes(), vocab_dir, label_defects={"1120-01-0": DEFECT}
    )
    assert bytes_index.get("na-ff").name == "sodium hexadecyl sulphate"
    assert bytes_index.by_synonym("sodium", "air") == []


def test_relabelled_count_defaults_to_zero():
    index = EfFlowIndex.from_tables([], [])
    assert index.relabelled_count == 0
    assert index.multi_name_codes == 0
    assert index.suppressed_vocab_synonyms == 0


@pytest.mark.skipif(not _REAL_INPUTS_AVAILABLE, reason="real EF inputs not available")
def test_real_index_names_the_surfactant_by_its_jrc_name(real_index):
    # CAS 1120-01-0 (a surfactant) carries the JRC name "sodium hexadecyl sulphate"
    # in the CF table; its wrong sentier-vocab pref_label ("sodium") is a known
    # defect and never becomes its name or a synonym now that JRC naming is
    # authoritative. Pinned so a future sentier-methods/sentier-vocab change (or a
    # regression here) is caught rather than silently changing behaviour.
    sodium_flows = [f for f in real_index if f.cas == "1120-01-0"]
    assert len(sodium_flows) == 11
    for flow in sodium_flows:
        assert flow.name == "sodium hexadecyl sulphate"
        assert "sodium" not in (s.lower() for s in flow.synonyms)


@pytest.mark.skipif(not _REAL_INPUTS_AVAILABLE, reason="real EF inputs not available")
def test_real_by_name_sodium_water_finds_only_the_real_inorganic_sodium(real_index):
    # the surfactant no longer answers to "sodium" at all (neither as its name nor
    # as a synonym); only the real inorganic Sodium (CAS 7440-23-5, uncharacterised)
    # does.
    hits = real_index.by_name("sodium", "water")
    assert hits
    assert {f.cas for f in hits} == {"7440-23-5"}


@pytest.mark.skipif(not _REAL_INPUTS_AVAILABLE, reason="real EF inputs not available")
def test_real_by_name_benzal_chloride_finds_only_the_real_uncharacterised_one(real_index):
    # CAS 29797-40-8 (a dichlorotoluene mixture, JRC name starts "mixture of
    # 2,4-dichloro-1-methylbenzene") is wrongly labelled "Benzal chloride" in
    # sentier-vocab; the real Benzal chloride (CAS 98-87-3) exists in EF 3.1 but
    # carries no factor in any method. "benzal chloride" must resolve only to the
    # real, uncharacterised one now, in every bucket BAFU's own rows use.
    for bucket in ("air", "water"):
        hits = real_index.by_name("benzal chloride", bucket)
        assert hits
        assert {f.cas for f in hits} == {"98-87-3"}
        assert all(not f.characterised for f in hits)


@pytest.mark.skipif(not _REAL_INPUTS_AVAILABLE, reason="real EF inputs not available")
def test_real_index_counters_pin_the_current_ef_table(real_index):
    # upstream drift guard: a new EF table or vocab payload that changes either
    # number must be looked at, not absorbed silently
    assert real_index.multi_name_codes == 0
    assert real_index.suppressed_vocab_synonyms == 346
