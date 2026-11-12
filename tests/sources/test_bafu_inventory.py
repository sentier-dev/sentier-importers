"""Tests for the BAFU-2026 sentier-inventory sources (processes + exchanges)."""

import math

import pytest
from sentier_importers.core.registry import load_registry
from sentier_importers.core.source import SourceConfig
from sentier_importers.sources.bafu import ecospold
from sentier_importers.sources.bafu.inventory_exchanges import BafuInventoryExchangesSource
from sentier_importers.sources.bafu.inventory_processes import BafuInventoryProcessesSource

from tests.sources.bafu_fixture import UUID_ELEC, UUID_GAS, UUID_OBSOLETE, fixture_zip

#: Every fixture dataset with the sector its BAFU category routes to.
FIXTURE_SECTORS = {UUID_ELEC: "electricity", UUID_GAS: "energy", UUID_OBSOLETE: "obsolete"}


def _cfg(name, module):
    return SourceConfig(
        name=name,
        module=f"sentier_importers.sources.bafu.{module}",
        target="sentier_inventory",
        category=ecospold.SOURCE_FOLDER,
        fetch_url="unused://",
        fetch_format="zip",
        output_format="parquet",
        emit_filename=name.rsplit("-", 1)[-1],
    )


def _processes():
    src = BafuInventoryProcessesSource(_cfg("bafu-processes", "inventory_processes"))
    return src.transform(src.parse(fixture_zip()))


def _exchanges():
    src = BafuInventoryExchangesSource(_cfg("bafu-exchanges", "inventory_exchanges"))
    return src.transform(src.parse(fixture_zip()))


def _exchanges_by_name(process_id=UUID_ELEC):
    return {r["flow_name"]: r for r in _exchanges() if r["process_id"] == process_id}


def _process(uuid):
    (row,) = [r for r in _processes() if r["process_id"] == uuid]
    return row


def test_processes_emits_every_dataset_once():
    # one source folder holds the whole release: no per-sector filtering any more
    assert [r["process_id"] for r in _processes()] == list(FIXTURE_SECTORS)


@pytest.mark.parametrize("uuid,sector", sorted(FIXTURE_SECTORS.items()))
def test_processes_sector_column_from_bafu_category(uuid, sector):
    assert _process(uuid)["sector"] == sector


def test_processes_sector_column_sits_after_process_type():
    # column order is the sentier-inventory schema/process.yaml order
    columns = list(_process(UUID_ELEC))
    assert columns[columns.index("process_type") + 1] == "sector"
    assert columns[: columns.index("technology")] == [
        "process_id",
        "name",
        "reference_product",
        "reference_unit",
        "reference_amount",
        "location",
        "process_type",
        "sector",
        "source",
        "source_version",
    ]


def test_processes_declares_mapping_for_whole_source_release():
    source = BafuInventoryProcessesSource(_cfg("bafu-processes", "inventory_processes"))
    assert [step.name for step in source.declared_transform_steps()] == [
        "map-process-rows",
    ]
    records = source.parse(fixture_zip())
    rows = records
    for step in source.declared_transform_steps():
        rows = step.function(rows)
    assert rows == source.transform(records)
    assert {row["process_id"] for row in rows} == set(FIXTURE_SECTORS)


def test_processes_row_matches_inventory_schema():
    row = _process(UUID_ELEC)
    assert row["name"] == "Electricity, at test plant"
    assert row["reference_product"] == "Electricity, at test plant"
    assert row["reference_unit"] == "kWh"
    assert row["reference_amount"] == 1.0
    assert row["location"] == "CH"
    assert row["process_type"] == "unit"
    assert row["technology"] == "Combined cycle test turbine"
    assert "electricity / production mix" in row["comment"]
    assert "BAFU:2026" in row["comment"]  # source citation travels with the data
    assert row["source"] == "bafu-2026"  # machine-readable tag, consumers filter on it
    assert row["source_version"] == "v1"


def test_processes_source_id_is_edition_not_version():
    # ``source`` names the edition only; a v1 -> v2 re-release must not rename the
    # database a consumer's project depends on. The release lives in source_version.
    row = _process(UUID_ELEC)
    assert "v1" not in row["source"]
    assert row["source"] == ecospold.SOURCE_ID
    assert row["source_version"] == ecospold.SOURCE_VERSION


def test_processes_obsolete_sector_flags_comment():
    row = _process(UUID_OBSOLETE)
    assert row["sector"] == "obsolete"
    assert "obsolete" in row["comment"]


def test_exchanges_emits_every_dataset_and_carries_no_sector():
    rows = _exchanges()
    assert {r["process_id"] for r in rows} == set(FIXTURE_SECTORS)
    assert len([r for r in rows if r["process_id"] == UUID_ELEC]) == 10
    assert all("sector" not in r for r in rows)  # sector lives on processes only


def test_exchanges_flow_ids_and_types():
    by_name = _exchanges_by_name()

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
        "Carbon dioxide, fossil", "emissions to air", "unspecified", "kg"
    )

    water = by_name["Water, river"]
    assert (water["flow_type"], water["direction"]) == ("biosphere", "input")
    assert water["amount"] == 0.01


def test_exchanges_uncertainty_mapping():
    by_name = _exchanges_by_name()

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
    bafu = [c for c in load_registry() if c.name.startswith("bafu")]
    # 2 inventory tables (one source folder) + the vocab release (source term, process
    # terms, flow terms) + the curated crosswalk
    # (biosphere-1-curated) + the public-matching crosswalk (biosphere-3-matched)
    # + the coverage sidecar + the nomenclature crosswalk (biosphere-4-nomenclature)
    assert len(bafu) == 9
    assert all(not c.enabled for c in bafu)  # opt-in: run locally, no auto delivery

    mappings = [c for c in bafu if c.target == "sentier_mappings"]
    assert [c.name for c in mappings] == [
        "bafu-ef-biosphere",
        "bafu-ef-biosphere-matched",
        "bafu-ef-coverage",
        "bafu-ef-biosphere-nomenclature",
    ]
    # the pair folder contract names the file, not the source
    assert {m.emit_filename for m in mappings} == {
        "biosphere-1-curated",
        "biosphere-3-matched",
        "coverage",
        "biosphere-4-nomenclature",
    }

    # the inventory folder is the source release: data/bafu-2026-v1/{processes,exchanges}
    inventory = {c.name: c for c in bafu if c.target == "sentier_inventory"}
    assert list(inventory) == ["bafu-processes", "bafu-exchanges"]
    assert {c.category for c in inventory.values()} == {ecospold.SOURCE_FOLDER}
    assert [c.emit_filename for c in inventory.values()] == ["processes", "exchanges"]
    assert {c.access for c in inventory.values()} == {"licensed"}

    # the vocab release is one source term plus one source per term type
    vocab = [c for c in bafu if c.target == "sentier_vocab"]
    assert {c.category for c in vocab} == {ecospold.SOURCE_ID}
    assert {c.name: c.term_type for c in vocab} == {
        "bafu-2026-source": "source",
        "bafu-vocab-processes": "process",
        "bafu-vocab-flows": "elementary-flow",
    }
    assert all(c.emit_filename is None for c in vocab)


def test_coverage_sidecar_describes_the_same_computation_as_the_matched_payload():
    # the coverage sidecar must never drift from what bafu-ef-biosphere-matched actually
    # ran: same primary fetch, same named inputs, so a reader can trust coverage.json as
    # an account of that payload rather than of some other run. Unlike the nomenclature
    # source, the coverage sidecar recomputes the matched package itself (two-pass) and
    # so carries no ``matched`` input at all.
    bafu = {c.name: c for c in load_registry() if c.name.startswith("bafu")}
    matched = bafu["bafu-ef-biosphere-matched"]
    coverage = bafu["bafu-ef-coverage"]
    assert coverage.fetch_url == matched.fetch_url
    assert coverage.fetch_format == matched.fetch_format
    assert coverage.inputs == matched.inputs
    assert "matched" not in coverage.inputs


def test_nomenclature_source_shares_the_matched_source_inputs_plus_matched():
    # the nomenclature package must run over exactly the same BAFU universe and EF
    # public inputs as the matched package (fetch, ef_cfs, ef_vocab, curated, inferred)
    # -- only the extra ``matched`` input (the nomenclature package's own exclusion
    # set) sets it apart.
    bafu = {c.name: c for c in load_registry() if c.name.startswith("bafu")}
    matched = bafu["bafu-ef-biosphere-matched"]
    nomenclature = bafu["bafu-ef-biosphere-nomenclature"]
    assert nomenclature.fetch_url == matched.fetch_url
    assert nomenclature.fetch_format == matched.fetch_format
    for name in ("ef_cfs", "ef_vocab", "curated", "inferred"):
        assert nomenclature.inputs[name] == matched.inputs[name]
    assert "matched" in nomenclature.inputs and "matched" not in matched.inputs
