"""bafu-2026-v1 -> EF 3.1 CF keys by public matching (rank 7).

For every BAFU-2026 v1 elementary flow that neither the rank-3 nor the rank-6 bridge
maps, run ``matching.pipeline.default_pipeline`` (name, land-use class, synonym,
qualifier, alias, region-stripped name, CAS) against the public EF flow index and emit
one ``replace`` entry per match. Withheld flows are emitted by the sibling coverage
source.

Inputs: the ecoSpold zip (primary), ``rank3`` and ``rank6`` payloads (exclusion) and
``ef_cfs`` (sentier-methods CF table) all go through the content-addressed fetch
cache like any other input. Only ``ef_vocab`` (a DIRECTORY of sentier-vocab
elementary-flow shards) bypasses it, read from its local path directly in ``parse``:
a directory has no single content digest to cache against.

Unit/dimension conversion (``unit_conversion``, ``conversion_for``, ``ENERGY_CONTENT``,
``nomenclature_unit``) lives in the sibling ``ef_units`` module.
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
from sentier_importers.matching.ef_index import EfFlowIndex, normalise_cas
from sentier_importers.matching.matchers import load_aliases
from sentier_importers.matching.pipeline import Match, MatchPipeline, Unmatched, default_pipeline
from sentier_importers.sources.bafu.ecospold import parse_ecospold_zip
from sentier_importers.sources.bafu.ef_units import (
    WATER_USE_METHOD,
    conversion_for,
    nomenclature_unit,
)
from sentier_importers.sources.eaternity.bridge import BafuFlow, BafuFlowIndex

_VOCAB_DIR = "ef_vocab"
_REQUIRED = ("rank3", "rank6", "ef_cfs")
_BIOSPHERE_GROUP = 4

#: Ion / oxidation-state markers BAFU bakes into a name: a trailing ``, ion`` /
#: `` ion``, a trailing roman numeral (``II``-``VI``), or a trailing ``+``. EF 3.1
#: does not carry per-species factors for these; collapsing them onto the element
#: would silently assert a factor for the wrong chemical species.
_ION = re.compile(r"(,\s?ion$|\sion$|\s(II|III|IV|V|VI)$|\+$)")

#: Species markers EF itself uses in an *EF* flow name (a bracketed roman numeral
#: oxidation state, a bracketed charge like ``(6+)``/``(2+)``/``(2-)``, or the bare
#: word ``ion``): when the EF target already names a specific species this way, a
#: same-shaped BAFU ion name is not a species-collapsing guess, it is the right match.
_EF_SPECIES = re.compile(
    r"\((?:i{1,3}|iv|v|vi)\)|\(\d\+\)|\bion\b|\d\+\)|\(\d[+-]\)", re.IGNORECASE
)

#: Match tiers considered curated (a human checked the BAFU name against the EF
#: preferred label by hand -- see ``matching/aliases.yaml``); the speciation guard
#: (Decision 4) defers to that judgement rather than second-guessing it.
_CURATED_TIERS = {"alias", "region/alias"}

#: EF characterises carbon oxides only with a qualifier (fossil/biogenic/land use
#: change); a bare BAFU name with none can never resolve, no matter the sub-compartment.
_CARBON_OXIDES = {"Carbon dioxide", "Carbon monoxide"}

#: EF's water-use method characterises freshwater deprivation only: a BAFU salt-water
#: resource flow can never receive an EF water factor, even when the pipeline finds a
#: same-named candidate (that candidate is the freshwater flow). ``Water, fossil`` is
#: deliberately NOT here (decision 2026-09-13): it is taken as non-renewable
#: groundwater and resolved via the curated ``water, fossil`` alias instead (see
#: ``matching/aliases.yaml``), each such entry carrying that decision as a caveat.
_NON_FRESHWATER = ("Water, salt",)


@dataclass(frozen=True)
class ParsedInputs:
    """Everything ``BafuEfMatchedSource.parse`` builds once, for ``outcomes``/``transform``
    to reuse: the BAFU flow universe, the per-substance CAS table (and its conflicts,
    for the sibling coverage source to report), the rank-3, rank-6 and rank-7
    source-code sets (kept separate so the coverage sidecar can tell which bridge
    mapped a flow; see ``excluded``), the EF flow index and the matching pipeline built
    over it.

    ``rank7_codes`` is empty unless the registry entry declares a ``rank7`` input (only
    the rank-8 nomenclature source does): rank 7 and rank 8 both run over the
    characterised-only index's leftovers, so rank 8 must also skip whatever rank 7
    itself mapped, not just rank 3/6.

    ``inclusive_index``/``inclusive_pipeline`` are the sibling coverage source's own
    fields (``None`` here): its ``parse`` override attaches an inclusive
    (``include_uncharacterised=True``) index and the pipeline built over it, so its
    ``transform`` can run bridge 8's second pass without touching ``self.inputs`` or
    ``self.config`` at all.
    """

    bafu: BafuFlowIndex
    cas: Mapping[str, str]
    cas_conflicts: Mapping[str, tuple[str, ...]]
    rank3_codes: frozenset[str]
    rank6_codes: frozenset[str]
    index: EfFlowIndex
    pipeline: MatchPipeline
    rank7_codes: frozenset[str] = frozenset()
    inclusive_index: EfFlowIndex | None = None
    inclusive_pipeline: MatchPipeline | None = None

    @property
    def excluded(self) -> frozenset[str]:
        """Every source code rank 3, 6 or 7 already maps -- this source's exclusion set.

        A derived union rather than a stored field: ``rank3_codes``/``rank6_codes``/
        ``rank7_codes`` are the single source of truth (the coverage sidecar needs them
        apart), and this property keeps ``outcomes`` (which only needs the union)
        unchanged.
        """
        return self.rank3_codes | self.rank6_codes | self.rank7_codes


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
    refined, into ``speciation`` (an ion/oxidation-state name) or
    ``qualifier_missing`` (a bare carbon oxide with no fossil/biogenic/land-use-change
    qualifier). The non-freshwater decision does NOT live here: it applies to a real
    ``Match`` too (see ``_decide``), so it is handled once, ahead of this function.
    """
    if outcome.reason != "no_ef_flow":
        return outcome
    if _ION.search(flow.name):
        return Unmatched(
            "speciation",
            f"{flow.name!r} names an ion or oxidation state; not collapsed onto an EF species",
        )
    if flow.name in _CARBON_OXIDES and flow.category == "emissions to air":
        return Unmatched(
            "qualifier_missing",
            "EF characterises carbon oxides only as (fossil), (biogenic) or (land use change)",
        )
    return outcome


def _decide(flow: BafuFlow, outcome: Match | Unmatched, index: EfFlowIndex) -> Match | Unmatched:
    """The single ordered decision chain applied to every pipeline outcome.

    1. non-freshwater water (Decision 5): a BAFU ``Water, salt`` flow is never
       characterised by EF's water-use method, whether the pipeline found a real
       ``Match`` (it would be the freshwater flow of the same name) or came back
       ``Unmatched`` -- the reason is distinct (``non_freshwater``) so the coverage
       source can single these flows out. ``Water, fossil`` is handled differently
       (decision 2026-09-13): it is taken as non-renewable groundwater and resolved
       through the curated ``water, fossil`` alias onto ``Ground Water`` instead, so
       it never reaches this branch;
    2. ocean-discharge water (Task 6 correction (3)): a ``Match`` onto a
       water-use-only EF target for an ``emissions to water`` / ``ocean`` flow is
       likewise never right -- EF's water-use method never characterises sea-water
       discharge. Must run before the unit check below: a kg-denominated ocean flow
       would otherwise pass the water-density special case and be wrongly emitted;
    3. speciation (Decision 4): a ``Match`` for an ion/oxidation-state-shaped BAFU
       name (``_ION``) is withheld -- EF 3.1 does not carry per-species factors, so
       collapsing e.g. ``Copper ion`` onto plain ``Copper`` would silently assert a
       factor for the wrong chemical species. Two escapes: a curated tier
       (``alias``/``region/alias``, see ``_CURATED_TIERS``) means a human already
       checked this exact pairing by hand; an EF target name that itself carries a
       species marker (``_EF_SPECIES``, e.g. ``Chromium(6+)``, ``Copper (II)``) means
       the match is onto the right species, not a collapse onto the bare element;
    4. unit_mismatch (Task 6 correction (1)): applies only when the match target is
       characterised (``EfFlow.characterised``) -- an uncharacterised target has no EF
       reference unit at all (``EfFlowIndex.reference_unit`` returns ``None`` for it),
       so ``conversion_for`` would always report a mismatch for it; rank 8
       (``mappings_biosphere_nomenclature``) uses ``nomenclature_unit`` instead, at
       entry-building time, not here. For a characterised target: a real ``Match``
       with no fixed unit conversion (``conversion_for``) onto the EF flow's reference
       unit is withheld rather than emitted with a fabricated factor;
    5. everything else: a ``Match`` is returned as-is; an ``Unmatched`` is refined by
       ``_refine_unmatched`` into ``speciation`` (Decision 4, the ``no_ef_flow`` twin
       of step 3 above) or ``qualifier_missing`` (Decision 3, unqualified carbon
       oxides).

    Rules 1 and 2 are both, in spirit, EF-water-use guards, but they behave
    differently for an uncharacterised target (``EfFlow.characterised=False``, no CF
    vector at all -- ``index.vector`` is always ``{}`` for one). Rule 2 keys directly
    off that vector (``set(index.vector(outcome.code)) == {WATER_USE_METHOD}``), which
    can never be true for ``{}``: it is literally inert for an uncharacterised target,
    and correctly so -- there is no water-use method to wrongly characterise sea-water
    discharge with. Rule 1 keys only off the BAFU flow's own name, not the index, so it
    still runs regardless of characterisation; but by the time rank 8's inclusive pass
    ever sees a ``Water, salt`` flow, this same ``_decide`` has already withheld it as
    ``non_freshwater`` once already (in the characterised-only pass every flow goes
    through first -- see ``outcome_for``), so rule 1 only ever reconfirms an outcome
    already reached, never lets a real "Water, salt" flow through as rank 8's Match.
    """
    if flow.name.startswith(_NON_FRESHWATER):
        return Unmatched(
            "non_freshwater", "EF water use characterises freshwater deprivation only"
        )
    if isinstance(outcome, Match):
        if (
            flow.category == "emissions to water"
            and flow.subcategory == "ocean"
            and set(index.vector(outcome.code)) == {WATER_USE_METHOD}
        ):
            return Unmatched("no_ef_flow", "EF water use has no sea-water discharge flow")
        if outcome.tier not in _CURATED_TIERS and _ION.search(flow.name):
            ef_name = index.get(outcome.code).name
            if not _EF_SPECIES.search(ef_name):
                return Unmatched(
                    "speciation",
                    f"{flow.name!r} names an ion or oxidation state; "
                    f"EF target {ef_name!r} does not",
                )
        # only the None-ness matters here; the caveat conversion_for also returns is
        # discarded and recomputed by entry_for when the entry is actually built. An
        # uncharacterised target has no EF reference unit for conversion_for to check
        # against at all (see the docstring point 4), so this step is skipped for one.
        if index.get(outcome.code).characterised and conversion_for(flow, outcome, index) is None:
            ef_unit = index.reference_unit(outcome.code)
            return Unmatched(
                "unit_mismatch",
                f"BAFU unit {flow.unit} vs EF reference unit {ef_unit} for "
                f"{index.get(outcome.code).name}; no fixed conversion",
            )
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
    """Match every BAFU flow rank 3 and rank 6 leave uncovered against the EF index."""

    #: Whether ``parse`` builds the EF index with uncharacterised flows included (see
    #: ``EfFlowIndex.from_bytes``). ``False`` here -- this source's targets always
    #: carry a real factor. The rank-8 nomenclature source (``mappings_biosphere_
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
        rank3 = orjson.loads(self.inputs["rank3"].content)
        rank6 = orjson.loads(self.inputs["rank6"].content)
        rank7_input = self.inputs.get("rank7")
        rank7_codes = (
            frozenset(codes_of(orjson.loads(rank7_input.content)))
            if rank7_input is not None
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
            rank3_codes=frozenset(codes_of(rank3)),
            rank6_codes=frozenset(codes_of(rank6)),
            rank7_codes=rank7_codes,
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

        For an uncharacterised target (rank 8 only -- ``_decide`` never lets one reach
        here for the default, characterised-only source), there is no EF reference
        unit at all: the target unit and factor come from ``nomenclature_unit``
        instead, and the comment always starts with a fixed disclosure that the target
        carries no factor in any EF 3.1 method, followed by a branch/sub-compartment
        caveat when the target's context itself was uncertain
        (``EfFlow.context_uncertain``), then the match's own caveats -- except a
        ``resource_branch_fallback`` caveat, which is never honest to repeat verbatim
        for an uncharacterised target: the pipeline's own wording
        ("EF has ... only as ...") asserts a fact about EF's *characterised* branches
        that has no bearing here, so it is replaced with a caveat that instead says
        plainly that this is an inferred placement on an uncharacterised flow.
        """
        ef_flow = index.get(match.code)
        source: Record = _base_source(flow)
        if flow.unit:
            source["unit"] = flow.unit
        if flow.context:
            source["context"] = flow.context

        target: Record = {"code": match.code}
        if ef_flow.name:
            target["name"] = ef_flow.name
        if ef_flow.context:
            target["context"] = list(ef_flow.context)
        if match.location:
            target["location"] = match.location

        if not ef_flow.characterised:
            unit, factor = nomenclature_unit(flow.unit)
            target["unit"] = unit
            entry: Record = {"source": source, "target": target}
            if factor is not None:
                entry["conversion_factor"] = factor
            comments = ["uncharacterised in EF 3.1: no factor in any method"]
            if ef_flow.context_uncertain:
                comments.append(
                    "EF context inferred from the Brightway context code, "
                    "branch/sub-compartment uncertain"
                )
            if match.placement == "resource_branch_fallback":
                comments.append(
                    f"BAFU files this resource under {flow.subcategory}; placed on "
                    f"the inferred EF resource branch {ef_flow.leaf} (uncharacterised, "
                    "context from the Brightway code)"
                )
                comments.extend(match.caveats[1:])
            else:
                comments.extend(match.caveats)
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
        if comments:
            entry["comment"] = "; ".join(comments)
        return entry

    def outcomes(self, records: Records) -> list[tuple[BafuFlow, Match | Unmatched]]:
        """Every non-excluded BAFU flow with its final outcome, sorted for determinism.

        A flow rank 3, rank 6 or rank 7 already maps is skipped entirely -- not just
        its entry withheld -- since those bridges keep precedence and this source's
        job is only to fill the gap they leave.
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
        flag never being flipped by accident. The rank-8 nomenclature source
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
