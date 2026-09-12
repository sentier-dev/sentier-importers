import pytest
from sentier_importers.core.errors import ParseError
from sentier_importers.matching.ef_index import EfFlow, EfFlowIndex, normalise_cas

from tests.matching.ef_fixtures import (
    AIR_RURAL,
    AIR_UNSPEC,
    AIR_URBAN,
    LAND_OCC,
    cf_row,
    vocab_row,
    write_ef_inputs,
)

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
    cf_row("occ", "occupation, arable", LAND_OCC, method="ef-3.1:land-use", value=1.0),
    cf_row("nullctx", "ghost", None),
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
    assert len(index) == 6
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
    assert index.by_name("occupation, arable", "resource")[0].code == "occ"


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
    assert len(index) == 6
