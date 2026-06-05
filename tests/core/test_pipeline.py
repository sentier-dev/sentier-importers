from pathlib import Path

import orjson
import yaml
from sentier_importers.core import pipeline
from sentier_importers.core.context import RunContext
from sentier_importers.core.source import Source, SourceConfig
from sentier_importers.sources.example_csv.source import ExampleCsvSource

COLLECTION_SCHEMA = Path(__file__).parent / "fixtures" / "product_collection.yaml"


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

    def fake_deliver(files, target, *, branch, title, body, ctx):
        called["branch"] = branch
        return "https://github.com/o/r/pull/9"

    monkeypatch.setattr(pipeline.deliver_mod, "deliver", fake_deliver)
    pipeline.run_source(_source(), _ctx(tmp_path, dry_run=False))
    assert called["branch"] == "import/example-csv"


def test_validate_source_returns_row_count(tmp_path):
    assert pipeline.validate_source(_source(), _ctx(tmp_path)) == 3


def test_assemble_passthrough_without_collection():
    rows = [{"a": 1}]
    assert pipeline._assemble(rows, _source().config) is rows


def test_assemble_wraps_rows_into_collection():
    cfg = _source(
        collection_class="ProductCollection",
        collection_items_key="products",
        collection_scheme="https://vocab.sentier.dev/products/",
    ).config
    rows = [{"iri": "x", "pref_label": "A"}]
    assert pipeline._assemble(rows, cfg) == {
        "scheme": "https://vocab.sentier.dev/products/",
        "products": rows,
    }


class _ProductSource(Source):
    """Emits two product rows shaped for the ProductCollection schema."""

    def fetch(self, ctx):
        return None

    def parse(self, raw):
        return []

    def transform(self, records):
        return [
            {"iri": "https://vocab.sentier.dev/products/electricity", "pref_label": "Electricity"},
            {"iri": "https://vocab.sentier.dev/products/wheat", "pref_label": "Wheat"},
        ]


def _product_source(**overrides):
    base = dict(
        name="agribalyse-products",
        module="m",
        target="sentier_vocab",
        category="products",
        fetch_url="unused",
        fetch_format="xlsx",
        output_format="yaml",
        collection_class="ProductCollection",
        collection_items_key="products",
        collection_scheme="https://vocab.sentier.dev/products/",
        schema_file="product",
        validate_against="Product",
        dedup_check_existing=False,  # no Layer B network in this test
    )
    base.update(overrides)
    return _ProductSource(SourceConfig(**base))


def test_run_source_emits_collection_mapping(tmp_path, monkeypatch):
    monkeypatch.setattr(
        pipeline.schema_provider, "resolve_schema", lambda target, sid, ctx: COLLECTION_SCHEMA
    )
    out = pipeline.run_source(_product_source(), _ctx(tmp_path))
    assert out == tmp_path / "o" / "sentier_vocab" / "products" / "agribalyse-products.yaml"
    loaded = yaml.safe_load(out.read_text())
    assert isinstance(loaded, dict)
    assert loaded["scheme"] == "https://vocab.sentier.dev/products/"
    assert {p["pref_label"] for p in loaded["products"]} == {"Electricity", "Wheat"}


def test_run_source_resolves_schema_by_file_id(tmp_path, monkeypatch):
    captured = {}

    def fake_resolve(target, schema_id, ctx):
        captured["schema_id"] = schema_id
        return COLLECTION_SCHEMA

    monkeypatch.setattr(pipeline.schema_provider, "resolve_schema", fake_resolve)
    pipeline.run_source(_product_source(), _ctx(tmp_path))
    # Resolves by the schema FILE id ("product"), not the class name ("ProductCollection").
    assert captured["schema_id"] == "product"


def test_collection_source_validates_with_linkml_collection(tmp_path, monkeypatch):
    captured = {}

    monkeypatch.setattr(pipeline.schema_provider, "resolve_schema", lambda *a: COLLECTION_SCHEMA)

    def fake_validate(payload, validator, schema_path, schema_id):
        captured["validator"] = validator
        captured["schema_id"] = schema_id
        captured["is_mapping"] = isinstance(payload, dict)

    monkeypatch.setattr(pipeline.validate_mod, "validate", fake_validate)
    pipeline.run_source(_product_source(), _ctx(tmp_path))
    assert captured["validator"] == "linkml_collection"
    assert captured["schema_id"] == "ProductCollection"
    assert captured["is_mapping"] is True


def test_run_source_validates_when_schema_declared(tmp_path, monkeypatch):
    captured = {}

    def fake_resolve(target, schema_id, ctx):
        captured["schema_id"] = schema_id
        return tmp_path / "schema.yaml"

    def fake_validate(rows, validator, schema_path, schema_id):
        captured["validated"] = (validator, schema_id)

    monkeypatch.setattr(pipeline.schema_provider, "resolve_schema", fake_resolve)
    monkeypatch.setattr(pipeline.validate_mod, "validate", fake_validate)
    src = _source(target="sentier_vocab", validate_against="Widget", output_format="yaml")
    pipeline.run_source(src, _ctx(tmp_path))
    assert captured["schema_id"] == "Widget"
    assert captured["validated"] == ("linkml", "Widget")
