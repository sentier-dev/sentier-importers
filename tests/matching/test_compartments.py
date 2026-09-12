"""BAFU compartments against EF context leafs."""

import pytest
from sentier_importers.matching.compartments import (
    Placement,
    bucket_of_bafu_category,
    bucket_of_ef_context,
    leaf_matches,
    place,
    unspecified_leaf,
)

AGRI = "Emissions / Emissions to soil / Emissions to agricultural soil"
NON_AGRI = "Emissions / Emissions to soil / Emissions to non-agricultural soil"
GROUND = "Resources / Resources from ground / Non-renewable element resources from ground"
AIR_URBAN = "Emissions / Emissions to air / Emissions to urban air close to ground"
WATER_FRESH = "Emissions / Emissions to water / Emissions to fresh water"


def test_buckets():
    assert bucket_of_bafu_category("emissions to air") == "air"
    assert bucket_of_bafu_category("resources") == "resource"
    assert bucket_of_bafu_category("economic issues") is None
    assert bucket_of_ef_context(GROUND) == "resource"
    assert bucket_of_ef_context("Resources / Land use / Land occupation") == "resource"
    assert bucket_of_ef_context(AGRI) == "soil"
    assert bucket_of_ef_context(AIR_URBAN) == "air"
    assert bucket_of_ef_context(WATER_FRESH) == "water"


@pytest.mark.parametrize(
    "sub, leaf, expected",
    [
        ("agricultural", AGRI, True),
        ("agricultural", NON_AGRI, False),
        ("industrial", NON_AGRI, True),
        ("forestry", NON_AGRI, True),
        ("high. pop.", AIR_URBAN, True),
        (
            "low. pop.",
            "Emissions / Emissions to air / Emissions to non-urban air or from high stacks",
            True,
        ),
        ("low. pop.", AIR_URBAN, False),
        (
            "low. pop., long-term",
            "Emissions / Emissions to air / Emissions to air, unspecified (long-term)",
            True,
        ),
        (
            "low. pop., long-term",
            "Emissions / Emissions to air / Emissions to air, unspecified",
            False,
        ),
        (
            "stratosphere + troposphere",
            "Emissions / Emissions to air / Emissions to lower stratosphere and upper troposphere",
            True,
        ),
        ("indoor", "Emissions / Emissions to air / Emissions to air, indoor", True),
        ("river", WATER_FRESH, True),
        ("lake", WATER_FRESH, True),
        (
            "river, long-term",
            "Emissions / Emissions to water / Emissions to water, unspecified (long-term)",
            True,
        ),
        ("ocean", "Emissions / Emissions to water / Emissions to sea water", True),
        ("groundwater", "Emissions / Emissions to water / Emissions to ground water", True),
        (
            "groundwater",
            "Emissions / Emissions to water / Emissions to ground water, long-term",
            False,
        ),
        (
            "groundwater, long-term",
            "Emissions / Emissions to water / Emissions to ground water, long-term",
            True,
        ),
        ("fossilwater", "Emissions / Emissions to water / Emissions to ground water", True),
        (
            "unspecified",
            "Emissions / Emissions to water / Emissions to water, unspecified",
            True,
        ),
        (
            "unspecified",
            "Emissions / Emissions to water / Emissions to water, unspecified (long-term)",
            False,
        ),
        ("unspecified", "Emissions / Emissions to air / Emissions to air, unspecified", True),
        ("unspecified", "Emissions / Emissions to soil / Emissions to soil, unspecified", True),
        ("in ground", GROUND, True),
        (
            "in ground",
            "Resources / Resources from ground / Non-renewable energy resources from ground",
            True,
        ),
        (
            "in water",
            "Resources / Resources from water / Renewable material resources from water",
            True,
        ),
        ("in water", GROUND, False),
        (
            "in air",
            "Resources / Resources from air / Renewable material resources from air",
            True,
        ),
        # a resource token must never match an emission leaf, whatever the words
        ("in water", WATER_FRESH, False),
        ("in ground", AIR_URBAN, False),
        ("in ground", "Emissions / Emissions to water / Emissions to ground water", False),
        # and an emission token must never match a resource leaf
        ("unspecified", GROUND, False),
        ("land", "Resources / Land use / Land occupation", True),
        ("land", "Resources / Land use / Land transformation", True),
        ("in ground", "Resources / Land use / Land occupation", False),
        (
            "biotic",
            "Resources / Resources from biosphere / Renewable material resources from biosphere",
            True,
        ),
        ("not a bafu sub", AGRI, False),
    ],
)
def test_leaf_matches(sub, leaf, expected):
    assert leaf_matches(sub, leaf) is expected


def test_unspecified_leaf_per_bucket():
    assert unspecified_leaf("air", long_term=False) == "emissions to air, unspecified"
    assert (
        unspecified_leaf("water", long_term=True) == "emissions to water, unspecified (long-term)"
    )
    assert unspecified_leaf("soil", long_term=True) is None  # EF has no long-term soil leaf
    assert unspecified_leaf("resource", long_term=False) is None


def test_place():
    assert place("agricultural", AGRI) is Placement.EXACT
    assert (
        place("groundwater", "Emissions / Emissions to water / Emissions to water, unspecified")
        is Placement.UNSPECIFIED
    )
    assert (
        place(
            "groundwater, long-term",
            "Emissions / Emissions to water / Emissions to water, unspecified",
        )
        is Placement.NONE
    )
    assert (
        place(
            "groundwater, long-term",
            "Emissions / Emissions to water / Emissions to water, unspecified (long-term)",
        )
        is Placement.UNSPECIFIED
    )
    assert place("agricultural", NON_AGRI) is Placement.NONE
    assert place("in ground", GROUND) is Placement.EXACT
    assert (
        place("high. pop.", "Emissions / Emissions to water / Emissions to water, unspecified")
        is Placement.NONE
    )  # wrong bucket
    # EF models long-term emissions as one compartment-level context: that IS the
    # equivalent, so EXACT
    assert (
        place(
            "low. pop., long-term",
            "Emissions / Emissions to air / Emissions to air, unspecified (long-term)",
        )
        is Placement.EXACT
    )
