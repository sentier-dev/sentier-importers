"""Tests for the BAFU-2026 sentier-vocab sources (terms, flows, provenance)."""

from sentier_importers.core.source import SourceConfig
from sentier_importers.sources.bafu import ecospold
from sentier_importers.sources.bafu.provenance import BAFU_PROVENANCE_IRI, BafuProvenance
from sentier_importers.sources.bafu.vocab_flows import BafuVocabFlowsSource
from sentier_importers.sources.bafu.vocab_processes import BafuVocabProcessesSource

from tests.sources.bafu_fixture import UUID_ELEC, UUID_GAS, UUID_OBSOLETE, fixture_zip


def _cfg(name, category, module, emit_filename=None):
    return SourceConfig(
        name=name,
        module=f"sentier_importers.sources.bafu.{module}",
        target="sentier_vocab",
        category=category,
        fetch_url="unused://",
        fetch_format="zip",
        output_format="parquet",
        emit_filename=emit_filename,
    )


def test_provenance_single_source_record_with_citation():
    src = BafuProvenance(_cfg("bafu-2026-source", "sources", "provenance"))
    (row,) = src.transform(src.parse(src.fetch(None)))
    assert row["iri"] == BAFU_PROVENANCE_IRI
    assert "Life Cycle Inventory database of the Swiss Federal Administration" in row["citation"]
    assert "BAFU:2026" in row["citation"]


def test_vocab_processes_one_term_per_dataset():
    src = BafuVocabProcessesSource(_cfg("bafu-processes", "processes", "vocab_processes"))
    rows = src.transform(src.parse(fixture_zip()))
    assert len(rows) == 3  # obsolete datasets stay: they are link targets
    elec = next(r for r in rows if r["notation"] == UUID_ELEC)
    assert elec["iri"] == f"https://vocab.sentier.dev/processes/bafu/{UUID_ELEC}"
    assert elec["pref_label"] == "Electricity, at test plant"
    assert elec["process_type"] == "unit"
    assert elec["source"] == BAFU_PROVENANCE_IRI
    obsolete = next(r for r in rows if r["notation"] == UUID_OBSOLETE)
    assert obsolete["definition"].startswith("[obsolete]")


def test_vocab_processes_filters_to_sector_file():
    # emit_filename is the sector slug: each registry entry emits one
    # content-named per-sector file (no source-named payload files).
    def rows_for(sector):
        src = BafuVocabProcessesSource(
            _cfg(f"bafu-processes-{sector}", "processes", "vocab_processes", emit_filename=sector)
        )
        return src.transform(src.parse(fixture_zip()))

    (elec,) = rows_for("electricity")
    assert elec["notation"] == UUID_ELEC
    (gas,) = rows_for("energy")  # natural gas routes to 05-energy
    assert gas["notation"] == UUID_GAS
    (obsolete,) = rows_for("obsolete")
    assert obsolete["notation"] == UUID_OBSOLETE
    assert rows_for("agriculture") == []


def test_vocab_flows_filters_to_compartment_file():
    def rows_for(compartment_slug):
        src = BafuVocabFlowsSource(
            _cfg(
                f"bafu-elementary-flows-{compartment_slug}",
                "elementary-flows",
                "vocab_flows",
                emit_filename=compartment_slug,
            )
        )
        return src.transform(src.parse(fixture_zip()))

    (co2,) = rows_for("emissions-to-air")
    assert co2["pref_label"] == "Carbon dioxide, fossil"
    (water,) = rows_for("resources")
    assert water["pref_label"] == "Water, river"
    assert rows_for("emissions-to-soil") == []


def test_vocab_flows_distinct_biosphere_terms():
    src = BafuVocabFlowsSource(_cfg("bafu-elementary-flows", "elementary-flows", "vocab_flows"))
    rows = src.transform(src.parse(fixture_zip()))
    assert len(rows) == 2  # CO2 + river water, deduped across datasets
    co2 = next(r for r in rows if r["pref_label"] == "Carbon dioxide, fossil")
    fid = ecospold.flow_id("Carbon dioxide, fossil", "emissions to air", "unspecified")
    assert co2["iri"] == f"https://vocab.sentier.dev/flows/{fid}"
    assert co2["notation"] == fid
    assert co2["compartment"] == "emissions to air"
    assert co2["sub_compartment"] == "unspecified"
    assert co2["cas_number"] == "000124-38-9"
    assert co2["source"] == BAFU_PROVENANCE_IRI
    water = next(r for r in rows if r["pref_label"] == "Water, river")
    assert water["compartment"] == "resources"
    assert "cas_number" not in water
