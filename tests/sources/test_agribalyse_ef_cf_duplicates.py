"""Integration tests for the EF 3.1 CF import's global-duplicate handling.

Covers the ``AgribalyseEfCfsSource`` end to end (parquet in, rows out): the water and
land resolution rules wired through ``transform``, the SimaPro arbiter fetched as a
named input, graceful fallback when that file is absent, and a real-data regression
check against the actual JRC/SimaPro files when present locally.
"""

import io
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from sentier_importers.core.context import RunContext
from sentier_importers.core.source import SourceConfig
from sentier_importers.core.types import RawData
from sentier_importers.sources.agribalyse.cfs import AgribalyseEfCfsSource
from sentier_importers.sources.agribalyse.ef_cf_dedup import (
    LAND_METHOD,
    SIMAPRO_INPUT,
    WATER_METHOD,
    _is_close,
    parse_simapro_index,
    resolve_global_duplicates,
)
from sentier_importers.sources.agribalyse.ef_common import parse_cf_table

_JRC_PATH = Path(
    "/home/laurenz/dds/dds-agribalyse/source/EF-LCIAMethod_CF(EF-v3.1)__lciamethods_CF.parquet"
)
_SIMAPRO_PATH = Path("/home/laurenz/dds/dds-agribalyse/source/simapro-EF31-adapted-cfs.parquet")


def _cf_raw(rows: list[dict]) -> RawData:
    """Build a JRC-shaped CF parquet from ``rows`` of
    ``{uuid, name, method, cf, location=None, class0=None, class1=None, class2=None}``.
    """
    cols = {
        "FLOW_uuid": [r["uuid"] for r in rows],
        "FLOW_name": [r["name"] for r in rows],
        "LCIAMethod_name": [r["method"] for r in rows],
        "CF EF3.1": [r["cf"] for r in rows],
        "LCIAMethod_location": [r.get("location") for r in rows],
        "FLOW_class0": [r.get("class0") for r in rows],
        "FLOW_class1": [r.get("class1") for r in rows],
        "FLOW_class2": [r.get("class2") for r in rows],
    }
    table = pa.table(
        {
            "FLOW_uuid": pa.array(cols["FLOW_uuid"], pa.string()),
            "FLOW_name": pa.array(cols["FLOW_name"], pa.string()),
            "LCIAMethod_name": pa.array(cols["LCIAMethod_name"], pa.string()),
            "CF EF3.1": pa.array(cols["CF EF3.1"], pa.float64()),
            "LCIAMethod_location": pa.array(cols["LCIAMethod_location"], pa.string()),
            "FLOW_class0": pa.array(cols["FLOW_class0"], pa.string()),
            "FLOW_class1": pa.array(cols["FLOW_class1"], pa.string()),
            "FLOW_class2": pa.array(cols["FLOW_class2"], pa.string()),
        }
    )
    buf = io.BytesIO()
    pq.write_table(table, buf)
    return RawData(content=buf.getvalue(), source_url="file://cf.parquet")


def _simapro_raw(rows: list[tuple[str, str, float]]) -> RawData:
    """Build a SimaPro-shaped export from ``(simapro_method, name, cf)`` rows."""
    table = pa.table(
        {
            "simapro_method": pa.array([r[0] for r in rows], pa.string()),
            "name": pa.array([r[1] for r in rows], pa.string()),
            "cf": pa.array([r[2] for r in rows], pa.float64()),
        }
    )
    buf = io.BytesIO()
    pq.write_table(table, buf)
    return RawData(content=buf.getvalue(), source_url="file://simapro.parquet")


def _cfg(**overrides) -> SourceConfig:
    base = dict(
        name="characterization-factors",
        module="sentier_importers.sources.agribalyse.cfs",
        target="sentier_methods",
        category="01-ef-3.1",
        fetch_url="unused://",
        fetch_format="parquet",
        output_format="parquet",
        inputs={},
    )
    base.update(overrides)
    return SourceConfig(**base)


def test_water_and_land_duplicates_resolved_end_to_end():
    raw = _cf_raw(
        [
            # Water: two global rows, keep the 42.95-family value.
            {"uuid": "w1", "name": "Water to Cooling", "method": WATER_METHOD, "cf": 42.95},
            {"uuid": "w1", "name": "Water to Cooling", "method": WATER_METHOD, "cf": 37.8},
            # Same flow, country-level: untouched regardless of the global resolution.
            {
                "uuid": "w1",
                "name": "Water to Cooling",
                "method": WATER_METHOD,
                "cf": 10.0,
                "location": "FR",
            },
            # Land: ambiguous from/to pair, resolved via the SimaPro arbiter.
            {"uuid": "p_from", "name": "from parcel", "method": LAND_METHOD, "cf": -100.0},
            {"uuid": "p_from", "name": "from parcel", "method": LAND_METHOD, "cf": -50.0},
            {"uuid": "p_to", "name": "to parcel", "method": LAND_METHOD, "cf": 100.0},
            {"uuid": "p_to", "name": "to parcel", "method": LAND_METHOD, "cf": 50.0},
            # Land: no from/to partner and no arbiter match -- larger |value| kept.
            {"uuid": "m1", "name": "meadow", "method": LAND_METHOD, "cf": 30.0},
            {"uuid": "m1", "name": "meadow", "method": LAND_METHOD, "cf": -10.0},
            # Untouched: single global value, repeated identically (pre-existing dedup).
            {"uuid": "co2", "name": "Carbon dioxide", "method": "Climate change", "cf": 1.0},
            {"uuid": "co2", "name": "Carbon dioxide", "method": "Climate change", "cf": 1.0},
        ]
    )
    simapro = _simapro_raw(
        [
            ("Land use", "Transformation, from parcel", -100.0),
            ("Land use", "Transformation, to parcel", 50.0),
        ]
    )
    src = AgribalyseEfCfsSource(_cfg())
    src.inputs[SIMAPRO_INPUT] = simapro
    rows = src.transform(src.parse(raw))

    by_flow_and_location = {(r["flow_name"], r.get("location")): r for r in rows}

    water_global = by_flow_and_location[("Water to Cooling", None)]
    assert water_global["factor_value"] == 42.95
    water_fr = by_flow_and_location[("Water to Cooling", "FR")]
    assert water_fr["factor_value"] == 10.0
    assert water_fr["location"] == "FR"

    assert by_flow_and_location[("from parcel", None)]["factor_value"] == -100.0
    assert by_flow_and_location[("to parcel", None)]["factor_value"] == 50.0
    assert by_flow_and_location[("meadow", None)]["factor_value"] == 30.0
    assert by_flow_and_location[("Carbon dioxide", None)]["factor_value"] == 1.0

    # Exactly one row per (flow, location) -- no leftover duplicate rows.
    assert len(rows) == len(by_flow_and_location)


def test_arbiter_value_matching_neither_or_both_candidates_falls_back_to_larger():
    raw = _cf_raw(
        [
            {"uuid": "n1", "name": "nomatch", "method": LAND_METHOD, "cf": 30.0},
            {"uuid": "n1", "name": "nomatch", "method": LAND_METHOD, "cf": -10.0},
            {"uuid": "b1", "name": "bothmatch", "method": LAND_METHOD, "cf": 30.0},
            {"uuid": "b1", "name": "bothmatch", "method": LAND_METHOD, "cf": -10.0},
        ]
    )
    simapro = _simapro_raw(
        [
            ("Land use", "Occupation, nomatch", 999.0),
            ("Land use", "Occupation, bothmatch", 30.0),
            ("Land use", "Occupation, bothmatch", -10.0),
        ]
    )
    src = AgribalyseEfCfsSource(_cfg())
    src.inputs[SIMAPRO_INPUT] = simapro
    rows = src.transform(src.parse(raw))
    by_flow = {r["flow_name"]: r for r in rows}
    # Both fall back to larger |value| -- neither hit disambiguates.
    assert by_flow["nomatch"]["factor_value"] == 30.0
    assert by_flow["bothmatch"]["factor_value"] == 30.0


def test_forest_resolves_via_simapro_fallback_not_the_wrong_natural_donor():
    # "forest" must never borrow "forest, natural"'s SimaPro evidence (11.459, giving
    # the wrong 7.5969): "forest, natural" is itself a distinct global Land use flow.
    # It should instead resolve via "forest, unspecified" (21.463, exact).
    raw = _cf_raw(
        [
            {"uuid": "forest", "name": "forest", "method": LAND_METHOD, "cf": 21.463},
            {"uuid": "forest", "name": "forest", "method": LAND_METHOD, "cf": 7.5969},
            {"uuid": "forest_nat", "name": "forest, natural", "method": LAND_METHOD, "cf": 11.459},
            {
                "uuid": "forest_nat",
                "name": "forest, natural",
                "method": LAND_METHOD,
                "cf": -2.4888,
            },
        ]
    )
    simapro = _simapro_raw(
        [
            ("Land use", "Occupation, forest, natural", 11.459),
            ("Land use", "Occupation, forest, unspecified", 21.463),
        ]
    )
    src = AgribalyseEfCfsSource(_cfg())
    src.inputs[SIMAPRO_INPUT] = simapro
    rows = src.transform(src.parse(raw))
    by_flow = {r["flow_name"]: r for r in rows}
    assert by_flow["forest"]["factor_value"] == 21.463


def test_fetch_falls_back_when_simapro_arbiter_file_is_missing(tmp_path):
    cf_path = tmp_path / "cf.parquet"
    raw = _cf_raw([{"uuid": "u1", "name": "CO2", "method": "Climate change", "cf": 1.0}])
    cf_path.write_bytes(raw.content)

    config = _cfg(
        fetch_url=f"file://{cf_path}",
        inputs={"simapro": "file:///no/such/simapro-arbiter.parquet"},
    )
    src = AgribalyseEfCfsSource(config)
    ctx = RunContext(cache_dir=tmp_path / "cache", output_dir=tmp_path / "out")

    fetched = src.fetch(ctx)  # must not raise despite the missing arbiter file
    assert fetched.content == raw.content
    assert src.inputs.get("simapro") is None

    rows = src.transform(src.parse(fetched))
    assert rows == [
        {
            "method_id": rows[0]["method_id"],
            "impact_category": "Climate change",
            "flow": rows[0]["flow"],
            "flow_name": "CO2",
            "factor_value": 1.0,
            "unit": rows[0]["unit"],
        }
    ]


def test_transform_skips_records_missing_required_fields():
    src = AgribalyseEfCfsSource(_cfg())
    records = [
        {"flow_uuid": None, "flow_name": "x", "method_name": "Climate change", "cf": 1.0},
        {"flow_uuid": "u", "flow_name": "x", "method_name": "", "cf": 1.0},
        {"flow_uuid": "u", "flow_name": "x", "method_name": "Climate change", "cf": None},
    ]
    assert src.transform(records) == []


def test_fetch_with_no_simapro_input_configured(tmp_path):
    cf_path = tmp_path / "cf.parquet"
    raw = _cf_raw([{"uuid": "u1", "name": "CO2", "method": "Climate change", "cf": 1.0}])
    cf_path.write_bytes(raw.content)

    config = _cfg(fetch_url=f"file://{cf_path}", inputs={})
    src = AgribalyseEfCfsSource(config)
    ctx = RunContext(cache_dir=tmp_path / "cache", output_dir=tmp_path / "out")

    src.fetch(ctx)
    assert src.inputs.get("simapro") is None


@pytest.mark.skipif(
    not (_JRC_PATH.exists()), reason="dds-agribalyse JRC source file not available locally"
)
def test_real_jrc_data_has_no_remaining_global_duplicates():
    raw = RawData(content=_JRC_PATH.read_bytes(), source_url=f"file://{_JRC_PATH}")
    simapro_raw = (
        RawData(content=_SIMAPRO_PATH.read_bytes(), source_url=f"file://{_SIMAPRO_PATH}")
        if _SIMAPRO_PATH.exists()
        else None
    )
    src = AgribalyseEfCfsSource(_cfg())
    if simapro_raw is not None:
        src.inputs[SIMAPRO_INPUT] = simapro_raw
    records = src.parse(raw)
    rows = src.transform(records)

    groups: dict[tuple[str, str, str | None], set[float]] = {}
    for row in rows:
        key = (row["impact_category"], row["flow"], row.get("location"))
        groups.setdefault(key, set()).add(row["factor_value"])
    assert all(len(values) == 1 for values in groups.values())

    # (impact_category, flow_name, location) -> value, so land and water names never
    # collide even though flow names are not globally unique across methods.
    by_key = {
        (r["impact_category"], r["flow_name"], r.get("location")): r["factor_value"] for r in rows
    }

    from_flows = {
        name
        for (method, name, loc) in by_key
        if loc is None and method == LAND_METHOD and name.startswith("from ")
    }
    checked_pairs = 0
    for name in from_flows:
        to_name = "to " + name[len("from ") :]
        to_key = (LAND_METHOD, to_name, None)
        if to_key in by_key:
            checked_pairs += 1
            assert abs(by_key[(LAND_METHOD, name, None)]) == abs(by_key[to_key])
    assert checked_pairs > 0

    water_checked = 0
    for (method, _name, loc), value in by_key.items():
        if method == WATER_METHOD and loc is None:
            water_checked += 1
            assert round(abs(value), 3) == 42.95
    assert water_checked > 0

    # "forest" must resolve via SimaPro's "forest, unspecified" (21.463), never by
    # borrowing "forest, natural"'s evidence (11.459, which would wrongly give 7.5969).
    assert by_key[(LAND_METHOD, "forest", None)] == 21.463
    assert by_key[(LAND_METHOD, "from forest", None)] == -214.63
    assert by_key[(LAND_METHOD, "to forest", None)] == 214.63


@pytest.mark.skipif(
    not (_JRC_PATH.exists() and _SIMAPRO_PATH.exists()),
    reason="dds-agribalyse JRC or SimaPro source file not available locally",
)
def test_real_jrc_data_every_simapro_decision_matches_arbiter_value_within_tolerance():
    raw = RawData(content=_JRC_PATH.read_bytes(), source_url=f"file://{_JRC_PATH}")
    simapro_raw = RawData(content=_SIMAPRO_PATH.read_bytes(), source_url=f"file://{_SIMAPRO_PATH}")

    records = parse_cf_table(raw)
    simapro_index = parse_simapro_index(simapro_raw)
    resolution = resolve_global_duplicates(records, simapro_index)

    land_names = frozenset(
        rec["flow_name"].strip().lower()
        for rec in records
        if rec.get("method_name") == LAND_METHOD and not rec.get("location")
    )

    simapro_entries = [
        r for r in resolution.report if r["rule"] in ("simapro", "simapro-fallback")
    ]
    assert simapro_entries  # the arbiter must resolve at least some pairs
    for entry in simapro_entries:
        key = entry["flow_name"].strip().lower()
        if entry["rule"] == "simapro":
            sp_values = simapro_index.primary_values(key) or []
        else:
            sp_values = simapro_index.fallback_values(key, land_names) or []
        assert any(_is_close(entry["kept"], sv) for sv in sp_values), entry


def test_transform_harmonises_the_water_return_rounded_to_three_decimals():
    # JRC rounds one 42.95-family member to -42.955; the emitted table carries -42.95 so
    # intakes and returns cancel exactly (decision 2026-09-14). Country rows untouched.
    raw = _cf_raw(
        [
            {"uuid": "wr", "name": "Water", "method": WATER_METHOD, "cf": -42.955},
            {"uuid": "wr", "name": "Water", "method": WATER_METHOD, "cf": -37.8},
            {
                "uuid": "wr",
                "name": "Water",
                "method": WATER_METHOD,
                "cf": -42.955,
                "location": "CH",
            },
        ]
    )
    src = AgribalyseEfCfsSource(_cfg())
    rows = src.transform(src.parse(raw))
    by_location = {r.get("location"): r["factor_value"] for r in rows if r["flow_name"] == "Water"}
    assert by_location == {None: -42.95, "CH": -42.955}
