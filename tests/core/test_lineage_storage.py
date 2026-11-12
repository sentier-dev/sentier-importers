import json
import shutil
from dataclasses import replace

import pytest
from sentier_importers.core import inventory_lineage, lineage, pipeline
from sentier_importers.core.errors import SentierImporterError

from tests.core.test_inventory_lineage import InventorySource, bundle_directory, run_inventory
from tests.core.test_lineage_contract import run_consumer


def test_current_bundle_matches_separate_immutable_history(tmp_path):
    _, artifact, ctx, config = run_inventory(tmp_path, local=False)
    first = inventory_lineage.read_bundle(artifact)
    archive = ctx.output_dir / "lineage/runs" / first[0]["runId"]
    original = (archive / "manifest.json").read_bytes()
    assert (bundle_directory(artifact) / "manifest.json").read_bytes() == original
    assert (bundle_directory(artifact) / "events.jsonl").read_bytes() == (
        archive / "events.jsonl"
    ).read_bytes()

    pipeline.run_source(InventorySource(config), ctx)
    second = inventory_lineage.read_bundle(artifact)
    assert first[0]["runId"] != second[0]["runId"]
    assert (archive / "manifest.json").read_bytes() == original
    assert len(list((ctx.output_dir / "lineage/runs").iterdir())) == 2
    assert set(path.name for path in bundle_directory(artifact).parent.iterdir()) == {
        artifact.name
    }


def test_moving_artifact_and_current_bundle_needs_no_archive(tmp_path):
    _, upstream, *_ = run_inventory(tmp_path, local=False)
    artifact = run_consumer(tmp_path, upstream)
    expected = inventory_lineage._bundle_snapshot(artifact)
    destination = tmp_path / "portable" / artifact.name
    destination.parent.mkdir()
    shutil.copy2(artifact, destination)
    shutil.copytree(bundle_directory(artifact), bundle_directory(destination))
    assert inventory_lineage._bundle_snapshot(destination) == expected
    assert len(expected["children"]) == 1
    assert not (destination.parent / "lineage").exists()


def test_working_log_override_does_not_move_default_archive(tmp_path):
    _, artifact, ctx, config = run_inventory(tmp_path, local=False)
    ctx = replace(ctx, lineage_dir=tmp_path / "logs")
    pipeline.run_source(InventorySource(config), ctx)
    manifest, _ = inventory_lineage.read_bundle(artifact)
    assert lineage.lineage_path(config, ctx).is_file()
    assert (ctx.output_dir / "lineage/runs" / manifest["runId"] / "manifest.json").is_file()
    assert not (ctx.lineage_dir / "runs").exists()


def test_legacy_pointer_is_readable_and_replaced_only_on_new_run(tmp_path):
    _, artifact, ctx, config = run_inventory(tmp_path, local=False)
    expected = inventory_lineage.read_bundle(artifact)
    legacy_archive = bundle_directory(artifact).parent / expected[0]["runId"]
    bundle_directory(artifact).rename(legacy_archive)
    pointer = artifact.parent / ".lineage" / f"{artifact.name}.json"
    pointer.write_text(json.dumps({"manifest": f"{expected[0]['runId']}/manifest.json"}))
    original = pointer.read_bytes()
    assert inventory_lineage.read_bundle(artifact) == expected
    assert pointer.read_bytes() == original

    pipeline.run_source(InventorySource(config), ctx)
    assert not pointer.exists()
    assert legacy_archive.is_dir()  # Existing immutable history is retained.
    assert inventory_lineage.read_bundle(artifact)[0]["runId"] != expected[0]["runId"]


def test_corrupt_current_bundle_never_falls_back_to_legacy_pointer(tmp_path):
    _, artifact, *_ = run_inventory(tmp_path, local=False)
    manifest, _ = inventory_lineage.read_bundle(artifact)
    legacy = bundle_directory(artifact).parent / manifest["runId"]
    shutil.copytree(bundle_directory(artifact), legacy)
    (legacy.parent / f"{artifact.name}.json").write_text(
        json.dumps({"manifest": f"{manifest['runId']}/manifest.json"})
    )
    (bundle_directory(artifact) / "events.jsonl").write_text("{}\n")
    with pytest.raises(SentierImporterError, match="event checksum"):
        inventory_lineage.read_bundle(artifact)


def test_failed_bundle_preparation_keeps_previous_current_bundle(tmp_path, monkeypatch):
    _, artifact, *_ = run_inventory(tmp_path, local=False)
    expected = inventory_lineage._bundle_snapshot(artifact)

    def fail(*args):
        raise OSError("No space for prepared bundle")

    monkeypatch.setattr(inventory_lineage, "_write_archive", fail)
    with pytest.raises(OSError, match="No space"):
        inventory_lineage._install_current(artifact, expected)
    assert inventory_lineage._bundle_snapshot(artifact) == expected
    assert set(p.name for p in bundle_directory(artifact).parent.iterdir()) == {artifact.name}


def test_archive_reuse_checks_immutable_history(tmp_path):
    _, artifact, ctx, _ = run_inventory(tmp_path, local=False)
    snapshot = inventory_lineage._bundle_snapshot(artifact)
    store = ctx.output_dir / "lineage/runs"
    folder = inventory_lineage._archive_run(store, snapshot)
    manifest = json.loads((folder / "manifest.json").read_text())
    manifest["execution"]["contributor"] = "changed"
    (folder / "manifest.json").write_text(json.dumps(manifest))
    with pytest.raises(SentierImporterError, match="immutable"):
        inventory_lineage._archive_run(store, snapshot)


def test_archive_cannot_overlap_replaceable_current_bundle(tmp_path):
    _, artifact, ctx, config = run_inventory(tmp_path, local=False)
    with pytest.raises(SentierImporterError, match="must be separate"):
        pipeline.run_source(
            InventorySource(config), replace(ctx, lineage_store=bundle_directory(artifact))
        )
