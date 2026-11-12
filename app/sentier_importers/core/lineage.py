"""OpenLineage JSONL recording for importer parent and child runs."""

from __future__ import annotations

import hashlib
import json
import traceback
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, time, timezone
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any, TypeVar
from urllib.parse import unquote, urlsplit

from sentier_importers.core import code_metadata
from sentier_importers.core.context import RunContext
from sentier_importers.core.source import SourceConfig
from sentier_importers.core.types import RawData

PRODUCER = "https://github.com/sentier-dev/sentier-importers"
RUN_EVENT_SCHEMA = "https://openlineage.io/spec/2-0-2/OpenLineage.json#/$defs/RunEvent"
PARENT_FACET_SCHEMA = "https://openlineage.io/spec/facets/1-0-0/ParentRunFacet.json"
DOCUMENTATION_FACET_SCHEMA = "https://openlineage.io/spec/facets/1-0-0/DocumentationJobFacet.json"
ERROR_FACET_SCHEMA = "https://openlineage.io/spec/facets/1-0-0/ErrorMessageRunFacet.json"
SCHEMA_FACET_SCHEMA = "https://openlineage.io/spec/facets/1-2-0/SchemaDatasetFacet.json"
INPUT_STATISTICS_FACET_SCHEMA = (
    "https://openlineage.io/spec/facets/1-0-0/InputStatisticsInputDatasetFacet.json"
)
OUTPUT_STATISTICS_FACET_SCHEMA = (
    "https://openlineage.io/spec/facets/1-0-2/OutputStatisticsOutputDatasetFacet.json"
)
DATASET_DOCUMENTATION_SCHEMA = (
    "https://openlineage.io/spec/facets/1-0-0/DocumentationDatasetFacet.json"
)
DATASET_VERSION_SCHEMA = "https://openlineage.io/spec/facets/1-0-1/DatasetVersionDatasetFacet.json"
SENTIER_FACET_ROOT = (
    "https://raw.githubusercontent.com/sentier-dev/sentier-importers/"
    "main/openlineage/facets/1-0-0"
)
PREVIEW_FACET_SCHEMA = f"{SENTIER_FACET_ROOT}/SentierPreviewDatasetFacet.json"
TRANSFORMATION_FACET_SCHEMA = f"{SENTIER_FACET_ROOT}/SentierTransformationRunFacet.json"
EXECUTION_FACET_SCHEMA = f"{SENTIER_FACET_ROOT}/SentierExecutionRunFacet.json"

T = TypeVar("T")


@dataclass(frozen=True)
class DatasetSnapshot:
    """Tabular metadata observed for one pipeline value."""

    row_count: int
    fields: tuple[dict[str, object], ...]
    preview: tuple[dict[str, object], ...]
    json_encoded_columns: tuple[str, ...] = ()

    @property
    def columns(self) -> set[str]:
        return {str(item["name"]) for item in self.fields}


@dataclass
class DatasetRef:
    """Stable OpenLineage dataset identity plus metadata observed during this run."""

    namespace: str
    name: str
    snapshot: DatasetSnapshot | None = field(default=None, repr=False)
    description: str | None = None
    version: str | None = None
    provenance: dict | None = None
    source_metadata: dict | None = None

    def capture(self, value: object) -> None:
        self.snapshot = _snapshot(value)

    def as_event_value(self, *, role: str | None = None) -> dict[str, object]:
        value: dict[str, object] = {"namespace": self.namespace, "name": self.name}
        facets: dict[str, object] = {}
        if self.description:
            facets["documentation"] = {
                "_producer": PRODUCER,
                "_schemaURL": DATASET_DOCUMENTATION_SCHEMA,
                "description": self.description,
            }
        if self.version:
            facets["version"] = {
                "_producer": PRODUCER,
                "_schemaURL": DATASET_VERSION_SCHEMA,
                "datasetVersion": self.version,
            }
        if self.provenance:
            facets["sentier_provenance"] = {
                "_producer": PRODUCER,
                "_schemaURL": f"{SENTIER_FACET_ROOT}/SentierProvenanceDatasetFacet.json",
                **self.provenance,
            }
        if self.source_metadata:
            facets["sentier_source"] = {
                "_producer": PRODUCER,
                "_schemaURL": f"{SENTIER_FACET_ROOT}/SentierSourceDatasetFacet.json",
                **self.source_metadata,
            }
            metadata = self.source_metadata["metadata"]
            if metadata.get("source_url"):
                facets["dataSource"] = {
                    "_producer": PRODUCER,
                    "_schemaURL": "https://openlineage.io/spec/facets/1-0-0/DatasourceDatasetFacet.json",
                    "name": metadata["source"],
                    "uri": metadata["source_url"],
                }
        if facets:
            value["facets"] = facets
        if self.snapshot is None:
            return value
        facets.update(
            {
                "schema": {
                    "_producer": PRODUCER,
                    "_schemaURL": SCHEMA_FACET_SCHEMA,
                    "fields": list(self.snapshot.fields),
                },
                "sentier_preview": {
                    "_producer": PRODUCER,
                    "_schemaURL": PREVIEW_FACET_SCHEMA,
                    "rows": list(self.snapshot.preview),
                    "rowCount": self.snapshot.row_count,
                    "truncated": self.snapshot.row_count > len(self.snapshot.preview),
                },
            }
        )
        value["facets"] = facets
        if self.snapshot.json_encoded_columns:
            facets["sentier_preview"]["jsonEncodedColumns"] = list(
                self.snapshot.json_encoded_columns
            )
        if role == "input":
            value["inputFacets"] = {
                "inputStatistics": {
                    "_producer": PRODUCER,
                    "_schemaURL": INPUT_STATISTICS_FACET_SCHEMA,
                    "rowCount": self.snapshot.row_count,
                }
            }
        elif role == "output":
            value["outputFacets"] = {
                "outputStatistics": {
                    "_producer": PRODUCER,
                    "_schemaURL": OUTPUT_STATISTICS_FACET_SCHEMA,
                    "rowCount": self.snapshot.row_count,
                }
            }
        return value


def lineage_path(config: SourceConfig, ctx: RunContext) -> Path:
    """Return the single-run JSONL path for ``config``."""
    root = ctx.lineage_dir or ctx.output_dir / "lineage"
    return root / f"{config.name}.openlineage.jsonl"


def external_dataset(url: str) -> DatasetRef:
    """Split an input URL into an OpenLineage namespace and dataset name."""
    if url.startswith("file://"):
        # Registry file URLs also accept native paths (file://C:\\... and
        # file://../...). Split those as filesystem paths, rather than treating
        # the Windows drive and backslashes as a URL authority.
        local = unquote(url[len("file://") :]).replace("\\", "/")
        windows = PureWindowsPath(local.lstrip("/"))
        if windows.drive.endswith(":") and windows.is_absolute():
            path = windows
        elif local.startswith("/"):
            path = PurePosixPath(local)
        else:
            path = Path(local).resolve()
        return DatasetRef("sentier://local/external", path.name)
    parsed = urlsplit(url)
    path = PurePosixPath(unquote(parsed.path))
    name = path.name or "source"
    namespace = (
        "sentier://remote/external"
        if parsed.scheme in ("https", "http")
        else "sentier://external/static"
    )
    return DatasetRef(namespace or url, name)


def intermediate_dataset(config: SourceConfig, name: str, run_id: str = "working") -> DatasetRef:
    return DatasetRef(f"inmemory://sentier-importers/{config.name}/{run_id}", name)


def local_dataset(path: Path) -> DatasetRef:
    resolved = path.resolve()
    return DatasetRef("sentier://local/external", resolved.name)


def target_dataset(config: SourceConfig, repo: str, filename: str) -> DatasetRef:
    return DatasetRef(repo.removesuffix(".git"), f"data/{config.category}/{filename}")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _json_value(value: object) -> object:
    """Convert common importer values to compact JSON-safe preview values."""
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, (datetime, date, time)):
        return value.isoformat()
    if isinstance(value, bytes):
        return f"<{len(value)} bytes>"
    if isinstance(value, Mapping):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, Sequence):
        return [_json_value(item) for item in value]
    return str(value)


def _rows(value: object) -> list[Mapping[str, Any]] | None:
    """Expose the tabular rows represented by an intermediate pipeline value."""
    if isinstance(value, RawData):
        return None
    if isinstance(value, list):
        if all(isinstance(item, Mapping) for item in value):
            return value
        return None
    if isinstance(value, Mapping):
        collections = [
            item
            for item in value.values()
            if isinstance(item, list) and all(isinstance(row, Mapping) for row in item)
        ]
        if len(collections) == 1:
            return collections[0]
        return [value]
    return None


def _type_name(values: Sequence[object]) -> str:
    names: list[str] = []
    for value in values:
        if value is None:
            name = "null"
        elif isinstance(value, bool):
            name = "boolean"
        elif isinstance(value, int):
            name = "integer"
        elif isinstance(value, float):
            name = "number"
        elif isinstance(value, str):
            name = "string"
        elif isinstance(value, bytes):
            name = "bytes"
        elif isinstance(value, Mapping):
            name = "object"
        elif isinstance(value, Sequence):
            name = "array"
        else:
            name = type(value).__name__
        if name not in names:
            names.append(name)
    return " | ".join(names) if names else "unknown"


def _snapshot(value: object) -> DatasetSnapshot | None:
    rows = _rows(value)
    if rows is None:
        return None
    names: list[str] = []
    for row in rows:
        for name in row:
            text = str(name)
            if text not in names:
                names.append(text)
    fields = []
    for index, name in enumerate(names, start=1):
        examples = []
        keys = set()
        for row in rows:
            value = row.get(name)
            if value is None:
                continue
            key = json.dumps(_json_value(value), sort_keys=True, ensure_ascii=False, default=str)
            if key not in keys:
                keys.add(key)
                examples.append(key if len(key) <= 120 else key[:117] + "...")
                if len(examples) == 3:
                    break
        field = {
            "name": name,
            "type": _type_name([row.get(name) for row in rows]),
            "ordinal_position": index,
        }
        if examples:
            field["description"] = "Examples (sample): " + "; ".join(examples)
        fields.append(field)
    # Marquez indexes preview columns across datasets. Inventory's scalar source
    # and randonneur's object source otherwise conflict in its existing index.
    encoded = tuple(
        name
        for name in names
        if name in {"source", "target"} and any(isinstance(row.get(name), Mapping) for row in rows)
    )
    preview = tuple(
        {
            str(key): (
                json.dumps(_json_value(item), ensure_ascii=False, sort_keys=True)
                if key in encoded and isinstance(item, Mapping)
                else _json_value(item)
            )
            for key, item in row.items()
        }
        for row in rows[:5]
    )
    return DatasetSnapshot(len(rows), tuple(fields), preview, encoded)


def _transformation_facet(
    inputs: Sequence[DatasetRef], outputs: Sequence[DatasetRef]
) -> dict[str, object] | None:
    before = next((item.snapshot for item in inputs if item.snapshot is not None), None)
    after = next((item.snapshot for item in outputs if item.snapshot is not None), None)
    if before is None or after is None:
        return None
    removed_columns = sorted(before.columns - after.columns)
    added_columns = sorted(after.columns - before.columns)
    return {
        "_producer": PRODUCER,
        "_schemaURL": TRANSFORMATION_FACET_SCHEMA,
        "inputRowCount": before.row_count,
        "outputRowCount": after.row_count,
        "rowsRemoved": max(before.row_count - after.row_count, 0),
        "inputColumnCount": len(before.fields),
        "outputColumnCount": len(after.fields),
        "columnsRemoved": removed_columns,
        "columnsAdded": added_columns,
    }


class LineageRecorder:
    """Write a parent import run and its direct child step runs as JSONL."""

    def __init__(
        self,
        config: SourceConfig,
        path: Path,
        parent_source: object | None = None,
        ctx: RunContext | None = None,
    ) -> None:
        self.config = config
        self.path = path
        self.parent_source = parent_source
        self.parent_run_id = str(uuid.uuid4())
        self.parent_job_name = config.name
        self._inputs: list[DatasetRef] = []
        self.started_at = _now()
        self.execution = {
            "_producer": PRODUCER,
            "_schemaURL": EXECUTION_FACET_SCHEMA,
            "startedAt": self.started_at,
        }
        if ctx and ctx.contributor:
            self.execution["contributor"] = ctx.contributor
        scope_file = (ctx.output_dir if ctx else path.parent) / ".lineage" / "workspace-id"
        scope_file.parent.mkdir(parents=True, exist_ok=True)
        try:
            with scope_file.open("x", encoding="utf-8") as scope:
                scope.write(uuid.uuid4().hex)
        except FileExistsError:
            pass
        self.workspace_id = scope_file.read_text(encoding="utf-8").strip()
        if not self.workspace_id or any(c not in "0123456789abcdef" for c in self.workspace_id):
            raise ValueError("Invalid opaque lineage workspace identifier")
        self.dependencies = {}
        self._code = {}
        path.parent.mkdir(parents=True, exist_ok=True)
        self._stream = path.open("w", encoding="utf-8", newline="\n")

    def close(self) -> None:
        self._stream.close()

    def flush(self) -> None:
        self._stream.flush()

    def artifact(self, path: Path, location="staged") -> DatasetRef:
        return DatasetRef(
            f"sentier://local/{self.workspace_id}/{self.config.target.replace('_', '-')}/"
            f"{self.config.category}/{location}/run/{self.parent_run_id}",
            path.name,
        )

    def intermediate(self, name: str) -> DatasetRef:
        return intermediate_dataset(self.config, name, self.parent_run_id)

    def describe_artifact(self, dataset: DatasetRef, path: Path) -> None:
        digest = file_sha256(path)
        dataset.version = f"sha256:{digest}"
        dataset.namespace += f"/sha256/{digest}"
        dataset.description = f"{self.config.category}: {path.stem.replace('-', ' ')}."

    def start_parent(self, inputs: Sequence[DatasetRef]) -> None:
        self._inputs = list(inputs)
        self._write_parent("START", ())

    def complete_parent(self, outputs: Sequence[DatasetRef]) -> None:
        self._write_parent("COMPLETE", outputs)

    def fail_parent(self, error: BaseException) -> None:
        self._write_parent("FAIL", (), error)

    def _write_parent(
        self,
        event_type: str,
        outputs: Sequence[DatasetRef],
        error: BaseException | None = None,
    ) -> None:
        self._write(
            event_type=event_type,
            run_id=self.parent_run_id,
            job_name=self.parent_job_name,
            description=(
                f"Import {self.config.name} into " f"{self.config.target}/{self.config.category}."
            ),
            inputs=self._inputs,
            outputs=outputs,
            source=self.parent_source,
            error=error,
        )

    def run_child(
        self,
        name: str,
        description: str,
        inputs: Sequence[DatasetRef],
        outputs: Sequence[DatasetRef],
        operation: Callable[[], T],
        *,
        source: object | None = None,
        code_dependencies: tuple[str, ...] = (),
        include_source: bool = False,
        capture_outputs: bool = True,
    ) -> T:
        """Run ``operation`` between a child START and terminal event."""
        run_id = str(uuid.uuid4())
        job_name = f"{self.config.name}.{name}"
        source = source or operation
        self._code[job_name] = code_metadata.capture(source, code_dependencies, include_source)
        self._write(
            event_type="START",
            run_id=run_id,
            job_name=job_name,
            description=description,
            inputs=inputs,
            outputs=(),
            source=source,
            child=True,
        )
        try:
            result = operation()
        except BaseException as error:
            self._write(
                event_type="FAIL",
                run_id=run_id,
                job_name=job_name,
                description=description,
                inputs=inputs,
                outputs=(),
                source=source,
                child=True,
                error=error,
            )
            raise
        if capture_outputs:
            for output in outputs:
                output.capture(result)
        self._write(
            event_type="COMPLETE",
            run_id=run_id,
            job_name=job_name,
            description=description,
            inputs=inputs,
            outputs=outputs,
            source=source,
            child=True,
        )
        return result

    def _write(
        self,
        *,
        event_type: str,
        run_id: str,
        job_name: str,
        description: str,
        inputs: Sequence[DatasetRef],
        outputs: Sequence[DatasetRef],
        source: object | None = None,
        child: bool = False,
        error: BaseException | None = None,
    ) -> None:
        run_facets: dict[str, object] = {"sentier_execution": self.execution}
        if child:
            run_facets["parent"] = {
                "_producer": PRODUCER,
                "_schemaURL": PARENT_FACET_SCHEMA,
                "run": {"runId": self.parent_run_id},
                "job": {"namespace": PRODUCER, "name": self.parent_job_name},
            }
        transformation = _transformation_facet(inputs, outputs)
        if transformation is not None:
            run_facets["sentier_transformation"] = transformation
        if error is not None:
            run_facets["errorMessage"] = {
                "_producer": PRODUCER,
                "_schemaURL": ERROR_FACET_SCHEMA,
                "message": f"{type(error).__name__}: step failed; see local execution logs",
                "programmingLanguage": "python",
                "stackTrace": "\n".join(
                    f"{frame.name}: line {frame.lineno}"
                    for frame in traceback.extract_tb(error.__traceback__)
                ),
            }
        job_facets: dict[str, object] = {
            "documentation": {
                "_producer": PRODUCER,
                "_schemaURL": DOCUMENTATION_FACET_SCHEMA,
                "description": description,
            }
        }
        if job_name not in self._code:
            self._code[job_name] = code_metadata.capture(source)
        job_facets.update(self._code[job_name])
        event = {
            "eventTime": _now(),
            "eventType": event_type,
            "producer": PRODUCER,
            "schemaURL": RUN_EVENT_SCHEMA,
            "run": {"runId": run_id, "facets": run_facets},
            "job": {
                "namespace": PRODUCER,
                "name": job_name,
                "facets": job_facets,
            },
            "inputs": [dataset.as_event_value(role="input") for dataset in inputs],
            "outputs": [dataset.as_event_value(role="output") for dataset in outputs],
        }
        self._stream.write(json.dumps(event, separators=(",", ":")) + "\n")
        self._stream.flush()
