"""Unit/dimension conversion helpers for the bafu-2026-v1 -> EF 3.1 bridges.

Split out of ``mappings_biosphere_matched`` so the unit-conversion domain (physical
dimensions, the ecoinvent v2 energy-content table, the water-density special case, and
the nomenclature-only unit respelling rank 8 uses for an uncharacterised target) has
its own home apart from the matching/decision pipeline itself. Everything here is a
pure function or lookup table; nothing touches ``BafuFlow``/EF index construction.
"""

from __future__ import annotations

from sentier_importers.matching.ef_index import EfFlowIndex
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
#: BAFU-2026 inventory is built from. Decision 2026-09-13. `Gas, natural/m3` exists in
#: both m3 and Nm3 in BAFU; both are treated as normal cubic metres. Decision (g),
#: 2026-09-13: coal-mine off-gas (which the pipeline itself matches onto EF's "Natural
#: Gas" resource flow, by CAS -- both share CAS 8006-14-2) is approximated at the same
#: 38.3 MJ/m3 natural-gas value, in both its m3 and Nm3 variants; see
#: ``ENERGY_CONTENT_NOTES`` for the extra caveat text these two keys carry.
ENERGY_CONTENT: dict[tuple[str, str], float] = {
    ("Coal, hard", "kg"): 19.1,
    ("Coal, brown", "kg"): 9.9,
    ("Oil, crude", "kg"): 45.8,
    ("Peat", "kg"): 9.9,
    ("Uranium", "kg"): 560_000.0,
    ("Gas, natural/m3", "m3"): 38.3,
    ("Gas, natural/m3", "Nm3"): 38.3,
    ("Gas, mine, off-gas, process, coal mining/m3", "m3"): 38.3,
    ("Gas, mine, off-gas, process, coal mining/m3", "Nm3"): 38.3,
}

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

#: BAFU unit -> EF spelling of the exact same physical scale, used only for an
#: uncharacterised EF target (rank 8, ``mappings_biosphere_nomenclature``): such a
#: target has no reference unit at all (``EfFlowIndex.reference_unit`` returns
#: ``None`` for it, and there is no CF-method convention to read one off of), so rank
#: 8 must never rescale an amount -- only respell the unit BAFU already reports, one
#: for one. Every entry here is a same-scale spelling pair only (``kg``/``kilogram``,
#: ``m3``/``Nm3``/``cubic meter``, ``m2a``/``m2*a``); ``Bq``, ``kBq``, ``kWh`` and
#: ``m2`` map to themselves -- unlike ``unit_conversion``/``conversion_for``, this
#: table carries no scaled pair at all (no Bq->kBq, no kWh->megajoule): those need a
#: factor, and rank 8 has none to apply. A unit not listed here is passed through
#: unchanged.
_NOMENCLATURE_UNITS: dict[str, str] = {
    "kg": "kilogram",
    "Bq": "Bq",
    "kBq": "kBq",
    "m3": "cubic meter",
    "Nm3": "cubic meter",
    "MJ": "megajoule",
    "kWh": "kWh",
    "m2": "m2",
    "m2a": "m2*a",
}


def nomenclature_unit(bafu_unit: str) -> str:
    """The EF spelling of ``bafu_unit``, same physical scale, never a factor.

    Used only when the match target is uncharacterised (rank 8): such a target has no
    EF reference unit at all (``EfFlowIndex.reference_unit`` returns ``None`` for it),
    so this is what ``entry_for`` uses to fill ``target["unit"]`` instead --
    ``entry_for`` never sets ``conversion_factor`` for one. ``bafu_unit`` outside
    :data:`_NOMENCLATURE_UNITS` is returned unchanged -- this is a spelling courtesy,
    not a claim of physical accuracy.
    """
    return _NOMENCLATURE_UNITS.get(bafu_unit, bafu_unit)


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
    share the same EF target; then the water density special case (depends on the
    target's characterisation method, so it cannot live in the generic, method-blind
    ``unit_conversion`` table); then the generic, unit-string-only fallback.

    A key also present in ``ENERGY_CONTENT_NOTES`` (decision (g), 2026-09-13: the two
    coal-mine off-gas keys) has that extra text appended to the caveat, disclosing an
    assumption the factor alone would not.
    """
    if index.reference_unit(match.code) == "megajoule":
        energy = ENERGY_CONTENT.get((flow.name, flow.unit))
        if energy is not None:
            caveat = (
                f"energy content {energy:g} MJ/{flow.unit} (net calorific value convention "
                "of the BAFU-2026 source inventory)"
            )
            note = ENERGY_CONTENT_NOTES.get((flow.name, flow.unit))
            if note:
                caveat = f"{caveat}; {note}"
            return energy, caveat
    if flow.unit == "kg" and set(index.vector(match.code)) == {WATER_USE_METHOD}:
        # water is the only substance with a fixed mass -> volume factor (density);
        # this depends on the target's characterisation method, so it cannot live in
        # the generic, method-blind ``unit_conversion`` table above.
        return 0.001, None
    factor = unit_conversion(flow.unit, index.reference_unit(match.code))
    return None if factor is None else (factor, None)
