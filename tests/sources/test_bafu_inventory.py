"""Tests for the BAFU-2026 sentier-inventory sources (processes + exchanges)."""

import math

from sentier_importers.core.source import SourceConfig
from sentier_importers.sources.bafu import ecospold
from sentier_importers.sources.bafu.inventory_exchanges import BafuInventoryExchangesSource
from sentier_importers.sources.bafu.inventory_processes import BafuInventoryProcessesSource

from tests.sources.bafu_fixture import UUID_ELEC, UUID_GAS, UUID_OBSOLETE, fixture_zip


def _cfg(name, category, module):
    return SourceConfig(
        name=name,
        module=f"sentier_importers.sources.bafu.{module}",
        target="sentier_inventory",
        category=category,
        fetch_url="unused://",
        fetch_format="zip",
        output_format="parquet",
        emit_filename=name.rsplit("-", 1)[-1],
    )


def _processes(category="02-electricity"):
    src = BafuInventoryProcessesSource(
        _cfg("bafu-electricity-processes", category, "inventory_processes")
    )
    return src.transform(src.parse(fixture_zip()))


def _exchanges(category="02-electricity"):
    src = BafuInventoryExchangesSource(
        _cfg("bafu-electricity-exchanges", category, "inventory_exchanges")
    )
    return src.transform(src.parse(fixture_zip()))


def test_processes_filters_to_sector():
    rows = _processes()
    assert [r["process_id"] for r in rows] == [UUID_ELEC]


def test_processes_row_matches_inventory_schema():
    (row,) = _processes()
    assert row["name"] == "Electricity, at test plant"
    assert row["reference_product"] == "Electricity, at test plant"
    assert row["reference_unit"] == "kWh"
    assert row["reference_amount"] == 1.0
    assert row["location"] == "CH"
    assert row["process_type"] == "unit"
    assert row["technology"] == "Combined cycle test turbine"
    assert "electricity / production mix" in row["comment"]
    assert "BAFU:2026" in row["comment"]  # source citation travels with the data


def test_processes_obsolete_sector_flags_comment():
    (row,) = _processes(category="99-obsolete")
    assert row["process_id"] == UUID_OBSOLETE
    assert "obsolete" in row["comment"]


def test_exchanges_filters_to_sector_processes():
    rows = _exchanges()
    assert {r["process_id"] for r in rows} == {UUID_ELEC}
    assert len(rows) == 4


def test_exchanges_flow_ids_and_types():
    rows = _exchanges()
    by_name = {r["flow_name"]: r for r in rows}

    prod = by_name["Electricity, at test plant"]
    assert (prod["flow_type"], prod["direction"]) == ("production", "output")
    assert prod["flow"] == UUID_ELEC  # a process's production flow is itself

    tech = by_name["Natural gas, test supply"]
    assert (tech["flow_type"], tech["direction"]) == ("technosphere", "input")
    assert tech["flow"] == UUID_GAS  # resolved through the export-wide number->uuid map
    assert tech["unit"] == "MJ"
    assert tech["location"] == "CH"

    co2 = by_name["Carbon dioxide, fossil"]
    assert (co2["flow_type"], co2["direction"]) == ("biosphere", "output")
    assert co2["flow"] == ecospold.flow_id(
        "Carbon dioxide, fossil", "emissions to air", "unspecified"
    )

    water = by_name["Water, river"]
    assert (water["flow_type"], water["direction"]) == ("biosphere", "input")
    assert water["amount"] == 0.01


def test_exchanges_uncertainty_mapping():
    rows = _exchanges()
    by_name = {r["flow_name"]: r for r in rows}

    tech = by_name["Natural gas, test supply"]  # EcoSpold lognormal -> bw 2
    assert tech["uncertainty_type"] == 2
    assert math.isclose(tech["loc"], math.log(2.0))
    assert math.isclose(tech["scale"], math.log(math.sqrt(1.21)))

    co2 = by_name["Carbon dioxide, fossil"]  # EcoSpold normal -> bw 3
    assert (co2["uncertainty_type"], co2["loc"], co2["scale"]) == (3, 0.5, 0.05)

    prod = by_name["Electricity, at test plant"]  # type 0 -> undefined
    assert prod["uncertainty_type"] is None
    assert prod["minimum"] is None and prod["maximum"] is None


def test_registry_declares_full_bafu_family():
    from sentier_importers.core.registry import load_registry

    bafu = [c for c in load_registry() if c.name.startswith("bafu")]
    # 11 sectors x 2 inventory tables + source record + 11 per-sector process
    # term files + 6 per-compartment flow term files + the EF crosswalk
    assert len(bafu) == 41
    assert all(not c.enabled for c in bafu)  # opt-in: run locally, no auto delivery

    mappings = [c for c in bafu if c.target == "sentier_mappings"]
    assert [c.name for c in mappings] == ["bafu-ef-biosphere"]
    # the bridge folder contract names the file, not the source
    assert mappings[0].emit_filename == "biosphere"

    inventory = [c for c in bafu if c.target == "sentier_inventory"]
    assert len(inventory) == 22
    assert {c.category for c in inventory} == set(ecospold.SECTORS)
    assert {c.emit_filename for c in inventory} == {"processes", "exchanges"}

    vocab = [c for c in bafu if c.target == "sentier_vocab"]
    processes = {c.emit_filename for c in vocab if c.category == "processes"}
    assert processes == {sector.split("-", 1)[1] for sector in ecospold.SECTORS}
    flows = {c.emit_filename for c in vocab if c.category == "elementary-flows"}
    assert flows == {
        "emissions-to-air",
        "emissions-to-water",
        "emissions-to-soil",
        "resources",
        "non-material-emissions",
        "economic-issues",
    }
    (provenance,) = [c for c in vocab if c.category == "sources"]
    assert provenance.emit_filename == "bafu-2026"
    # delivered payload files are content-named, never source-named
    assert all("bafu" not in c.emit_filename for c in vocab if c.category != "sources")
