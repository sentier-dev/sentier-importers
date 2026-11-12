import json
import shutil
import subprocess
from dataclasses import replace
from pathlib import Path

import httpx
import pytest
from sentier_importers.core import deliver, inventory_lineage, pipeline
from sentier_importers.core.context import RunContext
from sentier_importers.core.errors import DeliveryError, SentierImporterError
from sentier_importers.core.source import SourceConfig
from sentier_importers.core.targets import get_target

from tests.core.test_inventory_lineage import InventorySource, run_inventory
from tests.core.test_lineage_contract import run_consumer
from tests.core.test_pipeline import VOCAB_SCHEMAS, _products, _provenance


def git(*args, cwd=None):
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout


@pytest.mark.parametrize(
    "target,fmt,name",
    [
        ("sentier_inventory", "parquet", "processes"),
        ("sentier_vocab", "parquet", "demo-1"),
        ("sentier_methods", "parquet", "methods"),
        ("sentier_mappings", "json", "biosphere"),
    ],
)
def test_pr_clone_is_self_contained(tmp_path, monkeypatch, target, fmt, name):
    original = tmp_path / "r"
    original.mkdir()
    git("init", "--initial-branch=main", cwd=original)
    (original / ".gitignore").write_text(".lineage/\nlineage/\n")
    (original / "data/other").mkdir(parents=True)
    (original / "data/other/keep.json").write_text("{}")
    git("add", ".", cwd=original)
    git(
        "-c",
        "user.name=Lineage Test",
        "-c",
        "user.email=test@example.invalid",
        "commit",
        "-m",
        "fixture",
        cwd=original,
    )
    config = SourceConfig(
        name="import-test",
        module=__name__,
        target=target,
        category="demo-v1",
        emit_filename=name,
        fetch_url="static://fixture",
        fetch_format="json",
        output_format=fmt,
    )
    ctx = RunContext(
        cache_dir=tmp_path / "c",
        output_dir=tmp_path / "o",
        lineage_store=tmp_path / "s",
        dry_run=False,
    )
    real = deliver._run
    committed = []
    relative_dir = "terms" if target == "sentier_vocab" else "data/demo-v1"

    def fake_network(cmd, cwd=None):
        if cmd[:2] == ["git", "clone"]:
            return real(["git", "clone", "--no-hardlinks", str(original), cmd[-1]])
        if cmd[:2] == ["git", "commit"]:
            artifact = Path(cwd) / relative_dir / f"{name}.{fmt}"
            manifest, events = inventory_lineage.read_bundle(artifact)
            assert events[-1]["eventType"] == "COMPLETE"
            assert not any(e["job"]["name"].endswith(".deliver") for e in events)
            assert (artifact.parent / ".lineage" / artifact.name / "manifest.json").is_file()
            assert not (artifact.parent / ".lineage" / f"{artifact.name}.json").exists()
            committed.append(manifest)
            return real(
                [
                    "git",
                    "-c",
                    "user.name=Lineage Test",
                    "-c",
                    "user.email=test@example.invalid",
                    *cmd[1:],
                ],
                cwd=cwd,
            )
        if cmd[:2] == ["git", "push"]:
            return ""
        if cmd[:3] == ["gh", "pr", "create"]:
            return "https://example.org/pr/1"
        return real(cmd, cwd=cwd)

    monkeypatch.setattr(deliver, "_run", fake_network)
    if target == "sentier_vocab":
        ctx = replace(ctx, schema_dir=VOCAB_SCHEMAS)
        staged, relation = pipeline.run_release("demo-1", [_products(), _provenance()], ctx)
    else:
        staged = pipeline.run_source(InventorySource(config), ctx)
    checkout = ctx.cache_dir / "delivery" / target
    tree = git("ls-tree", "-r", "--name-only", "HEAD", cwd=checkout).splitlines()
    assert f"{relative_dir}/{staged.name}" in tree
    assert f"{relative_dir}/.lineage/{staged.name}/manifest.json" in tree
    assert f"{relative_dir}/.lineage/{staged.name}/events.jsonl" in tree
    if target == "sentier_vocab":
        assert f"relations/{relation.name}" in tree
        assert f"relations/.lineage/{relation.name}/manifest.json" in tree
    assert not any(path.startswith("lineage/runs/") for path in tree)
    assert "data/other/keep.json" in tree
    assert f"data/{staged.name}" not in tree
    fresh = tmp_path / "clone"
    git("-c", "core.autocrlf=true", "clone", "--no-hardlinks", str(checkout), str(fresh))
    for folder in (ctx.output_dir, ctx.lineage_store):
        assert folder.resolve().is_relative_to(tmp_path.resolve())
        shutil.rmtree(folder)
    artifact = fresh / relative_dir / staged.name
    manifest, _ = inventory_lineage.read_bundle(artifact)
    assert manifest == committed[0]
    if target == "sentier_vocab":
        relation_manifest, _ = inventory_lineage.read_bundle(fresh / "relations" / relation.name)
        assert relation_manifest["runId"] == manifest["runId"]
        assert relation_manifest["artifactKey"] != manifest["artifactKey"]
    real_client = httpx.Client
    monkeypatch.setattr(
        inventory_lineage.httpx,
        "Client",
        lambda **kw: real_client(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(200, json={"graph": [{}]})
            ),
            **kw,
        ),
    )
    assert inventory_lineage.inspect_artifact(artifact, "http://api", "http://web")


def test_pr_failure_does_not_fail_import(tmp_path, monkeypatch):
    config = SourceConfig(
        name="import-test",
        module=__name__,
        target="sentier_inventory",
        category="demo-v1",
        fetch_url="static://fixture",
        fetch_format="json",
        output_format="json",
    )
    ctx = RunContext(cache_dir=tmp_path / "c", output_dir=tmp_path / "o", dry_run=False)

    def fail(*args, **kwargs):
        raise DeliveryError("PR creation failed")

    monkeypatch.setattr(deliver, "deliver", fail)
    with pytest.raises(DeliveryError, match="PR creation failed"):
        pipeline.run_source(InventorySource(config), ctx)
    manifest, events = inventory_lineage.read_bundle(
        ctx.output_dir / "sentier_inventory/demo-v1/import-test.json"
    )
    assert events[-1]["eventType"] == "COMPLETE"
    assert not any(e["eventType"] == "FAIL" for e in events)


def test_delivery_requires_bundle_before_git(tmp_path, monkeypatch):
    artifact = tmp_path / "data.json"
    artifact.write_text("{}")
    monkeypatch.setattr(
        deliver, "_run", lambda *args, **kwargs: pytest.fail("Unexpected Git call")
    )
    with pytest.raises(DeliveryError, match="No completed lineage"):
        deliver.deliver(
            [artifact],
            get_target("sentier_inventory"),
            category="demo",
            with_lineage=True,
            branch="test",
            title="test",
            body="test",
            ctx=RunContext(cache_dir=tmp_path / "c", output_dir=tmp_path / "o", dry_run=False),
        )


def test_upstream_export(tmp_path):
    _, upstream, *_ = run_inventory(tmp_path)
    source = run_consumer(tmp_path, upstream)
    root = tmp_path / "copy"
    dest = root / "data" / source.name
    dest.parent.mkdir(parents=True)
    shutil.copy2(source, dest)
    inventory_lineage.export_bundle(source, dest, root)
    snapshot = inventory_lineage._bundle_snapshot(dest)
    assert len(snapshot["children"]) == 1
    assert snapshot["children"][0]["manifest"]["sha256"] == inventory_lineage.file_sha256(upstream)
    inventory_lineage.export_bundle(source, dest, root)  # Identical histories may be reused.


def test_export_refuses_wrong_bytes_and_replaces_current_bundle(tmp_path):
    _, source, *_ = run_inventory(tmp_path, local=False)
    root = tmp_path / "r"
    root.mkdir()
    dest = root / source.name
    dest.write_bytes(b"wrong")
    with pytest.raises(SentierImporterError, match="differs"):
        inventory_lineage.export_bundle(source, dest, root)
    shutil.copy2(source, dest)
    (folder,) = inventory_lineage.export_bundle(source, dest, root)
    manifest = json.loads((folder / "manifest.json").read_text())
    manifest["execution"]["contributor"] = "changed"
    (folder / "manifest.json").write_text(json.dumps(manifest))
    inventory_lineage.export_bundle(source, dest, root)
    assert inventory_lineage.read_bundle(dest) == inventory_lineage.read_bundle(source)


@pytest.mark.parametrize("category", ["../outside", "/absolute", "C:/outside", "a\\b"])
def test_delivery_rejects_invalid_folders(tmp_path, category):
    with pytest.raises(DeliveryError):
        deliver.deliver_local(
            [], get_target("sentier_inventory"), root=tmp_path, category=category
        )
