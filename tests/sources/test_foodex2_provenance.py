"""The foodex2-source plugin emits one valid Source provenance record."""

from pathlib import Path

import yaml
from sentier_importers.core import pipeline
from sentier_importers.core.context import RunContext
from sentier_importers.core.source import SourceConfig
from sentier_importers.sources.foodex2.provenance import (
    PROVENANCE,
    PROVENANCE_IRI,
    Foodex2Provenance,
)

SOURCE_SCHEMA = Path(__file__).parent / "fixtures" / "source_collection.yaml"
SCHEME = "https://vocab.sentier.dev/sources/"


def _config(**overrides) -> SourceConfig:
    base = dict(
        name="foodex2-source",
        module="sentier_importers.sources.foodex2.provenance",
        target="sentier_vocab",
        category="sources",
        fetch_url="static://foodex2-source",
        fetch_format="json",
        output_format="yaml",
        collection_class="SourceCollection",
        collection_items_key="sources",
        collection_scheme=SCHEME,
        schema_file="source",
        validate_against="Source",
        dedup_check_existing=False,
    )
    base.update(overrides)
    return SourceConfig(**base)


def _ctx(tmp_path) -> RunContext:
    return RunContext(cache_dir=tmp_path / "c", output_dir=tmp_path / "o")


def test_emits_single_source_record():
    src = Foodex2Provenance(_config())
    rows = src.transform(src.parse(src.fetch(_ctx(Path("/tmp")))))
    assert len(rows) == 1
    assert rows[0]["iri"] == PROVENANCE_IRI
    assert rows[0]["identifier"] == "10.2903/sp.efsa.2015.EN-804"


def test_provenance_iri_under_sources_scheme():
    assert PROVENANCE_IRI.startswith(SCHEME)
    assert PROVENANCE["pref_label"] == "FoodEx2"


def test_run_source_emits_valid_source_collection(tmp_path, monkeypatch):
    monkeypatch.setattr(pipeline.schema_provider, "resolve_schema", lambda *a, **k: SOURCE_SCHEMA)
    out = pipeline.run_source(Foodex2Provenance(_config()), _ctx(tmp_path))
    assert out == tmp_path / "o" / "sentier_vocab" / "sources" / "foodex2-source.yaml"

    loaded = yaml.safe_load(out.read_text())
    assert loaded["scheme"] == SCHEME
    assert [s["iri"] for s in loaded["sources"]] == [PROVENANCE_IRI]
