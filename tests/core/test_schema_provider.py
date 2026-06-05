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
