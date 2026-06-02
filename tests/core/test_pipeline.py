import orjson

from sentier_importers.core import pipeline
from sentier_importers.core.context import RunContext
from sentier_importers.core.source import SourceConfig
from sentier_importers.sources.example_csv.source import ExampleCsvSource


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
