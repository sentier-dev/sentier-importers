"""Match a BAFU flow to one EF flow: matchers in tier order, then placement, then
disambiguation. Anything that cannot be asserted comes back as ``Unmatched`` with a
reason a reviewer can act on.

Tier order: exact name, synonym, CAS, qualifier spelling, curated alias, then the same
five applied to the region-stripped name. The first matcher that yields any candidate
in the flow's compartment decides the outcome: a later tier never rescues a placement
failure of an earlier one, because "the exact-name EF flow exists but only in another
sub-compartment" is information, not a miss.
"""

from __future__ import annotations

import difflib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from sentier_importers.matching.compartments import (
    KNOWN_SUBCATEGORIES,
    Placement,
    bucket_of_bafu_category,
    leaf_of,
    place,
)
from sentier_importers.matching.ef_index import EfFlowIndex
from sentier_importers.matching.matchers import (
    Alias,
    AliasMatcher,
    CasMatcher,
    ExactNameMatcher,
    Matcher,
    QualifierMatcher,
    RegionStripMatcher,
    SynonymMatcher,
)
from sentier_importers.sources.eaternity.bridge import BafuFlow

#: BAFU sub-compartment -> how EF would have named it, for the fallback caveat.
_LEAF_HUMAN = {
    "groundwater": "ground water",
    "groundwater, long-term": "ground water, long-term",
    "river": "fresh water",
    "lake": "fresh water",
    "ocean": "sea water",
    "high. pop.": "urban air close to ground",
    "low. pop.": "non-urban air or from high stacks",
    "stratosphere + troposphere": "lower stratosphere and upper troposphere",
    "agricultural": "agricultural soil",
    "industrial": "non-agricultural soil",
    "forestry": "non-agricultural soil",
    "fossilwater": "ground water",
    "indoor": "indoor air",
}
_NO_MATCH = (
    "no EF 3.1 flow with a factor matches by name, synonym, CAS, qualifier or alias "
    "in the {bucket} compartment"
)


@dataclass(frozen=True)
class Match:
    """One BAFU flow resolved to one EF flow."""

    code: str
    tier: str
    placement: str
    location: str | None
    candidates: int
    caveats: tuple[str, ...]


@dataclass(frozen=True)
class Unmatched:
    """A BAFU flow that could not be resolved, with a reason a reviewer can act on."""

    reason: str
    detail: str


class MatchPipeline:
    """Runs matchers in tier order, then places and disambiguates the result."""

    def __init__(
        self,
        matchers: Sequence[Matcher],
        index: EfFlowIndex,
        *,
        unspecified_fallback: bool = True,
    ) -> None:
        """Build the pipeline from an ordered ``matchers`` sequence and its ``index``.

        ``unspecified_fallback`` controls whether a candidate that only exists on the
        bucket-level "unspecified" EF context is accepted (with a caveat) when the
        BAFU sub-compartment names something more specific; disabling it turns that
        case into a ``sub_compartment_absent`` ``Unmatched`` instead.
        """
        self._matchers = list(matchers)
        self._index = index
        self._fallback = unspecified_fallback

    def match(self, flow: BafuFlow, cas: str | None) -> Match | Unmatched:
        """Resolve ``flow`` (with an optional ``cas`` number) to one EF flow, or report why not."""
        bucket = bucket_of_bafu_category(flow.category)
        if bucket is None:
            return Unmatched("non_ef_compartment", flow.category)
        if flow.subcategory not in KNOWN_SUBCATEGORIES:
            return Unmatched("unknown_sub_compartment", flow.subcategory)
        for matcher in self._matchers:
            candidates = matcher.candidates(flow, cas, self._index)
            if not candidates:
                continue
            placed = {
                c: place(flow.category, flow.subcategory, c.flow.context_path) for c in candidates
            }
            exact = [c for c, p in placed.items() if p is Placement.EXACT]
            if exact:
                return self._pick(exact, flow, cas, matcher.tier, Placement.EXACT, ())
            fallback = [c for c, p in placed.items() if p is Placement.UNSPECIFIED]
            if fallback and self._fallback:
                human = _LEAF_HUMAN.get(flow.subcategory, flow.subcategory)
                caveat = (
                    f"EF has no {human} flow for this substance; the unspecified context is used"
                )
                return self._pick(
                    fallback, flow, cas, matcher.tier, Placement.UNSPECIFIED, (caveat,)
                )
            leafs = sorted({leaf_of(c.flow.context_path) for c in candidates})
            name = sorted({c.flow.name.lower() for c in candidates})[0]
            return Unmatched(
                "sub_compartment_absent", f"EF has {name} only in: " + ", ".join(leafs)
            )
        return Unmatched("no_ef_flow", _NO_MATCH.format(bucket=bucket))

    def _pick(self, candidates, flow, cas, tier, placement, caveats) -> Match | Unmatched:
        names = sorted({c.flow.name.lower() for c in candidates})
        chosen = sorted(candidates, key=lambda c: c.flow.code)
        if len(names) > 1:
            identities = {self._index.identity(c.flow.code) for c in candidates}
            if len(identities) > 1:
                key = f"CAS {cas}" if tier == "cas" else f"{tier} match"
                return Unmatched(
                    "ambiguous_substances",
                    f"{key} names {len(names)} EF substances with different factors: "
                    + ", ".join(names),
                )
            # identical factors: the choice is free, take the name closest to the source.
            # BAFU often bakes several comma-separated synonyms into one flow name (e.g.
            # "Methane, tetrachloro-, CFC-10"); score each candidate against whichever
            # segment lines up best, not the whole name at once.
            segments = [s.strip() for s in flow.name.lower().split(",")] or [flow.name.lower()]
            chosen = sorted(
                candidates,
                key=lambda c: (
                    -max(
                        difflib.SequenceMatcher(None, seg, c.flow.name.lower()).ratio()
                        for seg in segments
                    ),
                    c.flow.code,
                ),
            )
        pick = chosen[0]
        extra: tuple[str, ...] = ()
        if pick.caveat:
            extra += (pick.caveat,)
        if pick.region:
            extra += (
                f"regional aggregate {pick.region} in the source name; "
                "EF applies the global default factor",
            )
        return Match(
            code=pick.flow.code,
            tier=pick.tier or tier,
            placement=placement.value,
            location=pick.location,
            candidates=len(candidates),
            caveats=caveats + extra,
        )


def default_pipeline(
    index: EfFlowIndex, aliases: Mapping[str, str | Alias], *, unspecified_fallback: bool = True
) -> MatchPipeline:
    """Build the standard pipeline: name, synonym, CAS, qualifier, alias, then region-stripped.

    ``aliases`` is the curated BAFU-name -> EF-preferred-label table (see
    ``matchers.load_aliases``). ``unspecified_fallback`` is forwarded to
    ``MatchPipeline``.
    """
    base: list[Matcher] = [
        ExactNameMatcher(),
        SynonymMatcher(),
        CasMatcher(),
        QualifierMatcher(),
        AliasMatcher(aliases),
    ]
    return MatchPipeline(
        [*base, RegionStripMatcher(base)], index, unspecified_fallback=unspecified_fallback
    )
