"""BAFU ecoSpold compartments against EF 3.1 context paths.

BAFU keys a flow by ``(category, subCategory)`` (``emissions to air`` / ``high. pop.``);
EF keys it by a context path whose last segment (the *leaf*) carries the
sub-compartment (``Emissions to urban air close to ground``). Matching happens on
whole leafs and within one compartment bucket: ``agricultural soil`` must never
match ``non-agricultural soil``, and a resource token never matches an emission leaf.
"""

from __future__ import annotations

from enum import Enum

BAFU_BUCKET: dict[str, str] = {
    "emissions to air": "air",
    "emissions to water": "water",
    "emissions to soil": "soil",
    "resources": "resource",
}

#: BAFU subCategory -> (bucket(s) it belongs to, whole EF leafs it places on, lowercased,
#: without the ``emissions to `` prefix). ``unspecified`` exists in every emission bucket.
_EMISSION_LEAFS: dict[str, tuple[frozenset[str], frozenset[str]]] = {
    "unspecified": (
        frozenset({"air", "water", "soil"}),
        frozenset({"air, unspecified", "water, unspecified", "soil, unspecified"}),
    ),
    "high. pop.": (frozenset({"air"}), frozenset({"urban air close to ground"})),
    "low. pop.": (frozenset({"air"}), frozenset({"non-urban air or from high stacks"})),
    "low. pop., long-term": (frozenset({"air"}), frozenset({"air, unspecified (long-term)"})),
    "stratosphere + troposphere": (
        frozenset({"air"}),
        frozenset({"lower stratosphere and upper troposphere"}),
    ),
    "indoor": (frozenset({"air"}), frozenset({"air, indoor"})),
    "river": (frozenset({"water"}), frozenset({"fresh water"})),
    "lake": (frozenset({"water"}), frozenset({"fresh water"})),
    "river, long-term": (frozenset({"water"}), frozenset({"water, unspecified (long-term)"})),
    "ocean": (frozenset({"water"}), frozenset({"sea water"})),
    "groundwater": (frozenset({"water"}), frozenset({"ground water"})),
    "fossilwater": (frozenset({"water"}), frozenset({"ground water"})),
    "groundwater, long-term": (frozenset({"water"}), frozenset({"ground water, long-term"})),
    "agricultural": (frozenset({"soil"}), frozenset({"agricultural soil"})),
    "industrial": (frozenset({"soil"}), frozenset({"non-agricultural soil"})),
    "forestry": (frozenset({"soil"}), frozenset({"non-agricultural soil"})),
}
#: Resource subCategory -> the whole-leaf suffix EF uses (``Non-renewable element resources
#: from ground``), or the full leafs for the land family.
_RESOURCE_SUFFIX: dict[str, str] = {
    "in ground": "resources from ground",
    "in water": "resources from water",
    "in air": "resources from air",
    "biotic": "resources from biosphere",
}
_LAND_LEAFS = frozenset({"land occupation", "land transformation"})


class Placement(Enum):
    EXACT = "exact"
    UNSPECIFIED = "unspecified_fallback"
    NONE = "none"


def bucket_of_bafu_category(category: str) -> str | None:
    return BAFU_BUCKET.get(category)


def bucket_of_ef_context(context: str) -> str | None:
    path = context.lower()
    if path.startswith("resources") or "resource" in path or "land use" in path:
        return "resource"
    for bucket in ("air", "water", "soil"):
        if f"emissions to {bucket}" in path:
            return bucket
    return None


def leaf_of(context: str) -> str:
    return context.split(" / ")[-1].strip().lower()


def _strip_emissions_prefix(leaf: str) -> str:
    return leaf[len("emissions to ") :] if leaf.startswith("emissions to ") else leaf


def leaf_matches(bafu_subcategory: str, context: str) -> bool:
    leaf = leaf_of(context)
    bucket = bucket_of_ef_context(context)
    if bafu_subcategory in _EMISSION_LEAFS:
        buckets, leafs = _EMISSION_LEAFS[bafu_subcategory]
        return bucket in buckets and _strip_emissions_prefix(leaf) in leafs
    if bucket != "resource":
        return False
    if bafu_subcategory in _RESOURCE_SUFFIX:
        return leaf.endswith(_RESOURCE_SUFFIX[bafu_subcategory])
    if bafu_subcategory == "land":
        return leaf in _LAND_LEAFS
    return False


def unspecified_leaf(bucket: str, *, long_term: bool) -> str | None:
    if bucket not in ("air", "water", "soil"):
        return None
    if long_term:
        return None if bucket == "soil" else f"emissions to {bucket}, unspecified (long-term)"
    return f"emissions to {bucket}, unspecified"


def place(bafu_subcategory: str, context: str) -> Placement:
    if leaf_matches(bafu_subcategory, context):
        return Placement.EXACT
    bucket = bucket_of_ef_context(context)
    allowed = _EMISSION_LEAFS.get(bafu_subcategory, (frozenset(), frozenset()))[0]
    if bucket not in allowed:
        return Placement.NONE
    fallback = unspecified_leaf(bucket, long_term="long-term" in bafu_subcategory)
    if fallback is not None and leaf_of(context) == fallback:
        return Placement.UNSPECIFIED
    return Placement.NONE
