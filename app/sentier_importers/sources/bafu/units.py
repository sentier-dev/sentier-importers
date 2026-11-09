"""BAFU-2026 unit strings -> unit IRIs for the ``qudt:hasUnit`` relation of a flow term.

A BAFU elementary flow is identified by (name, category, subCategory, unit), so its
vocab term states the unit as a relation. The object is the QUDT unit where QUDT
defines one, else the unit of Sentier's own units scheme, which sentier-vocab holds
as a term of type ``unit`` in the ``sentier-core`` release.
"""

from sentier_importers.core.errors import ValidationError

QUDT = "http://qudt.org/vocab/unit/"
SENTIER = "https://vocab.sentier.dev/units/unit/"

#: Every unit of a biosphere exchange in the BAFU-2026 v1 EcoSpold export.
UNIT_IRI: dict[str, str] = {
    "Bq": QUDT + "BQ",
    "kBq": QUDT + "KiloBQ",
    "kg": QUDT + "KiloGM",
    "km": QUDT + "KiloM",
    "kWh": QUDT + "KiloW-HR",
    "m": QUDT + "M",
    "m2": QUDT + "M2",
    "m3": QUDT + "M3",
    "MJ": QUDT + "MegaJ",
    "Nm3": QUDT + "NCM",  # normal cubic metre
    # QUDT has no area-time, volume-time or transport-performance unit:
    "m2a": SENTIER + "M2-YR",
    "m3y": SENTIER + "M3-YR",
    "personkm": SENTIER + "PERSON-KiloM",
    "tkm": SENTIER + "KiloM-TONNE",
}

#: Prefix of the ``additional_notations`` entry keeping the unit as BAFU spells it.
NOTATION_PREFIX = "bafu-unit:"


def unit_iri(unit: str) -> str:
    """IRI of a BAFU unit string; an unmapped unit is an error, never a silent gap."""
    try:
        return UNIT_IRI[unit]
    except KeyError:
        raise ValidationError(
            f"BAFU unit {unit!r} has no unit IRI; add it to sources/bafu/units.py"
        ) from None
