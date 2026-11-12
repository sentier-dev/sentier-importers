import json
from dataclasses import replace

import httpx
import pytest
from sentier_importers.core import inventory_lineage, pipeline, registry
from sentier_importers.core.context import RunContext

from tests.sources.test_agribalyse_ef_lcia import _raw as cf_raw
from tests.sources.test_agribalyse_mappings import _raw as mapping_raw


@pytest.mark.parametrize("name", ["methods", "characterization-factors", "biosphere"])
@pytest.mark.parametrize("deliver", [False, True])
def test_methods_and_mapping_artifacts_have_inspectable_archives(
    tmp_path, monkeypatch, name, deliver
):
    raw = mapping_raw() if name == "biosphere" else cf_raw()
    path = tmp_path / "input.parquet"
    path.write_bytes(raw.content)
    config = registry.get_config(name)
    metadata = {"source": config.input_provenance["source"]} if config.input_provenance else {}
    config = replace(config, fetch_url=f"file://{path}", inputs={}, input_provenance=metadata)
    ctx = RunContext(
        cache_dir=tmp_path / "cache",
        output_dir=tmp_path / "output",
        deliver_local_root=tmp_path / "target" if deliver else None,
    )
    if deliver:
        ctx.deliver_local_root.mkdir()
    staged = pipeline.run_source(registry.load_source(config), ctx)
    artifact = (
        (ctx.deliver_local_root / "data" / config.category / staged.name) if deliver else staged
    )
    manifest, events = inventory_lineage.read_bundle(artifact)
    assert f"/{config.target.replace('_', '-')}/" in manifest["dataset"]["namespace"]
    assert (
        manifest["dataset"]["facets"]["version"]["datasetVersion"]
        == f"sha256:{manifest['sha256']}"
    )
    assert events[-1]["eventType"] == "COMPLETE"
    assert ("/delivered/" in manifest["dataset"]["namespace"]) is deliver
    if name == "biosphere":
        step = next(
            e
            for e in events
            if e["eventType"] == "COMPLETE"
            and e["job"]["name"].endswith(".filter-shippable-mappings")
        )
        assert step["inputs"][0]["facets"]["sentier_preview"]["rowCount"] == 5
        assert step["outputs"][0]["facets"]["sentier_preview"]["rowCount"] == 2
        preview = manifest["dataset"]["facets"]["sentier_preview"]
        assert set(preview["jsonEncodedColumns"]) == {"source", "target"}
        assert json.loads(preview["rows"][0]["source"])["name"] == "Nitrogen, total"
        assert isinstance(json.loads(artifact.read_text())["replace"][0]["source"], dict)
    if name == "characterization-factors":
        step = next(
            e
            for e in events
            if e["eventType"] == "COMPLETE"
            and e["job"]["name"].endswith(".resolve-and-map-cf-rows")
        )
        assert "without a SimaPro arbiter" in step["job"]["facets"]["documentation"]["description"]
    real_client = httpx.Client
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, json={"graph": [{"id": "output"}]})

    monkeypatch.setattr(
        inventory_lineage.httpx,
        "Client",
        lambda **kwargs: real_client(transport=httpx.MockTransport(handler), **kwargs),
    )
    assert inventory_lineage.inspect_artifact(artifact, "http://api", "http://web", 16)
    assert requests[-1].method == "GET"
