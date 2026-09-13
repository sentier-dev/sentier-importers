import pytest
from sentier_importers.core.errors import ParseError
from sentier_importers.matching.ef_index import EfFlow, EfFlowIndex, normalise_cas

from tests.matching.ef_fixtures import (
    AIR_RURAL,
    AIR_UNSPEC,
    AIR_URBAN,
    LAND_OCC,
    RES_WATER,
    cf_row,
    vocab_row,
    write_ef_inputs,
)

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
    assert flow.name == "Copper"
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
    assert index.get("cu-urban").name == "Copper"
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
