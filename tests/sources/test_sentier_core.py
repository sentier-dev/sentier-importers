"""The sentier-core seed is delivered as a release like any other source."""

from pathlib import Path

import orjson
import pyarrow.parquet as pq
import pytest
import yaml
from sentier_importers.core import pipeline, vocab_layers
from sentier_importers.core.context import RunContext
from sentier_importers.core.errors import ParseError
from sentier_importers.core.registry import get_config
from sentier_importers.core.types import RawData
from sentier_importers.sources.bafu import units as bafu_units
from sentier_importers.sources.sentier_core.source import SEED, SentierCoreSource

#: A copy of sentier-vocab's schemas/ folder (term.yaml, relation.yaml).
VOCAB_SCHEMAS = Path(__file__).parents[1] / "core" / "fixtures" / "vocab"
CORE = "https://vocab.sentier.dev/sources/sentier-core"


def _source() -> SentierCoreSource:
    return SentierCoreSource(get_config("sentier-core"))


def _ctx(tmp_path) -> RunContext:
    return RunContext(
        cache_dir=tmp_path / "c", output_dir=tmp_path / "o", schema_dir=VOCAB_SCHEMAS
    )


def test_registry_block_is_a_mixed_type_release():
    config = get_config("sentier-core")
    assert (config.target, config.category) == ("sentier_vocab", "sentier-core")
    assert config.term_type == vocab_layers.ROW_TYPE
    assert config.dedup_on_existing == "error"


def test_every_seed_row_carries_its_term_type():
    source = _source()
    rows = source.transform(source.parse(source.fetch(None)))
    seed = yaml.safe_load(SEED.read_text(encoding="utf-8"))
    assert len(rows) == sum(len(group) for group in seed.values())
    assert {row["type"] for row in rows} == set(seed)
    assert len({row["iri"] for row in rows}) == len(rows)


def test_seed_is_read_from_disk_not_from_the_fetch_cache(tmp_path):
    ctx = _ctx(tmp_path)
    _source().fetch(ctx)
    assert not ctx.cache_dir.exists()  # an edited seed can never be served stale


def test_a_seed_that_is_not_lists_per_type_is_a_parse_error():
    with pytest.raises(ParseError, match="one list of rows per term type"):
        _source().parse(RawData(content=b"- iri: x\n", source_url="file://seed.yaml"))


def test_release_holds_its_source_term_and_passes_the_contracts(tmp_path):
    terms_file, relations_file = pipeline.run_release("sentier-core", [_source()], _ctx(tmp_path))
    assert terms_file == tmp_path / "o" / "sentier_vocab" / "terms" / "sentier-core.parquet"
    terms = {t["iri"]: t for t in pq.read_table(terms_file).to_pylist()}
    assert terms[CORE]["type"] == "source"
    assert {t["source"] for t in terms.values()} == {CORE}  # also the IPCC AR6 source term
    climate = terms["https://vocab.sentier.dev/impact-categories/climate-change"]
    assert orjson.loads(climate["attributes"])["normalization_factor"] == 1.0
    edges = pq.read_table(relations_file).to_pylist()
    assert {
        (e["subject"].rsplit("/", 1)[-1], e["predicate"], e["object"].rsplit("/", 1)[-1])
        for e in edges
    } >= {
        ("climate-change", "ont:method", "ipcc-2021"),
        ("climate-change", "qudt:hasUnit", "KiloGM"),
        ("mass", "ont:hasUnit", "GM"),
        ("ENVO_00002010", "skos:broader", "ENVO_00002006"),
    }


def test_seed_defines_every_bafu_unit_that_qudt_lacks(tmp_path):
    (terms_file, _) = pipeline.run_release("sentier-core", [_source()], _ctx(tmp_path))
    unit_terms = {
        t["iri"]: t for t in pq.read_table(terms_file).to_pylist() if t["type"] == "unit"
    }
    needed = {iri for iri in bafu_units.UNIT_IRI.values() if iri.startswith(bafu_units.SENTIER)}
    assert needed == set(unit_terms)
    assert unit_terms[bafu_units.SENTIER + "M2-YR"]["notation"] == "m2a"
