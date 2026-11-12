import importlib.util
import io
import json
import subprocess
from dataclasses import replace
from pathlib import Path

import httpx
import pyarrow.parquet as pq
import pytest
from sentier_importers import __main__ as cli
from sentier_importers.core import code_metadata, inventory_lineage, lineage, pipeline
from sentier_importers.core.context import RunContext
from sentier_importers.core.errors import SentierImporterError, SourceContractError
from sentier_importers.core.source import SourceConfig, TransformStep
from sentier_importers.core.types import RawData

from tests.core.test_inventory_lineage import InventorySource, bundle_directory, run_inventory


def test_examples_scan_past_first_five_rows_and_skip_nulls():
    snapshot = lineage._snapshot(
        [{"value": None, "empty": None}] * 5
        + [{"value": "A"}] * 3
        + [{"value": "B"}, {"value": "C"}, {"value": "D"}]
    )
    assert snapshot.fields[0]["description"] == 'Examples (sample): "A"; "B"; "C"'
    assert "description" not in snapshot.fields[1]
    assert len(snapshot.preview) == 5


def test_private_execution_details_do_not_enter_events(tmp_path):
    _, artifact, *_ = run_inventory(tmp_path)
    manifest, events = inventory_lineage.read_bundle(artifact)
    text = json.dumps(events)
    assert str(tmp_path).replace("\\", "\\\\") not in text
    assert tmp_path.as_posix() not in text
    assert all(
        set(e["run"]["facets"]["sentier_execution"]) == {"_producer", "_schemaURL", "startedAt"}
        for e in events
    )
    transform = next(e for e in events if e["job"]["name"].endswith(".transform"))
    assert "def transform" in transform["job"]["facets"]["sourceCode"]["sourceCode"]
    assert "files" in transform["job"]["facets"]["sentier_code"]
    assert manifest["dataset"]["name"] == "processes.parquet"


def test_private_failure_message_and_trace_are_not_exported(tmp_path):
    private_message = str(tmp_path / "sensitive-user" / "token.txt")

    class Failing(InventorySource):
        def transform(self, rows):
            raise RuntimeError(private_message)

    root, _, ctx, config = run_inventory(tmp_path)
    with pytest.raises(RuntimeError):
        pipeline.run_source(Failing(config), ctx)
    events = lineage.lineage_path(config, ctx).read_text(encoding="utf-8")
    assert "sensitive-user" not in events
    assert str(tmp_path).replace("\\", "\\\\") not in events


def test_external_store_keeps_self_contained_adjacent_bundle(tmp_path):
    _, artifact, ctx, config = run_inventory(tmp_path)
    store = tmp_path / "shared-events"
    pipeline.run_source(InventorySource(config), replace(ctx, lineage_store=store))
    assert not (artifact.parent / ".lineage" / f"{artifact.name}.json").exists()
    manifest, _ = inventory_lineage.read_bundle(artifact)
    assert (store / manifest["runId"] / "events.jsonl").is_file()
    store.rename(tmp_path / "unavailable-store")
    assert inventory_lineage.read_bundle(artifact)[0] == manifest


def test_depth_is_positive_and_used_in_both_graph_request_and_link(tmp_path, monkeypatch):
    root, *_ = run_inventory(tmp_path)
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, json={"graph": [{"id": "output"}]})

    real = httpx.Client
    monkeypatch.setattr(
        inventory_lineage.httpx,
        "Client",
        lambda **kw: real(transport=httpx.MockTransport(handler), **kw),
    )
    url = inventory_lineage.inspect_inventory(
        "demo-v1", "processes", root, "http://api", "http://web", 12
    )
    assert url.endswith("?depth=12")
    assert requests[-1].url.params["depth"] == "12"
    with pytest.raises(SystemExit):
        cli.main(["inspect-lineage", "demo-v1", "--processes", "--depth", "0"])


class MappedSource(InventorySource):
    def fetch(self, ctx):
        url = self.config.inputs["mapping"]
        self.inputs = {"mapping": RawData(Path(url[7:]).read_bytes(), url)}
        return None

    def transform_steps(self):
        return (
            TransformStep(
                "map-process-rows",
                "Use observed mapping amounts.",
                self.transform,
                inputs=("mapping",),
            ),
        )

    def transform(self, rows):
        mapping = pq.read_table(io.BytesIO(self.inputs["mapping"].content)).to_pylist()
        return [{**row, "amount": mapping[0]["amount"]} for row in rows]


def run_consumer(tmp_path, upstream, cls=MappedSource):
    (tmp_path / "inventory").mkdir(exist_ok=True)
    config = SourceConfig(
        name="mapped-processes",
        module=__name__,
        target="sentier_inventory",
        category="consumer-v1",
        emit_filename="processes",
        fetch_url="static://demo",
        fetch_format="json",
        output_format="parquet",
        inputs={"mapping": f"file://{upstream}"},
    )
    ctx = RunContext(
        cache_dir=tmp_path / "cache",
        output_dir=tmp_path / "consumer-stage",
        deliver_local_root=tmp_path / "inventory",
    )
    pipeline.run_source(cls(config), ctx)
    return tmp_path / "inventory" / "data" / "consumer-v1" / "processes.parquet"


def test_dependencies_are_linked_at_consuming_step_and_upstream_is_frozen(tmp_path, monkeypatch):
    _, upstream, ctx, config = run_inventory(tmp_path)
    original, _ = inventory_lineage.read_bundle(upstream)
    output = run_consumer(tmp_path, upstream)
    downstream, events = inventory_lineage.read_bundle(output)
    consuming = next(
        e
        for e in events
        if e["eventType"] == "COMPLETE" and e["job"]["name"].endswith(".map-process-rows")
    )
    mapping = consuming["inputs"][1]
    assert mapping["namespace"] == original["dataset"]["namespace"]
    assert mapping["facets"]["sentier_provenance"]["status"] == "available"
    assert downstream["dependencies"][0]["runId"] == original["runId"]

    # A later run replaces the source endpoint; downstream must still use frozen A.
    class LaterVersion(InventorySource):
        def transform(self, rows):
            return [{**row, "amount": 99.0} for row in rows]

    pipeline.run_source(LaterVersion(config), ctx)
    later, _ = inventory_lineage.read_bundle(upstream)
    assert later["runId"] != original["runId"]
    assert later["sha256"] != original["sha256"]
    posted = []

    def handler(request):
        if request.method == "POST":
            posted.append(json.loads(request.content))
        return httpx.Response(200, json={"graph": [{"id": "output"}]})

    real = httpx.Client
    monkeypatch.setattr(
        inventory_lineage.httpx,
        "Client",
        lambda **kw: real(transport=httpx.MockTransport(handler), **kw),
    )
    inventory_lineage.inspect_inventory(
        "consumer-v1", "processes", tmp_path / "inventory", "http://api", "http://web"
    )
    ids = {e["run"]["runId"] for e in posted}
    assert original["runId"] in ids and later["runId"] not in ids
    assert posted[-1]["run"]["runId"] == downstream["runId"]


def test_legacy_mapping_remains_a_visible_boundary(tmp_path):
    _, upstream, *_ = run_inventory(tmp_path)
    legacy = tmp_path / "legacy.parquet"
    legacy.write_bytes(upstream.read_bytes())
    output = run_consumer(tmp_path, legacy)
    _, events = inventory_lineage.read_bundle(output)
    consuming = next(
        e
        for e in events
        if e["eventType"] == "COMPLETE" and e["job"]["name"].endswith(".map-process-rows")
    )
    ref = consuming["inputs"][1]
    assert ref["name"] == "legacy.parquet"
    assert ref["facets"]["sentier_provenance"]["status"] == "not-recorded"
    assert "Upstream lineage not recorded" in ref["facets"]["documentation"]["description"]


def test_undeclared_step_input_is_rejected(tmp_path):
    class Undeclared(MappedSource):
        def transform_steps(self):
            return (TransformStep("mapping", "Undeclared input.", self.transform),)

    _, upstream, *_ = run_inventory(tmp_path)
    with pytest.raises(SourceContractError, match="undeclared input"):
        run_consumer(tmp_path, upstream, Undeclared)


def test_unused_secondary_input_does_not_create_a_dependency(tmp_path):
    class Unused(MappedSource):
        def transform(self, rows):
            return rows

    _, upstream, *_ = run_inventory(tmp_path)
    output = run_consumer(tmp_path, upstream, Unused)
    manifest, events = inventory_lineage.read_bundle(output)
    assert not manifest["dependencies"]
    assert all(len(e["inputs"]) <= 1 for e in events)


def test_corrupt_upstream_bundle_is_not_treated_as_legacy(tmp_path):
    _, upstream, *_ = run_inventory(tmp_path)
    event_file = bundle_directory(upstream) / "events.jsonl"
    event_file.write_text("{}")
    # Use a separate consumer setup so the producer's current bundle is not replaced.
    config = SourceConfig(
        name="consumer",
        module=__name__,
        target="sentier_inventory",
        category="consumer-v1",
        emit_filename="processes",
        fetch_url="static://demo",
        fetch_format="json",
        output_format="parquet",
        inputs={"mapping": f"file://{upstream}"},
    )
    ctx = RunContext(cache_dir=tmp_path / "cache", output_dir=tmp_path / "consumer-stage")
    with pytest.raises(SentierImporterError, match="checksum"):
        pipeline.run_source(MappedSource(config), ctx)


def test_missing_frozen_upstream_fails_before_contacting_marquez(tmp_path, monkeypatch):
    _, upstream, *_ = run_inventory(tmp_path)
    output = run_consumer(tmp_path, upstream)
    manifest, _ = inventory_lineage.read_bundle(output)
    archive = bundle_directory(output)
    (archive / manifest["dependencies"][0]["manifest"]).unlink()

    def unexpected(*args, **kwargs):
        raise AssertionError("Invalid provenance must not contact Marquez")

    monkeypatch.setattr(inventory_lineage.httpx, "Client", unexpected)
    with pytest.raises(SentierImporterError, match="Cannot inspect lineage"):
        inventory_lineage.inspect_inventory(
            "consumer-v1", "processes", tmp_path / "inventory", "http://api", "http://web"
        )


@pytest.mark.parametrize("mutate", [False, True])
def test_directory_input_records_file_hashes_and_rejects_changes(tmp_path, mutate):
    directory = tmp_path / "mapping-files"
    directory.mkdir()
    (directory / "amount.txt").write_text("42")

    class DirectorySource(MappedSource):
        def fetch(self, ctx):
            return None

        def transform(self, rows):
            path = self.input_path("mapping") / "amount.txt"
            amount = float(path.read_text())
            if mutate:
                path.write_text("43")
            return [{**row, "amount": amount} for row in rows]

    if mutate:
        with pytest.raises(SourceContractError, match="changed"):
            run_consumer(tmp_path, directory, DirectorySource)
        return
    output = run_consumer(tmp_path, directory, DirectorySource)
    _, events = inventory_lineage.read_bundle(output)
    event = next(
        e
        for e in events
        if e["eventType"] == "COMPLETE" and e["job"]["name"].endswith(".map-process-rows")
    )
    files = event["inputs"][1]["facets"]["sentier_provenance"]["files"]
    assert files == [
        {"name": "amount.txt", "sha256": lineage.file_sha256(directory / "amount.txt")}
    ]


def test_code_status_is_job_specific_and_hash_is_actual_file(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    root.mkdir()

    def git(*args):
        subprocess.run(["git", *args], cwd=root, check=True, capture_output=True)

    git("init")
    git("config", "user.name", "test")
    git("config", "user.email", "test@example.invalid")
    code = root / "plugin.py"
    code.write_text("def transform(rows):\n    return rows\n")
    (root / "README.md").write_text("initial\n")
    git("add", ".")
    git("commit", "-m", "initial")
    spec = importlib.util.spec_from_file_location("lineage_test_plugin", code)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(code_metadata, "ROOT", root)
    (root / "README.md").write_text("unrelated change\n")
    evidence = code_metadata.capture(module.transform, include_source=True)
    assert evidence["sentier_code"]["codeDirty"] is False
    (root / "helper.py").write_text("VALUE = 1\n")
    evidence = code_metadata.capture(module.transform, dependencies=("helper.py",))
    assert evidence["sentier_code"]["codeDirty"] is True
    assert evidence["sentier_code"]["files"][1]["dirty"] is True
    assert code_metadata.capture(module.transform)["sentier_code"]["codeDirty"] is False
    code.write_text("def transform(rows):\n    return list(rows)\n")
    evidence = code_metadata.capture(module.transform)
    assert evidence["sentier_code"]["codeDirty"] is True
    assert evidence["sentier_code"]["files"][0]["sha256"] == lineage.file_sha256(code)
    assert evidence["sentier_code"]["linkRepresents"] == "committed-baseline"
    git("add", "plugin.py")
    assert code_metadata.capture(module.transform)["sentier_code"]["codeDirty"] is True
    assert code_metadata.capture(None)["sentier_code"]["codeDirty"] is None
    new_code = root / "new_plugin.py"
    new_code.write_text("def transform(rows):\n    return rows\n")
    spec = importlib.util.spec_from_file_location("lineage_new_plugin", new_code)
    new_module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(new_module)
    assert "sourceCodeLocation" not in code_metadata.capture(new_module.transform)
    git("add", "new_plugin.py")
    evidence = code_metadata.capture(new_module.transform)
    assert evidence["sentier_code"]["codeDirty"] is True
    assert "sourceCodeLocation" not in evidence  # staged additions have no committed baseline
