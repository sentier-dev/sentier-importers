from sentier_importers.matching.ef_index import EfFlow, EfFlowIndex, normalise_cas

from tests.matching.fixtures import (
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
    cf_row("cu-rural", "copper", AIR_RURAL, value=2.0),
    cf_row("cu-unspec", "copper", AIR_UNSPEC, value=2.0),
    cf_row("ccl4", "carbon tetrachloride", AIR_UNSPEC, value=3.0),
    cf_row("cfc10", "cfc-10", AIR_UNSPEC, value=3.0),
    cf_row("occ", "occupation, arable", LAND_OCC, method="ef-3.1:land-use", value=1.0),
    cf_row("nullctx", "ghost", None),
]
VOCAB = [
    vocab_row("cu-urban", "Copper", alt=["Cu"], cas="007440-50-8"),
    vocab_row("cu-rural", "Copper", alt=["Cu"], cas="7440-50-8"),
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


def test_vector_is_the_global_factor_per_method_rounded_to_12_digits(tmp_path):
    index = EfFlowIndex.from_files(*write_ef_inputs(tmp_path, CF, VOCAB))
    # the DE-located row must not overwrite the global (location-less) factor
    assert index.vector("cu-urban") == {
        "ef-3.1:human-toxicity-cancer": 2.0,
        "ef-3.1:ecotoxicity-freshwater": 5.0,
    }
    assert index.vector("unknown") == {}


def test_only_cf_bearing_ef_flows_are_indexed(tmp_path):
    index = EfFlowIndex.from_files(*write_ef_inputs(tmp_path, CF, VOCAB))
    assert index.get("bafu-x") is None  # a BAFU vocab row, not EF
    assert index.get("nocf") is None  # EF vocab row without any factor
    assert index.get("nullctx") is None  # a CF row with no context cannot be placed
    assert len(index) == 6


def test_lookups_are_case_insensitive_and_bucket_scoped(tmp_path):
    index = EfFlowIndex.from_files(*write_ef_inputs(tmp_path, CF, VOCAB))
    assert {f.code for f in index.by_name("COPPER", "air")} == {
        "cu-urban",
        "cu-rural",
        "cu-unspec",
    }
    assert index.by_name("copper", "water") == []
    assert {f.code for f in index.by_synonym("cu", "air")} == {"cu-urban", "cu-rural"}
    assert {f.code for f in index.by_cas("000056-23-5", "air")} == {"ccl4", "cfc10"}
    assert index.by_cas(None, "air") == []
    assert {f.code for f in index.by_synonym("methane, tetrachloro-, cfc-10", "air")} == {"ccl4"}


def test_land_context_is_a_resource(tmp_path):
    index = EfFlowIndex.from_files(*write_ef_inputs(tmp_path, CF, VOCAB))
    assert index.get("occ").bucket == "resource"
    assert index.by_name("occupation, arable", "resource")[0].code == "occ"


def test_flow_without_vocab_row_keeps_the_cf_table_name(tmp_path):
    index = EfFlowIndex.from_files(
        *write_ef_inputs(tmp_path, [cf_row("lonely", "ozone", AIR_UNSPEC)], [])
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
