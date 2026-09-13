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
from sentier_importers.sources.eaternity.bridge import BafuFlow, BafuFlowIndex

_VOCAB_DIR = "ef_vocab"
_REQUIRED = ("rank3", "rank6", "ef_cfs")
_BIOSPHERE_GROUP = 4
_WATER_USE = "ef-3.1:water-use"

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

#: Physical dimension per unit spelling (BAFU and EF spellings both included), used by
#: ``unit_conversion`` to tell a safe same-dimension unit respelling from an unsafe
#: cross-dimension pair. ``Nm3`` (normal cubic meter, some BAFU gas flows) is volume
#: like ``m3``: still the wrong dimension for an energy or mass EF reference unit.
_DIMENSION: dict[str, str] = {
    "kg": "mass",
    "kilogram": "mass",
    "Bq": "activity",
    "kBq": "activity",
    "m3": "volume",
    "Nm3": "volume",
    "cubic meter": "volume",
    "m2": "area",
    "m2a": "area-time",
    "m2*a": "area-time",
    "MJ": "energy",
    "megajoule": "energy",
    "kWh": "energy",
}

#: The few same-dimension unit pairs whose physical scale actually differs: an SI
#: activity prefix (BAFU sometimes reports becquerel where EF's reference unit is
#: kilobecquerel) and the historical kWh/MJ energy pair. Every other same-dimension
#: pair (kg/kilogram, m3/cubic meter, kBq/kBq, m2, m2*a, MJ/megajoule) is the same
#: physical scale and needs no factor (1.0) -- see ``unit_conversion``. The reverse
#: direction, kBq -> Bq, never occurs: EF's reference unit for ionising radiation is
#: always kBq (``EfFlowIndex.reference_unit``), never Bq.
_SCALED: dict[tuple[str, str], float] = {("Bq", "kBq"): 0.001, ("kWh", "megajoule"): 3.6}

#: ecoinvent v2 net calorific values, MJ per BAFU unit, keyed by (BAFU name, BAFU unit) so a
#: conversion is never applied by accident. These are the resource-flow definitions the
#: BAFU-2026 inventory is built from. Decision Laurenz 2026-09-13. `Gas, natural/m3` exists in
#: both m3 and Nm3 in BAFU; both are treated as normal cubic metres.
ENERGY_CONTENT: dict[tuple[str, str], float] = {
    ("Coal, hard", "kg"): 19.1,
    ("Coal, brown", "kg"): 9.9,
    ("Oil, crude", "kg"): 45.8,
    ("Peat", "kg"): 9.9,
    ("Uranium", "kg"): 560_000.0,
    ("Gas, natural/m3", "m3"): 38.3,
    ("Gas, natural/m3", "Nm3"): 38.3,
}

#: BAFU unit -> (EF spelling of that unit's physical dimension, factor onto it), used
#: only for an uncharacterised EF target (rank 8, ``mappings_biosphere_nomenclature``):
#: such a target carries no CF vector at all, so ``EfFlowIndex.reference_unit`` has
#: nothing to infer a reference unit from, and the plain unit-string respelling below
#: stands in for it. Impact is zero by construction regardless of the unit chosen, so
#: this is a nomenclature courtesy, not a physical-scale assertion the way
#: ``unit_conversion``/``ENERGY_CONTENT`` are. A unit not listed here is passed through
#: unchanged with no factor.
_NOMENCLATURE_UNITS: dict[str, tuple[str, float | None]] = {
    "kg": ("kilogram", None),
    "Bq": ("kBq", 0.001),
    "kBq": ("kBq", None),
    "m3": ("cubic meter", None),
    "Nm3": ("cubic meter", None),
    "MJ": ("megajoule", None),
    "kWh": ("megajoule", 3.6),
    "m2": ("m2", None),
    "m2a": ("m2*a", None),
}


def nomenclature_unit(bafu_unit: str) -> tuple[str, float | None]:
    """The EF spelling of ``bafu_unit``'s dimension, and a factor onto it (or ``None``).

    Used only when the match target is uncharacterised (rank 8): such a target has no
    EF reference unit at all (``EfFlowIndex.reference_unit`` returns ``None`` for it),
    so this is what ``entry_for`` uses to fill ``target["unit"]``/``conversion_factor``
    instead. ``bafu_unit`` outside :data:`_NOMENCLATURE_UNITS` is returned unchanged
    with no factor -- this is a labelling courtesy, not a claim of physical accuracy.
    """
    return _NOMENCLATURE_UNITS.get(bafu_unit, (bafu_unit, None))


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
    """

    bafu: BafuFlowIndex
    cas: Mapping[str, str]
    cas_conflicts: Mapping[str, tuple[str, ...]]
    rank3_codes: frozenset[str]
    rank6_codes: frozenset[str]
    index: EfFlowIndex
    pipeline: MatchPipeline
    rank7_codes: frozenset[str] = frozenset()

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


def unit_conversion(bafu_unit: str, ef_unit: str) -> float | None:
    """Fixed multiplier from ``bafu_unit`` onto EF's ``ef_unit``, or ``None`` when unsafe.

    The same unit spelling is trivially 1.0. Otherwise a conversion is only ever safe
    within one physical dimension (see ``_DIMENSION``): most BAFU/EF spelling pairs
    within a dimension are the same physical scale and need no factor at all (1.0,
    e.g. ``kg``/``kilogram``, ``m3``/``cubic meter``, ``m2*a``/``m2a``); the handful
    that are not (an SI activity prefix, the historical kWh/MJ energy pair) have one
    fixed factor in ``_SCALED``. A cross-dimension pair (e.g. a volume-denominated
    BAFU flow onto an EF flow whose reference unit is energy or mass), or either unit
    outside ``_DIMENSION`` entirely, returns ``None`` -- there is no general fixed
    conversion between different physical quantities, so the caller must withhold the
    flow rather than assert a fabricated factor.

    The one true mass -> volume conversion this system asserts (a BAFU kilogram water
    flow onto an EF water-use cubic-meter flow, using water's density) is deliberately
    NOT handled here: it depends on the EF flow's characterisation method, which this
    function -- unit strings only -- cannot see. It is applied as a special case at
    the call site instead (see ``conversion_for``).
    """
    if bafu_unit == ef_unit:
        return 1.0
    if (bafu_unit, ef_unit) in _SCALED:
        return _SCALED[(bafu_unit, ef_unit)]
    dimension = _DIMENSION.get(bafu_unit)
    if dimension is None or dimension != _DIMENSION.get(ef_unit):
        return None
    return 1.0


def conversion_for(
    flow: BafuFlow, match: Match, index: EfFlowIndex
) -> tuple[float, str | None] | None:
    """The multiplier from ``flow.unit`` onto ``match``'s EF reference unit, paired with
    any caveat that factor itself carries, or ``None`` when no fixed conversion exists
    and the flow must be withheld.

    Checked in this order: an energy-content factor (``ENERGY_CONTENT``) whenever the
    target's reference unit is megajoule and ``(flow.name, flow.unit)`` is a key BAFU
    actually reports -- checked first, and keyed on the exact BAFU (name, unit) pair,
    so it is never applied by accident to an unrelated flow that merely happens to
    share the same EF target; then the water density special case (depends on the
    target's characterisation method, so it cannot live in the generic, method-blind
    ``unit_conversion`` table); then the generic, unit-string-only fallback.
    """
    if index.reference_unit(match.code) == "megajoule":
        energy = ENERGY_CONTENT.get((flow.name, flow.unit))
        if energy is not None:
            caveat = f"energy content {energy:g} MJ/{flow.unit} (ecoinvent v2 net calorific value)"
            return energy, caveat
    if flow.unit == "kg" and set(index.vector(match.code)) == {_WATER_USE}:
        # water is the only substance with a fixed mass -> volume factor (density);
        # this depends on the target's characterisation method, so it cannot live in
        # the generic, method-blind ``unit_conversion`` table above.
        return 0.001, None
    factor = unit_conversion(flow.unit, index.reference_unit(match.code))
    return None if factor is None else (factor, None)


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
    """
    if flow.name.startswith(_NON_FRESHWATER):
        return Unmatched(
            "non_freshwater", "EF water use characterises freshwater deprivation only"
        )
    if isinstance(outcome, Match):
        if (
            flow.category == "emissions to water"
            and flow.subcategory == "ocean"
            and set(index.vector(outcome.code)) == {_WATER_USE}
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
        carries no factor in any EF 3.1 method, followed by a sub-compartment caveat
        when the target's context itself was uncertain (``EfFlow.context_uncertain``),
        then the match's own caveats.
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
                    "sub-compartment uncertain"
                )
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

    @staticmethod
    def outcome_for(
        flow: BafuFlow, cas: str | None, pipeline: MatchPipeline, index: EfFlowIndex
    ) -> Match | Unmatched:
        """Match ``flow`` against ``pipeline``, then run the result through ``_decide``.

        The one place the per-flow "match, then decide" computation lives, shared by
        ``outcomes`` (this source's own single pass) and the coverage sidecar's second
        pass (``BafuEfCoverageSource.transform``, the inclusive index): both passes
        differ only in which ``pipeline``/``index`` get passed in here.
        """
        match = pipeline.match(flow, cas)
        return _decide(flow, match, index)

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
            result.append((flow, self.outcome_for(flow, cas, inputs.pipeline, inputs.index)))
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
