"""Unit tests for the EF 3.1 global-duplicate resolution rules (no parquet I/O except
for the SimaPro-index parsing tests, which need real column bytes)."""

from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from loguru import logger
from sentier_importers.core.types import RawData
from sentier_importers.sources.agribalyse.ef_cf_dedup import (
    LAND_METHOD,
    WATER_METHOD,
    DuplicateResolution,
    SimaproLandIndex,
    _apply_prefix_alias,
    _arbiter_hits,
    _expand_pluralizations,
    _is_close,
    _is_region_variant,
    _land_index_keys,
    _pluralize_variants,
    _split_direction,
    _strip_simapro_prefix,
    harmonise_water_family,
    parse_simapro_index,
    resolve_global_duplicates,
)


@pytest.fixture
def warnings():
    """Capture loguru WARNING-level messages (loguru bypasses pytest's caplog)."""
    messages: list[str] = []
    sink_id = logger.add(lambda m: messages.append(str(m)), level="WARNING")
    yield messages
    logger.remove(sink_id)


def _rec(uuid, name, method, cf, location=None):
    return {
        "flow_uuid": uuid,
        "flow_name": name,
        "method_name": method,
        "cf": cf,
        "location": location,
    }


def test_no_duplicates_returns_empty_resolution():
    records = [
        _rec("u1", "CO2", "Climate change", 1.0),
        _rec("u2", "CH4", "Climate change", 28.0),
    ]
    resolution = resolve_global_duplicates(records, None)
    assert resolution == DuplicateResolution()


def test_water_duplicate_kept_by_42_95_family_positive():
    records = [
        _rec("w1", "Water to Cooling", WATER_METHOD, 42.95),
        _rec("w1", "Water to Cooling", WATER_METHOD, 37.8),
    ]
    resolution = resolve_global_duplicates(records, None)
    assert resolution.kept[(WATER_METHOD, "w1")] == 42.95
    assert resolution.report == [
        {
            "flow_name": "Water to Cooling",
            "method": WATER_METHOD,
            "kept": 42.95,
            "dropped": 37.8,
            "rule": "water",
        }
    ]


def test_water_duplicate_kept_by_42_95_family_negative_unspecified():
    # -42.955 is closer to the 42.95 anchor than -37.8: the "unspecified" sub-context row.
    records = [
        _rec("w2", "Water", WATER_METHOD, -42.955),
        _rec("w2", "Water", WATER_METHOD, -37.8),
    ]
    resolution = resolve_global_duplicates(records, None)
    assert resolution.kept[(WATER_METHOD, "w2")] == -42.955
    assert resolution.report[0]["rule"] == "water"


def test_land_symmetric_partner_resolved_from_single_deterministic_value():
    records = [
        # "to field" has one distinct value: an unambiguous partner.
        _rec("f_to", "to field", LAND_METHOD, 75.0),
        _rec("f_to", "to field", LAND_METHOD, 75.0),
        # "from field" is ambiguous; only -75.0 matches the partner in absolute terms.
        _rec("f_from", "from field", LAND_METHOD, -75.0),
        _rec("f_from", "from field", LAND_METHOD, -60.0),
    ]
    resolution = resolve_global_duplicates(records, None)
    assert resolution.kept[(LAND_METHOD, "f_from")] == -75.0
    entry = next(r for r in resolution.report if r["flow_name"] == "from field")
    assert entry["rule"] == "symmetric"
    assert entry["dropped"] == -60.0


def test_land_symmetric_partner_uses_tolerance_not_exact_equality():
    # Partner's single value carries floating-point noise relative to our candidate:
    # still symmetric within _ARBITER_RTOL.
    records = [
        _rec("f_to", "to noisy", LAND_METHOD, 75.00000000001),
        _rec("f_from", "from noisy", LAND_METHOD, -75.0),
        _rec("f_from", "from noisy", LAND_METHOD, -60.0),
    ]
    resolution = resolve_global_duplicates(records, None)
    assert resolution.kept[(LAND_METHOD, "f_from")] == -75.0
    entry = next(r for r in resolution.report if r["flow_name"] == "from noisy")
    assert entry["rule"] == "symmetric"


def test_land_partner_single_value_mismatch_falls_through_to_arbiter():
    # The partner's single value (200.0) matches neither candidate: symmetry can't
    # decide, so we fall through -- here straight to the larger-|value| fallback since
    # no arbiter is supplied.
    records = [
        _rec("g_to", "to gap", LAND_METHOD, 200.0),
        _rec("g_from", "from gap", LAND_METHOD, -75.0),
        _rec("g_from", "from gap", LAND_METHOD, -60.0),
    ]
    resolution = resolve_global_duplicates(records, None)
    entry = next(r for r in resolution.report if r["flow_name"] == "from gap")
    assert entry["rule"] == "larger"
    assert entry["kept"] == -75.0


def test_land_partner_ambiguous_but_only_one_candidate_matches():
    # Partner ("to edge") is itself a duplicate group, but only one of our candidates'
    # |values| appears among the partner's -- symmetry resolves it without an arbiter.
    records = [
        _rec("e_from", "from edge", LAND_METHOD, -40.0),
        _rec("e_from", "from edge", LAND_METHOD, -30.0),
        _rec("e_to", "to edge", LAND_METHOD, 40.0),
        _rec("e_to", "to edge", LAND_METHOD, 99.0),
    ]
    resolution = resolve_global_duplicates(records, None)
    entry = next(r for r in resolution.report if r["flow_name"] == "from edge")
    assert entry["rule"] == "symmetric"
    assert entry["kept"] == -40.0


def test_land_from_partner_entirely_absent_falls_through_to_arbiter():
    # "to lonely" does not appear anywhere (not a single, not a duplicate) -- partner
    # lookup finds nothing and we go straight to the arbiter/larger fallback.
    records = [
        _rec("z_from", "from lonely", LAND_METHOD, -12.0),
        _rec("z_from", "from lonely", LAND_METHOD, -9.0),
    ]
    resolution = resolve_global_duplicates(records, None)
    entry = next(r for r in resolution.report if r["flow_name"] == "from lonely")
    assert entry["rule"] == "larger"
    assert entry["kept"] == -12.0


def test_land_ambiguous_partner_uses_simapro_arbiter_exact_match():
    records = [
        _rec("p_from", "from parcel", LAND_METHOD, -100.0),
        _rec("p_from", "from parcel", LAND_METHOD, -50.0),
        _rec("p_to", "to parcel", LAND_METHOD, 100.0),
        _rec("p_to", "to parcel", LAND_METHOD, 50.0),
    ]
    simapro_index = SimaproLandIndex(primary={"from parcel": [-100.0], "to parcel": [50.0]})
    resolution = resolve_global_duplicates(records, simapro_index)
    assert resolution.kept[(LAND_METHOD, "p_from")] == -100.0
    assert resolution.kept[(LAND_METHOD, "p_to")] == 50.0
    from_entry = next(r for r in resolution.report if r["flow_name"] == "from parcel")
    to_entry = next(r for r in resolution.report if r["flow_name"] == "to parcel")
    assert from_entry["rule"] == "simapro"
    assert to_entry["rule"] == "simapro"


def test_land_arbiter_value_matching_neither_candidate_falls_back_to_larger(warnings):
    records = [
        _rec("n1", "nomatch", LAND_METHOD, 30.0),
        _rec("n1", "nomatch", LAND_METHOD, -10.0),
    ]
    simapro_index = SimaproLandIndex(primary={"nomatch": [999.0]})
    resolution = resolve_global_duplicates(records, simapro_index)
    assert resolution.kept[(LAND_METHOD, "n1")] == 30.0
    assert resolution.report[0]["rule"] == "larger"
    assert any("nomatch" in m and "undecided" in m for m in warnings)


def test_land_arbiter_matching_both_candidates_falls_back_to_larger():
    # A degenerate arbiter entry (or a colliding normalization) that equals BOTH of
    # our candidates can't disambiguate: two hits is as useless as zero.
    records = [
        _rec("b1", "bothmatch", LAND_METHOD, 30.0),
        _rec("b1", "bothmatch", LAND_METHOD, -10.0),
    ]
    simapro_index = SimaproLandIndex(primary={"bothmatch": [30.0, -10.0]})
    resolution = resolve_global_duplicates(records, simapro_index)
    assert resolution.report[0]["rule"] == "larger"
    assert resolution.kept[(LAND_METHOD, "b1")] == 30.0


def test_land_ambiguous_partner_arbiter_absent_falls_back_to_larger(warnings):
    records = [
        _rec("q_from", "from plot", LAND_METHOD, -30.0),
        _rec("q_from", "from plot", LAND_METHOD, -20.0),
        _rec("q_to", "to plot", LAND_METHOD, 30.0),
        _rec("q_to", "to plot", LAND_METHOD, 20.0),
    ]
    resolution = resolve_global_duplicates(records, None)
    assert resolution.kept[(LAND_METHOD, "q_from")] == -30.0
    assert resolution.kept[(LAND_METHOD, "q_to")] == 30.0
    assert all(r["rule"] == "larger" for r in resolution.report)
    assert any("from plot" in m and "undecided" in m for m in warnings)
    assert any("to plot" in m and "undecided" in m for m in warnings)


def test_land_ambiguous_partner_present_but_no_matching_arbiter_entry():
    records = [
        _rec("r_from", "from ridge", LAND_METHOD, -12.0),
        _rec("r_from", "from ridge", LAND_METHOD, -8.0),
        _rec("r_to", "to ridge", LAND_METHOD, 12.0),
        _rec("r_to", "to ridge", LAND_METHOD, 8.0),
    ]
    simapro_index = SimaproLandIndex(primary={"from somewhere else": [-99.0]})
    resolution = resolve_global_duplicates(records, simapro_index)
    entry = next(r for r in resolution.report if r["flow_name"] == "from ridge")
    assert entry["rule"] == "larger"
    assert entry["kept"] == -12.0


def test_land_no_partner_occupation_flow_uses_primary_arbiter():
    records = [
        _rec("m1", "meadow", LAND_METHOD, 30.0),
        _rec("m1", "meadow", LAND_METHOD, -10.0),
    ]
    simapro_index = SimaproLandIndex(primary={"meadow": [30.0]})
    resolution = resolve_global_duplicates(records, simapro_index)
    assert resolution.kept[(LAND_METHOD, "m1")] == 30.0
    assert resolution.report[0]["rule"] == "simapro"


def test_land_no_partner_occupation_flow_uses_fallback_tier_unguarded_alias():
    # Primary has nothing for "pasture"; the fallback tier's unguarded (alias-style,
    # donor=None) entry resolves it instead.
    records = [
        _rec("m2", "pasture", LAND_METHOD, 30.0),
        _rec("m2", "pasture", LAND_METHOD, -10.0),
    ]
    simapro_index = SimaproLandIndex(fallback={"pasture": [(30.0, None)]})
    resolution = resolve_global_duplicates(records, simapro_index)
    assert resolution.kept[(LAND_METHOD, "m2")] == 30.0
    assert resolution.report[0]["rule"] == "simapro-fallback"


def test_land_fallback_entry_guarded_when_donor_is_a_distinct_jrc_flow():
    # "forest" would borrow "forest, natural"'s SimaPro evidence, but "forest, natural"
    # is itself a distinct global Land use flow -- the guard must reject it, leaving
    # only the (unguarded) "forest, unspecified"-derived entry.
    records = [
        _rec("forest_uuid", "forest", LAND_METHOD, 21.463),
        _rec("forest_uuid", "forest", LAND_METHOD, 7.5969),
        _rec("natural_uuid", "forest, natural", LAND_METHOD, 11.459),
        _rec("natural_uuid", "forest, natural", LAND_METHOD, -2.4888),
    ]
    simapro_index = SimaproLandIndex(
        fallback={
            "forest": [
                (11.459, "forest, natural"),  # guarded: donor is a distinct JRC flow
                (21.463, "forest, unspecified"),  # unguarded: no such JRC flow exists
            ]
        }
    )
    resolution = resolve_global_duplicates(records, simapro_index)
    forest_entry = next(r for r in resolution.report if r["flow_name"] == "forest")
    assert forest_entry["kept"] == 21.463
    assert forest_entry["rule"] == "simapro-fallback"


def test_land_fallback_tier_zero_or_two_hits_falls_back_to_larger():
    records = [
        _rec("f1", "fallbacknomatch", LAND_METHOD, 30.0),
        _rec("f1", "fallbacknomatch", LAND_METHOD, -10.0),
    ]
    simapro_index = SimaproLandIndex(fallback={"fallbacknomatch": [(999.0, None)]})
    resolution = resolve_global_duplicates(records, simapro_index)
    assert resolution.report[0]["rule"] == "larger"

    records_both = [
        _rec("f2", "fallbackbothmatch", LAND_METHOD, 30.0),
        _rec("f2", "fallbackbothmatch", LAND_METHOD, -10.0),
    ]
    simapro_index_both = SimaproLandIndex(
        fallback={"fallbackbothmatch": [(30.0, None), (-10.0, None)]}
    )
    resolution_both = resolve_global_duplicates(records_both, simapro_index_both)
    assert resolution_both.report[0]["rule"] == "larger"


def test_land_no_partner_occupation_flow_no_arbiter_falls_back_to_larger():
    records = [
        _rec("m3", "pasture", LAND_METHOD, 30.0),
        _rec("m3", "pasture", LAND_METHOD, -10.0),
    ]
    resolution = resolve_global_duplicates(records, None)
    assert resolution.kept[(LAND_METHOD, "m3")] == 30.0
    assert resolution.report[0]["rule"] == "larger"


def test_unhandled_method_duplicate_falls_back_to_larger_and_logs(warnings):
    records = [
        _rec("x1", "Some flow", "Ozone depletion", 5.0),
        _rec("x1", "Some flow", "Ozone depletion", -9.0),
    ]
    resolution = resolve_global_duplicates(records, None)
    assert resolution.kept[("Ozone depletion", "x1")] == -9.0
    assert resolution.report[0]["rule"] == "larger"
    assert any("unhandled" in m and "Some flow" in m for m in warnings)


def test_country_level_rows_are_ignored_in_grouping():
    records = [
        _rec("c1", "Water, FR", WATER_METHOD, 1.0, location="FR"),
        _rec("c1", "Water, FR", WATER_METHOD, 2.0, location="FR"),
    ]
    resolution = resolve_global_duplicates(records, None)
    assert resolution == DuplicateResolution()


def test_records_missing_required_fields_are_skipped():
    records = [
        {"flow_uuid": None, "flow_name": "x", "method_name": WATER_METHOD, "cf": 1.0},
        {"flow_uuid": "u", "flow_name": "x", "method_name": "", "cf": 1.0},
        {"flow_uuid": "u", "flow_name": "x", "method_name": WATER_METHOD, "cf": None},
    ]
    resolution = resolve_global_duplicates(records, None)
    assert resolution == DuplicateResolution()


# --- tolerance / equality primitives ---------------------------------------------


def test_is_close_exact_and_within_and_outside_tolerance():
    assert _is_close(21.463, 21.463)
    assert _is_close(21.463, 21.463 + 1e-11)
    assert not _is_close(21.463, 21.463 + 1e-3)


def test_arbiter_hits_filters_to_equal_candidates_only():
    assert _arbiter_hits([21.463, 7.5969], [21.463]) == [21.463]
    assert _arbiter_hits([21.463, 7.5969], [999.0]) == []
    assert _arbiter_hits([21.463, 7.5969], [21.463, 7.5969]) == [21.463, 7.5969]


# --- SimaPro name normalization: the individual building blocks -----------------


def test_strip_simapro_prefix_all_three_forms_and_passthrough():
    assert _strip_simapro_prefix("Occupation, meadow") == "meadow"
    assert _strip_simapro_prefix("Transformation, from parcel") == "from parcel"
    assert _strip_simapro_prefix("Transformation, to parcel") == "to parcel"
    assert _strip_simapro_prefix("Ammonia") == "Ammonia"


def test_is_region_variant_iso2_and_named_region_and_no_match():
    assert _is_region_variant("annual crop, AD")
    assert _is_region_variant("annual crop, RER")
    assert _is_region_variant("annual crop, UN-OCEANIA")
    assert not _is_region_variant("annual crop, non-irrigated")
    assert not _is_region_variant("urban, discontinuously built")


def test_split_direction_from_to_and_bare():
    assert _split_direction("from arable") == ("from ", "arable")
    assert _split_direction("to arable") == ("to ", "arable")
    assert _split_direction("arable") == ("", "arable")


def test_apply_prefix_alias_matches_and_passes_through():
    assert _apply_prefix_alias(["pasture", "man made", "extensive"]) == [
        "pasture/meadow",
        "extensive",
    ]
    assert _apply_prefix_alias(["arable", "irrigated"]) == ["arable", "irrigated"]


def test_pluralize_variants_single_word_and_skips_already_plural():
    assert _pluralize_variants("permanent crop") == {"permanent crops"}
    assert _pluralize_variants("wetlands") == set()  # already plural: nothing to add


def test_pluralize_variants_each_slash_part_independently():
    variants = _pluralize_variants("field margin/hedgerow")
    assert "field margins/hedgerow" in variants
    assert "field margin/hedgerows" in variants


def test_expand_pluralizations_reaches_a_both_sides_plural_fixed_point():
    # Neither single-toggle variant matches; only pluralizing BOTH sides does.
    expanded = _expand_pluralizations({"field margin/hedgerow"})
    assert "field margins/hedgerows" in expanded
    assert "field margin/hedgerow" in expanded  # the original is kept too


def test_expand_pluralizations_stable_input_returns_it_unchanged():
    assert _expand_pluralizations({"wetlands"}) == {"wetlands"}


def test_land_index_keys_field_margins_hedgerows_needs_both_sides_pluralized():
    primary, _fallback = _land_index_keys("field margin/hedgerow")
    assert "field margins/hedgerows" in primary


def test_land_index_keys_annual_crop_synonym_and_pluralization():
    primary, fallback = _land_index_keys("annual crop, non-irrigated")
    assert "arable, non-irrigated" in primary
    assert fallback == {}


def test_land_index_keys_permanent_crop_pluralized():
    primary, _fallback = _land_index_keys("permanent crop, irrigated, extensive")
    assert "permanent crops, irrigated, extensive" in primary


def test_land_index_keys_non_use_suffix_stripped():
    primary, _fallback = _land_index_keys("bare area (non-use)")
    assert "bare area" in primary


def test_land_index_keys_pasture_man_made_prefix_alias():
    primary, _fallback = _land_index_keys("pasture, man made, extensive")
    assert "pasture/meadow, extensive" in primary


def test_land_index_keys_direction_preserved():
    primary, _fallback = _land_index_keys("from annual crop, irrigated")
    assert "from arable, irrigated" in primary


def test_land_index_keys_lowercased():
    primary, _fallback = _land_index_keys("Occupation-less Mixed Case, Segment")
    assert all(key == key.lower() for key in primary)


def test_land_index_keys_natural_segment_dropped_only_as_fallback_with_donor():
    primary, fallback = _land_index_keys("grassland, natural (non-use)")
    # The as-carried (non-use-stripped) name is the primary candidate...
    assert "grassland, natural" in primary
    # ...and the filler-dropped form is fallback-only, guarded by its donor.
    assert "grassland" not in primary
    assert fallback["grassland"] == "grassland, natural"


def test_land_index_keys_unspecified_segment_dropped_as_fallback():
    # The new "X, unspecified" -> "X" normalization: "forest, unspecified" is
    # SimaPro's spelling for EF's bare "forest".
    primary, fallback = _land_index_keys("forest, unspecified")
    assert "forest, unspecified" in primary
    assert fallback["forest"] == "forest, unspecified"


def test_land_index_keys_natural_segment_not_dropped_when_it_is_the_whole_class():
    # EF *does* have a bare "unspecified, natural" class: nothing to drop here since
    # there's no additional distinguishing segment, but this exercises the same path
    # as the grassland case with a 2-segment name.
    primary, fallback = _land_index_keys("unspecified, natural (non-use)")
    assert "unspecified, natural" in primary
    assert fallback["unspecified"] == "unspecified, natural"


def test_land_index_keys_sclerophyllous_dropped_as_fallback():
    primary, fallback = _land_index_keys("shrub land, sclerophyllous")
    assert "shrub land, sclerophyllous" in primary
    assert fallback["shrub land"] == "shrub land, sclerophyllous"


def test_land_index_keys_all_segments_droppable_yields_no_empty_fallback_key():
    # Degenerate case: nothing survives the drop, so no fallback key is produced.
    _primary, fallback = _land_index_keys("natural, sclerophyllous")
    assert "" not in fallback


def test_land_index_keys_grassland_not_used_fallback_alias_is_unguarded_and_direction_agnostic():
    _primary, fallback = _land_index_keys("grassland")
    assert fallback["grassland, not used"] is None
    # Applied after the direction prefix is split off, so it carries through the
    # same way for "from"/"to" names -- EF's own "from grassland, not used" has no
    # dedicated SimaPro row at all (only "to" does).
    _primary_from, fallback_from = _land_index_keys("from grassland")
    assert fallback_from["from grassland, not used"] is None


def test_land_index_keys_empty_and_blank_inputs_produce_nothing():
    assert _land_index_keys("") == (set(), {})
    assert _land_index_keys(",") == (set(), {})


def test_simapro_land_index_primary_then_fallback_guard_then_none():
    index = SimaproLandIndex(
        primary={"a": [1.0]},
        fallback={"a": [(2.0, None)], "b": [(3.0, None)], "c": [(4.0, "donor")]},
    )
    assert index.primary_values("a") == [1.0]
    assert index.primary_values("b") is None
    assert index.fallback_values("b", frozenset()) == [3.0]
    assert index.fallback_values("c", frozenset({"donor"})) is None
    assert index.fallback_values("c", frozenset()) == [4.0]
    assert index.fallback_values("missing", frozenset()) is None


def test_parse_simapro_index_returns_none_without_raw():
    assert parse_simapro_index(None) is None


def test_parse_simapro_index_returns_none_and_warns_on_malformed_content(warnings):
    raw = RawData(content=b"not a parquet file", source_url="file://bad.parquet")
    assert parse_simapro_index(raw) is None
    assert any("could not be parsed" in m for m in warnings)


def _simapro_table(path: Path, rows: list[tuple[str, str, float]]) -> RawData:
    table = pa.table(
        {
            "simapro_method": pa.array([r[0] for r in rows], pa.string()),
            "name": pa.array([r[1] for r in rows], pa.string()),
            "cf": pa.array([r[2] for r in rows], pa.float64()),
        }
    )
    pq.write_table(table, path)
    return RawData(content=path.read_bytes(), source_url=f"file://{path}")


def test_parse_simapro_index_end_to_end_normalization_and_filtering(tmp_path):
    raw = _simapro_table(
        tmp_path / "simapro.parquet",
        [
            ("Land use", "Occupation, annual crop, non-irrigated", 50.191),
            ("Land use", "Occupation, permanent crop", 50.191),
            ("Land use", "Occupation, pasture, man made", 35.641),
            ("Land use", "Occupation, grassland, natural (non-use)", 35.641),
            ("Land use", "Occupation, shrub land, sclerophyllous", 17.602),
            ("Land use", "Occupation, forest, unspecified", 21.463),
            ("Land use", "Occupation, annual crop, AD", 999.0),  # region variant, skipped
            ("Acidification", "Ammonia", 3.02),  # non-land, skipped
        ],
    )
    index = parse_simapro_index(raw)
    assert isinstance(index, SimaproLandIndex)
    assert index.primary_values("arable, non-irrigated") == [50.191]
    assert index.primary_values("permanent crops") == [50.191]
    assert index.primary_values("pasture/meadow") == [35.641]
    assert index.fallback_values("grassland", frozenset()) == [35.641]
    assert index.fallback_values("shrub land", frozenset()) == [17.602]
    assert index.fallback_values("forest", frozenset()) == [21.463]
    # Guarding "forest" against a hypothetical distinct "forest, unspecified" JRC flow
    # (there isn't one in the real table, but the mechanism must still honor the guard).
    assert index.fallback_values("forest", frozenset({"forest, unspecified"})) is None
    assert index.primary_values("annual crop, ad") is None
    assert index.primary_values("ammonia") is None


@pytest.mark.parametrize(
    "value,expected",
    [
        (-42.955, -42.95),
        (42.95, 42.95),
        (-42.95, -42.95),
        (42.949, 42.95),
        (37.8, 37.8),
        (-37.8, -37.8),
        (0.0, 0.0),
    ],
)
def test_harmonise_water_family_snaps_only_the_rounding_artefact(value, expected):
    assert harmonise_water_family(value) == expected
