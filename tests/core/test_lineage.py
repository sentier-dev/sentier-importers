import json

import pytest
from sentier_importers.core import pipeline
from sentier_importers.core.context import RunContext
from sentier_importers.core.lineage import PRODUCER, external_dataset, lineage_path, local_dataset
from sentier_importers.core.source import Source, SourceConfig, TransformStep


@pytest.mark.parametrize(
    "url",
    [
        r"file://C:\data\source_data\BAFU-2026 v1_ecoSpold v1.zip",
        "file://C:/data/source_data/BAFU-2026 v1_ecoSpold v1.zip",
        "file:///C:/data/source_data/BAFU-2026%20v1_ecoSpold%20v1.zip",
    ],
)
def test_windows_source_identity_is_independent_of_file_url_spelling(url):
    dataset = external_dataset(url)
    assert dataset.namespace == "sentier://local/external"
    assert dataset.name == "BAFU-2026 v1_ecoSpold v1.zip"


def test_posix_source_identity_keeps_path_out_of_namespace():
    dataset = external_dataset("file:///data/source_data/BAFU-2026%20v1.zip")
    assert dataset.namespace == "sentier://local/external"
    assert dataset.name == "BAFU-2026 v1.zip"


def test_relative_source_identity_matches_local_path(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    dataset = external_dataset("file://source_data/input.zip")
    assert dataset.namespace == "sentier://local/external"
    assert dataset.name == "input.zip"


def test_external_and_emitted_file_have_same_identity(tmp_path):
    path = tmp_path / "folder with spaces" / "input.zip"
    assert external_dataset(f"file://{path}") == local_dataset(path)
    assert external_dataset(path.as_uri()) == local_dataset(path)


def _config(tmp_path):
    source = tmp_path / "input.json"
    source.write_text('[{"keep": true}, {"keep": false}]', encoding="utf-8")
    return SourceConfig(
        name="lineage-example",
        module="tests.fake",
        target="sentier_inventory",
        category="98-test",
        # The fetcher currently accepts the repository's established file://<Path>
        # spelling on Windows rather than Path.as_uri()'s file:///C:/ spelling.
        fetch_url=f"file://{source}",
        fetch_format="json",
        output_format="json",
    )


def _ctx(tmp_path):
    return RunContext(cache_dir=tmp_path / "cache", output_dir=tmp_path / "output")


def _events(path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


class _SteppedSource(Source):
    def transform(self, records):
        return self._map(self._filter(records))

    def transform_steps(self):
        return (
            TransformStep("filter-records", "Keep selected records.", self._filter),
            TransformStep("map-rows", "Map selected records to output rows.", self._map),
        )

    @staticmethod
    def _filter(records):
        return [record for record in records if record["keep"]]

    @staticmethod
    def _map(records):
        return [{"selected": record["keep"]} for record in records]


def test_run_records_parent_and_ordered_child_runs(tmp_path):
    config = _config(tmp_path)
    ctx = _ctx(tmp_path)
    pipeline.run_source(_SteppedSource(config), ctx)

    events = _events(lineage_path(config, ctx))
    assert events[0]["eventType"] == "START"
    assert events[0]["job"] == {
        "namespace": PRODUCER,
        "name": "lineage-example",
        "facets": events[0]["job"]["facets"],
    }
    assert events[-1]["eventType"] == "COMPLETE"
    assert events[-1]["run"]["runId"] == events[0]["run"]["runId"]

    child_starts = [event for event in events if event["eventType"] == "START"][1:]
    assert [event["job"]["name"] for event in child_starts] == [
        "lineage-example.fetch",
        "lineage-example.parse",
        "lineage-example.filter-records",
        "lineage-example.map-rows",
        "lineage-example.deduplicate",
        "lineage-example.assemble",
        "lineage-example.validate",
        "lineage-example.emit",
    ]
    parent_run_id = events[0]["run"]["runId"]
    assert all(
        event["run"]["facets"]["parent"]["run"]["runId"] == parent_run_id for event in child_starts
    )
    assert all("inputs" in event and "outputs" in event for event in events)

    filter_complete = next(
        event
        for event in events
        if event["eventType"] == "COMPLETE"
        and event["job"]["name"] == "lineage-example.filter-records"
    )
    source_location = filter_complete["job"]["facets"]["sourceCodeLocation"]
    assert source_location["path"] == "tests/core/test_lineage.py"
    assert "tests/core/test_lineage.py#L" in source_location["url"]

    transformation = filter_complete["run"]["facets"]["sentier_transformation"]
    assert transformation["inputRowCount"] == 2
    assert transformation["outputRowCount"] == 1
    assert transformation["rowsRemoved"] == 1
    assert transformation["columnsRemoved"] == []

    output = filter_complete["outputs"][0]
    assert output["facets"]["schema"]["fields"] == [
        {
            "name": "keep",
            "type": "boolean",
            "ordinal_position": 1,
            "description": "Examples (sample): true",
        }
    ]
    assert output["facets"]["sentier_preview"]["rows"] == [{"keep": True}]
    assert output["outputFacets"]["outputStatistics"]["rowCount"] == 1

    map_complete = next(
        event
        for event in events
        if event["eventType"] == "COMPLETE" and event["job"]["name"] == "lineage-example.map-rows"
    )
    assert map_complete["run"]["facets"]["sentier_transformation"]["columnsRemoved"] == ["keep"]
    assert map_complete["run"]["facets"]["sentier_transformation"]["columnsAdded"] == ["selected"]


class _FailingSource(_SteppedSource):
    @staticmethod
    def _map(records):
        raise RuntimeError("mapping failed")


def test_failure_records_child_and_parent_fail_events(tmp_path):
    config = _config(tmp_path)
    ctx = _ctx(tmp_path)

    with pytest.raises(RuntimeError, match="mapping failed"):
        pipeline.run_source(_FailingSource(config), ctx)

    events = _events(lineage_path(config, ctx))
    assert [event["eventType"] for event in events[-2:]] == ["FAIL", "FAIL"]
    assert events[-2]["job"]["name"] == "lineage-example.map-rows"
    assert events[-1]["job"]["name"] == "lineage-example"
    assert events[-1]["run"]["facets"]["errorMessage"]["message"].startswith("RuntimeError:")
    assert "mapping failed" not in events[-1]["run"]["facets"]["errorMessage"]["message"]
