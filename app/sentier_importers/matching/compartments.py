"""BAFU ecoSpold compartments against EF 3.1 context paths.

BAFU keys a flow by ``(category, subCategory)`` (``emissions to air`` / ``high. pop.``);
EF keys it by a context path whose last segment (the *leaf*) carries the
sub-compartment (``Emissions to urban air close to ground``). Matching happens on
whole leafs and within one compartment bucket: ``agricultural soil`` must never
match ``non-agricultural soil``, and a resource token never matches an emission leaf.
Placement is category-aware: a BAFU ``(category, subCategory)`` only ever matches a
context whose bucket agrees with ``category`` (via ``bucket_of_bafu_category``), so a
``resources`` category never places on an ``Emissions to air`` context regardless of
what the subCategory token happens to be, and an unrecognised category (e.g. an
ecoSpold ``economic issues`` compartment) never places anywhere.

Emission matching is on the whole leaf tail (after stripping the ``emissions to ``
connective); resource matching is on the whole leaf by suffix (e.g. ``in ground``
matches any leaf ending in ``resources from ground``), except the land family, which
matches the full ``Land use / ...`` leaf directly.

EF 3.1 has neither a "ground water" leaf nor a "resources from biosphere" leaf, so the
BAFU ``groundwater``/``fossilwater`` tokens (kept below to document BAFU vocabulary)
never match a leaf exactly; they only ever place through the bucket-level
``water, unspecified`` fallback (see ``place``). ``biotic`` never places at all.
"""

from __future__ import annotations

from enum import Enum
from functools import lru_cache
from typing import NamedTuple

BAFU_BUCKET: dict[str, str] = {
    "emissions to air": "air",
    "emissions to water": "water",
    "emissions to soil": "soil",
    "resources": "resource",
}


class _LeafFamily(NamedTuple):
    """The bucket(s) a BAFU subCategory is valid in, and the EF leaf tail(s) it matches.

    Leaf tails are lowercased, with the ``emissions to `` connective already stripped.
    """

    buckets: frozenset[str]
    leafs: frozenset[str]


#: BAFU emission subCategory -> its ``_LeafFamily``. ``unspecified`` exists in every
#: emission bucket. ``groundwater, long-term`` also accepts the bucket-level
#: ``water, unspecified (long-term)`` leaf: EF models long-term emissions as one
#: compartment-level context, so that context IS the long-term ground-water equivalent,
#: not merely a fallback (compare ``river, long-term``, which works the same way).
_EMISSION_LEAFS: dict[str, _LeafFamily] = {
    "unspecified": _LeafFamily(
        frozenset({"air", "water", "soil"}),
        frozenset({"air, unspecified", "water, unspecified", "soil, unspecified"}),
    ),
    "high. pop.": _LeafFamily(frozenset({"air"}), frozenset({"urban air close to ground"})),
    "low. pop.": _LeafFamily(frozenset({"air"}), frozenset({"non-urban air or from high stacks"})),
    "low. pop., long-term": _LeafFamily(
        frozenset({"air"}), frozenset({"air, unspecified (long-term)"})
    ),
    "stratosphere + troposphere": _LeafFamily(
        frozenset({"air"}),
        frozenset({"lower stratosphere and upper troposphere"}),
    ),
    "indoor": _LeafFamily(frozenset({"air"}), frozenset({"air, indoor"})),
    "river": _LeafFamily(frozenset({"water"}), frozenset({"fresh water"})),
    "lake": _LeafFamily(frozenset({"water"}), frozenset({"fresh water"})),
    "river, long-term": _LeafFamily(
        frozenset({"water"}), frozenset({"water, unspecified (long-term)"})
    ),
    "ocean": _LeafFamily(frozenset({"water"}), frozenset({"sea water"})),
    "groundwater": _LeafFamily(frozenset({"water"}), frozenset({"ground water"})),
    "fossilwater": _LeafFamily(frozenset({"water"}), frozenset({"ground water"})),
    "groundwater, long-term": _LeafFamily(
        frozenset({"water"}),
        frozenset({"ground water, long-term", "water, unspecified (long-term)"}),
    ),
    "agricultural": _LeafFamily(frozenset({"soil"}), frozenset({"agricultural soil"})),
    "industrial": _LeafFamily(frozenset({"soil"}), frozenset({"non-agricultural soil"})),
    "forestry": _LeafFamily(frozenset({"soil"}), frozenset({"non-agricultural soil"})),
}

#: BAFU resource subCategory -> the whole-leaf suffix EF uses, e.g. ``in ground``
#: matches any leaf ending in ``resources from ground`` (``Non-renewable element
#: resources from ground``, ``Non-renewable energy resources from ground``, ...).
#: ``biotic`` documents BAFU vocabulary but has no EF 3.1 counterpart (there is no
#: "resources from biosphere" leaf), so it never matches.
_RESOURCE_SUFFIX: dict[str, str] = {
    "in ground": "resources from ground",
    "in water": "resources from water",
    "in air": "resources from air",
    "biotic": "resources from biosphere",
}

#: The land family: BAFU's ``land`` subCategory matches either whole EF leaf under
#: ``Land use`` (``Land use / Land occupation`` or ``Land use / Land transformation``),
#: which is not a suffix match like the rest of the resource family.
_LAND_LEAFS = frozenset({"land occupation", "land transformation"})

#: Every BAFU subCategory this module recognises, across both the emission and
#: resource families.
KNOWN_SUBCATEGORIES: frozenset[str] = (
    frozenset(_EMISSION_LEAFS) | frozenset(_RESOURCE_SUFFIX) | frozenset({"land"})
)


class Placement(Enum):
    EXACT = "exact"
    UNSPECIFIED = "unspecified_fallback"
    RESOURCE_BRANCH = "resource_branch_fallback"
    NONE = "none"


#: Resource sub-compartments that, on their own, carry no information about the
#: extraction medium -- see ``is_uninformative_resource_sub``.
_UNINFORMATIVE_RESOURCE_SUBS = frozenset({"unspecified", "land", "biotic"})


def is_uninformative_resource_sub(category: str, subcategory: str, name: str) -> bool:
    """Whether a BAFU resource flow's sub-compartment carries no extraction-medium
    information, so it may be placed on the one EF resource branch that holds the
    substance instead of being reported ``sub_compartment_absent`` (Laurenz's decision
    (b), 2026-09-13).

    ``True`` when ``category`` is ``resources`` and ``subcategory`` is one of
    ``unspecified``, ``land`` or ``biotic`` (none of these name a medium at all), or
    when ``subcategory`` is ``in ground`` and ``name`` starts with ``Water`` (BAFU
    files well and cooling water under ``in ground``, while EF keeps all water under
    resources-from-water).

    Deliberately NOT uninformative, so this stays ``False`` for them: ``in water``
    (EF may hold the substance only from ground -- Bromine, Iodine, Magnesium: a
    sea-water extraction is not a ground extraction, so the distinction is real
    information, not noise), ``in air``, and ``in ground`` for a non-water name (a
    real, informative medium already).
    """
    if category != "resources":
        return False
    if subcategory in _UNINFORMATIVE_RESOURCE_SUBS:
        return True
    return subcategory == "in ground" and name.startswith("Water")


def bucket_of_bafu_category(category: str) -> str | None:
    """Map a BAFU ecoSpold ``category`` (e.g. ``emissions to air``) to its bucket.

    Returns ``None`` for a category this module does not recognise (e.g. ecoSpold's
    ``economic issues``), which is how such a category is excluded from matching.
    """
    return BAFU_BUCKET.get(category)


def bucket_of_ef_context(context: str) -> str | None:
    """Map an EF 3.1 context path to its bucket (``air``, ``water``, ``soil``, ``resource``).

    ``context`` must be a full path containing at least one of an ``Emissions to
    <bucket>`` segment, a ``Land use`` segment, or a ``Resources`` segment; a bare leaf
    (e.g. ``"high. pop."``) or an unrecognised path yields ``None``.
    """
    path = context.lower()
    if "resource" in path or "land use" in path:
        return "resource"
    for bucket in ("air", "water", "soil"):
        if f"emissions to {bucket}" in path:
            return bucket
    return None


def leaf_of(context: str) -> str:
    """Return the lowercased last segment of a ``/``-separated EF context path.

    Tolerant of both ``" / "`` and bare ``"/"`` as the segment separator.
    """
    return context.rsplit("/", 1)[-1].strip().lower()


def _strip_emissions_prefix(leaf: str) -> str:
    return leaf[len("emissions to ") :] if leaf.startswith("emissions to ") else leaf


@lru_cache(maxsize=None)
def _bucket_and_leaf(context: str) -> tuple[str | None, str]:
    """Cached ``(bucket_of_ef_context(context), leaf_of(context))``, computed once per context."""
    return bucket_of_ef_context(context), leaf_of(context)


@lru_cache(maxsize=None)
def leaf_matches(bafu_category: str, bafu_subcategory: str, context: str) -> bool:
    """Whether ``(bafu_category, bafu_subcategory)`` matches the whole leaf of ``context``.

    ``context`` must be a full EF context path (see ``bucket_of_ef_context``). Always
    ``False`` when ``bucket_of_bafu_category(bafu_category)`` disagrees with
    ``bucket_of_ef_context(context)`` -- a resource category never matches an emission
    leaf, an emission category never matches a resource leaf, and an unrecognised
    category never matches anything.
    """
    cat_bucket = bucket_of_bafu_category(bafu_category)
    ctx_bucket, leaf = _bucket_and_leaf(context)
    if cat_bucket is None or cat_bucket != ctx_bucket:
        return False
    if bafu_subcategory in _EMISSION_LEAFS:
        family = _EMISSION_LEAFS[bafu_subcategory]
        return ctx_bucket in family.buckets and _strip_emissions_prefix(leaf) in family.leafs
    if ctx_bucket != "resource":
        return False
    if bafu_subcategory in _RESOURCE_SUFFIX:
        return leaf.endswith(_RESOURCE_SUFFIX[bafu_subcategory])
    if bafu_subcategory == "land":
        return leaf in _LAND_LEAFS
    return False


def unspecified_leaf(bucket: str, *, long_term: bool) -> str | None:
    """The bucket-level "unspecified" EF leaf a BAFU subCategory can fall back to.

    Returns ``None`` when ``bucket`` is not an emission bucket, or when the long-term
    variant does not exist for that bucket (EF 3.1 has no long-term soil leaf).
    """
    if bucket not in ("air", "water", "soil"):
        return None
    if long_term:
        return None if bucket == "soil" else f"emissions to {bucket}, unspecified (long-term)"
    return f"emissions to {bucket}, unspecified"


@lru_cache(maxsize=None)
def place(bafu_category: str, bafu_subcategory: str, context: str) -> Placement:
    """Resolve where ``(bafu_category, bafu_subcategory)`` places on ``context``.

    ``context`` must be a full EF context path (see ``bucket_of_ef_context``).
    ``Placement.EXACT`` for a whole-leaf match, ``Placement.UNSPECIFIED`` when it falls
    back to the bucket-level "unspecified" leaf, ``Placement.NONE`` otherwise --
    including whenever ``bafu_category`` disagrees with ``context``'s bucket, since the
    allowed bucket is derived from the BAFU side, not just from the leaf being matched.
    """
    if leaf_matches(bafu_category, bafu_subcategory, context):
        return Placement.EXACT
    cat_bucket = bucket_of_bafu_category(bafu_category)
    ctx_bucket, leaf = _bucket_and_leaf(context)
    if cat_bucket is None or cat_bucket != ctx_bucket:
        return Placement.NONE
    allowed = _EMISSION_LEAFS.get(bafu_subcategory, _LeafFamily(frozenset(), frozenset())).buckets
    if ctx_bucket not in allowed:
        return Placement.NONE
    fallback = unspecified_leaf(ctx_bucket, long_term="long-term" in bafu_subcategory)
    if fallback is not None and leaf == fallback:
        return Placement.UNSPECIFIED
    return Placement.NONE
