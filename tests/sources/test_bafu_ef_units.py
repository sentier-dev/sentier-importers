import pytest
from sentier_importers.matching.ef_index import EfFlowIndex
from sentier_importers.matching.pipeline import Match
from sentier_importers.sources.bafu.ef_units import (
    ENERGY_CONTENT,
    conversion_for,
    nomenclature_unit,
    unit_conversion,
)
from sentier_importers.sources.eaternity.bridge import BafuFlow

from tests.matching.ef_fixtures import RES_GROUND, cf_row, vocab_row, write_ef_inputs


@pytest.mark.parametrize(
    "bafu_unit,ef_unit,expected",
    [
        ("kg", "kilogram", 1.0),
        ("kilogram", "kg", 1.0),
        ("m3", "cubic meter", 1.0),
        ("cubic meter", "m3", 1.0),
        ("kBq", "kBq", 1.0),
        ("m2", "m2", 1.0),
        ("m2*a", "m2*a", 1.0),
        ("m2a", "m2*a", 1.0),  # BAFU spells it without the asterisk; same area-time
        ("MJ", "megajoule", 1.0),
        ("Bq", "kBq", 0.001),
        ("kWh", "megajoule", 3.6),
        ("m3", "megajoule", None),  # volume vs energy: no fixed conversion
        ("kg", "kBq", None),  # mass vs activity: no fixed conversion
        ("Nm3", "kilogram", None),  # volume vs mass: no fixed conversion
        ("m2", "m2*a", None),  # area vs area-time: no fixed conversion
        ("kg", "unknown-unit", None),  # unit outside the dimension table at all
    ],
)
def test_unit_conversion_fixed_factors_and_cross_dimension_mismatches(
    bafu_unit, ef_unit, expected
):
    assert unit_conversion(bafu_unit, ef_unit) == expected


@pytest.mark.parametrize(
    "bafu_unit,expected",
    [
        ("kg", ("kilogram", None)),
        ("Bq", ("kBq", 0.001)),
        ("kBq", ("kBq", None)),
        ("m3", ("cubic meter", None)),
        ("Nm3", ("cubic meter", None)),
        ("MJ", ("megajoule", None)),
        ("kWh", ("megajoule", 3.6)),
        ("m2", ("m2", None)),
        ("m2a", ("m2*a", None)),
        ("unknown-unit", ("unknown-unit", None)),  # passed through unchanged
    ],
)
def test_nomenclature_unit_covers_every_bafu_unit(bafu_unit, expected):
    assert nomenclature_unit(bafu_unit) == expected


#: Hardcoded independently of ENERGY_CONTENT itself (not derived from the dict under
#: test) so that deleting a key or changing a value actually fails a case below,
#: rather than merely shrinking the parametrization.
_ENERGY_CONTENT_CASES = [
    ("Coal, hard", "kg", 19.1),
    ("Coal, brown", "kg", 9.9),
    ("Oil, crude", "kg", 45.8),
    ("Peat", "kg", 9.9),
    ("Uranium", "kg", 560_000.0),
    ("Gas, natural/m3", "m3", 38.3),
    ("Gas, natural/m3", "Nm3", 38.3),
]


def test_energy_content_table_has_exactly_these_seven_entries():
    assert dict(ENERGY_CONTENT) == {(n, u): f for n, u, f in _ENERGY_CONTENT_CASES}


@pytest.mark.parametrize("name,unit,factor", _ENERGY_CONTENT_CASES)
def test_conversion_for_applies_every_energy_content_table_entry(
    tmp_path_factory, name, unit, factor
):
    # every (name, unit) key ENERGY_CONTENT is expected to carry, including both
    # "Gas, natural/m3" units (m3 and Nm3), pinned directly against conversion_for: a
    # tiny index with one fossil-resource-method EF flow named exactly like the BAFU
    # flow, so the megajoule reference-unit branch fires and the table factor comes
    # back intact.
    cf = [
        cf_row("target", name.lower(), RES_GROUND, method="ef-3.1:resource-use-fossils", value=1.0)
    ]
    vocab = [vocab_row("target", name)]
    index = EfFlowIndex.from_files(
        *write_ef_inputs(tmp_path_factory.mktemp("energy-content"), cf, vocab)
    )
    flow = BafuFlow(name, "resources", "in ground", unit)
    match = Match(
        code="target", tier="name", placement="exact", location=None, candidates=1, caveats=()
    )
    got = conversion_for(flow, match, index)
    assert got == (
        factor,
        f"energy content {factor:g} MJ/{unit} (ecoinvent v2 net calorific value)",
    )


def test_conversion_for_applies_the_water_density_special_case(tmp_path):
    # depends on the target's characterisation method (water-use only), so it cannot
    # live in the generic unit_conversion table -- pinned directly here.
    cf = [cf_row("water-em", "water", RES_GROUND, method="ef-3.1:water-use", value=-37.8)]
    vocab = [vocab_row("water-em", "Water", cas="7732-18-5")]
    index = EfFlowIndex.from_files(*write_ef_inputs(tmp_path, cf, vocab))
    flow = BafuFlow("Water", "emissions to water", "unspecified", "kg")
    match = Match(
        code="water-em", tier="name", placement="exact", location=None, candidates=1, caveats=()
    )
    assert conversion_for(flow, match, index) == (0.001, None)


def test_conversion_for_falls_back_to_unit_conversion(tmp_path):
    cf = [cf_row("co2", "carbon dioxide", RES_GROUND, value=1.0)]
    vocab = [vocab_row("co2", "Carbon dioxide")]
    index = EfFlowIndex.from_files(*write_ef_inputs(tmp_path, cf, vocab))
    flow = BafuFlow("Carbon dioxide", "emissions to air", "unspecified", "kg")
    match = Match(
        code="co2", tier="name", placement="exact", location=None, candidates=1, caveats=()
    )
    assert conversion_for(flow, match, index) == (1.0, None)


def test_conversion_for_returns_none_when_nothing_applies(tmp_path):
    cf = [
        cf_row(
            "u238",
            "uranium-238",
            RES_GROUND,
            method="ef-3.1:ionising-radiation-human-health",
            value=1.0,
        )
    ]
    vocab = [vocab_row("u238", "Uranium-238")]
    index = EfFlowIndex.from_files(*write_ef_inputs(tmp_path, cf, vocab))
    flow = BafuFlow("Uranium-238", "emissions to air", "unspecified", "kg")
    match = Match(
        code="u238", tier="name", placement="exact", location=None, candidates=1, caveats=()
    )
    assert conversion_for(flow, match, index) is None
