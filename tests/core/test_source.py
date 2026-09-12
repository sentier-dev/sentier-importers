import pytest
from sentier_importers.core.context import RunContext
from sentier_importers.core.source import Source, SourceConfig
from sentier_importers.core.types import RawData


def _config(tmp_path):
    src = tmp_path / "in.csv"
    src.write_text("id,name\n1,alpha\n")
    return SourceConfig(
        name="demo",
        module="x.y",
        target="sentier_inventory",
        category="demo",
        fetch_url=f"file://{src}",
        fetch_format="csv",
        output_format="json",
    )


class _Demo(Source):
    def transform(self, records):
        return [{"id": r["id"], "label": r["name"].upper()} for r in records]


def test_default_fetch_and_parse(tmp_path):
    ctx = RunContext(cache_dir=tmp_path / "c", output_dir=tmp_path / "o")
    source = _Demo(_config(tmp_path))
    raw = source.fetch(ctx)
    assert isinstance(raw, RawData)
    records = source.parse(raw)
    assert records == [{"id": "1", "name": "alpha"}]
    assert source.transform(records) == [{"id": "1", "label": "ALPHA"}]


def test_config_defaults(tmp_path):
    config = _config(tmp_path)
    assert config.validate_against is None
    assert config.enabled is True


def test_source_is_abstract():
    with pytest.raises(TypeError):
        Source(_config_stub())  # cannot instantiate without transform


def _config_stub():
    return SourceConfig(
        name="x",
        module="m",
        target="t",
        category="c",
        fetch_url="file:///x",
        fetch_format="csv",
        output_format="json",
    )


def test_named_inputs_are_fetched_alongside_the_primary(tmp_path):
    """``config.inputs`` are secondary URLs a source joins against; fetched with the same
    cache and exposed as ``source.inputs[name]``."""
    side = tmp_path / "side.json"
    side.write_text('{"k": 1}')
    config = SourceConfig(
        name="demo",
        module="x.y",
        target="sentier_inventory",
        category="demo",
        fetch_url=_config(tmp_path).fetch_url,
        fetch_format="csv",
        output_format="json",
        inputs={"side": f"file://{side}"},
    )
    ctx = RunContext(cache_dir=tmp_path / "c", output_dir=tmp_path / "o")
    source = _Demo(config)
    assert source.inputs == {}
    raw = source.fetch(ctx)
    assert raw.content == b"id,name\n1,alpha\n"
    assert source.inputs["side"].content == b'{"k": 1}'
    assert source.inputs["side"].source_url == f"file://{side}"


def test_inputs_default_to_empty(tmp_path):
    assert _config(tmp_path).inputs == {}
