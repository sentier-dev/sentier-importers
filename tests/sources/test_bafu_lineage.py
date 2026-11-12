import hashlib
import json
from dataclasses import replace
from pathlib import Path

import jsonschema
import pyarrow.parquet as pq
from sentier_importers.core import fetch as fetch_mod
from sentier_importers.core import inventory_lineage, pipeline, registry
from sentier_importers.core.context import RunContext
from sentier_importers.sources.bafu import ecospold

from tests.sources.bafu_fixture import fixture_zip


def test_standard_zip_to_parquet_maps_process_rows_as_one_transform_job(tmp_path):
    raw = fixture_zip()
    zip_path = tmp_path / "bafu.zip"
    zip_path.write_bytes(raw.content)
    config = replace(registry.get_config("bafu-processes"), fetch_url=f"file://{zip_path}")
    source = registry.load_source(config)
    assert [step.name for step in source.declared_transform_steps()] == ["map-process-rows"]
    ctx = RunContext(cache_dir=tmp_path / "cache", output_dir=tmp_path / "output")
    output = pipeline.run_source(source, ctx)
    assert output == ctx.output_dir / "sentier_inventory/bafu-2026-v1/processes.parquet"
    table = pq.read_table(output)
    records = ecospold.parse_ecospold_zip(raw)
    standard = registry.load_source(registry.get_config("bafu-processes")).transform(records)
    assert table.to_pylist() == [{**row, "source": "bafu-2026"} for row in standard]
    assert table.to_pylist() == source.transform(records)
    assert "exchanges" not in table.column_names
    assert {row["source_version"] for row in table.to_pylist()} == {"v1"}
    assert {row["sector"] for row in table.to_pylist()} == {"electricity", "energy", "obsolete"}
    assert all("uuid" in record and "exchanges" in record for record in records)
    _, events = inventory_lineage.read_bundle(output)
    jobs = [e["job"]["name"].split(".")[-1] for e in events if e["eventType"] == "COMPLETE"]
    assert jobs == [
        "fetch",
        "parse",
        "map-process-rows",
        "deduplicate",
        "assemble",
        "validate",
        "emit",
        "bafu-processes",
    ]
    mapping = next(
        e
        for e in events
        if e["eventType"] == "COMPLETE" and e["job"]["name"].endswith(".map-process-rows")
    )
    before = {f["name"] for f in mapping["inputs"][0]["facets"]["schema"]["fields"]}
    after = {f["name"] for f in mapping["outputs"][0]["facets"]["schema"]["fields"]}
    assert {"uuid", "exchanges"} <= before
    assert {"process_id", "reference_product", "sector", "source", "source_version"} <= after
    assert "exchanges" not in after
    fetch = next(
        e for e in events if e["eventType"] == "COMPLETE" and e["job"]["name"].endswith(".fetch")
    )
    facets = fetch["inputs"][0]["facets"]
    provenance = facets["sentier_source"]
    assert provenance["metadata"] == config.input_provenance["source"]
    assert provenance["metadata"]["source"] == "bafu-2026"
    assert provenance["metadata"]["source_version"] == "v1"
    assert provenance["metadata"]["date"] is None
    assert provenance["metadata"]["source_url"] is None
    assert "dataSource" not in facets  # No invented provider URL.
    assert facets["sentier_provenance"]["status"] == "not-recorded"
    observation = provenance["observation"]
    assert observation["sha256"] == hashlib.sha256(raw.content).hexdigest()
    assert observation["sizeBytes"] == len(raw.content)
    assert observation["acquisitionMethod"] == "local-file"
    assert observation["downloadedAt"] is None
    assert observation["readAt"] is not None
    assert str(tmp_path) not in json.dumps(events)
    manifest, _ = inventory_lineage.read_bundle(output)
    assert manifest["dependencies"][0]["sourceMetadata"] == {
        key: provenance[key] for key in ("metadata", "observation")
    }
    schema_path = (
        Path(__file__).resolve().parents[2]
        / "openlineage/facets/1-0-0/SentierSourceDatasetFacet.json"
    )
    definitions = json.loads(schema_path.read_text())["$defs"]
    jsonschema.validate(provenance["metadata"], definitions["Metadata"])
    jsonschema.validate(observation, definitions["Observation"])
    pipeline.run_source(registry.load_source(config), ctx)
    _, cached_events = inventory_lineage.read_bundle(output)
    cached = next(
        e
        for e in cached_events
        if e["eventType"] == "COMPLETE" and e["job"]["name"].endswith(".fetch")
    )["inputs"][0]["facets"]["sentier_source"]
    assert cached["observation"]["acquisitionMethod"] == "cache"
    assert cached["observation"]["downloadedAt"] is None
    assert cached["metadata"]["date"] is None


def test_download_timestamp_is_observed_without_inventing_publication_date(tmp_path, monkeypatch):
    raw = fixture_zip()

    class Response:
        content = raw.content

        def raise_for_status(self):
            pass

    monkeypatch.setattr(fetch_mod.httpx, "get", lambda *args, **kwargs: Response())
    config = replace(
        registry.get_config("bafu-processes"), fetch_url="https://example.org/bafu.zip"
    )
    ctx = RunContext(cache_dir=tmp_path / "cache", output_dir=tmp_path / "output")
    output = pipeline.run_source(registry.load_source(config), ctx)
    _, events = inventory_lineage.read_bundle(output)
    fetch = next(
        e for e in events if e["eventType"] == "COMPLETE" and e["job"]["name"].endswith(".fetch")
    )
    facet = fetch["inputs"][0]["facets"]["sentier_source"]
    assert facet["observation"]["acquisitionMethod"] == "download"
    assert facet["observation"]["downloadedAt"] == facet["observation"]["readAt"]
    assert facet["observation"]["downloadedAt"] is not None
    assert facet["metadata"]["date"] is None
