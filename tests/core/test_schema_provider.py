import pytest
from sentier_importers.core import schema_provider
from sentier_importers.core.context import RunContext
from sentier_importers.core.errors import ValidationError
from sentier_importers.core.targets import Target


def _ctx(tmp_path):
    return RunContext(cache_dir=tmp_path / "cache", output_dir=tmp_path / "out")


def test_raw_url_builds_pinned_github_raw_path():
    target = Target(
        name="sentier_vocab",
        repo="https://github.com/sentier-dev/sentier-vocab.git",
        output_subdir="data",
        schema_ref="v1.2.3",
        validator="linkml",
    )
    url = schema_provider._raw_url(target, "unit-group")
    assert url == (
        "https://raw.githubusercontent.com/sentier-dev/sentier-vocab/"
        "v1.2.3/schemas/unit-group.yaml"
    )


def test_resolve_schema_fetches_and_writes_file(tmp_path):
    # Seed a local schema file and point a target's "raw url" at it via file://
    schema_src = tmp_path / "unit-group.yaml"
    schema_src.write_text("id: x\nname: x\n", encoding="utf-8")
    target = Target(
        name="t",
        repo="https://github.com/o/r.git",
        output_subdir="data",
        schema_ref="ref1",
        validator="linkml",
    )
    # Monkeypatch URL builder to a file:// URL we control.
    original = schema_provider._raw_url
    schema_provider._raw_url = lambda t, sid: f"file://{schema_src}"
    try:
        path = schema_provider.resolve_schema(target, "unit-group", _ctx(tmp_path))
    finally:
        schema_provider._raw_url = original
    assert path.exists()
    assert path.read_text(encoding="utf-8") == "id: x\nname: x\n"


def test_resolve_schema_without_pin_raises(tmp_path):
    target = Target(name="t", repo="https://github.com/o/r.git", output_subdir="data")
    with pytest.raises(ValidationError):
        schema_provider.resolve_schema(target, "unit-group", _ctx(tmp_path))


_PINNED = Target(
    name="t", repo="https://github.com/o/r.git", output_subdir="data", schema_ref="ref1"
)


def test_resolve_schema_fetches_local_imports(tmp_path, monkeypatch):
    """A schema's local (non-CURIE) imports are fetched beside it so SchemaView resolves
    them; CURIE imports like ``linkml:types`` are left to LinkML."""
    schemas = {
        "product": b"id: ex:product\nname: product\nimports:\n  - linkml:types\n  - common\n",
        "common": b"id: ex:common\nname: common\nimports:\n  - linkml:types\n",
    }
    monkeypatch.setattr(
        schema_provider.fetch_mod,
        "fetch",
        lambda url, ctx: __import__("sentier_importers.core.types", fromlist=["RawData"]).RawData(
            content=schemas[url.rsplit("/", 1)[-1].removesuffix(".yaml")], source_url=url
        ),
    )
    path = schema_provider.resolve_schema(_PINNED, "product", _ctx(tmp_path))
    assert path.name == "product.yaml"
    assert (path.parent / "common.yaml").exists()  # local import fetched alongside


def test_resolve_schema_import_cycle_terminates(tmp_path, monkeypatch):
    cyclic = {
        "a": b"id: ex:a\nname: a\nimports:\n  - b\n",
        "b": b"id: ex:b\nname: b\nimports:\n  - a\n",
    }
    monkeypatch.setattr(
        schema_provider.fetch_mod,
        "fetch",
        lambda url, ctx: __import__("sentier_importers.core.types", fromlist=["RawData"]).RawData(
            content=cyclic[url.rsplit("/", 1)[-1].removesuffix(".yaml")], source_url=url
        ),
    )
    path = schema_provider.resolve_schema(_PINNED, "a", _ctx(tmp_path))  # must not loop
    assert (path.parent / "a.yaml").exists() and (path.parent / "b.yaml").exists()


def test_schema_dir_resolves_locally_without_fetching(tmp_path, monkeypatch):
    """With ctx.schema_dir set, read the local file and never touch the network."""
    schema_dir = tmp_path / "schemas"
    schema_dir.mkdir()
    (schema_dir / "product.yaml").write_text("id: ex:product\nname: product\n")

    def _boom(*a, **k):
        raise AssertionError("fetch must not be called when schema_dir is set")

    monkeypatch.setattr(schema_provider.fetch_mod, "fetch", _boom)
    ctx = RunContext(cache_dir=tmp_path / "c", output_dir=tmp_path / "o", schema_dir=schema_dir)

    path = schema_provider.resolve_schema(_PINNED, "product", ctx)
    assert path == schema_dir / "product.yaml"


def test_schema_dir_missing_file_raises(tmp_path):
    ctx = RunContext(cache_dir=tmp_path / "c", output_dir=tmp_path / "o", schema_dir=tmp_path)
    with pytest.raises(ValidationError):
        schema_provider.resolve_schema(_PINNED, "nope", ctx)
