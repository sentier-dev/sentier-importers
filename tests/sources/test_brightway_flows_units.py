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
