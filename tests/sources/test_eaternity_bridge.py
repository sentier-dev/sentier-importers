"""Eaternity flat flow -> BAFU-2026 v1 flow resolution (the bridge)."""

from sentier_importers.sources.bafu.ecospold import flow_id
from sentier_importers.sources.eaternity.bridge import (
    BafuFlow,
    BafuFlowIndex,
    bafu_subcategory,
    resolve,
)

CO2_HIGH = BafuFlow("Carbon dioxide, fossil", "emissions to air", "high. pop.", "kg")
CO2_UNSPEC = BafuFlow("Carbon dioxide, fossil", "emissions to air", "unspecified", "kg")
MIBK_RIVER = BafuFlow("4-Methyl-2-pentanone", "emissions to water", "river", "kg")
RADON_BQ = BafuFlow("Radon-222", "emissions to air", "low. pop.", "Bq")
RADON_KBQ = BafuFlow("Radon-222", "emissions to air", "low. pop.", "kBq")
INDEX = BafuFlowIndex.from_flows([CO2_HIGH, CO2_UNSPEC, MIBK_RIVER, RADON_BQ, RADON_KBQ])


def test_flow_code_is_the_vocab_flow_id():
    assert CO2_HIGH.code == flow_id(
        "Carbon dioxide, fossil", "emissions to air", "high. pop.", "kg"
    )


def test_index_from_ecospold_records_keeps_only_biosphere_exchanges():
    records = [
        {
            "exchanges": [
                {
                    "name": "Carbon dioxide, fossil",
                    "category": "emissions to air",
                    "subcategory": "high. pop.",
                    "unit": "kg",
                    "group_code": 4,
                },
                {
                    "name": "Carbon dioxide, fossil",
                    "category": "emissions to air",
                    "subcategory": "high. pop.",
                    "unit": "kg",
                    "group_code": 4,
                },
                {
                    "name": "Electricity",
                    "category": "electricity",
                    "subcategory": "mix",
                    "unit": "kWh",
                    "group_code": 5,
                },
            ]
        }
    ]
    index = BafuFlowIndex.from_ecospold(records)
    assert list(index) == [CO2_HIGH]


def test_b3_sub_compartment_maps_to_bafu_subcategory():
    assert bafu_subcategory(["air", "urban air close to ground"]) == "high. pop."
    assert bafu_subcategory(["water", "ground-, long-term"]) == "groundwater, long-term"
    assert bafu_subcategory(["natural resource", "in ground"]) == "in ground"


def test_root_only_b3_context_means_unspecified():
    assert bafu_subcategory(["air"]) == "unspecified"


def test_unknown_b3_sub_compartment_is_none_not_guessed():
    assert bafu_subcategory(["air", "outer space"]) is None


def test_exact_resolution_uses_the_b3_sub_compartment():
    got = resolve(
        INDEX, "Carbon dioxide, fossil", "air", "kg", ["air", "urban air close to ground"]
    )
    assert [(r.flow, r.conversion_factor) for r in got] == [(CO2_HIGH, 1.0)]


def test_name_match_is_case_insensitive():
    got = resolve(INDEX, "carbon dioxide, FOSSIL", "air", "kg", ["air"])
    assert [r.flow for r in got] == [CO2_UNSPEC]


def test_sub_compartment_is_strict_never_a_fallback():
    """A biosphere3 ocean flow must not be spread onto BAFU river/unspecified: the EF
    sea-water and fresh-water factors differ by orders of magnitude."""
    assert resolve(INDEX, "4-Methyl-2-pentanone", "water", "kg", ["water", "ocean"]) == []


def test_radionuclide_unit_twin_resolves_both_with_a_conversion():
    got = resolve(INDEX, "Radon-222", "air", "kBq", ["air", "non-urban air or from high stacks"])
    assert {(r.flow.unit, r.conversion_factor) for r in got} == {("kBq", 1.0), ("Bq", 0.001)}


def test_unknown_root_compartment_resolves_nothing():
    assert resolve(INDEX, "Carbon dioxide, fossil", "economic", "kg", ["air"]) == []


def test_unknown_b3_sub_compartment_resolves_nothing():
    assert resolve(INDEX, "Carbon dioxide, fossil", "air", "kg", ["air", "outer space"]) == []
