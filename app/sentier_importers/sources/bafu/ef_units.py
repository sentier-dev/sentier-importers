"""Unit/dimension conversion helpers for the bafu-2026-v1 -> EF 3.1 bridges.

Split out of ``mappings_biosphere_matched`` so the unit-conversion domain (physical
dimensions, the energy-content table, the stoichiometric factor table, the
water-density special case, and the nomenclature-package target-unit convention for an
uncharacterised target) has its own home apart from the matching/decision pipeline
itself. Everything here is a pure function or lookup table; nothing touches
``BafuFlow``/EF index construction.

Round 5, decision 2026-09-14: ``nomenclature_target_unit`` replaces the earlier
same-scale-only ``nomenclature_unit`` respelling (kept the SOURCE unit's own spelling,
applied no factor at all). That respelling let one EF flow fed by both a ``Bq`` source
and a ``kBq`` source (BAFU carries both twins for many radionuclides) end up with two
different ``target["unit"]`` values and no ``conversion_factor`` on either -- silently
off by 1000x whenever a downstream consumer merged the two amounts onto one EF node
(91 affected codes; also 2 water-substance codes fed by ``kg`` and ``m3``, and one
fed by ``kWh`` and ``MJ``). ``nomenclature_target_unit`` instead canonicalises: one
EF-convention unit per physical dimension, exactly what the nomenclature package's own
sentier-mappings metadata already promises ("kilogram, kBq with a Bq -> kBq factor,
cubic meter, megajoule, m2, m2*a"), with the source-to-target factor made explicit via
its returned ``conversion_factor`` instead of silently assumed 1.

That canonicalisation is only safe when a fixed factor bridges every source unit onto
one target dimension (water's density does); when it does not (EF's "Wood" is counted
by mass, but BAFU also reports standing wood by volume, with no density convention
anywhere in this codebase), decision 2026-09-14 withholds the wrong-dimension source
instead of inventing one -- ``NOMENCLATURE_TARGET_DIMENSION``/
``nomenclature_unit_mismatch``, consulted by ``mappings_biosphere_matched._decide``.
"""

from __future__ import annotations

from sentier_importers.matching.ef_index import EfFlow, EfFlowIndex
from sentier_importers.matching.pipeline import Match
from sentier_importers.sources.eaternity.bridge import BafuFlow

#: EF 3.1's water-use method id, used by ``conversion_for``'s water-density special
#: case below; also imported by ``mappings_biosphere_matched._decide`` for the
#: ocean-discharge guard, so the two modules never risk stating the id differently.
WATER_USE_METHOD = "ef-3.1:water-use"

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
    #: BAFU's own cubic-meter-year unit (a reservoir/storage volume held over time);
    #: EF states no convention for this dimension at all, so ``nomenclature_target_unit``
    #: keeps BAFU's own spelling rather than inventing an EF one (decision 2026-09-14).
    "m3y": "volume-time",
}

#: The few same-dimension unit pairs whose physical scale actually differs: an SI
#: activity prefix (BAFU sometimes reports becquerel where EF's reference unit is
#: kilobecquerel) and the historical kWh/MJ energy pair. Every other same-dimension
#: pair (kg/kilogram, m3/cubic meter, kBq/kBq, m2, m2*a, MJ/megajoule) is the same
#: physical scale and needs no factor (1.0) -- see ``unit_conversion``. The reverse
#: direction, kBq -> Bq, never occurs: EF's reference unit for ionising radiation is
#: always kBq (``EfFlowIndex.reference_unit``), never Bq. Also consulted by
#: ``nomenclature_target_unit`` (round 5, decision 2026-09-14) for the very same two
#: scaled pairs -- one table, so a characterised and an uncharacterised match of the
#: same source unit are never rescaled by two different factors.
_SCALED: dict[tuple[str, str], float] = {("Bq", "kBq"): 0.001, ("kWh", "megajoule"): 3.6}

#: The fixed mass -> volume factor for water (density, 1 kg == 0.001 m3), shared by
#: ``conversion_for``'s characterised water-use special case and by
#: ``nomenclature_target_unit``'s own uncharacterised one (round 5, decision
#: 2026-09-14) -- one constant, so the two paths never risk stating the density
#: differently.
_WATER_DENSITY_FACTOR = 0.001

#: Round 5, decision 2026-09-14: the EF-convention target unit for an uncharacterised
#: match (the nomenclature package), one per physical dimension -- what
#: ``nomenclature_target_unit`` fills ``target["unit"]`` with. Every value here is
#: exactly the spelling the nomenclature package's own sentier-mappings metadata
#: promises ("kilogram, kBq with a Bq -> kBq factor, cubic meter, megajoule, m2,
#: m2*a"). ``volume-time`` has no EF convention at all -- BAFU's own ``m3y`` spelling
#: is kept as-is; listed here anyway so ``nomenclature_target_unit`` never needs a
#: second table.
_NOMENCLATURE_TARGET_UNIT: dict[str, str] = {
    "mass": "kilogram",
    "activity": "kBq",
    "volume": "cubic meter",
    "area": "m2",
    "area-time": "m2*a",
    "energy": "megajoule",
    "volume-time": "m3y",
}

#: EF target-name -> the one physical dimension EF's own convention counts it in,
#: consulted only for an uncharacterised nomenclature-package match (round 5, decision
#: 2026-09-14): unlike water (a genuine, fixed mass -> volume density factor,
#: ``_WATER_DENSITY_FACTOR``), a name here has NO valid cross-dimension conversion at
#: all -- ``nomenclature_target_unit`` would otherwise happily canonicalise a
#: cubic-meter source onto ``"cubic meter"`` right next to a kilogram source of the
#: SAME EF flow canonicalised onto ``"kilogram"``, two different target units for one
#: EF node with no way to reconcile them. "Wood" is the one such name today: EF counts
#: it by mass, but BAFU also reports standing wood by volume (``m3``, a wood-density
#: assumption this codebase does not carry anywhere else) -- decision 2026-09-14:
#: withhold the wrong-dimension source (``unit_mismatch``) rather than invent one.
NOMENCLATURE_TARGET_DIMENSION: dict[str, str] = {"Wood": "mass"}


def nomenclature_unit_mismatch(flow: BafuFlow, ef_flow: EfFlow) -> str | None:
    """The withholding detail when ``flow.unit``'s physical dimension disagrees with
    ``NOMENCLATURE_TARGET_DIMENSION``'s entry for ``ef_flow.name``, or ``None`` when
    ``ef_flow.name`` has no entry there at all, or the dimension agrees.

    Consulted by ``mappings_biosphere_matched._decide`` for every match onto an
    uncharacterised target, ahead of ``nomenclature_target_unit``: a name in this table
    fixes the substance to one physical dimension, and a source outside it must be
    withheld rather than silently canonicalised onto a target unit that does not
    correspond to the same physical measurement at all (unlike the water density
    special case, there is no fixed factor to bridge the two here).
    """
    dimension = NOMENCLATURE_TARGET_DIMENSION.get(ef_flow.name)
    if dimension is None or _DIMENSION.get(flow.unit) == dimension:
        return None
    return (
        f"EF {ef_flow.name} is counted by {dimension}; no density convention "
        f"converts {flow.unit} (decision 2026-09-14)"
    )


#: Net calorific values, MJ per BAFU unit, keyed by (BAFU name, BAFU unit) so a
#: conversion is never applied by accident. These are the resource-flow definitions the
#: BAFU-2026 inventory is built from. `Gas, natural/m3` exists in both m3 and Nm3 in
#: BAFU; both are treated as normal cubic metres. Decision (g), 2026-09-13: coal-mine
#: off-gas (which the pipeline itself matches onto EF's "Natural Gas" resource flow, by
#: CAS -- both share CAS 8006-14-2) is approximated at the same natural-gas value, in
#: both its m3 and Nm3 variants; see ``ENERGY_CONTENT_NOTES`` for the extra caveat text
#: these two keys carry.
#:
#: Round 7, decision 2026-09-14: Coal (hard and brown), Oil (crude) and both natural-gas
#: keys (including the coal-mine off-gas approximation, which shares the natural-gas
#: value) were replaced with values implied by a parity check against BAFU's own
#: published EF 3.1 results -- a regression of BAFU's published resource-use score
#: against the source amount, R2 1.0 -- rather than the earlier generic net calorific
#: value convention; see ``_REGRESSION_INFERRED_ENERGY_CONTENT`` for the caveat wording
#: this earns. Peat and Uranium are unchanged and keep the original wording.
ENERGY_CONTENT: dict[tuple[str, str], float] = {
    ("Coal, hard", "kg"): 17.73,
    ("Coal, brown", "kg"): 9.41,
    ("Oil, crude", "kg"): 43.40,
    ("Peat", "kg"): 9.9,
    ("Uranium", "kg"): 560_000.0,
    ("Gas, natural/m3", "m3"): 35.98,
    ("Gas, natural/m3", "Nm3"): 35.98,
    ("Gas, mine, off-gas, process, coal mining/m3", "m3"): 35.98,
    ("Gas, mine, off-gas, process, coal mining/m3", "Nm3"): 35.98,
}

#: Round 7, decision 2026-09-14: the ``ENERGY_CONTENT`` keys whose factor was replaced
#: by a value implied by BAFU's own published EF 3.1 resource-use scores (a regression
#: against the source amount, R2 1.0) rather than the generic net calorific value
#: convention the other keys still use -- ``conversion_for`` gives these one of the
#: two inline caveat wordings it builds, naming that provenance instead of the
#: generic NCV one. Peat and Uranium are deliberately absent: both are unchanged
#: from before this round and keep the original wording.
_REGRESSION_INFERRED_ENERGY_CONTENT: frozenset[tuple[str, str]] = frozenset(
    {
        ("Coal, hard", "kg"),
        ("Coal, brown", "kg"),
        ("Oil, crude", "kg"),
        ("Gas, natural/m3", "m3"),
        ("Gas, natural/m3", "Nm3"),
        ("Gas, mine, off-gas, process, coal mining/m3", "m3"),
        ("Gas, mine, off-gas, process, coal mining/m3", "Nm3"),
    }
)

#: Extra caveat text appended (after "; ") to the energy-content caveat for specific
#: ``ENERGY_CONTENT`` keys, when the NCV factor alone would not disclose an assumption
#: baked into the key itself. Decision (g), 2026-09-13: the two coal-mine off-gas keys
#: are not natural-gas extraction at all -- the factor is a deliberate approximation,
#: and every entry using it must say so. A key absent here carries no extra text.
ENERGY_CONTENT_NOTES: dict[tuple[str, str], str] = {
    ("Gas, mine, off-gas, process, coal mining/m3", "m3"): (
        "coal-mine off-gas approximated as natural gas (decision 2026-09-13)"
    ),
    ("Gas, mine, off-gas, process, coal mining/m3", "Nm3"): (
        "coal-mine off-gas approximated as natural gas (decision 2026-09-13)"
    ),
}

#: Round 4, decision 2026-09-13: the two BAFU ``TiO2, ...`` ore-composite-shaped names
#: that ``matching.matchers.OreCompositeMatcher`` does NOT decompose (its ``_ORE_RE``
#: requires a bare element as the leading segment; ``TiO2`` names a compound, not an
#: element) but that the curated alias table still maps onto plain ``Titanium`` --
#: BAFU's amount is kg of TiO2, not kg of titanium metal, so it needs its own factor
#: (the titanium mass fraction of TiO2, 47.9/79.9 rounded) applied the same way
#: ``ENERGY_CONTENT`` is: keyed on the exact BAFU (name, unit) pair, consulted by
#: ``conversion_for`` whenever the target's reference unit is kilogram.
STOICHIOMETRIC: dict[tuple[str, str], float] = {
    ("TiO2, 54% in ilmenite, 2.6% in crude ore", "kg"): 0.5995,
    ("TiO2, 95% in rutile, 0.40% in crude ore", "kg"): 0.5995,
}

#: The caveat text for each ``STOICHIOMETRIC`` key, same shape as
#: ``ENERGY_CONTENT_NOTES``: unlike the energy-content caveat (one generic template,
#: the factor and unit filled in), there is no substance-neutral wording for "this
#: amount is really kg of a different compound" -- the compound name is part of the
#: sentence itself, so each key's full caveat is stored here rather than assembled.
STOICHIOMETRIC_NOTES: dict[tuple[str, str], str] = {
    ("TiO2, 54% in ilmenite, 2.6% in crude ore", "kg"): (
        "amount is kg TiO2; 0.5995 is the titanium mass fraction of TiO2 (decision 2026-09-13)"
    ),
    ("TiO2, 95% in rutile, 0.40% in crude ore", "kg"): (
        "amount is kg TiO2; 0.5995 is the titanium mass fraction of TiO2 (decision 2026-09-13)"
    ),
}


#: EF names whose substance is water for the purpose of the density conversion of an
#: uncharacterised target. Deliberately narrow: EF's water-use method characterises
#: nine names (Water, Ground Water, freshwater, lake water, river water, Water To
#: Cooling, Water from cooling, Water to/from turbine), and the uncharacterised index
#: also carries "Sea Water", "Green Water", "Water Vapour", "Water, In Air" and more.
#: Only "Water" is reached by a BAFU source today (55 kg rows, 8 m3 rows, all of them
#: the region-stripped water family). Widen this table, and re-run the one-unit-per-
#: EF-code delivery check, if a source ever lands on another water name.
_WATER_NAMES = frozenset({"water"})


def _is_water_flow(ef_flow: EfFlow) -> bool:
    """Whether ``ef_flow`` is a water substance for the density conversion.

    ``conversion_for``'s own water-density special case identifies a characterised
    water-use match by its CF vector (``{WATER_USE_METHOD}`` exactly); an
    uncharacterised match (the nomenclature package's own targets) carries no vector
    at all (``EfFlowIndex.vector`` is always ``{}`` for one), so the name is the only
    handle. See ``_WATER_NAMES`` for how narrow that handle deliberately is.
    """
    return ef_flow.name.strip().lower() in _WATER_NAMES


def nomenclature_target_unit(flow: BafuFlow, ef_flow: EfFlow) -> tuple[str, float | None]:
    """The EF-convention unit for ``flow.unit``'s physical dimension, paired with the
    factor that rescales the source amount onto it (``None`` when the source is
    already at that scale) -- used only when the match target is uncharacterised (the
    nomenclature package): such a target has no EF reference unit of its own to defer
    to (``EfFlowIndex.reference_unit`` returns ``None`` for it), so ``entry_for`` uses
    this instead.

    Round 5, decision 2026-09-14: replaces the earlier same-scale-only
    ``nomenclature_unit`` respelling (kept the SOURCE unit's own spelling, never a
    factor) -- see the module docstring for why that was wrong: it let one EF flow fed
    by two differently-scaled BAFU sources (``Bq``/``kBq``, ``kg``/``m3`` for water,
    ``kWh``/``MJ``) end up with two different ``target["unit"]`` values and no
    ``conversion_factor`` to reconcile them.

    Per physical dimension (``_DIMENSION``, canonicalised via
    ``_NOMENCLATURE_TARGET_UNIT``): mass -> kilogram (``kg`` needs no factor), EXCEPT
    a water flow (``_is_water_flow`` on ``ef_flow``, never on ``flow`` -- the source
    name is not what is being checked) -> cubic meter, reusing the exact water-density
    factor ``conversion_for`` applies to a characterised water-use match
    (``_WATER_DENSITY_FACTOR``); activity -> kBq (``Bq`` rescales by
    ``_SCALED[("Bq", "kBq")]``, ``kBq`` needs no factor); energy -> megajoule (``kWh``
    rescales by ``_SCALED[("kWh", "megajoule")]``, ``MJ`` needs no factor); volume ->
    cubic meter (``m3``/``Nm3`` need no factor, same physical scale); area -> ``m2``;
    area-time -> ``m2*a`` (``m2a`` needs no factor, same physical scale); volume-time
    has no EF convention at all -- BAFU's own ``m3y`` spelling is kept as-is (the one
    real BAFU flow in this dimension, "Volume occupied, reservoir").

    A source unit outside every dimension table entirely is returned unchanged, with
    no factor -- a spelling courtesy, not a claim of physical accuracy, same as the
    ``nomenclature_unit`` respelling this replaces.

    Never called for a (``flow``, ``ef_flow``) pair ``nomenclature_unit_mismatch``
    would refuse (round 5, decision 2026-09-14, e.g. a cubic-meter "Wood" source):
    ``_decide`` withholds any such pair as ``unit_mismatch`` before an entry -- and
    this function -- is ever reached for it, the same contract ``entry_for``'s own
    ``ValueError`` documents for a characterised mismatch.
    """
    dimension = _DIMENSION.get(flow.unit)
    if dimension is None:
        return flow.unit, None
    if dimension == "mass" and _is_water_flow(ef_flow):
        return "cubic meter", _WATER_DENSITY_FACTOR
    target_unit = _NOMENCLATURE_TARGET_UNIT[dimension]
    if flow.unit == target_unit:
        return target_unit, None
    return target_unit, _SCALED.get((flow.unit, target_unit))


def unit_conversion(bafu_unit: str, ef_unit: str | None) -> float | None:
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
    share the same EF target; then a stoichiometric factor (``STOICHIOMETRIC``,
    decision 2026-09-13) whenever the target's reference unit is kilogram, the same
    way and for the same reason; then the water density special case (depends on the
    target's characterisation method, so it cannot live in the generic, method-blind
    ``unit_conversion`` table); then the generic, unit-string-only fallback.

    The energy-content caveat's own wording depends on the key (round 7, decision
    2026-09-14): a key in ``_REGRESSION_INFERRED_ENERGY_CONTENT`` names the parity-check
    regression that produced its factor; every other key keeps the original net
    calorific value wording. A key also present in ``ENERGY_CONTENT_NOTES`` (decision
    (g), 2026-09-13: the two coal-mine off-gas keys) has that extra text appended to
    the caveat on top of either wording, disclosing an assumption the factor alone
    would not.
    """
    if index.reference_unit(match.code) == "megajoule":
        energy = ENERGY_CONTENT.get((flow.name, flow.unit))
        if energy is not None:
            if (flow.name, flow.unit) in _REGRESSION_INFERRED_ENERGY_CONTENT:
                caveat = (
                    f"energy content {energy:g} MJ/{flow.unit} (net calorific value "
                    "inferred from BAFU's published EF 3.1 resource-use scores, "
                    "regression R2 1.0, decision 2026-09-14)"
                )
            else:
                caveat = (
                    f"energy content {energy:g} MJ/{flow.unit} (net calorific value convention "
                    "of the BAFU-2026 source inventory)"
                )
            note = ENERGY_CONTENT_NOTES.get((flow.name, flow.unit))
            if note:
                caveat = f"{caveat}; {note}"
            return energy, caveat
    if index.reference_unit(match.code) == "kilogram":
        stoichiometric = STOICHIOMETRIC.get((flow.name, flow.unit))
        if stoichiometric is not None:
            return stoichiometric, STOICHIOMETRIC_NOTES[(flow.name, flow.unit)]
    if flow.unit == "kg" and set(index.vector(match.code)) == {WATER_USE_METHOD}:
        # water is the only substance with a fixed mass -> volume factor (density);
        # this depends on the target's characterisation method, so it cannot live in
        # the generic, method-blind ``unit_conversion`` table above. Shared with
        # nomenclature_target_unit's own uncharacterised water special case (round 5,
        # decision 2026-09-14) via _WATER_DENSITY_FACTOR, so the two never disagree.
        return _WATER_DENSITY_FACTOR, None
    factor = unit_conversion(flow.unit, index.reference_unit(match.code))
    return None if factor is None else (factor, None)
