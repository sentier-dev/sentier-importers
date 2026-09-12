"""biosphere3 code -> EF 3.1 flow via characterization-factor vector identity."""

import pytest
from sentier_importers.sources.eaternity.bridge import BafuFlow
from sentier_importers.sources.eaternity.cf_identity import (
    CfVectors,
    EfLabels,
    Twin,
    Withheld,
    find_twin,
)

ECO = "ecoinvent-3.9.1-biosphere"
B3_CU_AIR = "b3-copper-air"
B3_CU_RES = "b3-copper-ground"
B3_UNCHAR = "b3-nothing"
B3_LONELY = "b3-lonely"
B3_ZN_AIR = "b3-zinc-air"
B3_BA_AIR = "b3-barium-air"

EF_CU_AIR_URBAN = "ef-cu-urban"
EF_CU_AIR_RURAL = "ef-cu-rural"
EF_CU_GROUND = "ef-cu-ground"
EF_ZN_URBAN = "ef-zn-urban"
EF_BA_URBAN_A = "ef-ba-urban-a"
EF_BA_URBAN_B = "ef-ba-urban-b"
EF_OTHER_RURAL = "ef-other-rural"

TABLES = {
    "tox": [
        {"database": ECO, "code": B3_CU_AIR, "amount": 1.5},
        {"database": ECO, "code": B3_CU_RES, "amount": 0.7},
        {"database": ECO, "code": B3_LONELY, "amount": 99.0},
        {"database": ECO, "code": B3_ZN_AIR, "amount": 2.0},
        {"database": ECO, "code": B3_BA_AIR, "amount": 3.0},
        {"database": "ef", "code": EF_CU_AIR_URBAN, "amount": 1.5},
        {"database": "ef", "code": EF_CU_AIR_RURAL, "amount": 1.5},
        {"database": "ef", "code": EF_CU_GROUND, "amount": 0.7},
        {"database": "ef", "code": EF_OTHER_RURAL, "amount": 1.5},
        {"database": "ef", "code": EF_ZN_URBAN, "amount": 2.0},
        {"database": "ef", "code": EF_BA_URBAN_A, "amount": 3.0},
        {"database": "ef", "code": EF_BA_URBAN_B, "amount": 3.0},
    ],
    "adp": [
        {"database": ECO, "code": B3_CU_RES, "amount": 0.001},
        {"database": "ef", "code": EF_CU_GROUND, "amount": 0.001},
        {"database": "ef", "code": EF_CU_AIR_URBAN, "amount": 0.001},
    ],
    "pm": [
        {"database": "ef", "code": EF_ZN_URBAN, "amount": 5.0},
        {"database": "ef", "code": EF_BA_URBAN_A, "amount": 7.0},
        {"database": "ef", "code": EF_BA_URBAN_B, "amount": 8.0},
    ],
}
AIR_URBAN = "Emissions / Emissions to air / Emissions to urban air close to ground"
AIR_RURAL = "Emissions / Emissions to air / Emissions to non-urban air or from high stacks"
LABELS = EfLabels.from_rows(
    [
        {
            "flow": f"https://vocab.sentier.dev/flows/{EF_CU_AIR_URBAN}",
            "flow_name": "copper",
            "flow_context": AIR_URBAN,
        },
        {"flow": EF_CU_AIR_RURAL, "flow_name": "copper", "flow_context": AIR_RURAL},
        {"flow": EF_OTHER_RURAL, "flow_name": "2-hydroxybenzonitrile", "flow_context": AIR_RURAL},
        {
            "flow": EF_CU_GROUND,
            "flow_name": "copper",
            "flow_context": "Resources / Resources from ground",
        },
        {"flow": EF_ZN_URBAN, "flow_name": "zinc (ii)", "flow_context": AIR_URBAN},
        {"flow": EF_BA_URBAN_A, "flow_name": "barium (ii)", "flow_context": AIR_URBAN},
        {"flow": EF_BA_URBAN_B, "flow_name": "barium", "flow_context": AIR_URBAN},
    ]
)
VECTORS = CfVectors.from_tables(TABLES)

CU_HIGH = BafuFlow("Copper", "emissions to air", "high. pop.", "kg")
CU_LOW = BafuFlow("Copper", "emissions to air", "low. pop.", "kg")
CU_GROUND = BafuFlow("Copper", "resources", "in ground", "kg")
ZN_HIGH = BafuFlow("Zinc", "emissions to air", "high. pop.", "kg")
BA_HIGH = BafuFlow("Barium", "emissions to air", "high. pop.", "kg")


def test_labels_strip_the_vocab_iri_prefix_and_split_the_context():
    assert LABELS.name(EF_CU_AIR_URBAN) == "copper"
    assert LABELS.context(EF_CU_AIR_URBAN) == [
        "Emissions",
        "Emissions to air",
        "Emissions to urban air close to ground",
    ]
    assert LABELS.bucket(EF_CU_AIR_URBAN) == "air"
    assert LABELS.bucket(EF_CU_GROUND) == "resource"


def test_exact_twin_prefers_matching_name_then_matching_sub_compartment():
    twin = find_twin(VECTORS, LABELS, B3_CU_AIR, CU_LOW)
    assert twin == Twin(
        code=EF_CU_AIR_RURAL, tier="T2", candidates=2, name_matched=True, sub_matched=True
    )


def test_exact_twin_in_another_sub_compartment_is_flagged_not_dropped():
    twin = find_twin(VECTORS, LABELS, B3_CU_AIR, CU_HIGH)
    # both copper flows carry the same factors; the urban one has an extra ADP method so is
    # not CF-identical, leaving the rural flow as the only name-matched exact twin
    assert twin.code == EF_CU_AIR_RURAL
    assert twin.sub_matched is False


def test_compartment_gate_withholds_a_resource_twin_for_an_emission():
    """Identical factors are not identical flows: an emission may never resolve to a
    resource flow (or vice versa)."""
    got = find_twin(VECTORS, LABELS, B3_CU_RES, CU_HIGH)
    assert got == Withheld(
        reason="compartment_mismatch",
        detail="1 CF-identical EF flow(s), none in the air compartment",
    )


def test_uncharacterised_partner_is_withheld():
    assert find_twin(VECTORS, LABELS, B3_UNCHAR, CU_HIGH) == Withheld(
        reason="uncharacterised", detail="biosphere3 partner carries no EF 3.1 factor"
    )


def test_no_compatible_ef_flow_is_withheld():
    got = find_twin(VECTORS, LABELS, B3_LONELY, CU_HIGH)
    assert got.reason == "no_cf_compatible_ef_flow"


def test_superset_twin_requires_a_name_match():
    """zinc: the EF flow agrees on tox and adds PM. Name-matched (``zinc (ii)`` reads as
    zinc), so it is a T3 twin."""
    twin = find_twin(VECTORS, LABELS, B3_ZN_AIR, ZN_HIGH)
    assert twin == Twin(
        code=EF_ZN_URBAN, tier="T3", candidates=1, name_matched=True, sub_matched=True
    )


def test_superset_candidates_disagreeing_on_added_methods_are_withheld():
    got = find_twin(VECTORS, LABELS, B3_BA_AIR, BA_HIGH)
    assert got.reason == "superset_candidates_disagree"


def test_vectors_round_to_twelve_significant_digits():
    vectors = CfVectors.from_tables(
        {
            "m": [
                {"database": ECO, "code": "a", "amount": 0.1 + 0.2},
                {"database": "ef", "code": "b", "amount": 0.3},
            ]
        }
    )
    assert vectors.exact(vectors.vector(ECO, "a")) == ["b"]


def test_superset_candidates_adding_different_methods_are_withheld():
    """Two name-matched supersets that never conflict numerically but add disjoint
    methods characterise different things; no pick is made."""
    tables = {
        "tox": [
            {"database": ECO, "code": "b3", "amount": 1.0},
            {"database": "ef", "code": "ef-a", "amount": 1.0},
            {"database": "ef", "code": "ef-b", "amount": 1.0},
        ],
        "pm": [{"database": "ef", "code": "ef-a", "amount": 2.0}],
        "adp": [{"database": "ef", "code": "ef-b", "amount": 3.0}],
    }
    labels = EfLabels.from_rows(
        [
            {"flow": "ef-a", "flow_name": "lead", "flow_context": AIR_URBAN},
            {"flow": "ef-b", "flow_name": "lead (ii)", "flow_context": AIR_URBAN},
        ]
    )
    got = find_twin(
        CfVectors.from_tables(tables),
        labels,
        "b3",
        BafuFlow("Lead", "emissions to air", "high. pop.", "kg"),
    )
    assert got.reason == "superset_candidates_disagree"


_SUB_MATCH_LABELS = EfLabels.from_rows(
    [
        {
            "flow": "ef-agri",
            "flow_name": "iron",
            "flow_context": "Emissions / Emissions to soil / Emissions to agricultural soil",
        },
        {
            "flow": "ef-non",
            "flow_name": "iron",
            "flow_context": ("Emissions / Emissions to soil / Emissions to non-agricultural soil"),
        },
        {
            "flow": "ef-ground",
            "flow_name": "iron",
            "flow_context": (
                "Resources / Resources from ground / "
                "Non-renewable element resources from ground"
            ),
        },
        {
            "flow": "ef-land-occupation",
            "flow_name": "occupation, arable land",
            "flow_context": "Resources / Land use / Land occupation",
        },
        {
            "flow": "ef-land-transformation",
            "flow_name": "transformation, to arable land",
            "flow_context": "Resources / Land use / Land transformation",
        },
        {
            "flow": "ef-groundwater-long-term",
            "flow_name": "arsenic",
            "flow_context": (
                "Emissions / Emissions to water / Emissions to ground water, long-term"
            ),
        },
        {
            "flow": "ef-air-unspecified-long-term",
            "flow_name": "particulates",
            "flow_context": (
                "Emissions / Emissions to air / Emissions to air, unspecified (long-term)"
            ),
        },
        {
            "flow": "ef-air-unspecified",
            "flow_name": "particulates",
            "flow_context": "Emissions / Emissions to air / Emissions to air, unspecified",
        },
        {
            "flow": "ef-air-rural",
            "flow_name": "iron",
            "flow_context": AIR_RURAL,
        },
    ]
)

_SUB_MATCH_CASES = [
    # the original substring bug: "agricultural" must not match the "non-agricultural"
    # leaf just because it is a substring of it.
    ("ef-agri", "agricultural", True),
    ("ef-non", "agricultural", False),
    ("ef-non", "industrial", True),
    ("ef-ground", "in ground", True),
    # the "land" token accepts either EF land leaf (a tuple of accepted tails), and must
    # not bleed into the unrelated "in ground" resource token.
    ("ef-land-occupation", "land", True),
    ("ef-land-transformation", "land", True),
    ("ef-land-occupation", "in ground", False),
    # long-term is part of the leaf tail, so it must match exactly, not by substring.
    ("ef-groundwater-long-term", "groundwater", False),
    ("ef-groundwater-long-term", "groundwater, long-term", True),
    ("ef-air-unspecified-long-term", "unspecified", False),
    ("ef-air-unspecified", "unspecified", True),
    ("ef-ground", "in water", False),
    ("ef-air-rural", "high. pop.", False),
]


@pytest.mark.parametrize("code, bafu_subcategory, expected", _SUB_MATCH_CASES)
def test_sub_matches(code, bafu_subcategory, expected):
    assert _SUB_MATCH_LABELS.sub_matches(code, bafu_subcategory) is expected
