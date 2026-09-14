"""Match a BAFU flow to one EF flow: matchers in tier order, then placement, then
disambiguation. Anything that cannot be asserted comes back as ``Unmatched`` with a
reason a reviewer can act on.

Tier order: exact name, land-use class, ore composite, synonym, qualifier spelling,
carbon-oxide rewrite, ion-strip, curated alias, the same eight tiers again applied to
the region-stripped name (``RegionStripMatcher`` applies that same first-hit rule
among its own inner matchers), then CAS last. The first matcher whose candidates
resolve to a ``Match``, or to an ``Unmatched`` other than ``sub_compartment_absent``,
decides the outcome. ``sub_compartment_absent`` alone is not final (round 4, decision
2026-09-13): it means this matcher's candidates exist in EF but not in the flow's own
sub-compartment, which a later tier's candidates may still place correctly (e.g. a
name-tier hit that only exists on an unplaceable leaf, followed by a CAS-tier hit on
the very same substance in the right leaf) -- so the pipeline keeps trying later tiers
instead of stopping there. The first ``Match`` any tier produces wins; if none ever
does, the FIRST ``sub_compartment_absent`` seen is returned (it names the substance,
so it is the most informative of however many dead tiers followed). Every other
``Unmatched`` reason (``ambiguous_substances`` included) still stops the pipeline
outright, exactly as before. When a ``Match`` is only reached this way and its own
tier is ``cas``, an extra caveat names the CAS used, since the EF target's own name
found by CAS may not resemble the source name at all (BAFU
``2-Methyl-4-chlorophenoxyacetic acid`` onto EF's
``(4-Chloro-2-methylphenoxy)acetic acid``, CAS 94-74-6, MCPA, is exactly such a case).

After a matcher's candidates fail both EXACT and UNSPECIFIED placement, one more
placement is tried before giving up: the resource-branch fallback (decision (b),
2026-09-13). A BAFU resource sub-compartment that carries no information about the
extraction medium (``compartments.is_uninformative_resource_sub``) may still be placed
on the one EF resource branch that holds the substance, provided every candidate the
matcher found lands on the very same EF leaf -- if the candidates spread over more
than one leaf, there is nothing to choose between and the flow stays
``sub_compartment_absent``.

Nomenclature package only (decision (f)(1), 2026-09-13): when every remaining candidate is
uncharacterised (``not Candidate.flow.characterised``) and the index was built with
``include_uncharacterised=True``, one more placement -- ``Placement.NOMENCLATURE`` --
is tried before ``sub_compartment_absent``: the candidates carry no factor in any
case, so relaxing which sub-compartment they are asserted on is a nomenclature
statement only, never a factor claim. When the candidates share one EF leaf that
leaf is used outright; when they spread over several, the preferred leaf is the
bucket-level unspecified leaf for THIS source's own long-term-ness (``unspecified_leaf``
with ``long_term`` read off the BAFU sub-compartment), falling back to the plain
(non-long-term) unspecified leaf, then the alphabetically first leaf -- the two-step
fallback matters because the long-term leaf never actually holds an uncharacterised
candidate in practice, so a ``*, long-term`` source still lands on the ordinary
unspecified leaf when that is what the candidates offer, rather than skipping straight
to an arbitrary alphabetical pick. The caveat names every leaf the name was found in
when there is more than one, and which one was picked, so it never overstates
"only" when several exist. This never fires for the matched package's
(biosphere-3-matched) characterised-only index: that index carries no uncharacterised
flow at all, so the ``not characterised`` condition can never hold for it.

Round 4, decision 2026-09-13, two more placements, tried in this order, both after
NOMENCLATURE and before giving up as ``sub_compartment_absent`` (so they never steal
the eight existing nomenclature-package ``groundwater, long-term`` ->
``Placement.NOMENCLATURE`` rows, which are settled before either ever runs):

- ``Placement.LONG_TERM_COLLAPSED``: whenever the BAFU sub-compartment carries
  ``, long-term`` and nothing has placed yet, placement is retried with that suffix
  stripped (``river, long-term`` -> ``river``, ``groundwater, long-term`` ->
  ``groundwater``, ``low. pop., long-term`` -> ``low. pop.``) -- EF has no long-term
  leaf at all for this substance, so the immediate-emission flow is used instead, with
  a caveat saying so (plus the ordinary unspecified-fallback caveat too, when the
  stripped placement itself only reached EXACT via that fallback).
- ``Placement.DEFAULT_LEAF``: a ``water`` / ``unspecified`` BAFU source whose
  candidates exist on neither the unspecified nor the unspecified (long-term) leaf, but
  do exist on fresh water, is placed there instead (``DEFAULT_LEAF`` table below); air,
  soil and resource get no such fallback -- there is no single obvious "default" leaf
  for them the way fresh water is the default body of water.

Round 4, decision 2026-09-13, one more tiebreak inside ``_pick``, tried only after the
source CAS itself fails to single out exactly one candidate (no source CAS, or the CAS
matches none or several of them -- a CAS that DOES single one out is positive evidence
and always wins, never overridden by the refrigerant code): when several same-leaf
candidates carry different CF identities (normally ``ambiguous_substances``) and the
source name ends in a refrigerant code (``, CFC-10``, ``, HCFC-140``, ...) that names
exactly one of the candidates, that candidate is used -- but only when its own CF
identity characterises every impact-category method id at least one other candidate
does (coordinator decision, round 4 review): BAFU sometimes bakes several synonyms for
one substance into its own name, and the refrigerant code is the most specific of them,
but two EF flows for the same substance can be genuinely complementary (one carries
climate/POF/ecotoxicity factors, the other ozone-depletion/human-toxicity ones) rather
than duplicates, and picking the code-named one must never silently drop an impact
category the other one alone would have supplied -- when it would, this tiebreak
declines and the ambiguity is reported as usual.

Round 6, decision 2026-09-14: a label-collision guard inside ``_pick``
(``_label_collision``) catches the sentier-vocab pref_label-defect shape that
``ef_index.load_label_defects`` fixes for known cases -- two or more same-leaf
candidates sharing the exact same lowercase name but carrying different, present CAS
numbers (two distinct real substances filed under one EF preferred label). When the
source CAS matches NONE of them (no source CAS at all, or one naming a substance
outside this candidate set), this is reported as
``Unmatched("ambiguous_substances", ...)`` instead of silently resolved by name
similarity or code order, the way an ordinary "several synonymous EF flows, identical
factors" tie is. A source CAS that DOES match at least one candidate is treated as
real evidence and never triggers the guard, even when more than one candidate shares
that CAS (a genuine duplicate entry rather than a different substance) -- the ordinary
free-choice narrowing (with its own caveat) still applies among those.
"""

from __future__ import annotations

import difflib
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace

from sentier_importers.matching.compartments import (
    KNOWN_SUBCATEGORIES,
    Placement,
    bucket_of_bafu_category,
    is_uninformative_resource_sub,
    place,
    unspecified_leaf,
)
from sentier_importers.matching.ef_index import EfFlowIndex, normalise_cas
from sentier_importers.matching.matchers import (
    Alias,
    AliasMatcher,
    Candidate,
    CarbonOxideMatcher,
    CasMatcher,
    ExactNameMatcher,
    IonStripMatcher,
    LandUseMatcher,
    Matcher,
    OreCompositeMatcher,
    QualifierMatcher,
    RegionStripMatcher,
    SynonymMatcher,
)
from sentier_importers.sources.eaternity.bridge import BafuFlow

#: BAFU sub-compartment -> how EF would have named it, for the fallback caveat.
#: Absent here: ``unspecified`` (already exact on its own bucket-level leaf) and
#: every ``*, long-term`` subcategory, whose family already owns the bucket-level
#: long-term-unspecified leaf too (see compartments.py) -- neither ever reaches this
#: fallback branch (the ordinary UNSPECIFIED one, keyed on the source's own
#: sub-compartment), so neither needs a translation here. A ``*, long-term``
#: subcategory's STRIPPED form (``river``, ``groundwater``, ``low. pop.``) is looked up
#: here too, though, by ``Placement.LONG_TERM_COLLAPSED`` below -- and every stripped
#: form is already a plain key in this same table.
_LEAF_HUMAN = {
    "groundwater": "ground water",
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
    "indoor": "air, indoor",
}


def _unspecified_caveat(subcategory: str) -> str:
    """The caveat for landing on the bucket-level unspecified fallback leaf.

    Shared by the ordinary ``Placement.UNSPECIFIED`` branch (``subcategory`` is the
    flow's own) and ``Placement.LONG_TERM_COLLAPSED``'s own stripped-fallback branch
    (``subcategory`` is the ``, long-term``-stripped form) -- both phrase the same
    fact the same way, so this is the one place that wording lives.
    """
    human = _LEAF_HUMAN.get(subcategory, subcategory)
    return f"EF has no {human} flow for this substance; the unspecified context is used"


#: Suffix a BAFU emission sub-compartment carries when it is the long-term variant of
#: a plainer one; ``Placement.LONG_TERM_COLLAPSED`` strips it and retries placement.
_LONG_TERM_SUFFIX = ", long-term"
_LONG_TERM_CAVEAT = (
    "EF has no long-term leaf for this substance; the immediate-emission flow is used "
    "(decision 2026-09-13)"
)
#: Round 4, decision 2026-09-13: the one EF leaf a bucket's ``unspecified`` BAFU source
#: falls back onto (``Placement.DEFAULT_LEAF``) when EF has neither the unspecified nor
#: the unspecified (long-term) leaf for the substance. Only ``water`` gets one -- fresh
#: water is the obvious default body of water; air, soil and resource have no equally
#: obvious single default leaf, so they get none.
DEFAULT_LEAF: dict[str, str] = {"water": "emissions to fresh water"}
_DEFAULT_LEAF_CAVEAT = (
    "EF has no unspecified leaf for this substance; fresh water is used (decision 2026-09-13)"
)
#: Round 4, decision 2026-09-13: a refrigerant code BAFU trails a substance name with
#: (``Methane, tetrachloro-, CFC-10``), checked by ``MatchPipeline._pick`` when several
#: same-leaf candidates carry different CF identities and one of them is named exactly
#: by the code -- the code is the most specific synonym BAFU gives, and singles out the
#: EF flow that keeps its own factor apart from the substance's more generic name.
_REFRIGERANT_CODE_RE = re.compile(r"^(CFC|HCFC|HFC|HFE|PFC|Halon)-\S+$", re.IGNORECASE)
#: ``no_ef_flow`` detail template, picked by ``EfFlowIndex.includes_uncharacterised``:
#: a characterised-only index really did restrict the search to factor-bearing flows,
#: so it is honest to say so; the inclusive index (nomenclature package) searched
#: uncharacterised flows too, so the detail must not imply otherwise.
_NO_MATCH_CHARACTERISED = (
    "no EF 3.1 flow with a factor matches by name, synonym, qualifier, carbon-oxide, "
    "ion-strip, land-use class, ore composite, alias, region-stripped name or CAS in "
    "the {bucket} compartment"
)
_NO_MATCH_INCLUSIVE = (
    "no EF 3.1 flow, with or without a factor, matches by name, synonym, qualifier, "
    "carbon-oxide, ion-strip, land-use class, ore composite, alias, region-stripped "
    "name or CAS in the {bucket} compartment"
)


@dataclass(frozen=True)
class Match:
    """One BAFU flow resolved to one EF flow."""

    code: str
    tier: str
    placement: str
    location: str | None
    candidates: int  #: size of the winning placement group, before disambiguation
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
        resource_fallback: bool = True,
    ) -> None:
        """Build the pipeline from an ordered ``matchers`` sequence and its ``index``.

        ``unspecified_fallback`` controls whether a candidate that only exists on the
        bucket-level "unspecified" EF context is accepted (with a caveat) when the
        BAFU sub-compartment names something more specific; disabling it turns that
        case into a ``sub_compartment_absent`` ``Unmatched`` instead.

        ``resource_fallback`` controls the resource-branch fallback (decision (b),
        2026-09-13): whether an uninformative BAFU resource sub-compartment
        (``compartments.is_uninformative_resource_sub``) may be placed on the one EF
        resource branch its candidates agree on; disabling it turns that case into a
        ``sub_compartment_absent`` ``Unmatched`` too.
        """
        self._matchers = list(matchers)
        self._index = index
        self._fallback = unspecified_fallback
        self._resource_fallback = resource_fallback

    def match(self, flow: BafuFlow, cas: str | None) -> Match | Unmatched:
        """Resolve ``flow`` (with an optional ``cas`` number) to one EF flow, or report why not.

        Guards the flow's compartment and sub-compartment, then tries each matcher in
        tier order, handing its candidates to ``_resolve``. The first ``Match`` any
        tier produces wins outright. An ``Unmatched`` other than
        ``sub_compartment_absent`` stops the pipeline immediately too -- a later tier
        never rescues, say, a genuine ``ambiguous_substances`` call. Round 4, decision
        2026-09-13: ``sub_compartment_absent`` alone does not stop the pipeline; the
        first one seen is remembered and later tiers still get a chance, since "the
        exact-name EF flow exists but only in another sub-compartment" does not mean a
        later tier's candidates (a synonym, an alias, the bare CAS number) cannot still
        place correctly. If no tier ever produces a ``Match``, the first
        ``sub_compartment_absent`` recorded is returned (the most informative of
        however many dead tiers followed, since it names the substance); if none of the
        tiers even produced that much, the generic ``no_ef_flow`` is returned instead.

        When a ``Match`` is only reached after skipping at least one
        ``sub_compartment_absent`` and its own tier is ``cas``, an extra caveat names
        the CAS: CAS is name-blind, so the EF flow it lands on may carry a name that
        does not resemble the source at all (module docstring).
        """
        bucket = bucket_of_bafu_category(flow.category)
        if bucket is None:
            return Unmatched("non_ef_compartment", flow.category)
        if flow.subcategory not in KNOWN_SUBCATEGORIES:
            return Unmatched("unknown_sub_compartment", flow.subcategory)
        first_absent: Unmatched | None = None
        for matcher in self._matchers:
            candidates = matcher.candidates(flow, cas, self._index)
            outcome = self._resolve(candidates, flow, cas, matcher)
            if outcome is None:
                continue
            if isinstance(outcome, Match):
                if first_absent is not None and outcome.tier == "cas":
                    normalised_cas = normalise_cas(cas)
                    caveat = (
                        f"an earlier tier's match could not be placed in this "
                        f"sub-compartment; resolved instead by CAS {normalised_cas}, "
                        "whose EF target name may not resemble the source name"
                    )
                    outcome = replace(outcome, caveats=outcome.caveats + (caveat,))
                return outcome
            if outcome.reason != "sub_compartment_absent":
                return outcome
            if first_absent is None:
                first_absent = outcome
        if first_absent is not None:
            return first_absent
        template = (
            _NO_MATCH_INCLUSIVE
            if self._index.includes_uncharacterised
            else _NO_MATCH_CHARACTERISED
        )
        return Unmatched("no_ef_flow", template.format(bucket=bucket))

    def _resolve(
        self,
        candidates: list[Candidate],
        flow: BafuFlow,
        cas: str | None,
        matcher: Matcher,
    ) -> Match | Unmatched | None:
        """Place and disambiguate one matcher's ``candidates``, or ``None`` to try the
        next matcher (an empty ``candidates`` list -- this matcher found nothing in
        the flow's compartment at all).

        In order: EXACT placement wins outright; failing that, UNSPECIFIED placement
        (the bucket-level fallback, ``unspecified_fallback`` permitting); failing
        that too, and only for the ``resource`` bucket, the resource-branch fallback
        (``resource_fallback`` permitting, and only when
        ``compartments.is_uninformative_resource_sub`` says the sub-compartment is
        uninformative and every candidate lands on the same EF leaf -- disambiguation
        can still turn this into ``ambiguous_substances`` when those candidates
        differ in identity and no CAS singles one out); failing that, and only when
        every remaining candidate is uncharacterised and the index includes
        uncharacterised flows (nomenclature package only, decision (f)(1)), the relaxed
        nomenclature placement (module docstring); failing that, and only when the BAFU
        sub-compartment carries ``, long-term``, ``Placement.LONG_TERM_COLLAPSED``
        (module docstring); failing that, and only for a ``water`` / ``unspecified``
        source, ``Placement.DEFAULT_LEAF`` (module docstring); otherwise
        ``sub_compartment_absent``, naming every distinct candidate name and leaf so a
        reviewer can see what EF actually offers.
        """
        if not candidates:
            return None
        # a list of (candidate, placement) pairs, not a dict keyed by candidate:
        # Candidate is a frozen dataclass, so distinct-but-equal candidates could
        # otherwise collapse and silently drop a duplicate.
        placed = [
            (
                c,
                place(
                    flow.category,
                    c.subcategory_override or flow.subcategory,
                    c.flow.context_path,
                ),
            )
            for c in candidates
        ]
        exact = [c for c, p in placed if p is Placement.EXACT]
        if exact:
            return self._pick(exact, flow, cas, matcher.tier, Placement.EXACT, ())
        fallback = [c for c, p in placed if p is Placement.UNSPECIFIED]
        if fallback and self._fallback:
            caveat = _unspecified_caveat(flow.subcategory)
            return self._pick(fallback, flow, cas, matcher.tier, Placement.UNSPECIFIED, (caveat,))
        leafs = sorted({c.flow.leaf for c in candidates})
        names = sorted({c.flow.name.lower() for c in candidates})
        bucket = bucket_of_bafu_category(flow.category)
        if (
            self._resource_fallback
            and bucket == "resource"
            and len(leafs) == 1
            and is_uninformative_resource_sub(flow.category, flow.subcategory, flow.name)
        ):
            caveat = (
                f"BAFU files this resource under {flow.subcategory}; "
                f"EF has {', '.join(names)} only as {leafs[0]}"
            )
            return self._pick(
                candidates, flow, cas, matcher.tier, Placement.RESOURCE_BRANCH, (caveat,)
            )
        if self._index.includes_uncharacterised and all(
            not c.flow.characterised for c in candidates
        ):
            # prefer the long-term unspecified leaf for a "*, long-term" source (it
            # never holds an uncharacterised candidate, so this is usually a no-op),
            # then the plain unspecified leaf, then the alphabetically first leaf.
            preferred = [
                unspecified_leaf(bucket, long_term="long-term" in flow.subcategory),
                unspecified_leaf(bucket, long_term=False),
            ]
            leaf = next((p for p in preferred if p in leafs), leafs[0])
            if len(leafs) == 1:
                nomenclature_caveat = (
                    f"EF has this name only in {leaf}; placed there for nomenclature "
                    "alignment (no factor)"
                )
            else:
                listed = ", ".join(repr(candidate_leaf) for candidate_leaf in leafs)
                nomenclature_caveat = (
                    f"EF has this name only in {listed}; placed on {leaf!r} "
                    "for nomenclature alignment (no factor)"
                )
            leaf_candidates = [c for c in candidates if c.flow.leaf == leaf]
            return self._pick(
                leaf_candidates,
                flow,
                cas,
                matcher.tier,
                Placement.NOMENCLATURE,
                (nomenclature_caveat,),
            )
        # Ordered after NOMENCLATURE, not before: test_relaxed_placement_falls_back_
        # to_the_alphabetically_first_leaf and test_relaxed_placement_prefers_the_
        # non_long_term_unspecified_leaf_too both pin the nomenclature package's own
        # long-term/unspecified leaf preference among uncharacterised candidates, and neither
        # LONG_TERM_COLLAPSED nor DEFAULT_LEAF (both characterised-or-not, unlike
        # NOMENCLATURE) may pre-empt that.
        if flow.subcategory.endswith(_LONG_TERM_SUFFIX):
            stripped_sub = flow.subcategory[: -len(_LONG_TERM_SUFFIX)]
            stripped_placed = [
                (c, place(flow.category, stripped_sub, c.flow.context_path)) for c in candidates
            ]
            stripped_exact = [c for c, p in stripped_placed if p is Placement.EXACT]
            if stripped_exact:
                return self._pick(
                    stripped_exact,
                    flow,
                    cas,
                    matcher.tier,
                    Placement.LONG_TERM_COLLAPSED,
                    (_LONG_TERM_CAVEAT,),
                )
            stripped_fallback = [c for c, p in stripped_placed if p is Placement.UNSPECIFIED]
            if stripped_fallback and self._fallback:
                fallback_caveat = _unspecified_caveat(stripped_sub)
                return self._pick(
                    stripped_fallback,
                    flow,
                    cas,
                    matcher.tier,
                    Placement.LONG_TERM_COLLAPSED,
                    (_LONG_TERM_CAVEAT, fallback_caveat),
                )
        if bucket in DEFAULT_LEAF and flow.subcategory == "unspecified":
            # Gated on the FULL candidate set, not just the ones on the default leaf:
            # a candidate already sitting on either unspecified leaf (plain or
            # long-term) is a strictly better placement than the default-leaf guess,
            # even though neither EXACT nor the ordinary UNSPECIFIED branch above
            # caught it here -- EXACT only ever catches the plain (non-long-term)
            # leaf for an "unspecified" source, and the ordinary UNSPECIFIED fallback
            # above is keyed off the SAME plain leaf too, so a candidate that exists
            # only on the long-term unspecified leaf slips past both unblocked.
            blocking_leafs = {
                leaf
                for leaf in (
                    unspecified_leaf(bucket, long_term=False),
                    unspecified_leaf(bucket, long_term=True),
                )
                if leaf is not None
            }
            if not any(c.flow.leaf in blocking_leafs for c in candidates):
                default_candidates = [c for c in candidates if c.flow.leaf == DEFAULT_LEAF[bucket]]
                if default_candidates:
                    return self._pick(
                        default_candidates,
                        flow,
                        cas,
                        matcher.tier,
                        Placement.DEFAULT_LEAF,
                        (_DEFAULT_LEAF_CAVEAT,),
                    )
        return Unmatched(
            "sub_compartment_absent",
            f"EF has {', '.join(names)} only in: " + ", ".join(leafs),
        )

    def _pick(
        self,
        candidates: list[Candidate],
        flow: BafuFlow,
        cas: str | None,
        tier: str,
        placement: Placement,
        caveats: tuple[str, ...],
    ) -> Match | Unmatched:
        """Choose one candidate, or report why several cannot be told apart.

        Runs whenever there is more than one candidate, whether or not their names
        agree (two EF flows can share a name and still carry different factors, e.g.
        two "Methanol" entries in different impact categories). In order:

        1. every candidate's CF identity (``EfFlowIndex.identity``, which deliberately
           excludes location-specific factors -- regionalised differences are never
           compared) agrees -- the choice is free, UNLESS it is actually a label
           collision (round 6, decision 2026-09-14): two or more candidates share the
           exact same lowercase name yet carry different, present CAS numbers, and the
           source CAS matches NONE of them (no source CAS at all, or one that names a
           substance absent from this candidate set entirely) -- that is not a free
           choice between interchangeable synonyms, it is the
           ``ef_index.load_label_defects`` shape (two distinct real substances sharing
           one EF preferred label) showing up on a substance no curated defect entry
           names yet, and it is reported as ``Unmatched("ambiguous_substances", ...)``
           (``_label_collision``) rather than silently resolved. A source CAS that DOES
           match at least one candidate is real evidence for one specific substance,
           even when more than one EF flow shares that CAS (a genuine duplicate entry)
           -- that is not a collision between different substances, so it falls
           through to the ordinary free-choice narrowing below instead (still with its
           own "never silent" caveat, since more than one candidate remains). Absent a
           collision, prefer a candidate the source CAS actually names, as long as that
           still leaves at least one; among what's left, take
           whichever candidate's name is textually closest to the source, then break
           any remaining tie by code. If the full candidate set carried more than one
           distinct CAS, or the source CAS is known but no candidate carries it while
           at least one candidate carries a different CAS, the choice is never silent:
           it gets a caveat naming what was picked and what it was picked over (and, in
           the latter case, that the source CAS matched none);
        2. identities disagree, but the source carries a CAS number that singles out
           exactly one candidate by ``flow.cas`` -- pick that one (no extra caveat: a
           matching CAS is positive evidence, not a guess, and it is checked FIRST:
           it must never be overridden by rule 3 below);
        3. identities disagree and rule 2 did not single one out (no source CAS, or
           the CAS matches none or several candidates), but the source name ends in a
           refrigerant code (``, CFC-10``, ``, HCFC-140``, ...; round 4, decision
           2026-09-13) that names exactly one candidate, AND that candidate's CF
           identity characterises every impact-category method id at least one other
           candidate does (coordinator decision, round 4 review: two EF flows sharing
           a CAS can be genuinely complementary rather than duplicates -- ``HCFC-140``
           carries climate/POF/ecotoxicity factors while ``1,1,1-trichloroethane``
           carries ozone-depletion/human-toxicity ones for the same CAS, and picking
           the code-named flow there would silently drop the other's impact
           categories) -- pick that one, with a caveat naming the other candidate(s)
           the code disambiguated against;
        4. otherwise, report ``Unmatched("ambiguous_substances", ...)``.
        """
        chosen = sorted(candidates, key=lambda c: c.flow.code)
        identity_caveat: tuple[str, ...] = ()
        if len(candidates) > 1:
            # () -- every factor is location-specific -- would compare equal here too;
            # no EF 3.1 flow has one today, but the comparison would still be correct.
            identities = {self._index.identity(c.flow.code) for c in candidates}
            normalised_cas = normalise_cas(cas)
            if len(identities) > 1:
                cas_matches = (
                    [c for c in candidates if c.flow.cas == normalised_cas]
                    if normalised_cas is not None
                    else []
                )
                if len(cas_matches) == 1:
                    # a singled-out CAS is positive evidence and is never overridden
                    # by the refrigerant-code tiebreak below.
                    chosen = cas_matches
                else:
                    refrigerant = self._refrigerant_tiebreak(candidates, flow.name)
                    if refrigerant is not None and self._refrigerant_covers_every_method(
                        refrigerant, candidates
                    ):
                        chosen = [refrigerant]
                        others = sorted(c.flow.name for c in candidates if c is not refrigerant)
                        identity_caveat = (
                            "EF carries a second flow for this CAS with different "
                            f"factors ({', '.join(others)}); the flow named by the "
                            "source's refrigerant code is used; it characterises "
                            "every impact category the other does (decision "
                            "2026-09-13)",
                        )
                    else:
                        return self._ambiguous(candidates, cas, tier)
            else:
                # identical factors: the choice is free. Prefer a candidate the
                # source CAS actually names (if any does); among what's left, take
                # the name closest to the source. BAFU often bakes several
                # comma-separated synonyms into one flow name (e.g. "Methane,
                # tetrachloro-, CFC-10"); score each candidate against whichever
                # segment lines up best, not the whole name at once.
                pool = candidates
                if normalised_cas is not None:
                    cas_pool = [c for c in candidates if c.flow.cas == normalised_cas]
                    if cas_pool:
                        pool = cas_pool
                segments = [s.strip() for s in flow.name.lower().split(",")]
                chosen = sorted(
                    pool,
                    key=lambda c: (
                        -max(
                            difflib.SequenceMatcher(None, seg, c.flow.name.lower()).ratio()
                            for seg in segments
                        ),
                        c.flow.code,
                    ),
                )
                distinct_cas = {c.flow.cas for c in candidates if c.flow.cas is not None}
                # Round 6, decision 2026-09-14: a label collision, not a free choice --
                # two or more of these identically-factored candidates share the exact
                # same lowercase name yet carry different, present CAS numbers (the
                # sentier-vocab pref_label-defect shape ``label_defects.yaml`` fixes:
                # two distinct real substances sharing one EF preferred label). Unlike
                # the "free choice among genuine synonyms" case below, silently picking
                # one here would repeat that very mistake, so this is checked first and
                # fails loud UNLESS the source CAS matches at least one candidate (real
                # evidence for one specific substance, even if more than one candidate
                # happens to share that CAS -- a duplicate entry, not a collision).
                if (
                    len({c.flow.name.strip().lower() for c in candidates}) == 1
                    and len(distinct_cas) > 1
                ):
                    singled_out = (
                        [c for c in candidates if c.flow.cas == normalised_cas]
                        if normalised_cas is not None
                        else []
                    )
                    if not singled_out:
                        return self._label_collision(candidates, cas)
                    # the source CAS matched at least one candidate: real evidence for
                    # the right substance, even if more than one EF flow shares that
                    # CAS (e.g. a genuine duplicate entry) -- not a guess between
                    # different substances, so this is not a label collision. Fall
                    # through to the ordinary free-choice/CAS-pool narrowing below,
                    # which still discloses every candidate it chose over.
                # the source CAS is known but names none of the tied candidates, while
                # at least one of them carries a different (non-None) CAS: the free
                # choice above is still silently arbitrary among them and needs the
                # same "never silent" treatment as the multi-CAS case below.
                cas_names_none = bool(
                    distinct_cas
                    and normalised_cas is not None
                    and normalised_cas not in distinct_cas
                )
                if len(distinct_cas) > 1 or cas_names_none:
                    winner = chosen[0]
                    others = sorted(
                        f"{c.flow.name} (CAS {c.flow.cas or 'none'})"
                        for c in candidates
                        if c is not winner
                    )
                    cas_note = (
                        f"source CAS {normalised_cas} matches none, " if cas_names_none else ""
                    )
                    identity_caveat = (
                        f"{len(candidates)} EF flows with identical factors; {cas_note}chose "
                        f"{winner.flow.name} (CAS {winner.flow.cas or 'none'}) over "
                        + ", ".join(others),
                    )
        pick = chosen[0]
        extra: tuple[str, ...] = identity_caveat
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

    @staticmethod
    def _refrigerant_tiebreak(candidates: list[Candidate], source_name: str) -> Candidate | None:
        """The one candidate a trailing refrigerant code in ``source_name`` names, or ``None``.

        BAFU sometimes trails a substance name with a refrigerant code as its last
        comma segment (``Methane, tetrachloro-, CFC-10``, ``Ethane,
        1,1,1-trichloro-, HCFC-140``); round 4, decision 2026-09-13: when that segment
        matches ``_REFRIGERANT_CODE_RE`` and exactly one candidate's own name equals it
        (case-insensitively), that candidate is the answer -- the code is the most
        specific synonym BAFU gives for the substance, and singles out the EF flow
        that keeps its own factor apart from the substance's more generic name. Any
        other shape (no trailing code, or the code matching zero or several
        candidates) yields ``None``, leaving ``_pick`` to fall through to its
        CAS-based disambiguation unchanged.
        """
        code = source_name.rsplit(",", 1)[-1].strip()
        if not _REFRIGERANT_CODE_RE.match(code):
            return None
        matches = [c for c in candidates if c.flow.name.strip().lower() == code.lower()]
        return matches[0] if len(matches) == 1 else None

    def _refrigerant_covers_every_method(
        self, refrigerant: Candidate, candidates: list[Candidate]
    ) -> bool:
        """Whether ``refrigerant``'s CF identity drops no impact category the other
        candidates alone would have supplied.

        Coordinator decision, round 4 review: two EF flows sharing one CAS can be
        genuinely complementary rather than duplicates of each other (one carries
        climate/POF/ecotoxicity factors, the other ozone-depletion/human-toxicity
        ones for the very same substance) -- picking the refrigerant-code-named flow
        must never silently drop an impact category only the OTHER candidate(s)
        characterise. Compares method ids only (``EfFlowIndex.identity`` pairs'
        first element), never factor values: the two flows are expected to disagree
        on values (that is exactly why ``_pick`` reached this branch at all), and
        this check is about category *coverage*, not agreement.
        """
        covered = {method for method, _ in self._index.identity(refrigerant.flow.code)}
        other_methods = {
            method
            for c in candidates
            if c is not refrigerant
            for method, _ in self._index.identity(c.flow.code)
        }
        return other_methods <= covered

    def _ambiguous(self, candidates: list[Candidate], cas: str | None, tier: str) -> Unmatched:
        """Report several candidates with different factors that no CAS singles out.

        Uses the first candidate's own tier when set (e.g. ``region/name``) rather
        than the matcher-level tier, so a region-wrapped ambiguity reads
        ``region/name match`` instead of the less specific ``region match``.
        """
        effective_tier = candidates[0].tier or tier
        normalised_cas = normalise_cas(cas)
        key = f"CAS {normalised_cas}" if effective_tier == "cas" else f"{effective_tier} match"
        names = ", ".join(sorted({c.flow.name.lower() for c in candidates}))
        source = (
            "no source CAS"
            if normalised_cas is None
            else f"the source CAS {normalised_cas} does not single one out"
        )
        return Unmatched(
            "ambiguous_substances",
            f"{key} finds {len(candidates)} EF flows with different factors for {names}; {source}",
        )

    @staticmethod
    def _label_collision(candidates: list[Candidate], cas: str | None) -> Unmatched:
        """Report a same-leaf EF pref_label collision: two or more candidates sharing
        the exact same lowercase name but carrying different, present CAS numbers,
        when the source CAS matches none of them (or there is no source CAS).

        Round 6, decision 2026-09-14: this is the sentier-vocab label-defect shape
        (``ef_index.load_label_defects``) surfacing on a substance no curated defect
        entry names yet -- two distinct real substances sharing one EF preferred
        label. Never resolved silently, unlike the ordinary "several EF flows share
        one name with identical factors" free choice (``_pick``'s own identity-agree
        branch): those really are interchangeable synonyms for one substance, this is
        not.

        The "N EF flows" count only ever counts CAS-bearing candidates (the ones the
        CAS list actually names); a candidate with no CAS at all can still be part of
        ``candidates`` (the guard only requires 2+ *distinct present* CAS among them),
        and is disclosed separately ("and M without CAS") rather than silently folded
        into a count the CAS list does not itself account for.
        """
        name = candidates[0].flow.name.strip().lower()
        with_cas = [c for c in candidates if c.flow.cas is not None]
        without_cas = len(candidates) - len(with_cas)
        cas_values = ", ".join(sorted({c.flow.cas for c in with_cas}))
        count = f"{len(with_cas)}" + (f" (and {without_cas} without CAS)" if without_cas else "")
        normalised_cas = normalise_cas(cas)
        source = (
            "no source CAS to arbitrate"
            if normalised_cas is None
            else f"the source CAS {normalised_cas} matches none of them"
        )
        return Unmatched(
            "ambiguous_substances",
            f"label collision: {count} EF flows named {name} with different "
            f"CAS ({cas_values}); {source}",
        )


def default_pipeline(
    index: EfFlowIndex,
    aliases: Mapping[str, str | Alias],
    *,
    unspecified_fallback: bool = True,
    resource_fallback: bool = True,
) -> MatchPipeline:
    """Build the standard pipeline: name, land-use class, ore composite, synonym,
    qualifier, carbon-oxide rewrite, ion-strip, alias, region-stripped, CAS.

    ``aliases`` is the curated BAFU-name -> EF-preferred-label table (see
    ``matchers.load_aliases``). ``unspecified_fallback`` and ``resource_fallback`` are
    both forwarded to ``MatchPipeline`` unchanged.

    ``LandUseMatcher`` runs second, right after ``ExactNameMatcher`` and before
    ``SynonymMatcher``: an EF flow's ``alt_labels`` are free-form BAFU-side data, and a
    synonym that happens to collide with a land-use name (``Occupation, dump site``,
    say) must not be allowed to steal the match away from the land-use rules -- once a
    matcher yields any candidate, ``MatchPipeline.match`` commits to it and never tries
    a later tier (see the module docstring). ``ExactNameMatcher`` is left ahead of it
    because an EF preferred label never looks like a BAFU ``Occupation``/
    ``Transformation`` name in practice, so there is nothing for it to steal.

    ``OreCompositeMatcher`` runs third, right after ``LandUseMatcher``: an ore
    composite's own name (``Zinc, Zn 0.63%, ..., in ore``) never looks like an EF
    preferred label or a BAFU land-use name, so ordering it there costs nothing, and
    it must still run ahead of ``SynonymMatcher``/``QualifierMatcher`` for the same
    reason ``LandUseMatcher`` does: an accidental synonym collision must not steal an
    ore composite's match away from the element-decomposition rule.

    CAS runs last, after every name-keyed tier including the region-stripped ones, not
    third: a shared CAS number (the biogenic/fossil/land-use-change carbon dioxide
    family, the EF water-use flows, ...) is ambiguous by construction, and an
    ambiguity stops the pipeline outright (see ``MatchPipeline.match``). The
    qualifier, alias and region-strip tiers exist precisely to resolve those same
    names before CAS gets a chance to declare them ambiguous -- a region-stripped name
    (``"Water, KR"`` -> ``"Water"``) is still name evidence, not weaker than CAS, so it
    must run before CAS too. With CAS first, "Carbon dioxide, fossil" was reported
    unmatched in every air sub-compartment instead of resolving through the qualifier
    spelling, and "Water, KR" resolved to (or, for the real EF water families,
    remained ambiguous by) a bare CAS lookup instead of the more specific
    region-stripped name match.

    ``CarbonOxideMatcher`` and ``IonStripMatcher`` both run right after
    ``QualifierMatcher``, ahead of ``AliasMatcher``: both rewrite a BAFU name to an EF
    spelling the same way ``QualifierMatcher`` does (decisions (d) and (e),
    2026-09-13). Since the pipeline commits to the first tier that yields any
    candidate, this ordering means a name either of them claims (a bare carbon oxide,
    an ion/oxidation-state-shaped name) is decided by them, carrying their own
    decision-specific caveat, and never reaches ``AliasMatcher`` at all -- a curated
    alias line for such a name would simply never fire (dead weight, not a second
    opinion). ``AliasMatcher`` only ever resolves the names neither of these two
    claims. Both must still run after ``LandUseMatcher``/``OreCompositeMatcher``/
    ``SynonymMatcher``, for the same reason those are ordered ahead of
    ``QualifierMatcher`` above: a more specific earlier tier's match is never stolen.
    """
    named: list[Matcher] = [
        ExactNameMatcher(),
        LandUseMatcher(),
        OreCompositeMatcher(),
        SynonymMatcher(),
        QualifierMatcher(),
        CarbonOxideMatcher(),
        IonStripMatcher(),
        AliasMatcher(aliases),
    ]
    return MatchPipeline(
        [*named, RegionStripMatcher(named), CasMatcher()],
        index,
        unspecified_fallback=unspecified_fallback,
        resource_fallback=resource_fallback,
    )
