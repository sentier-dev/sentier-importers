"""Registry of output writers: json | yaml | parquet. The framework never emits
TTL — RDF is an input concern only."""

from collections.abc import Callable
from pathlib import Path

import orjson
import pyarrow as pa
import pyarrow.parquet as pq
import yaml

from sentier_importers.core.errors import SentierImporterError
from sentier_importers.core.types import Rows

WriterFn = Callable[[Rows, Path], Path]

_WRITERS: dict[str, WriterFn] = {}

# Canonical file extension per output format.
EXTENSIONS: dict[str, str] = {"json": ".json", "yaml": ".yaml", "parquet": ".parquet"}


def register_writer(fmt: str, fn: WriterFn) -> None:
    """Register (or override) a writer for an output ``fmt`` string."""
    _WRITERS[fmt] = fn


def write(rows: Rows, path: Path, fmt: str) -> Path:
    """Write ``rows`` to ``path`` using the writer registered for ``fmt``.

    Creates parent directories as needed. Returns the written path.
    """
    try:
        writer = _WRITERS[fmt]
    except KeyError:
        raise SentierImporterError(f"no writer registered for format {fmt!r}") from None
    path.parent.mkdir(parents=True, exist_ok=True)
    return writer(rows, path)


def _write_json(rows: Rows, path: Path) -> Path:
    path.write_bytes(orjson.dumps(rows, option=orjson.OPT_INDENT_2))
    return path


def _write_yaml(rows: Rows, path: Path) -> Path:
    path.write_text(yaml.safe_dump(rows, sort_keys=True, allow_unicode=True), encoding="utf-8")
    return path


def _write_parquet(rows: Rows, path: Path) -> Path:
    pq.write_table(pa.Table.from_pylist(rows), path)
    return path


register_writer("json", _write_json)
register_writer("yaml", _write_yaml)
register_writer("parquet", _write_parquet)
