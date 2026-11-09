"""Tests for the BAFU-2026 sentier-vocab sources (terms, flows, provenance)."""

from pathlib import Path

import orjson
import pyarrow.parquet as pq
from sentier_importers.core import pipeline
from sentier_importers.core.context import RunContext
from sentier_importers.core.source import SourceConfig
from sentier_importers.sources.bafu import ecospold
from sentier_importers.sources.bafu.provenance import BAFU_PROVENANCE_IRI, BafuProvenance
from sentier_importers.sources.bafu.vocab_flows import BafuVocabFlowsSource
from sentier_importers.sources.bafu.vocab_processes import BafuVocabProcessesSource

from tests.sources.bafu_fixture import UUID_ELEC, UUID_OBSOLETE, fixture_zip

#: A copy of sentier-vocab's schemas/ folder (term.yaml, relation.yaml).
VOCAB_SCHEMAS = Path(__file__).parents[1] / "core" / "fixtures" / "vocab"


def _cfg(name, term_type, module, fetch_url="unused://"):
    return SourceConfig(
        name=name,
        module=f"sentier_importers.sources.bafu.{module}",
        target="sentier_vocab",
        category="bafu-2026",
        term_type=term_type,
        fetch_url=fetch_url,
        fetch_format="zip",
        output_format="parquet",
        dedup_check_existing=False,  # Layer B needs network; off for unit tests
    )


def test_provenance_single_source_record_with_citation():
    src = BafuProvenance(_cfg("bafu-2026-source", "source", "provenance"))
    (row,) = src.transform(src.parse(src.fetch(None)))
    assert row["iri"] == BAFU_PROVENANCE_IRI
    assert "Life Cycle Inventory database of the Swiss Federal Administration" in row["citation"]
    assert "BAFU:2026" in row["citation"]


def test_vocab_processes_one_term_per_dataset():
    src = BafuVocabProcessesSource(_cfg("bafu-vocab-processes", "process", "vocab_processes"))
    rows = src.transform(src.parse(fixture_zip()))
    assert len(rows) == 3  # obsolete datasets stay: they are link targets
    elec = next(r for r in rows if r["notation"] == UUID_ELEC)
    assert elec["iri"] == f"https://vocab.sentier.dev/processes/bafu/{UUID_ELEC}"
    assert elec["pref_label"] == "Electricity, at test plant"
    assert elec["process_type"] == "unit"
    assert elec["source"] == BAFU_PROVENANCE_IRI
    obsolete = next(r for r in rows if r["notation"] == UUID_OBSOLETE)
    assert obsolete["definition"].startswith("[obsolete]")


def test_vocab_flows_distinct_biosphere_terms():
    src = BafuVocabFlowsSource(_cfg("bafu-vocab-flows", "elementary-flow", "vocab_flows"))
    rows = src.transform(src.parse(fixture_zip()))
    # CO2 + river water + radon-222 + natural gas + mine off-gas + peat + land
    # occupation + uranium
    assert len(rows) == 8
    co2 = next(r for r in rows if r["pref_label"] == "Carbon dioxide, fossil")
    fid = ecospold.flow_id("Carbon dioxide, fossil", "emissions to air", "unspecified", "kg")
    assert co2["iri"] == f"https://vocab.sentier.dev/flows/{fid}"
    assert co2["notation"] == fid
    assert co2["compartment"] == "emissions to air"
    assert co2["sub_compartment"] == "unspecified"
    assert co2["cas_number"] == "000124-38-9"
    assert co2["source"] == BAFU_PROVENANCE_IRI
    water = next(r for r in rows if r["pref_label"] == "Water, river")
    assert water["compartment"] == "resources"
    assert "cas_number" not in water


def test_run_release_writes_the_whole_bafu_release(tmp_path):
    archive = tmp_path / "bafu.zip"
    archive.write_bytes(fixture_zip().content)
    url = f"file://{archive}"
    sources = [
        BafuProvenance(_cfg("bafu-2026-source", "source", "provenance")),
        BafuVocabProcessesSource(_cfg("bafu-vocab-processes", "process", "vocab_processes", url)),
        BafuVocabFlowsSource(_cfg("bafu-vocab-flows", "elementary-flow", "vocab_flows", url)),
    ]
    ctx = RunContext(cache_dir=tmp_path / "c", output_dir=tmp_path / "o", schema_dir=VOCAB_SCHEMAS)
    (out,) = pipeline.run_release("bafu-2026", sources, ctx)  # no relations yet

    assert out == tmp_path / "o" / "sentier_vocab" / "terms" / "bafu-2026.parquet"
    terms = pq.read_table(out).to_pylist()
    assert [t["type"] for t in terms] == ["elementary-flow"] * 8 + ["process"] * 3 + ["source"]
    assert {t["source"] for t in terms} == {BAFU_PROVENANCE_IRI}
    co2 = next(t for t in terms if t["pref_label"] == "Carbon dioxide, fossil")
    assert orjson.loads(co2["attributes"]) == {
        "cas_number": "000124-38-9",
        "compartment": "emissions to air",
        "sub_compartment": "unspecified",
    }
    process = next(t for t in terms if t["notation"] == UUID_ELEC)
    assert orjson.loads(process["attributes"]) == {"process_type": "unit"}
