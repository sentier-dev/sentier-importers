"""bafu-2026-v1 -> EF 3.1 CF keys by public matching (the characterised package,
biosphere-3-matched).

For every BAFU-2026 v1 elementary flow that neither the curated (biosphere-1-curated)
nor the inferred (biosphere-2-inferred) package maps, run
``matching.pipeline.default_pipeline`` (name, land-use class, ore composite, synonym,
qualifier, carbon-oxide, ion-strip, alias, the same eight tiers again on the
region-stripped name, CAS last) against the public EF flow index and emit one
``replace`` entry per match. Withheld flows are emitted by the sibling coverage
source. Round 4, decision 2026-09-13: a tier whose candidates exist in EF but not in
the flow's own sub-compartment (``sub_compartment_absent``) no longer stops the
pipeline outright; a later tier -- most often CAS, after every name-keyed tier has
failed to place -- may still resolve it (``pipeline.MatchPipeline.match``).

Inputs: the ecoSpold zip (primary), ``curated`` and ``inferred`` payloads (exclusion)
and ``ef_cfs`` (sentier-methods CF table) all go through the content-addressed fetch
cache like any other input. Only ``ef_vocab`` (a DIRECTORY of sentier-vocab
elementary-flow shards) bypasses it, read from its local path directly in ``parse``:
a directory has no single content digest to cache against.

Unit/dimension conversion (``unit_conversion``, ``conversion_for``, ``ENERGY_CONTENT``,
``STOICHIOMETRIC``, ``nomenclature_target_unit``) lives in the sibling ``ef_units`` module.

Round 5, decision 2026-09-14 (``_name_only_match``, wired into ``_decide``): a flow the
pipeline gives up on entirely (``Unmatched(reason="no_ef_flow")``) over the sibling
nomenclature package's inclusive index gets one more try -- "for the ones with names:
we map, else: nothing". Every EF flow anywhere in the inclusive index whose name
(``EfFlow.name``, the matching key, not ``EfFlow.label``) equals the source name,
case-insensitively, is looked up regardless of bucket
(``ef_index.EfFlowIndex.by_name_any_bucket``); if at least one exists and every one of
them is uncharacterised, the flow is aligned onto one of them by name alone, with no
context and no factor. A single characterised namesake vetoes the alignment entirely
(this source's own characterised-only index can never reach this rule at all --
gated on ``EfFlowIndex.includes_uncharacterised``). See ``_name_only_match`` and
``entry_for`` for the full rule and the comment it produces.

Round 6, decision 2026-09-14: a characterised EF target's matching key
(``EfFlow.name``) and its display name (``EfFlow.label``) are no longer the same
thing -- ``ef_index.EfFlowIndex.from_tables`` names a characterised flow from the CF
table's own JRC spelling for matching, but ``entry_for`` emits ``target["name"]``
from ``ef_flow.label`` (the vocab pref_label, when it was kept), so a BAFU flow that
matches on the JRC name can still emit the nicer, more familiar vocab spelling. See
``ef_index.py``'s own docstring and ``EfFlow.label`` for the full rule (curated
defects and same-bucket collisions suppress the vocab label back to the JRC name).
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass

import orjson
from sentier_importers.core import fetch as fetch_mod
from sentier_importers.core.context import RunContext
from sentier_importers.core.randonneur import codes_of
from sentier_importers.core.source import Source
from sentier_importers.core.types import RawData, Record, Records, Rows
from sentier_importers.matching.compartments import Placement, unspecified_leaf
from sentier_importers.matching.ef_index import (
    UNCERTAIN_RESOURCE_NAME,
    EfFlow,
    EfFlowIndex,
    normalise_cas,
)
from sentier_importers.matching.matchers import load_aliases
from sentier_importers.matching.pipeline import Match, MatchPipeline, Unmatched, default_pipeline
from sentier_importers.sources.bafu.ecospold import parse_ecospold_zip
from sentier_importers.sources.bafu.ef_units import (
    WATER_USE_METHOD,
    conversion_for,
    nomenclature_target_unit,
    nomenclature_unit_mismatch,
)
from sentier_importers.sources.eaternity.bridge import BafuFlow, BafuFlowIndex

_VOCAB_DIR = "ef_vocab"
_REQUIRED = ("curated", "inferred", "ef_cfs")
_BIOSPHERE_GROUP = 4

#: Ion / oxidation-state markers BAFU bakes into a name: a trailing ``, ion`` /
#: `` ion``, a trailing roman numeral (``II``-``VI``), or a trailing ``+``. Used only
#: by ``_refine_unmatched`` now: an ion-shaped name WITH an EF match is emitted, not
#: withheld (decision (d), 2026-09-13, ``matching.matchers.IonStripMatcher``); this
#: pattern still refines the case where EF carries no matching flow at all into the
#: more specific ``speciation`` reason.
_ION = re.compile(r"(,\s?ion$|\sion$|\s(II|III|IV|V|VI)$|\+$)")

#: EF's water-use method characterises freshwater deprivation only: a BAFU salt-water
#: resource flow can never receive an EF water factor, even when the pipeline finds a
#: same-named candidate (that candidate is the freshwater flow). ``Water, fossil`` is
#: deliberately NOT here (decision 2026-09-13): it is taken as non-renewable
#: groundwater and resolved via the curated ``water, fossil`` alias instead (see
#: ``matching/aliases.yaml``), each such entry carrying that decision as a caveat.
_NON_FRESHWATER = ("Water, salt",)

#: BAFU category -> the human phrase ``_name_only_match``'s own caveat names the
#: source as (round 5, decision 2026-09-14): "the source is a {phrase}, so the EF
#: context is omitted". Every category ``pipeline.MatchPipeline.match`` itself
#: recognises (``compartments.BAFU_BUCKET``) has an entry; a category outside that set
#: can never reach ``_name_only_match`` at all (the pipeline already refuses it as
#: ``non_ef_compartment`` before any tier runs), so no fallback spelling is needed.
_SOURCE_CATEGORY_HUMAN: dict[str, str] = {
    "resources": "resource extraction",
    "emissions to air": "emission to air",
    "emissions to water": "emission to water",
    "emissions to soil": "emission to soil",
}


@dataclass(frozen=True)
class ParsedInputs:
    """Everything ``BafuEfMatchedSource.parse`` builds once, for ``outcomes``/``transform``
    to reuse: the BAFU flow universe, the per-substance CAS table (and its conflicts,
    for the sibling coverage source to report), the curated, inferred and matched
    source-code sets (kept separate so the coverage sidecar can tell which package
    mapped a flow; see ``excluded``), the EF flow index and the matching pipeline built
    over it.

    ``matched_codes`` is empty unless the registry entry declares a ``matched`` input
    (only the nomenclature source does): the matched and nomenclature packages both run
    over the characterised-only index's leftovers, so the nomenclature package must
    also skip whatever the matched package itself mapped, not just curated/inferred.

    ``inclusive_index``/``inclusive_pipeline`` are the sibling coverage source's own
    fields (``None`` here): its ``parse`` override attaches an inclusive
    (``include_uncharacterised=True``) index and the pipeline built over it, so its
    ``transform`` can run the nomenclature package's second pass without touching
    ``self.inputs`` or ``self.config`` at all.
    """

    bafu: BafuFlowIndex
    cas: Mapping[str, str]
    cas_conflicts: Mapping[str, tuple[str, ...]]
    curated_codes: frozenset[str]
    inferred_codes: frozenset[str]
    index: EfFlowIndex
    pipeline: MatchPipeline
    matched_codes: frozenset[str] = frozenset()
    inclusive_index: EfFlowIndex | None = None
    inclusive_pipeline: MatchPipeline | None = None

    @property
    def excluded(self) -> frozenset[str]:
        """Every source code the curated, inferred or matched package already maps --
        this source's exclusion set.

        A derived union rather than a stored field: ``curated_codes``/
        ``inferred_codes``/``matched_codes`` are the single source of truth (the
        coverage sidecar needs them apart), and this property keeps ``outcomes``
        (which only needs the union) unchanged.
        """
        return self.curated_codes | self.inferred_codes | self.matched_codes


def flow_sort_key(flow: BafuFlow) -> tuple[str, str, str, str]:
    """The stable order every BAFU flow listing (``outcomes``, the coverage sidecar) is
    sorted by: name first, then category/subcategory/unit to break ties between flows
    sharing a name (e.g. ``Zinc`` to different sub-compartments).
    """
    return (flow.name, flow.category, flow.subcategory, flow.unit)


def _base_source(flow: BafuFlow) -> Record:
    """The two fields every ``source`` sub-record carries unconditionally."""
    return {"name": flow.name, "code": flow.code}


def source_record(flow: BafuFlow) -> Record:
    """The ``source`` sub-record shape the coverage sidecar carries for every flow:
    name, code, unit and context unconditionally (unit may be ``""`` for a unitless
    flow; the key is kept regardless). Contrast ``entry_for``, whose own ``source``
    omits a falsy unit/context -- the randonneur payload convention.
    """
    record = _base_source(flow)
    record["unit"] = flow.unit
    record["context"] = flow.context
    return record


def substance_cas(records: Records) -> tuple[dict[str, str], dict[str, tuple[str, ...]]]:
    """Normalised CAS number per substance NAME over every biosphere exchange (group 4).

    A BAFU flow name is shared across sub-compartments (``Methanol`` to air and to
    water, say); its CAS number is a property of the substance, not the placement, so
    it is collected per name rather than per (name, category, subcategory, unit).
    Every value is compared after ``normalise_cas`` (leading-zero padding, e.g.
    ``000067-56-1`` vs ``67-56-1``, must not read as two different substances), and a
    blank or missing CAS is ignored rather than counted as a value. A name whose
    exchanges disagree on (normalised) CAS gets no CAS at all -- returned instead in
    the second mapping, keyed by name and sorted for determinism, so a caller can see
    what was withheld and why (and so it round-trips through JSON unchanged).
    """
    per_name: dict[str, set[str]] = {}
    for record in records:
        for exchange in record.get("exchanges", []):
            if exchange.get("group_code") != _BIOSPHERE_GROUP:
                continue
            cas = normalise_cas(exchange.get("cas"))
            if cas is None:
                continue
            per_name.setdefault(exchange["name"], set()).add(cas)
    cas = {name: next(iter(values)) for name, values in per_name.items() if len(values) == 1}
    conflicts = {
        name: tuple(sorted(values)) for name, values in per_name.items() if len(values) > 1
    }
    return cas, conflicts


def _refine_unmatched(flow: BafuFlow, outcome: Unmatched) -> Unmatched:
    """Refine a plain "nothing matched" outcome into a more specific reason.

    A reason other than ``no_ef_flow`` is left alone -- it is already the most
    informative reason the pipeline could give. Only a plain "nothing matched" is
    refined, into ``speciation`` (an ion/oxidation-state name EF has no matching
    flow for at all -- decision (d), 2026-09-13: when EF DOES have one,
    ``matching.matchers.IonStripMatcher`` finds it and this function never runs, since
    the pipeline outcome is a ``Match``, not an ``Unmatched``, before ``_decide`` ever
    calls this). ``qualifier_missing`` (Decision 3) no longer originates here: decision
    (e), 2026-09-13's ``matching.matchers.CarbonOxideMatcher`` matches a bare
    ``Carbon dioxide``/``Carbon monoxide`` onto EF's fossil spelling whenever the air
    bucket has ANY qualified variant at all, which every EF 3.1 shard does -- so the
    pipeline never returns a plain ``no_ef_flow`` for one of these names any more, and
    this function has nothing left to refine that reason into. The non-freshwater
    decision does NOT live here: it applies to a real ``Match`` too (see ``_decide``),
    so it is handled once, ahead of this function.
    """
    if outcome.reason != "no_ef_flow":
        return outcome
    if _ION.search(flow.name):
        return Unmatched(
            "speciation",
            f"{flow.name!r} names an ion or oxidation state; not collapsed onto an EF species",
        )
    return outcome


def _sits_on_own_unspecified_leaf(flow: EfFlow) -> bool:
    """Whether ``flow`` sits on the bucket-level unspecified leaf of its OWN EF context.

    Used only by ``_name_only_match`` to pick a deterministic, preferred code among
    several factorless namesakes: the plain (non-long-term) unspecified leaf of
    whichever bucket ``flow`` itself lands in (``compartments.unspecified_leaf``),
    never the source flow's own bucket -- a name-only alignment crosses buckets by
    definition, so there is no "this source's bucket" to prefer here the way the
    ordinary nomenclature placement does. ``False`` for a flow whose context maps to
    no bucket at all (``EfFlow.bucket`` is ``None``). A resource- or land-bucket
    namesake has no unspecified leaf either (``unspecified_leaf`` returns ``None`` for
    those buckets), so the preference only ever fires among emission-bucket namesakes;
    resource namesakes fall through to the code-sorted first.
    """
    bucket = flow.bucket
    if bucket is None:
        return False
    return flow.leaf == unspecified_leaf(bucket, long_term=False)


def _name_only_match(flow: BafuFlow, index: EfFlowIndex) -> Match | None:
    """Round 5, decision 2026-09-14: "for the ones with names: we map, else: nothing".

    Called by ``_decide`` only after the pipeline itself has given up entirely
    (``Unmatched(reason="no_ef_flow")``) on the inclusive, ``include_uncharacterised``
    index (nomenclature package only -- see ``_decide``'s own gate). Looks up every EF
    flow whose label equals ``flow.name`` case-insensitively, in ANY bucket
    (``EfFlowIndex.by_name_any_bucket``), ignoring the source's own compartment
    entirely: the ordinary matchers are all bucket-scoped, so a same-named EF flow
    sitting in a different bucket (a resource extraction whose only EF namesake is an
    element on a soil-emission leaf, say) never reaches them at all.

    Returns ``None`` -- the flow stays ``no_ef_flow``, exactly as before this rule
    existed -- in two cases: no namesake exists at all, or at least one namesake IS
    characterised. The second guard is the one that matters most: a real factor
    living under the very same name elsewhere in EF (BAFU's salts, Talc, Uranium) means
    a nomenclature-only alignment here would silently sit next to a live impact
    number for the same substance name, which is never safe to assert -- so ANY
    characterised namesake vetoes the whole name, not just the characterised one.

    Otherwise every namesake is uncharacterised and the flow is aligned onto one of
    them by name alone, with no factor and no claim about EF context: the chosen code
    prefers whichever namesake sits on the unspecified leaf of its OWN bucket
    (``_sits_on_own_unspecified_leaf``, deterministic tiebreak among several such
    leaves is moot since a bucket has only one), falling back to the code-sorted first
    namesake when none does. The returned ``Match`` carries ``tier="name-only"`` and
    ``placement=Placement.NAME_ONLY.value`` so ``entry_for``/the coverage source both
    recognise it, and its one caveat both names every leaf the name was found on and
    discloses why the target carries no context at all -- ``entry_for`` drops
    ``target["context"]`` for this placement the same way it does for an
    energy-carrier match, and must never say both things at once (see ``entry_for``).
    Unlike ``pipeline.MatchPipeline``'s own NOMENCLATURE placement (round 7, decision
    2026-09-14's guard against a long-term source crossing onto a non-long-term
    leaf), a name-only alignment needs no such guard: it emits no target context at
    all, so a long-term source aligned this way asserts no immediate-emission
    context either -- and no factor can attach regardless, since any characterised
    namesake vetoes the whole name above.
    """
    namesakes = index.by_name_any_bucket(flow.name)
    if not namesakes or any(namesake.characterised for namesake in namesakes):
        return None
    chosen = next(
        (namesake for namesake in namesakes if _sits_on_own_unspecified_leaf(namesake)),
        namesakes[0],
    )
    leafs = ", ".join(sorted({namesake.leaf for namesake in namesakes}))
    category = _SOURCE_CATEGORY_HUMAN.get(flow.category, flow.category)
    caveat = (
        f"EF has this name only as {leafs}; the source is a {category}, so the EF "
        "context is omitted (name alignment only, decision 2026-09-14)"
    )
    return Match(
        code=chosen.code,
        tier="name-only",
        placement=Placement.NAME_ONLY.value,
        location=None,
        candidates=len(namesakes),
        caveats=(caveat,),
    )


def _decide(flow: BafuFlow, outcome: Match | Unmatched, index: EfFlowIndex) -> Match | Unmatched:
    """The single ordered decision chain applied to every pipeline outcome.

    1. non-freshwater water (Decision 5): a BAFU ``Water, salt`` flow is never
       characterised by EF's water-use method, whether the pipeline found a real
       ``Match`` (it would be the freshwater flow of the same name) or came back
       ``Unmatched`` -- the reason is distinct (``non_freshwater``) so the coverage
       source can single these flows out. ``Water, fossil`` is handled differently
       (decision 2026-09-13): it is taken as groundwater and resolved through the
       curated ``water, fossil`` alias onto ``Ground Water`` instead, so it never
       reaches this branch;
    2. ocean-discharge water (Task 6 correction (3)): a ``Match`` onto a
       water-use-only EF target for an ``emissions to water`` / ``ocean`` flow is
       likewise never right -- EF's water-use method never characterises sea-water
       discharge. Must run before the unit check below: a kg-denominated ocean flow
       would otherwise pass the water-density special case and be wrongly emitted;
    3. unit_mismatch, split by characterisation (an uncharacterised target has no EF
       reference unit at all -- ``EfFlowIndex.reference_unit`` returns ``None`` for
       it, so ``conversion_for`` would always report a mismatch for it, and cannot be
       reused as-is):
       a. characterised (Task 6 correction (1)): a real ``Match`` with no fixed unit
          conversion (``conversion_for``) onto the EF flow's reference unit is
          withheld rather than emitted with a fabricated factor;
       b. uncharacterised (round 5, decision 2026-09-14): the nomenclature package
          (``mappings_biosphere_nomenclature``) uses ``nomenclature_target_unit``
          instead, at entry-building time, not here (an EF-convention unit per
          physical dimension, with its own factor when the source differs). But a
          name in ``ef_units.NOMENCLATURE_TARGET_DIMENSION`` (EF's "Wood", counted by
          mass) has no such fixed factor for a wrong-dimension source at all (BAFU
          also reports standing wood by volume) -- ``ef_units.
          nomenclature_unit_mismatch`` withholds that pairing here, before
          ``nomenclature_target_unit`` is ever asked to guess a conversion that does
          not exist;
    4. name-only alignment (round 5, decision 2026-09-14, ``_name_only_match``): only
       when the pipeline came back with a plain ``no_ef_flow`` AND ``index`` is the
       inclusive one (``EfFlowIndex.includes_uncharacterised`` -- the nomenclature
       package's own pass and the coverage sidecar's second pass; never the matched
       package's characterised-only pass). Tried before ``_refine_unmatched`` gets a
       chance to narrow the reason to ``speciation``: a namesake that resolves the
       flow by name alone is more informative than refining why nothing did. Its own
       ``Match`` is then run back through rules 2 and 3 above like any other (rule 3b
       is the one that can actually apply to it);
    5. everything else: a ``Match`` is returned as-is; an ``Unmatched`` still without a
       name-only alignment is refined by ``_refine_unmatched`` into ``speciation``
       (Decision 4, the ``no_ef_flow`` twin of ion-shaped names EF has no match for at
       all).

    Decision (d), 2026-09-13, removed two withholding rules this chain used to carry
    for a ``Match``: the ion/oxidation-state speciation guard (an ion-shaped BAFU name
    is now emitted, in whichever package its target lives, with a caveat --
    ``matching.matchers.IonStripMatcher`` is what actually produces such a ``Match``
    now) and, decision (f)(2), the nomenclature-package energy-carrier
    ``context_unresolved`` withholding (such a ``Match`` is now emitted too;
    ``entry_for`` omits the unrecoverable ``target["context"]`` and says so in the
    comment instead of asserting a branch the bw-context crosswalk cannot actually
    place).

    Rules 1 and 2 are both, in spirit, EF-water-use guards, but they behave
    differently for an uncharacterised target (``EfFlow.characterised=False``, no CF
    vector at all -- ``index.vector`` is always ``{}`` for one). Rule 2 keys directly
    off that vector (``set(index.vector(outcome.code)) == {WATER_USE_METHOD}``), which
    can never be true for ``{}``: it is literally inert for an uncharacterised target,
    and correctly so -- there is no water-use method to wrongly characterise sea-water
    discharge with. Rule 1 keys only off the BAFU flow's own name, not the index, so it
    still runs regardless of characterisation; but by the time the nomenclature
    package's inclusive pass ever sees a ``Water, salt`` flow, this same ``_decide``
    has already withheld it as ``non_freshwater`` once already (in the
    characterised-only pass every flow goes through first -- see ``outcome_for``), so
    rule 1 only ever reconfirms an outcome already reached, never lets a real "Water,
    salt" flow through as the nomenclature package's Match.

    Key, phase 1 -> the plan's numbered Decisions and phase 2 -> the 2026-09-13
    lettered decisions: Decision 3 = ``qualifier_missing`` (bare carbon oxides, now
    superseded by decision (e)'s ``CarbonOxideMatcher``), Decision 4 = ``speciation``
    (ion/oxidation guard, now superseded by decision (d)'s ``IonStripMatcher``),
    Decision 5 = ``non_freshwater`` (salt water, rule 1 above); decision (a) = the
    energy-content and ore-composite conversions (``ef_units.py``,
    ``matching/matchers.py``), decision (b) = the resource-branch fallback
    (``compartments.is_uninformative_resource_sub``), decision (c) = the fossil-water
    (taken as groundwater) and "Nitrogen" (taken as total nitrogen) aliases
    (``matching/aliases.yaml``), decision (d) = ion collapse (``IonStripMatcher``),
    decision (e) = unqualified carbon oxides (``CarbonOxideMatcher``), decision (f) =
    nomenclature-package-only relaxed placement (``pipeline.Placement.NOMENCLATURE``),
    energy-carrier context omission (``entry_for``) and the wood/water aliases
    (``matching/aliases.yaml``), decision (g) = the mine-gas energy content
    (``ef_units.ENERGY_CONTENT``/``ENERGY_CONTENT_NOTES``). Round 5, decision
    2026-09-14 = name-only alignment (rule 4 above, ``_name_only_match``) and the
    nomenclature-package dimension-mismatch withholding (rule 3b above,
    ``ef_units.nomenclature_unit_mismatch``).
    """
    if flow.name.startswith(_NON_FRESHWATER):
        return Unmatched(
            "non_freshwater", "EF water use characterises freshwater deprivation only"
        )
    if (
        not isinstance(outcome, Match)
        and index.includes_uncharacterised
        and outcome.reason == "no_ef_flow"
    ):
        name_only = _name_only_match(flow, index)
        if name_only is not None:
            outcome = name_only
    if isinstance(outcome, Match):
        if (
            flow.category == "emissions to water"
            and flow.subcategory == "ocean"
            and set(index.vector(outcome.code)) == {WATER_USE_METHOD}
        ):
            return Unmatched("no_ef_flow", "EF water use has no sea-water discharge flow")
        ef_flow = index.get(outcome.code)
        if ef_flow.characterised:
            # only the None-ness matters here; the caveat conversion_for also returns
            # is discarded and recomputed by entry_for when the entry is actually
            # built.
            if conversion_for(flow, outcome, index) is None:
                ef_unit = index.reference_unit(outcome.code)
                return Unmatched(
                    "unit_mismatch",
                    f"BAFU unit {flow.unit} vs EF reference unit {ef_unit} for "
                    f"{ef_flow.name}; no fixed conversion",
                )
        else:
            # round 5, decision 2026-09-14: a name in
            # ef_units.NOMENCLATURE_TARGET_DIMENSION fixes one physical dimension for
            # the substance (EF's "Wood" is counted by mass); a source outside it has
            # no fixed conversion the way water's density does, so it is withheld
            # rather than guessed. Reached by either an ordinary pipeline Match (an
            # alias landing on an uncharacterised target) or a name-only one.
            mismatch = nomenclature_unit_mismatch(flow, ef_flow)
            if mismatch is not None:
                return Unmatched("unit_mismatch", mismatch)
        return outcome
    return _refine_unmatched(flow, outcome)


def outcome_for(
    flow: BafuFlow, cas: str | None, pipeline: MatchPipeline, index: EfFlowIndex
) -> Match | Unmatched:
    """Match ``flow`` against ``pipeline``, then run the result through ``_decide``.

    The one place the per-flow "match, then decide" computation lives, shared by
    ``BafuEfMatchedSource.outcomes`` (this source's own single pass, over its own
    characterised-only ``pipeline``/``index``) and the coverage sidecar's second pass
    (``BafuEfCoverageSource.transform``, over its own inclusive pair): both passes
    differ only in which ``pipeline``/``index`` get passed in here. A module-level
    function, not a method: the two call sites live in two different ``Source``
    subclasses, and neither needs ``self``.
    """
    match = pipeline.match(flow, cas)
    return _decide(flow, match, index)


class BafuEfMatchedSource(Source):
    """Match every BAFU flow the curated and inferred packages leave uncovered against
    the EF index."""

    #: Whether ``parse`` builds the EF index with uncharacterised flows included (see
    #: ``EfFlowIndex.from_bytes``). ``False`` here -- this source's targets always
    #: carry a real factor. The nomenclature source (``mappings_biosphere_
    #: nomenclature.BafuEfNomenclatureSource``) is the one subclass that flips this.
    include_uncharacterised: bool = False

    def fetch(self, ctx: RunContext) -> RawData:
        """Fetch every named input except ``ef_vocab`` (a directory, read locally in ``parse``)."""
        self.inputs = {
            name: fetch_mod.fetch(url, ctx)
            for name, url in self.config.inputs.items()
            if name != _VOCAB_DIR
        }
        return fetch_mod.fetch(self.config.fetch_url, ctx)

    def parse(self, raw: RawData) -> Records:
        """One record holding a :class:`ParsedInputs`; ``outcomes``/``transform`` reuse it."""
        missing = [name for name in _REQUIRED if name not in self.inputs]
        if missing:
            raise RuntimeError(
                f"inputs {missing} not fetched: fetch() must run before parse(), and the "
                f"registry entry must declare inputs {list(_REQUIRED)}"
            )
        curated = orjson.loads(self.inputs["curated"].content)
        inferred = orjson.loads(self.inputs["inferred"].content)
        matched_input = self.inputs.get("matched")
        matched_codes = (
            frozenset(codes_of(orjson.loads(matched_input.content)))
            if matched_input is not None
            else frozenset()
        )
        records = parse_ecospold_zip(raw)
        cas, conflicts = substance_cas(records)
        vocab_dir = fetch_mod.local_path(self.config.inputs.get(_VOCAB_DIR), _VOCAB_DIR)
        index = EfFlowIndex.from_bytes(
            self.inputs["ef_cfs"].content,
            vocab_dir,
            include_uncharacterised=self.include_uncharacterised,
        )
        inputs = ParsedInputs(
            bafu=BafuFlowIndex.from_ecospold(records),
            cas=cas,
            cas_conflicts=conflicts,
            curated_codes=frozenset(codes_of(curated)),
            inferred_codes=frozenset(codes_of(inferred)),
            matched_codes=matched_codes,
            index=index,
            pipeline=default_pipeline(index, load_aliases()),
        )
        return [{"inputs": inputs}]

    def entry_for(self, flow: BafuFlow, match: Match, index: EfFlowIndex) -> Record:
        """One randonneur ``replace`` entry asserting ``flow`` resolves to ``match``.

        For a characterised target, the target unit is always the EF flow's own
        reference unit (``EfFlowIndex.reference_unit``) -- never a respelling of the
        BAFU unit -- and ``conversion_factor`` is set only when that factor is not
        1.0. Raises ``ValueError`` when no fixed conversion exists: ``outcomes()``
        withholds any such flow as ``unit_mismatch`` before an entry is ever built for
        it, so this can only fire on a direct call with a mismatched (flow, match)
        pair.

        For an uncharacterised target (the nomenclature package only -- ``_decide``
        never lets one reach here for the default, characterised-only source), there is
        no EF reference unit at all: the target unit is ``nomenclature_target_unit``'s
        EF-convention spelling for the source unit's physical dimension (round 5,
        decision 2026-09-14), and ``conversion_factor`` is set whenever that function
        returns a factor (a source unit already at the EF-convention scale gets
        ``None`` back and no key at all -- same as the characterised branch below,
        never a fabricated 1.0). ``target["context"]`` is omitted entirely in two
        distinct cases, never both disclosed at once:

        - the target is a resource-bucket flow whose name is energy-carrier-shaped
          (``ef_flow.bucket == "resource"`` and ``ef_index.UNCERTAIN_RESOURCE_NAME``
          matches, decision (f)(2), 2026-09-13) -- the bw-context crosswalk cannot
          reach an EF energy-resource branch at all (see
          ``ef_index.UNCERTAIN_RESOURCE_NAME``'s own docstring, which is specifically
          about the *resource* branch the crosswalk cannot reach; an emission-bucket
          flow that happens to share an energy-shaped name is unaffected and keeps its
          context), so asserting one, even as "uncertain", would overstate what is
          known -- and the comment says so ("EF context not recoverable ...") instead
          of the usual branch/sub-compartment-uncertain wording;
        - the match is a name-only alignment (round 5, decision 2026-09-14,
          ``match.placement == compartments.Placement.NAME_ONLY.value``, built by
          ``mappings_biosphere_matched._name_only_match``): the chosen EF namesake
          lives in a different bucket than the source flow's own category, so its
          context is never the source's context to assert -- the match's own one
          caveat already discloses this ("... so the EF context is omitted ..."), so
          neither the energy-carrier wording nor the branch/sub-compartment-uncertain
          wording is added for it (both would either be false or duplicate what the
          caveat already says).

        Any of the three placements whose own first caveat names a specific EF
        leaf/branch (``resource_branch_fallback``, ``unspecified_fallback``,
        ``nomenclature_placement``) drops that caveat for an energy carrier, since
        naming a specific leaf would contradict the "not recoverable" disclosure just
        given -- only trailing (non-placement) caveats, if any, are kept;
        ``resource_branch_fallback`` also drops its own "placed on the inferred EF
        resource branch ..." wording (below) for the same reason. A name-only match's
        own placement is never one of these three, so this dropping never applies to
        it -- its one caveat is always kept whole. Otherwise the comment always starts
        with a fixed disclosure that the target carries no factor in any EF 3.1 method
        and that the target unit is the EF convention for the source unit's dimension,
        followed by a branch/sub-compartment caveat when the target's context itself
        was uncertain (``EfFlow.context_uncertain``, skipped for a name-only match same
        as for an energy carrier), then the match's own caveats -- except a
        ``resource_branch_fallback`` caveat, which is never honest to repeat verbatim
        for an uncharacterised target: the pipeline's own wording ("EF has ... only as
        ...") asserts a fact about EF's *characterised* branches that has no bearing
        here, so it is replaced with a caveat that instead says plainly that this is an
        inferred placement on an uncharacterised flow.

        A BAFU ``..., resource correction`` flow (a correction entry against a
        substance's own extraction, not a distinct resource) gets one more caveat,
        appended after those. Last of all (round 5, decision 2026-09-14): when
        ``nomenclature_target_unit`` returns a real factor, ``"amount rescaled by
        {factor:g}"`` is appended too, so the comment always names the very factor
        ``conversion_factor`` carries.
        """
        ef_flow = index.get(match.code)
        source: Record = _base_source(flow)
        if flow.unit:
            source["unit"] = flow.unit
        if flow.context:
            source["context"] = flow.context

        uncertain_energy = (
            not ef_flow.characterised
            and ef_flow.bucket == "resource"
            and bool(UNCERTAIN_RESOURCE_NAME.match(ef_flow.name))
        )
        name_only = match.placement == Placement.NAME_ONLY.value
        omit_context = uncertain_energy or name_only

        target: Record = {"code": match.code}
        if ef_flow.label:
            # round 6, decision 2026-09-14 (third cut): display the vocab pref_label
            # when EfFlowIndex kept it (EfFlow.label), never the bare matching key --
            # for a defect-listed or collision-suppressed flow, or an uncharacterised
            # one, EfFlow.label already IS the matching key (ef_index.py).
            target["name"] = ef_flow.label
        if ef_flow.context and not omit_context:
            target["context"] = list(ef_flow.context)
        if match.location:
            target["location"] = match.location

        resource_correction_caveat = (
            "source is a resource-correction flow, mapped to the extraction of the same element"
            if flow.name.endswith(", resource correction")
            else None
        )

        if not ef_flow.characterised:
            target_unit, rescale_factor = nomenclature_target_unit(flow, ef_flow)
            target["unit"] = target_unit
            entry: Record = {"source": source, "target": target}
            if rescale_factor is not None:
                entry["conversion_factor"] = rescale_factor
            comments = [
                "uncharacterised in EF 3.1: no factor in any method; target unit is "
                "the EF convention for this dimension (EF states no reference unit "
                "for this flow)"
            ]
            if uncertain_energy and not name_only:
                comments.append("EF context not recoverable from the source context code")
            elif ef_flow.context_uncertain and not name_only:
                comments.append(
                    "EF context inferred from the Brightway context code, "
                    "branch/sub-compartment uncertain"
                )
            if match.placement == "resource_branch_fallback" and not uncertain_energy:
                # naming a specific inferred branch would contradict the
                # "not recoverable" disclosure just above for an energy carrier
                comments.append(
                    f"BAFU files this resource under {flow.subcategory}; placed on "
                    f"the inferred EF resource branch {ef_flow.leaf} (uncharacterised, "
                    "context from the Brightway code)"
                )
            # every one of these three placements' own caveats[0] names a specific EF
            # leaf/branch (the pipeline's "only as ..."/"EF has no ... flow"/"EF has
            # this name only in ..." wording); for an energy carrier whose context we
            # have just disclosed as unrecoverable, keeping that leaf-naming claim
            # would contradict the disclosure, so it is dropped -- only caveats
            # beyond it (region caveats, etc.) are kept.
            drops_leaf_naming_caveat = match.placement == "resource_branch_fallback" or (
                uncertain_energy
                and match.placement in ("unspecified_fallback", "nomenclature_placement")
            )
            if drops_leaf_naming_caveat:
                comments.extend(match.caveats[1:])
            else:
                comments.extend(match.caveats)
            if resource_correction_caveat:
                comments.append(resource_correction_caveat)
            if rescale_factor is not None:
                comments.append(f"amount rescaled by {rescale_factor:g}")
            entry["comment"] = "; ".join(comments)
            return entry

        ef_unit = index.reference_unit(match.code)
        conversion = conversion_for(flow, match, index)
        if conversion is None:
            raise ValueError(
                f"no fixed conversion from {flow.unit} to {ef_unit} for {match.code}; "
                "outcomes() withholds this as unit_mismatch"
            )
        factor, energy_caveat = conversion
        target["unit"] = ef_unit

        entry = {"source": source, "target": target}
        if factor != 1.0:
            entry["conversion_factor"] = factor
        comments = list(match.caveats) + ([energy_caveat] if energy_caveat else [])
        if resource_correction_caveat:
            comments.append(resource_correction_caveat)
        if comments:
            entry["comment"] = "; ".join(comments)
        return entry

    def outcomes(self, records: Records) -> list[tuple[BafuFlow, Match | Unmatched]]:
        """Every non-excluded BAFU flow with its final outcome, sorted for determinism.

        A flow the curated, inferred or matched package already maps is skipped
        entirely -- not just its entry withheld -- since those packages keep
        precedence and this source's job is only to fill the gap they leave.
        """
        (record,) = records
        inputs: ParsedInputs = record["inputs"]
        flows = sorted(inputs.bafu, key=flow_sort_key)
        result: list[tuple[BafuFlow, Match | Unmatched]] = []
        for flow in flows:
            if flow.code in inputs.excluded:
                continue
            cas = inputs.cas.get(flow.name)
            result.append((flow, outcome_for(flow, cas, inputs.pipeline, inputs.index)))
        return result

    def transform(self, records: Records) -> Rows:
        """Emit one entry per BAFU flow ``outcomes`` resolves to a characterised ``Match``.

        A ``Match`` onto an uncharacterised target is never emitted here -- with
        ``include_uncharacterised`` at its default (``False``) this cannot happen at
        all (the index built in ``parse`` carries no uncharacterised flow to match
        onto), but the guard makes that contract explicit rather than relying on the
        flag never being flipped by accident. The nomenclature source
        (``mappings_biosphere_nomenclature.BafuEfNomenclatureSource``) is the one
        place such a match is actually emitted.
        """
        (record,) = records
        index: EfFlowIndex = record["inputs"].index
        return [
            self.entry_for(flow, outcome, index)
            for flow, outcome in self.outcomes(records)
            if isinstance(outcome, Match) and index.get(outcome.code).characterised
        ]
