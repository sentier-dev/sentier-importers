import pytest
from sentier_importers.matching.ef_index import EfFlow, EfFlowIndex
from sentier_importers.matching.pipeline import Match
from sentier_importers.sources.bafu import ef_units
from sentier_importers.sources.bafu.ef_units import (
    _REGRESSION_INFERRED_ENERGY_CONTENT,
    ENERGY_CONTENT,
    ENERGY_CONTENT_NOTES,
    STOICHIOMETRIC,
    STOICHIOMETRIC_NOTES,
    conversion_for,
    conversion_onto,
    nomenclature_target_unit,
    stated_target_unit,
    unit_agrees,
    unit_conversion,
    unit_mismatch_detail,
)
from sentier_importers.sources.eaternity.bridge import BafuFlow

from tests.matching.ef_fixtures import RES_GROUND, cf_row, vocab_row, write_ef_inputs


def _ef_flow(name: str = "Widget") -> EfFlow:
    """A bare, uncharacterised ``EfFlow`` carrying only what ``nomenclature_target_unit``
    ever looks at: its name (for the water special case) -- context/code are
    irrelevant to the unit table, so kept minimal.
    """
    return EfFlow(code="x", name=name, context=(), characterised=False)


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


#: Round 5, decision 2026-09-14: one EF-convention target unit per physical
#: dimension, with the factor (``None`` when the source is already at that scale)
#: that ``nomenclature_target_unit`` must return for it. Every non-water case here
#: uses a plain "Widget" target -- the water density exception has its own
#: parametrization below.
@pytest.mark.parametrize(
    "bafu_unit,expected_unit,expected_factor",
    [
        ("kg", "kilogram", None),
        ("Bq", "kBq", 0.001),
        ("kBq", "kBq", None),
        ("m3", "cubic meter", None),
        ("Nm3", "cubic meter", None),
        ("MJ", "megajoule", None),
        ("kWh", "megajoule", 3.6),
        ("m2", "m2", None),
        ("m2a", "m2*a", None),
        ("m3y", "m3y", None),  # volume-time: EF has no convention, spelling kept as-is
        ("unknown-unit", "unknown-unit", None),  # outside every dimension table
    ],
)
def test_nomenclature_target_unit_covers_every_dimension(
    bafu_unit, expected_unit, expected_factor
):
    flow = BafuFlow("Widget", "emissions to air", "unspecified", bafu_unit)
    assert nomenclature_target_unit(flow, _ef_flow("Widget")) == (expected_unit, expected_factor)


@pytest.mark.parametrize(
    "bafu_unit,expected_unit,expected_factor",
    [
        ("kg", "cubic meter", 0.001),  # the one mass -> volume exception: water density
        ("m3", "cubic meter", None),  # already volume: no rescale needed
    ],
)
def test_nomenclature_target_unit_applies_the_water_density_exception(
    bafu_unit, expected_unit, expected_factor
):
    # "Water" is the one substance name EF's water-use method characterises, under
    # every context -- the uncharacterised nomenclature package identifies the same
    # substance by name (it carries no CF vector to check, unlike conversion_for's own
    # characterised water-use special case; see nomenclature_target_unit's docstring).
    flow = BafuFlow("Water", "resources", "in water", bafu_unit)
    assert nomenclature_target_unit(flow, _ef_flow("Water")) == (expected_unit, expected_factor)


def test_nomenclature_target_unit_water_exception_is_case_and_whitespace_insensitive():
    flow = BafuFlow("Water, KR", "emissions to water", "river", "kg")
    assert nomenclature_target_unit(flow, _ef_flow(" WATER ")) == ("cubic meter", 0.001)


def test_nomenclature_target_unit_never_applies_water_density_outside_mass():
    # a non-mass source unit onto EF's "Water" substance is not a density conversion
    # at all -- the exception only ever intercepts the mass dimension. "MJ" is
    # physically nonsensical for water, but exercises the code path: the ordinary
    # energy-dimension rule applies, not the 0.001 water density factor.
    flow = BafuFlow("Water", "resources", "in water", "MJ")
    assert nomenclature_target_unit(flow, _ef_flow("Water")) == ("megajoule", None)


#: Hardcoded independently of ENERGY_CONTENT itself (not derived from the dict under
#: test) so that deleting a key or changing a value actually fails a case below,
#: rather than merely shrinking the parametrization. The fourth element is the extra
#: ``ENERGY_CONTENT_NOTES`` text a key carries, or ``None`` for a plain NCV entry.
_ENERGY_CONTENT_CASES = [
    ("Coal, hard", "kg", 17.73, None),
    ("Coal, brown", "kg", 9.41, None),
    ("Oil, crude", "kg", 43.40, None),
    ("Peat", "kg", 9.9, None),
    ("Uranium", "kg", 560_000.0, None),
    ("Gas, natural/m3", "m3", 35.98, None),
    ("Gas, natural/m3", "Nm3", 35.98, None),
    (
        "Gas, mine, off-gas, process, coal mining/m3",
        "m3",
        35.98,
        "coal-mine off-gas approximated as natural gas (decision 2026-09-13)",
    ),
    (
        "Gas, mine, off-gas, process, coal mining/m3",
        "Nm3",
        35.98,
        "coal-mine off-gas approximated as natural gas (decision 2026-09-13)",
    ),
]


def test_energy_content_table_has_exactly_these_nine_entries():
    assert dict(ENERGY_CONTENT) == {(n, u): f for n, u, f, _note in _ENERGY_CONTENT_CASES}


def test_energy_content_notes_cover_exactly_the_two_mine_gas_keys():
    assert dict(ENERGY_CONTENT_NOTES) == {
        (n, u): note for n, u, _f, note in _ENERGY_CONTENT_CASES if note is not None
    }


def test_regression_inferred_energy_content_covers_exactly_these_keys():
    # round 7, decision 2026-09-14: Coal (hard/brown), Oil (crude) and both
    # natural-gas keys (including the coal-mine off-gas approximation) earn the
    # regression caveat wording; Peat and Uranium are unchanged and keep the
    # original wording.
    assert _REGRESSION_INFERRED_ENERGY_CONTENT == {
        ("Coal, hard", "kg"),
        ("Coal, brown", "kg"),
        ("Oil, crude", "kg"),
        ("Gas, natural/m3", "m3"),
        ("Gas, natural/m3", "Nm3"),
        ("Gas, mine, off-gas, process, coal mining/m3", "m3"),
        ("Gas, mine, off-gas, process, coal mining/m3", "Nm3"),
    }


@pytest.mark.parametrize("name,unit,factor,note", _ENERGY_CONTENT_CASES)
def test_conversion_for_applies_every_energy_content_table_entry(
    tmp_path_factory, name, unit, factor, note
):
    # every (name, unit) key ENERGY_CONTENT is expected to carry, including both
    # "Gas, natural/m3" units (m3 and Nm3) and the two coal-mine off-gas units,
    # pinned directly against conversion_for: a tiny index with one
    # fossil-resource-method EF flow named exactly like the BAFU flow, so the
    # megajoule reference-unit branch fires and the table factor comes back intact,
    # with the extra note appended for a key that carries one.
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
    if (name, unit) in _REGRESSION_INFERRED_ENERGY_CONTENT:
        caveat = (
            f"energy content {factor:g} MJ/{unit} (net calorific value inferred from "
            "BAFU's published EF 3.1 resource-use scores, regression R2 1.0, decision "
            "2026-09-14)"
        )
    else:
        caveat = (
            f"energy content {factor:g} MJ/{unit} (net calorific value convention of the "
            "BAFU-2026 source inventory)"
        )
    if note:
        caveat = f"{caveat}; {note}"
    assert got == (factor, caveat)


#: Hardcoded independently of STOICHIOMETRIC/STOICHIOMETRIC_NOTES themselves, same
#: rationale as _ENERGY_CONTENT_CASES above.
_STOICHIOMETRIC_CASES = [
    (
        "TiO2, 54% in ilmenite, 2.6% in crude ore",
        "kg",
        0.5995,
        "amount is kg TiO2; 0.5995 is the titanium mass fraction of TiO2 (decision 2026-09-13)",
    ),
    (
        "TiO2, 95% in rutile, 0.40% in crude ore",
        "kg",
        0.5995,
        "amount is kg TiO2; 0.5995 is the titanium mass fraction of TiO2 (decision 2026-09-13)",
    ),
]


def test_stoichiometric_table_has_exactly_these_two_entries():
    assert dict(STOICHIOMETRIC) == {(n, u): f for n, u, f, _note in _STOICHIOMETRIC_CASES}


def test_stoichiometric_notes_cover_exactly_the_same_two_keys():
    assert dict(STOICHIOMETRIC_NOTES) == {(n, u): note for n, u, _f, note in _STOICHIOMETRIC_CASES}


@pytest.mark.parametrize("key,factor", list(STOICHIOMETRIC.items()))
def test_every_stoichiometric_note_states_its_own_factor(key, factor):
    assert f"{factor:g}" in STOICHIOMETRIC_NOTES[key]


@pytest.mark.parametrize("name,unit,factor,note", _STOICHIOMETRIC_CASES)
def test_conversion_for_applies_every_stoichiometric_table_entry(
    tmp_path_factory, name, unit, factor, note
):
    cf = [cf_row("target", "titanium", RES_GROUND, value=1.0)]
    vocab = [vocab_row("target", "Titanium")]
    index = EfFlowIndex.from_files(
        *write_ef_inputs(tmp_path_factory.mktemp("stoichiometric"), cf, vocab)
    )
    flow = BafuFlow(name, "resources", "in ground", unit)
    match = Match(
        code="target", tier="alias", placement="exact", location=None, candidates=1, caveats=()
    )
    assert conversion_for(flow, match, index) == (factor, note)


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


# --- the unit gate: a candidate that states a unit the source cannot be converted to


def _stated(unit: str | None, name: str = "Widget") -> EfFlow:
    """A factorless ``EfFlow`` that states ``unit`` (``None``: states none)."""
    return EfFlow(code="x", name=name, context=(), characterised=False, stated_unit=unit)


_NO_INDEX = EfFlowIndex.from_tables([], [])


def _source(unit: str, name: str = "Widget") -> BafuFlow:
    return BafuFlow(name, "emissions to air", "unspecified", unit)


@pytest.mark.parametrize("unit", ["kg", "Bq", "m3", "MJ", "tkm", ""])
def test_a_candidate_that_states_no_unit_always_agrees(unit):
    assert unit_agrees(_source(unit), _stated(None), _NO_INDEX)


@pytest.mark.parametrize(
    ("bafu_unit", "stated"),
    [
        ("kg", "kilogram"),
        ("Bq", "kBq"),
        ("kBq", "kBq"),
        ("m3", "cubic meter"),
        ("Nm3", "cubic meter"),
        ("m2", "m2"),
        ("m2a", "m2*a"),
        ("m3y", "m3*a"),
        ("MJ", "megajoule"),
        ("kWh", "megajoule"),
    ],
)
def test_a_stated_unit_of_the_source_dimension_agrees(bafu_unit, stated):
    assert unit_agrees(_source(bafu_unit), _stated(stated), _NO_INDEX)


@pytest.mark.parametrize(
    ("bafu_unit", "stated"),
    [
        ("kg", "kBq"),  # the 2026-11-19 defect: Uranium in kg onto Uranium Alpha
        ("Bq", "kilogram"),
        ("kBq", "kilogram"),
        ("kg", "megajoule"),
        ("kg", "cubic meter"),
        ("m3", "kilogram"),
        ("m3", "megajoule"),
        ("m2", "m2*a"),
        ("m3", "m3*a"),
        ("kg", "kg*a"),
        ("m3", "Sm3"),  # a standard cubic metre is not a cubic metre of anything
        ("Nm3", "Sm3"),
        ("tkm", "kilogram"),  # a source unit of no known dimension
    ],
)
def test_a_stated_unit_of_another_dimension_does_not_agree(bafu_unit, stated):
    assert not unit_agrees(_source(bafu_unit), _stated(stated), _NO_INDEX)


def test_the_water_density_rule_covers_a_mass_source_on_a_factorless_water_flow():
    assert unit_agrees(_source("kg", "Water, DE"), _stated("cubic meter", "Water"), _NO_INDEX)
    # only water has a density here
    assert not unit_agrees(_source("kg", "Brine"), _stated("cubic meter", "Brine"), _NO_INDEX)


def test_the_water_density_rule_covers_a_mass_source_on_a_water_use_flow(tmp_path):
    cf = [cf_row("river", "river water", RES_GROUND, method="ef-3.1:water-use", value=42.95)]
    index = EfFlowIndex.from_files(*write_ef_inputs(tmp_path, cf, [vocab_row("river", "River")]))
    river = EfFlow(code="river", name="river water", context=(), stated_unit="cubic meter")
    assert unit_agrees(_source("kg", "Water, river"), river, index)


def test_an_energy_content_rule_covers_its_own_source_on_a_megajoule_flow():
    megajoule = _stated("megajoule", "Hard Coal")
    assert unit_agrees(_source("kg", "Coal, hard"), megajoule, _NO_INDEX)
    assert unit_agrees(_source("Nm3", "Gas, natural/m3"), megajoule, _NO_INDEX)
    assert not unit_agrees(_source("kg", "Coal, soft"), megajoule, _NO_INDEX)
    # the rule is a factor onto megajoule, not a licence to cross onto any unit
    assert not unit_agrees(_source("kg", "Uranium"), _stated("kBq", "Uranium Alpha"), _NO_INDEX)


def test_conversion_onto_is_what_conversion_for_applies(tmp_path):
    flow = _source("kg", "Water")
    assert conversion_onto(flow, "cubic meter", water_target=True) == (0.001, None)
    assert conversion_onto(flow, "cubic meter", water_target=False) is None
    assert conversion_onto(flow, "kilogram", water_target=True) == (1.0, None)
    assert conversion_onto(_source("Bq"), "kBq", water_target=False) == (0.001, None)
    factor, caveat = conversion_onto(_source("kg", "Peat"), "megajoule", water_target=False)
    assert factor == 9.9 and caveat.startswith("energy content 9.9 MJ/kg")


@pytest.mark.parametrize(
    ("flow", "ef_flow", "expected"),
    [
        (_source("kg"), _stated("kilogram"), ("kilogram", None, None)),
        (_source("Bq"), _stated("kBq"), ("kBq", 0.001, None)),
        (_source("kBq"), _stated("kBq"), ("kBq", None, None)),
        (_source("kWh"), _stated("megajoule"), ("megajoule", 3.6, None)),
        # the stated unit, not BAFU's own spelling the convention guess kept
        (_source("m3y"), _stated("m3*a"), ("m3*a", None, None)),
        (_source("kg", "Water"), _stated("cubic meter", "Water"), ("cubic meter", 0.001, None)),
        # a kilogram water flow that states kilogram is not moved onto cubic meter
        (_source("kg", "Water"), _stated("kilogram", "Water"), ("kilogram", None, None)),
    ],
)
def test_stated_target_unit_follows_the_stated_unit(flow, ef_flow, expected):
    assert stated_target_unit(flow, ef_flow) == expected


def test_stated_target_unit_carries_the_caveat_of_an_energy_content_factor():
    unit, factor, caveat = stated_target_unit(_source("kg", "Peat"), _stated("megajoule", "Peat"))
    assert (unit, factor) == ("megajoule", 9.9)
    assert caveat.startswith("energy content 9.9 MJ/kg")


def test_stated_target_unit_refuses_a_pair_the_gate_refuses():
    with pytest.raises(ValueError, match="kg.*kBq"):
        stated_target_unit(_source("kg", "Uranium"), _stated("kBq", "Uranium Alpha"))


def test_unit_mismatch_detail_names_the_units_and_the_flows():
    flows = [_stated("kBq", "Uranium Alpha"), _stated("kBq", "Uranium Alpha")]
    assert unit_mismatch_detail(_source("kg", "Uranium"), flows) == (
        "BAFU unit kg vs EF reference unit kBq for Uranium Alpha; no fixed conversion"
    )


def test_no_name_table_fixes_the_dimension_of_wood_any_more():
    # NOMENCLATURE_TARGET_DIMENSION = {"Wood": "mass"} (decision 2026-09-14) is
    # retired: EF "Wood" is an energy resource that states megajoule, so the table
    # was contradicted by the data, and the unit gate withholds both BAFU spellings.
    assert not hasattr(ef_units, "NOMENCLATURE_TARGET_DIMENSION")
    assert not hasattr(ef_units, "nomenclature_unit_mismatch")
    wood = _stated("megajoule", "Wood")
    assert not unit_agrees(_source("m3", "Wood, hard, standing"), wood, _NO_INDEX)
    assert not unit_agrees(_source("kg", "Wood, unspecified, standing/kg"), wood, _NO_INDEX)
