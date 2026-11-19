"""Brightway Flows unit codes of the EF 3.1 correspondence -> unit IRIs."""

import pytest
from sentier_importers.core.errors import ValidationError
from sentier_importers.sources.brightway_flows import units

QUDT = "http://qudt.org/vocab/unit/"
SENTIER = "https://vocab.sentier.dev/units/unit/"


@pytest.mark.parametrize("code", ["KiloGM", "KiloBQ", "M2", "M3", "MegaJ", "SCM_1ATM_15DEG_C_ISO"])
def test_a_unit_qudt_has_is_its_qudt_iri(code):
    assert units.unit_iri(code) == QUDT + code


@pytest.mark.parametrize("code", ["M2-YR", "M3-YR", "KiloGM-YR"])
def test_a_unit_qudt_lacks_is_a_sentier_core_unit(code):
    assert units.unit_iri(code) == SENTIER + code


def test_the_table_is_the_nine_units_of_the_ef_correspondence():
    assert len(units.UNIT_IRI) == 9


def test_an_unknown_code_is_an_error_and_never_a_guess():
    with pytest.raises(ValidationError, match="'KiloM'.*brightway_flows/units.py"):
        units.unit_iri("KiloM")


# --- the spelling the mapping packages use for a unit IRI ----------------------


@pytest.mark.parametrize(
    ("iri", "spelling"),
    [
        (QUDT + "KiloGM", "kilogram"),
        (QUDT + "KiloBQ", "kBq"),
        (QUDT + "M2", "m2"),
        (QUDT + "M3", "cubic meter"),
        (QUDT + "MegaJ", "megajoule"),
        (QUDT + "SCM_1ATM_15DEG_C_ISO", "Sm3"),
        (SENTIER + "M2-YR", "m2*a"),
        (SENTIER + "M3-YR", "m3*a"),
        (SENTIER + "KiloGM-YR", "kg*a"),
    ],
)
def test_a_unit_iri_has_the_spelling_of_the_mapping_packages(iri, spelling):
    assert units.unit_spelling(iri) == spelling


def test_every_unit_iri_has_a_spelling():
    assert set(units.UNIT_SPELLING) == set(units.UNIT_IRI.values())


def test_an_unknown_iri_has_no_spelling_and_is_an_error():
    with pytest.raises(ValidationError, match="KiloM.*brightway_flows/units.py"):
        units.unit_spelling(QUDT + "KiloM")
