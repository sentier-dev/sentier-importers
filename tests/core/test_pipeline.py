from pathlib import Path

import orjson
import pyarrow.parquet as pq
import pytest
from sentier_importers.core import pipeline
from sentier_importers.core.context import RunContext
from sentier_importers.core.errors import (
    DeliveryError,
    FetchError,
    MissingInputError,
    RegistryError,
    ValidationError,
)
from sentier_importers.core.source import Source, SourceConfig
from sentier_importers.sources.example_csv.source import ExampleCsvSource

#: A copy of sentier-vocab's schemas/ folder (term.yaml, relation.yaml).
VOCAB_SCHEMAS = Path(__file__).parent / "fixtures" / "vocab"
PRODUCTS = "https://vocab.sentier.dev/products/"
DEMO_SOURCE = "https://vocab.sentier.dev/sources/demo-1"


def _source(**overrides):
    base = dict(
        name="example-csv",
        module="sentier_importers.sources.example_csv.source",
        target="sentier_inventory",
        category="example",
        fetch_url="unused",
        fetch_format="csv",
        output_format="json",
    )
    base.update(overrides)
    return ExampleCsvSource(SourceConfig(**base))


def _ctx(tmp_path, **kw):
    return RunContext(cache_dir=tmp_path / "c", output_dir=tmp_path / "o", **kw)


def test_run_source_writes_output(tmp_path):
    out = pipeline.run_source(_source(), _ctx(tmp_path))
    assert out == tmp_path / "o" / "sentier_inventory" / "example" / "example-csv.json"
    data = orjson.loads(out.read_bytes())
    assert {r["label"] for r in data} == {"Alpha Widget", "Beta Widget", "Gamma Widget"}


def test_run_source_dry_run_does_not_deliver(tmp_path, monkeypatch):
    called = []
    monkeypatch.setattr(pipeline.deliver_mod, "deliver", lambda *a, **k: called.append(True))
    pipeline.run_source(_source(), _ctx(tmp_path, dry_run=True))
    assert called == []  # dry-run never delivers


def test_run_source_delivers_when_not_dry_run(tmp_path, monkeypatch):
    called = {}

    def fake_deliver(files, target, *, branch, title, body, ctx, category, with_lineage):
        called["branch"] = branch
        assert category == "example"
        assert with_lineage
        return "https://github.com/o/r/pull/9"

    monkeypatch.setattr(pipeline.deliver_mod, "deliver", fake_deliver)
    pipeline.run_source(_source(), _ctx(tmp_path, dry_run=False))
    assert called["branch"] == "import/example-csv"


def test_validate_source_returns_row_count(tmp_path):
    assert pipeline.validate_source(_source(), _ctx(tmp_path)) == 3


def test_assemble_passes_rows_through():
    rows = [{"a": 1}]
    assert pipeline._assemble(rows, _source().config) is rows


def test_assemble_wraps_rows_into_a_randonneur_package():
    cfg = _source(package_name="a__b-biosphere", package_version="0.1.0", package_verb="replace")
    rows = [{"source": {"name": "x"}, "target": {"name": "y"}}]
    assert pipeline._assemble(rows, cfg.config) == {
        "name": "a__b-biosphere",
        "version": "0.1.0",
        "replace": rows,
    }


# --- sentier-vocab: one source is a part, a release is delivered whole -------


class _RowsSource(Source):
    """Emits the rows it was built with; no fetch input."""

    ROWS: list[dict] = []

    def fetch(self, ctx):
        return None

    def parse(self, raw):
        return []

    def transform(self, records):
        return [dict(row) for row in self.ROWS]


class _ProductSource(_RowsSource):
    ROWS = [
        {"iri": PRODUCTS + "cereals", "pref_label": "Cereals", "source": DEMO_SOURCE},
        {
            "iri": PRODUCTS + "wheat",
            "pref_label": "Wheat",
            "cpc_code": "0111",
            "broader": PRODUCTS + "cereals",
            "source": DEMO_SOURCE,
        },
    ]


class _ProvenanceSource(_RowsSource):
    ROWS = [{"iri": DEMO_SOURCE, "pref_label": "Demo", "title": "Demo", "status": "published"}]


def _vocab_source(cls, name, term_type, **overrides):
    base = dict(
        name=name,
        module="m",
        target="sentier_vocab",
        category="demo-1",
        term_type=term_type,
        fetch_url="unused",
        fetch_format="json",
        output_format="parquet",
        dedup_check_existing=False,  # no Layer B network in these tests
    )
    base.update(overrides)
    return cls(SourceConfig(**base))


def _products(**overrides):
    return _vocab_source(_ProductSource, "demo-products", "product", **overrides)


def _provenance():
    return _vocab_source(_ProvenanceSource, "demo-source", "source")


def _vocab_ctx(tmp_path, **kw):
    return _ctx(tmp_path, schema_dir=VOCAB_SCHEMAS, **kw)


def test_run_source_stages_a_vocab_part_as_two_layer_files(tmp_path):
    out = pipeline.run_source(_products(), _vocab_ctx(tmp_path))
    part = tmp_path / "o" / "sentier_vocab" / "parts" / "demo-products"
    assert out == part / "terms" / "demo-1.parquet"
    terms = pq.read_table(out).to_pylist()
    assert [t["type"] for t in terms] == ["product", "product"]
    assert orjson.loads(terms[1]["attributes"]) == {"cpc_code": "0111"}
    edges = pq.read_table(part / "relations" / "demo-1.parquet").to_pylist()
    assert [(e["subject"], e["predicate"], e["object"]) for e in edges] == [
        (PRODUCTS + "wheat", "skos:broader", PRODUCTS + "cereals")
    ]


def test_validate_source_counts_the_terms_of_a_vocab_part(tmp_path):
    assert pipeline.validate_source(_products(), _vocab_ctx(tmp_path)) == 2


class _CerealsSource(_RowsSource):
    ROWS = [{"iri": PRODUCTS + "cereals", "pref_label": "Cereals"}]


def test_vocab_part_is_validated_against_the_term_contract(tmp_path):
    with pytest.raises(ValidationError, match="not in the organism namespace"):
        pipeline.validate_source(
            _vocab_source(_CerealsSource, "demo", "organism"), _vocab_ctx(tmp_path)
        )


def test_vocab_part_of_another_release_is_rejected(tmp_path):
    with pytest.raises(ValidationError, match="source must be"):
        pipeline.validate_source(_products(category="other-2"), _vocab_ctx(tmp_path))


@pytest.mark.parametrize("delivery", [{"dry_run": False}, {"deliver_local_root": Path("/x")}])
def test_a_single_vocab_part_is_never_delivered(tmp_path, delivery):
    with pytest.raises(DeliveryError, match="run --release demo-1"):
        pipeline.run_source(_products(), _vocab_ctx(tmp_path, **delivery))


def test_run_release_writes_every_part_into_one_release(tmp_path):
    files = pipeline.run_release("demo-1", [_products(), _provenance()], _vocab_ctx(tmp_path))
    root = tmp_path / "o" / "sentier_vocab"
    assert files == [root / "terms" / "demo-1.parquet", root / "relations" / "demo-1.parquet"]
    terms = pq.read_table(files[0]).to_pylist()
    assert [(t["type"], t["iri"]) for t in terms] == [
        ("product", PRODUCTS + "cereals"),
        ("product", PRODUCTS + "wheat"),
        ("source", DEMO_SOURCE),
    ]
    assert {t["source"] for t in terms} == {DEMO_SOURCE}


def test_run_release_requires_the_release_source_term(tmp_path):
    with pytest.raises(ValidationError, match="does not hold its source term"):
        pipeline.run_release("demo-1", [_products()], _vocab_ctx(tmp_path))


def test_run_release_rejects_sources_of_another_release_or_target(tmp_path):
    with pytest.raises(RegistryError, match="demo-1"):
        pipeline.run_release("demo-1", [_products(category="other-2")], _vocab_ctx(tmp_path))
    with pytest.raises(RegistryError):
        pipeline.run_release("demo-1", [], _vocab_ctx(tmp_path))
    with pytest.raises(RegistryError, match="not delivered per release"):
        pipeline.run_release("example", [_source()], _ctx(tmp_path))


def test_run_release_replaces_the_release_in_a_local_checkout(tmp_path):
    clone = tmp_path / "sentier-vocab"
    (clone / "terms").mkdir(parents=True)
    (clone / "relations").mkdir()
    (clone / "terms" / "demo-1-01.parquet").write_bytes(b"old shard")
    (clone / "terms" / "demo-1-02.parquet").write_bytes(b"old shard")
    (clone / "terms" / "other.parquet").write_bytes(b"another release")
    (clone / "relations" / "other.parquet").write_bytes(b"another release")

    pipeline.run_release(
        "demo-1", [_products(), _provenance()], _vocab_ctx(tmp_path, deliver_local_root=clone)
    )
    assert sorted(p.name for p in (clone / "terms").iterdir() if p.is_file()) == [
        "demo-1.parquet",
        "other.parquet",
    ]
    assert sorted(p.name for p in (clone / "relations").iterdir() if p.is_file()) == [
        "demo-1.parquet",
        "other.parquet",
    ]
    assert (clone / "terms" / "other.parquet").read_bytes() == b"another release"


def test_run_release_opens_one_pr_for_the_release(tmp_path, monkeypatch):
    called = {}

    def fake_deliver_release(written, target, release, *, branch, title, body, ctx, with_lineage):
        assert with_lineage
        called.update(release=release, branch=branch, layers=sorted(written), dry_run=ctx.dry_run)

    monkeypatch.setattr(pipeline.deliver_mod, "deliver_release", fake_deliver_release)
    pipeline.run_release(
        "demo-1", [_products(), _provenance()], _vocab_ctx(tmp_path, dry_run=False)
    )
    assert called == {
        "release": "demo-1",
        "branch": "import/demo-1",
        "layers": ["relations", "terms"],
        "dry_run": False,
    }


def test_vocab_contracts_come_from_the_pinned_ref_without_a_schema_dir(tmp_path, monkeypatch):
    asked = []

    def fake_resolve(target, schema_id, ctx):
        asked.append((target.name, schema_id))
        return VOCAB_SCHEMAS / f"{schema_id}.yaml"

    monkeypatch.setattr(pipeline.schema_provider, "resolve_schema", fake_resolve)
    pipeline.validate_source(_products(), _ctx(tmp_path))
    assert set(asked) == {("sentier_vocab", "term"), ("sentier_vocab", "relation")}


# --- missing local input: error carries source, input, root, access ---------


class _DefaultFetchSource(Source):
    def transform(self, records):
        return list(records)


def _rooted_source(tmp_path, access, **inputs):
    cfg = SourceConfig(
        name="rooted",
        module="tests",
        target="sentier_inventory",
        category="demo",
        fetch_url=f"file://{tmp_path / 'main.csv'}",
        fetch_format="csv",
        output_format="json",
        access=access,
        inputs=inputs,
    )
    return _DefaultFetchSource(cfg)


@pytest.mark.parametrize(
    "access, sentence",
    [
        ("private", "DdS-internal"),
        ("licensed", "provider"),
        ("public", "clone"),
    ],
)
def test_missing_fetch_url_error_has_context(tmp_path, access, sentence):
    src = _rooted_source(tmp_path, access)
    ctx = _ctx(tmp_path, data_root=tmp_path / "root")
    with pytest.raises(FetchError) as exc:
        pipeline.validate_source(src, ctx)
    text = str(exc.value)
    assert f"file not found: {tmp_path / 'main.csv'}" in text
    assert "input:  fetch.url of source 'rooted'" in text
    assert f"root:   {tmp_path / 'root'}" in text
    assert "--data-root" in text and "SENTIER_DATA_ROOT" in text
    assert f"access: {access}." in text
    assert sentence in text


def test_missing_named_input_error_names_the_input(tmp_path):
    (tmp_path / "main.csv").write_text("id,label\n1,a\n", encoding="utf-8")
    src = _rooted_source(tmp_path, "public", side=f"file://{tmp_path / 'side.json'}")
    with pytest.raises(FetchError) as exc:
        pipeline.validate_source(src, _ctx(tmp_path, data_root=tmp_path))
    assert "input:  side of source 'rooted'" in str(exc.value)


def test_missing_input_keeps_its_error_type_through_the_context_wrapper(tmp_path):
    with pytest.raises(MissingInputError):
        pipeline.validate_source(_rooted_source(tmp_path, "private"), _ctx(tmp_path))
