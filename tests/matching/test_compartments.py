"""BAFU compartments against EF context leafs.

Context strings used here are drawn verbatim from the real EF 3.1
characterization-factors context vocabulary (26 distinct ``flow_context`` values),
confirmed against ``sentier-methods/data/01-ef-3.1/characterization-factors.parquet``.
Notably, EF 3.1 has no "ground water" leaf and no "resources from biosphere" leaf, and
the land contexts are ``Land use / Land occupation`` / ``Land use / Land
transformation`` -- two segments, with no ``Resources /`` prefix.
"""

import pytest
from sentier_importers.matching.compartments import (
    KNOWN_SUBCATEGORIES,
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
AIR_UNSPEC = "Emissions / Emissions to air / Emissions to air, unspecified"
WATER_FRESH = "Emissions / Emissions to water / Emissions to fresh water"
WATER_UNSPEC = "Emissions / Emissions to water / Emissions to water, unspecified"
WATER_UNSPEC_LT = "Emissions / Emissions to water / Emissions to water, unspecified (long-term)"
LAND_OCC = "Land use / Land occupation"
LAND_TRANS = "Land use / Land transformation"


def test_buckets():
    assert bucket_of_bafu_category("emissions to air") == "air"
    assert bucket_of_bafu_category("resources") == "resource"
    assert bucket_of_bafu_category("economic issues") is None
    assert bucket_of_ef_context(GROUND) == "resource"
    assert bucket_of_ef_context(LAND_OCC) == "resource"
    assert bucket_of_ef_context(AGRI) == "soil"
    assert bucket_of_ef_context(AIR_URBAN) == "air"
    assert bucket_of_ef_context(WATER_FRESH) == "water"
    assert bucket_of_ef_context("") is None


def test_known_subcategories():
    assert "high. pop." in KNOWN_SUBCATEGORIES
    assert "land" in KNOWN_SUBCATEGORIES
    assert "foo" not in KNOWN_SUBCATEGORIES


def test_leaf_of_is_separator_tolerant():
    assert (
        leaf_matches(
            "emissions to air",
            "unspecified",
            "Emissions/Emissions to air/Emissions to air, unspecified",
        )
        is True
    )


@pytest.mark.parametrize(
    "category, sub, leaf, expected",
    [
        ("emissions to soil", "agricultural", AGRI, True),
        ("emissions to soil", "agricultural", NON_AGRI, False),
        ("emissions to soil", "industrial", NON_AGRI, True),
        ("emissions to soil", "forestry", NON_AGRI, True),
        ("emissions to air", "high. pop.", AIR_URBAN, True),
        (
            "emissions to air",
            "low. pop.",
            "Emissions / Emissions to air / Emissions to non-urban air or from high stacks",
            True,
        ),
        ("emissions to air", "low. pop.", AIR_URBAN, False),
        (
            "emissions to air",
            "low. pop., long-term",
            "Emissions / Emissions to air / Emissions to air, unspecified (long-term)",
            True,
        ),
        (
            "emissions to air",
            "low. pop., long-term",
            "Emissions / Emissions to air / Emissions to air, unspecified",
            False,
        ),
        (
            "emissions to air",
            "stratosphere + troposphere",
            "Emissions / Emissions to air / Emissions to lower stratosphere and upper troposphere",
            True,
        ),
        (
            "emissions to air",
            "indoor",
            "Emissions / Emissions to air / Emissions to air, indoor",
            True,
        ),
        ("emissions to water", "river", WATER_FRESH, True),
        ("emissions to water", "lake", WATER_FRESH, True),
        ("emissions to water", "river, long-term", WATER_UNSPEC_LT, True),
        (
            "emissions to water",
            "ocean",
            "Emissions / Emissions to water / Emissions to sea water",
            True,
        ),
        # EF has no ground-water leaf; the long-term bucket-level context IS the
        # long-term ground-water equivalent (compare river, long-term above).
        ("emissions to water", "groundwater, long-term", WATER_UNSPEC_LT, True),
        ("emissions to water", "unspecified", WATER_UNSPEC, True),
        ("emissions to water", "unspecified", WATER_UNSPEC_LT, False),
        ("emissions to air", "unspecified", AIR_UNSPEC, True),
        (
            "emissions to soil",
            "unspecified",
            "Emissions / Emissions to soil / Emissions to soil, unspecified",
            True,
        ),
        ("resources", "in ground", GROUND, True),
        (
            "resources",
            "in ground",
            "Resources / Resources from ground / Non-renewable energy resources from ground",
            True,
        ),
        (
            "resources",
            "in water",
            "Resources / Resources from water / Renewable material resources from water",
            True,
        ),
        ("resources", "in water", GROUND, False),
        (
            "resources",
            "in air",
            "Resources / Resources from air / Renewable material resources from air",
            True,
        ),
        # a resource category must never match an emission leaf, whatever the words
        ("resources", "in water", WATER_FRESH, False),
        ("resources", "in ground", AIR_URBAN, False),
        # and an emission category must never match a resource leaf
        ("emissions to air", "unspecified", GROUND, False),
        ("resources", "land", LAND_OCC, True),
        ("resources", "land", LAND_TRANS, True),
        ("resources", "in ground", LAND_OCC, False),
        ("emissions to soil", "not a bafu sub", AGRI, False),
        # category disagrees with the subCategory's actual bucket -> never a match,
        # even for a real subCategory / real leaf pairing
        ("resources", "unspecified", AIR_UNSPEC, False),
        # an unrecognised BAFU category never matches anything
        ("economic issues", "unspecified", AIR_UNSPEC, False),
        # recognised category and bucket, but an unrecognised subCategory token
        ("resources", "not a bafu sub", GROUND, False),
    ],
)
def test_leaf_matches(category, sub, leaf, expected):
    assert leaf_matches(category, sub, leaf) is expected


def test_unspecified_leaf_per_bucket():
    assert unspecified_leaf("air", long_term=False) == "emissions to air, unspecified"
    assert (
        unspecified_leaf("water", long_term=True) == "emissions to water, unspecified (long-term)"
    )
    assert unspecified_leaf("soil", long_term=True) is None  # EF has no long-term soil leaf
    assert unspecified_leaf("resource", long_term=False) is None


def test_place():
    assert place("emissions to soil", "agricultural", AGRI) is Placement.EXACT
    assert place("emissions to water", "groundwater", WATER_UNSPEC) is Placement.UNSPECIFIED
    assert place("emissions to water", "groundwater, long-term", WATER_UNSPEC) is Placement.NONE
    # long-term ground water IS the bucket-level long-term context: EXACT, not a fallback
    assert (
        place("emissions to water", "groundwater, long-term", WATER_UNSPEC_LT) is Placement.EXACT
    )
    assert place("emissions to soil", "agricultural", NON_AGRI) is Placement.NONE
    assert place("resources", "in ground", GROUND) is Placement.EXACT
    assert place("emissions to air", "high. pop.", WATER_UNSPEC) is Placement.NONE  # wrong bucket
    # EF models long-term emissions as one compartment-level context: that IS the
    # equivalent, so EXACT
    air_unspec_lt = "Emissions / Emissions to air / Emissions to air, unspecified (long-term)"
    assert place("emissions to air", "low. pop., long-term", air_unspec_lt) is Placement.EXACT
    # category disagrees with the context's bucket -> NONE, regardless of subCategory
    assert place("resources", "unspecified", AIR_UNSPEC) is Placement.NONE
    # category agrees with the context's bucket, but the subCategory itself is only
    # ever valid in a different bucket (soil-only "agricultural" against an air leaf)
    assert place("emissions to air", "agricultural", AIR_UNSPEC) is Placement.NONE
