import json
import shutil
from dataclasses import replace

import httpx
import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from sentier_importers import __main__ as cli
from sentier_importers.core import inventory_lineage, lineage, pipeline
from sentier_importers.core.errors import DeliveryError
from sentier_importers.core.source import TransformStep

from tests.core.test_pipeline import (
    PRODUCTS,
    VOCAB_SCHEMAS,
    _products,
    _ProductSource,
    _provenance,
    _vocab_ctx,
    _vocab_source,
)


@pytest.mark.parametrize("deliver", [False, True])
def test_vocabulary_outputs_have_versioned_local_bundles(tmp_path, monkeypatch, deliver):
    root = tmp_path / "vocab"
    root.mkdir()
    ctx = _vocab_ctx(tmp_path)
    if deliver:
        ctx = replace(ctx, deliver_local_root=root)
    staged = pipeline.run_release("demo-1", [_products(), _provenance()], ctx)
    manifests = []
    for path in staged:
        artifact = root / path.parent.name / path.name if deliver else path
        manifest, events = inventory_lineage.read_bundle(artifact)
        assert manifest["dataset"] in events[-1]["outputs"]
        assert manifest["sha256"] == lineage.file_sha256(artifact)
        assert manifest["dataset"]["facets"]["sentier_preview"]["rowCount"] == (
            3 if path.parent.name == "terms" else 1
        )
        manifests.append(manifest)
    assert manifests[0]["runId"] == manifests[1]["runId"]
    assert manifests[0]["artifactKey"] != manifests[1]["artifactKey"]
    assert len(manifests[0]["dependencies"]) == 3
    artifact = root / "terms" / staged[0].name if deliver else staged[0]
    manifest, events = inventory_lineage.read_bundle(artifact)
    assert manifest["sha256"] == lineage.file_sha256(artifact)
    assert manifest["dataset"]["name"] == artifact.name
    assert "/sentier-vocab/demo-1/" in manifest["dataset"]["namespace"]
    assert "/staged/terms/" in manifest["dataset"]["namespace"]
    assert all(str(root).replace("\\", "\\\\") not in json.dumps(event) for event in events)
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, json={"graph": [{"id": "output"}]})

    real_client = httpx.Client
    monkeypatch.setattr(
        inventory_lineage.httpx,
        "Client",
        lambda **kwargs: real_client(transport=httpx.MockTransport(handler), **kwargs),
    )
    cli.main(["inspect-lineage", "--artifact", str(artifact), "--depth", "12"])
    assert requests[-1].url.params["depth"] == "12"
    assert artifact.name in requests[-1].url.params["nodeId"]


def test_vocab_parts_execute_declared_steps_and_record_both_layers(tmp_path):
    class SteppedProducts(_ProductSource):
        def transform_steps(self):
            return (TransformStep("map-products", "Map vocabulary products.", self.map_products),)

        def map_products(self, records):
            return super().transform(records)

        def transform(self, records):
            raise AssertionError("The pipeline must execute the declared steps")

    source = _vocab_source(SteppedProducts, "demo-products", "product")
    ctx = _vocab_ctx(tmp_path)
    artifact = pipeline.run_source(source, ctx)
    manifest, events = inventory_lineage.read_bundle(artifact)
    assert any(event["job"]["name"].endswith(".map-products") for event in events)
    relation = artifact.parent.parent / "relations" / artifact.name
    other, _ = inventory_lineage.read_bundle(relation)
    assert manifest["runId"] == other["runId"]
    assert manifest["dataset"]["facets"]["sentier_preview"]["rowCount"] == 2
    assert other["dataset"]["facets"]["sentier_preview"]["rowCount"] == 1


def test_vocab_dedup_preserves_upstream_policy_and_records_consumed_terms(tmp_path):
    root = tmp_path / "target"
    shutil.copytree(VOCAB_SCHEMAS, root / "schemas")
    (root / "terms").mkdir()
    existing = root / "terms" / "other-release.parquet"
    pq.write_table(pa.Table.from_pylist([{"iri": PRODUCTS + "cereals"}]), existing)
    # A release's own previous output must not become a dedup dependency.
    pq.write_table(
        pa.Table.from_pylist([{"iri": PRODUCTS + "wheat"}]), root / "terms/demo-1.parquet"
    )
    source = _products(dedup_check_existing=True)
    ctx = replace(_vocab_ctx(tmp_path), schema_dir=root / "schemas")
    artifact = pipeline.run_source(source, ctx)
    assert pq.read_table(artifact).column("iri").to_pylist() == [PRODUCTS + "wheat"]
    manifest, events = inventory_lineage.read_bundle(artifact)
    assert len(manifest["dependencies"]) == 1
    dependency = manifest["dependencies"][0]
    assert dependency["role"] == "existing-vocab"
    assert dependency["dataset"]["name"] == existing.name
    assert dependency["sha256"] == lineage.file_sha256(existing)
    step = next(
        e
        for e in events
        if e["job"]["name"].endswith(".deduplicate") and e["eventType"] == "COMPLETE"
    )
    assert step["run"]["facets"]["sentier_transformation"]["rowsRemoved"] == 1


def test_release_pr_failure_preserves_successful_layer_bundles(tmp_path, monkeypatch):
    ctx = replace(_vocab_ctx(tmp_path), dry_run=False)

    def fail(*args, **kwargs):
        raise DeliveryError("PR creation failed")

    monkeypatch.setattr(pipeline.deliver_mod, "deliver_release", fail)
    with pytest.raises(DeliveryError, match="PR creation failed"):
        pipeline.run_release("demo-1", [_products(), _provenance()], ctx)
    for layer in ("terms", "relations"):
        artifact = ctx.output_dir / "sentier_vocab" / layer / "demo-1.parquet"
        manifest, events = inventory_lineage.read_bundle(artifact)
        assert events[-1]["eventType"] == "COMPLETE"
        assert manifest["dataset"] in events[-1]["outputs"]
        assert not any(event["eventType"] == "FAIL" for event in events)


def test_legacy_vocabulary_artifact_does_not_contact_marquez(tmp_path, monkeypatch, capsys):
    def unexpected(**kwargs):
        raise AssertionError("No lineage must not contact Marquez")

    monkeypatch.setattr(inventory_lineage.httpx, "Client", unexpected)
    cli.main(["inspect-lineage", "--artifact", str(tmp_path / "foodex2.parquet")])
    assert (
        "No lineage information available for this endpoint: foodex2.parquet"
        in capsys.readouterr().out
    )


@pytest.mark.parametrize(
    "selection",
    [
        ["--processes"],
        ["demo-v1", "--artifact", "foodex2.parquet"],
        ["demo-v1", "--processes", "--artifact", "foodex2.parquet"],
    ],
)
def test_inventory_and_artifact_selectors_cannot_be_confused(selection):
    with pytest.raises(SystemExit) as exc:
        cli.main(["inspect-lineage", *selection])
    assert exc.value.code != 0
