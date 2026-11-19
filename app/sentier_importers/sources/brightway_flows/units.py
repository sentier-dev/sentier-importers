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
    # The ISO standard cubic metre (15 degrees Celsius, 1 atm): QUDT has it
    # (http://qudt.org/vocab/unit/SCM_1ATM_15DEG_C_ISO answered 200 on 2026-11-19).
    "SCM_1ATM_15DEG_C_ISO": QUDT + "SCM_1ATM_15DEG_C_ISO",
    # QUDT has no area-time or volume-time unit (as in bafu.units):
    "M2-YR": SENTIER + "M2-YR",
    "M3-YR": SENTIER + "M3-YR",
    # QUDT has no mass-time unit of this kind
    # (http://qudt.org/vocab/unit/KiloGM-YR answered 404 on 2026-11-19):
    "KiloGM-YR": SENTIER + "KiloGM-YR",
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
