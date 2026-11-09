"""The foodex2-source plugin emits one valid Source provenance record."""

from pathlib import Path

import orjson
import pyarrow.parquet as pq
from sentier_importers.core import pipeline
from sentier_importers.core.context import RunContext
from sentier_importers.core.source import SourceConfig
from sentier_importers.sources.foodex2.provenance import (
    PROVENANCE,
    PROVENANCE_IRI,
    Foodex2Provenance,
)

#: A copy of sentier-vocab's schemas/ folder (term.yaml, relation.yaml).
VOCAB_SCHEMAS = Path(__file__).parents[1] / "core" / "fixtures" / "vocab"
SCHEME = "https://vocab.sentier.dev/sources/"


def _config(**overrides) -> SourceConfig:
    base = dict(
        name="foodex2-source",
        module="sentier_importers.sources.foodex2.provenance",
        target="sentier_vocab",
        category="foodex2",
        term_type="source",
        fetch_url="static://foodex2-source",
        fetch_format="json",
        output_format="parquet",
        dedup_check_existing=False,
    )
    base.update(overrides)
    return SourceConfig(**base)


def _ctx(tmp_path) -> RunContext:
    return RunContext(
        cache_dir=tmp_path / "c", output_dir=tmp_path / "o", schema_dir=VOCAB_SCHEMAS
    )


def test_emits_single_source_record():
    src = Foodex2Provenance(_config())
    rows = src.transform(src.parse(src.fetch(_ctx(Path("/tmp")))))
    assert len(rows) == 1
    assert rows[0]["iri"] == PROVENANCE_IRI
    assert rows[0]["identifier"] == "10.2903/sp.efsa.2015.EN-804"


def test_provenance_iri_under_sources_scheme():
    assert PROVENANCE_IRI.startswith(SCHEME)
    assert PROVENANCE["pref_label"] == "FoodEx2"


def test_run_release_emits_the_source_term(tmp_path):
    (out,) = pipeline.run_release("foodex2", [Foodex2Provenance(_config())], _ctx(tmp_path))
    assert out == tmp_path / "o" / "sentier_vocab" / "terms" / "foodex2.parquet"

    (term,) = pq.read_table(out).to_pylist()
    assert term["iri"] == term["source"] == PROVENANCE_IRI
    assert term["type"] == "source"
    assert term["status"] == "published"
    attributes = orjson.loads(term["attributes"])
    assert attributes["identifier"] == "10.2903/sp.efsa.2015.EN-804"
    assert attributes["creators"] == ["European Food Safety Authority (EFSA)"]
