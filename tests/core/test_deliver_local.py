"""Tests for the ``emit_filename`` override and local (no-PR) delivery.

Both exist for the BAFU-2026 regenerate-locally flow: inventory files must land
as ``processes.parquet``/``exchanges.parquet`` regardless of the source name,
and a regeneration must be able to fill a local target checkout without any
git/gh call.
"""

from pathlib import Path

from sentier_importers.core import deliver as deliver_mod
from sentier_importers.core import pipeline
from sentier_importers.core import registry as registry_mod
from sentier_importers.core.context import RunContext
from sentier_importers.core.source import SourceConfig
from sentier_importers.core.targets import get_target
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


def test_emit_filename_overrides_output_name(tmp_path):
    out = pipeline.run_source(_source(emit_filename="processes"), _ctx(tmp_path))
    assert out == tmp_path / "o" / "sentier_inventory" / "example" / "processes.json"


def test_emit_filename_defaults_to_source_name(tmp_path):
    out = pipeline.run_source(_source(), _ctx(tmp_path))
    assert out.name == "example-csv.json"


def test_registry_parses_emit_filename(tmp_path):
    path = tmp_path / "registry.yaml"
    path.write_text(
        """
sources:
  - name: bafu-electricity-processes
    module: m.a
    target: sentier_inventory
    category: 02-electricity
    emit_filename: processes
    fetch: { url: "file:///tmp/x.zip", format: zip }
    output_format: parquet
    enabled: false
""",
        encoding="utf-8",
    )
    (cfg,) = registry_mod.load_registry(path)
    assert cfg.emit_filename == "processes"
    assert cfg.enabled is False


def test_deliver_local_copies_into_category_dir(tmp_path):
    staged = tmp_path / "processes.parquet"
    staged.write_bytes(b"pq")
    clone = tmp_path / "clone"
    (clone / "data").mkdir(parents=True)

    dests = deliver_mod.deliver_local(
        [staged], get_target("sentier_inventory"), category="02-electricity", root=clone
    )

    assert dests == [clone / "data" / "02-electricity" / "processes.parquet"]
    assert dests[0].read_bytes() == b"pq"


def test_deliver_local_requires_existing_root(tmp_path):
    import pytest
    from sentier_importers.core.errors import DeliveryError

    with pytest.raises(DeliveryError):
        deliver_mod.deliver_local(
            [], get_target("sentier_inventory"), category="x", root=tmp_path / "nope"
        )


def test_run_source_deliver_local_no_pr(tmp_path, monkeypatch):
    pr_calls = []
    monkeypatch.setattr(pipeline.deliver_mod, "deliver", lambda *a, **k: pr_calls.append(True))
    clone = tmp_path / "clone"
    clone.mkdir()

    pipeline.run_source(
        _source(emit_filename="processes"),
        _ctx(tmp_path, deliver_local_root=clone),
    )

    assert (clone / "data" / "example" / "processes.json").exists()
    assert pr_calls == []  # local delivery never opens a PR


def test_cli_run_accepts_deliver_local(tmp_path, monkeypatch):
    from sentier_importers.__main__ import build_parser

    args = build_parser().parse_args(
        ["run", "example-csv", "--deliver-local", str(tmp_path), "--output-dir", str(tmp_path)]
    )
    assert Path(args.deliver_local) == tmp_path
