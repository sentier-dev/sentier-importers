"""Brightway Flows unit codes -> unit IRIs for the ``qudt:hasUnit`` relation of an
EF 3.1 flow term.

Brightway Flows states the unit of a flow as an IRI of its own units scheme whose
last segment is a QUDT-style code (``KiloGM``). The object of the vocab relation
follows the rule of ``bafu.units``: the QUDT unit where QUDT has it, else a term of
type ``unit`` of the ``sentier-core`` release (``sources/sentier_core/data/seed.yaml``).
"""

from sentier_importers.core.errors import ValidationError
from sentier_importers.sources.bafu.units import QUDT, SENTIER

#: Every unit of the EF 3.1 correspondence of Brightway Flows 1.1.0.
UNIT_IRI: dict[str, str] = {
    "KiloGM": QUDT + "KiloGM",
    "KiloBQ": QUDT + "KiloBQ",
    "M2": QUDT + "M2",
    "M3": QUDT + "M3",
    "MegaJ": QUDT + "MegaJ",
    # QUDT has no area-time or volume-time unit (as in bafu.units):
    "M2-YR": SENTIER + "M2-YR",
    "M3-YR": SENTIER + "M3-YR",
    # Not established as QUDT units from what the DdS repos hold (2026-11-19):
    # Sentier's own units scheme defines mass-time itself (KiloGM-SEC, KiloGM-DAY),
    # and the QUDT standard cubic metre it carries (SCM) is at 0 degrees Celsius,
    # not at the 15 degrees of this one. Both are sentier-core unit terms until a
    # QUDT IRI is confirmed.
    "KiloGM-YR": SENTIER + "KiloGM-YR",
    "SCM_1ATM_15DEG_C_ISO": SENTIER + "SCM_1ATM_15DEG_C_ISO",
}


def unit_iri(code: str) -> str:
    """IRI of a Brightway Flows unit code; an unmapped code is an error, never a guess."""
    try:
        return UNIT_IRI[code]
    except KeyError:
        raise ValidationError(
            f"Brightway Flows unit {code!r} has no unit IRI; add it to "
            "sources/brightway_flows/units.py"
        ) from None
