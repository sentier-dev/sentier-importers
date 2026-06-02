from sentier_importers.core.context import RunContext
from sentier_importers.core.source import SourceConfig
from sentier_importers.sources.example_csv.source import ExampleCsvSource


def _source():
    config = SourceConfig(
        name="example-csv",
        module="sentier_importers.sources.example_csv.source",
        target="sentier_inventory",
        category="example",
        fetch_url="unused",
        fetch_format="csv",
        output_format="json",
    )
    return ExampleCsvSource(config)


def test_transform_normalizes_records():
    rows = _source().transform([{"id": "1", "name": "  alpha widget ", "region": "EU"}])
    assert rows == [{"id": "1", "label": "Alpha Widget", "region": "EU"}]


def test_fetch_reads_bundled_data(tmp_path):
    ctx = RunContext(cache_dir=tmp_path / "c", output_dir=tmp_path / "o")
    source = _source()
    rows = source.transform(source.parse(source.fetch(ctx)))
    assert {r["label"] for r in rows} == {"Alpha Widget", "Beta Widget", "Gamma Widget"}
