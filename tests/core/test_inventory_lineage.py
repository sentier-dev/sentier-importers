import json

import httpx
import pytest
from sentier_importers import __main__ as cli
from sentier_importers.core import inventory_lineage, lineage, pipeline
from sentier_importers.core.context import RunContext
from sentier_importers.core.errors import SentierImporterError
from sentier_importers.core.source import Source, SourceConfig


class InventorySource(Source):
    def fetch(self, ctx):
        return None

    def parse(self, raw):
        return [{"process_id": "p1", "amount": 1.0}, {"process_id": "p2", "amount": 2.0}]

    def transform(self, records):
        return records


def run_inventory(tmp_path, name="processes", local=True):
    root = tmp_path / "inventory"
    root.mkdir(exist_ok=True)
    config = SourceConfig(
        name=f"demo-{name}",
        module=__name__,
        target="sentier_inventory",
        category="demo-v1",
        emit_filename=name,
        fetch_url="static://demo",
        fetch_format="json",
        output_format="parquet",
    )
    ctx = RunContext(
        cache_dir=tmp_path / "cache",
        output_dir=tmp_path / "output",
        deliver_local_root=root if local else None,
    )
    staged = pipeline.run_source(InventorySource(config), ctx)
    artifact = root / "data" / "demo-v1" / f"{name}.parquet" if local else staged
    return root, artifact, ctx, config


def bundle_directory(artifact):
    return artifact.parent / ".lineage" / artifact.name


def test_run_preserves_exact_local_artifact_and_complete_steps(tmp_path):
    root, artifact, ctx, config = run_inventory(tmp_path)
    manifest, events = inventory_lineage.read_bundle(artifact)
    assert manifest["sha256"] == lineage.file_sha256(artifact)
    assert manifest["dataset"]["namespace"].startswith("sentier://local/")
    assert "/sentier-inventory/demo-v1/delivered/run/" in manifest["dataset"]["namespace"]
    assert manifest["dataset"]["name"] == artifact.name
    assert "user" not in manifest["execution"] and "host" not in manifest["execution"]
    assert len(manifest["dataset"]["facets"]["documentation"]["description"]) < 100
    assert manifest["dataset"]["facets"]["sentier_preview"]["rowCount"] == 2
    assert events[-1]["eventType"] == "COMPLETE"
    assert len([e for e in events if e["eventType"] == "START"]) == 9
    assert [e for e in events if e["job"]["name"].endswith(".deliver")][-1][
        "eventType"
    ] == "COMPLETE"


def test_legacy_endpoint_does_not_contact_marquez(tmp_path, monkeypatch, capsys):
    def unexpected(*args, **kwargs):
        raise AssertionError("Legacy data must not contact Marquez")

    monkeypatch.setattr(inventory_lineage.httpx, "Client", unexpected)
    cli.main(["inspect-lineage", "legacy-v1", "--processes", "--inventory-root", str(tmp_path)])
    assert (
        "No lineage information available for this endpoint: legacy-v1/processes.parquet"
        in capsys.readouterr().out
    )


def test_modified_artifact_is_rejected_before_upload(tmp_path, monkeypatch):
    root, artifact, *_ = run_inventory(tmp_path)
    artifact.write_bytes(b"different artifact")
    with pytest.raises(SentierImporterError, match="Artifact checksum"):
        inventory_lineage.inspect_inventory(
            "demo-v1", "processes", root, "http://api", "http://web"
        )


def test_modified_events_are_rejected(tmp_path):
    _, artifact, *_ = run_inventory(tmp_path)
    event_path = bundle_directory(artifact) / "events.jsonl"
    event_path.write_text("{}\n")
    with pytest.raises(SentierImporterError, match="event checksum"):
        inventory_lineage.read_bundle(artifact)


def test_repeated_runs_preserve_history_and_independent_endpoints(tmp_path):
    _, artifact, ctx, _ = run_inventory(tmp_path)
    first, _ = inventory_lineage.read_bundle(artifact)
    _, artifact, *_ = run_inventory(tmp_path)
    second, _ = inventory_lineage.read_bundle(artifact)
    _, exchanges, *_ = run_inventory(tmp_path, "exchanges")
    assert first["runId"] != second["runId"]
    assert len(list((ctx.output_dir / "lineage/runs").glob("*/manifest.json"))) == 3
    assert set(p.name for p in (artifact.parent / ".lineage").iterdir()) == {
        "processes.parquet",
        "exchanges.parquet",
    }
    assert not (artifact.parents[2] / "lineage/runs").exists()
    assert inventory_lineage.read_bundle(exchanges)[0]["artifact"] == "exchanges.parquet"
    assert inventory_lineage.read_bundle(artifact)[0]["runId"] == second["runId"]


def test_staged_inventory_has_bundle_without_local_delivery(tmp_path):
    _, artifact, *_ = run_inventory(tmp_path, local=False)
    assert inventory_lineage.read_bundle(artifact)[0]["dataset"]["name"] == artifact.name


def test_inspection_uploads_then_verifies_graph_and_prints_encoded_link(
    tmp_path, monkeypatch, capsys
):
    root, artifact, *_ = run_inventory(tmp_path)
    manifest, events = inventory_lineage.read_bundle(artifact)
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(
            201 if request.method == "POST" else 200, json={"graph": [{"id": "output"}]}
        )

    real_client = httpx.Client
    monkeypatch.setattr(
        inventory_lineage.httpx,
        "Client",
        lambda **kwargs: real_client(transport=httpx.MockTransport(handler), **kwargs),
    )
    cli.main(
        [
            "inspect-lineage",
            "demo-v1",
            "--processes",
            "--inventory-root",
            str(root),
            "--marquez-api",
            "http://api",
            "--marquez-web",
            "http://web",
        ]
    )
    assert len([r for r in requests if r.method == "POST"]) == len(events)
    assert json.loads(requests[-2].content)["run"]["runId"] == manifest["runId"]
    assert (
        requests[-1].url.params["nodeId"]
        == f"dataset:{manifest['dataset']['namespace']}:{manifest['dataset']['name']}"
    )
    output = capsys.readouterr().out
    assert "Marquez: http://web/lineage/dataset/sentier%3A%2F%2F" in output
    assert "Executor:" not in output
    assert requests[-1].url.params["depth"] == "8"
    assert "?depth=8" in output


def test_unreachable_marquez_reports_actionable_error(tmp_path, monkeypatch):
    root, *_ = run_inventory(tmp_path)
    real_client = httpx.Client

    def fail(request):
        raise httpx.ConnectError("connection refused", request=request)

    monkeypatch.setattr(
        inventory_lineage.httpx,
        "Client",
        lambda **kwargs: real_client(transport=httpx.MockTransport(fail), **kwargs),
    )
    with pytest.raises(SentierImporterError, match="Check that the API is running"):
        inventory_lineage.inspect_inventory(
            "demo-v1", "processes", root, "http://api", "http://web"
        )


def test_failed_run_never_creates_a_bundle(tmp_path):
    class Broken(InventorySource):
        def transform(self, records):
            raise RuntimeError("bad transform")

    config = SourceConfig(
        name="broken",
        module=__name__,
        target="sentier_inventory",
        category="demo-v1",
        emit_filename="processes",
        fetch_url="static://demo",
        fetch_format="json",
        output_format="parquet",
    )
    ctx = RunContext(cache_dir=tmp_path / "cache", output_dir=tmp_path / "output")
    with pytest.raises(RuntimeError, match="bad transform"):
        pipeline.run_source(Broken(config), ctx)
    assert not list(tmp_path.rglob("manifest.json"))


def test_pointer_cannot_escape_bundle(tmp_path):
    _, artifact, *_ = run_inventory(tmp_path)
    bundle_directory(artifact).rename(artifact.parent / "old-bundle")
    pointer = artifact.parent / ".lineage" / "processes.parquet.json"
    pointer.write_text(json.dumps({"manifest": "../../outside.json"}))
    with pytest.raises(SentierImporterError, match="escapes"):
        inventory_lineage.read_bundle(artifact)
