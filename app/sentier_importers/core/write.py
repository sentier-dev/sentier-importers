"""Registry of output writers: json | yaml | parquet. The framework never emits
TTL — RDF is an input concern only.

The Parquet writer serves the bulk-data targets (inventory, methods): rows are sorted by
``notation`` (fallback ``iri``) for content-stability, and the Arrow schema is inferred
from *all* rows, because ``from_pylist`` infers from the first row and silently drops
optional columns absent there. sentier-vocab deliveries do not go through here: their
two layers have fixed schemas, see :mod:`sentier_importers.core.vocab_layers`.
"""

from collections.abc import Callable, Mapping
from pathlib import Path

import orjson
import pyarrow as pa
import pyarrow.parquet as pq
import yaml
from sentier_importers.core.errors import SentierImporterError
from sentier_importers.core.types import Payload, Rows

# (payload, path) -> path.
WriterFn = Callable[[Payload, Path], Path]

_WRITERS: dict[str, WriterFn] = {}

# Canonical file extension per output format.
EXTENSIONS: dict[str, str] = {"json": ".json", "yaml": ".yaml", "parquet": ".parquet"}


def register_writer(fmt: str, fn: WriterFn) -> None:
    """Register (or override) a writer for an output ``fmt`` string."""
    _WRITERS[fmt] = fn


def write(payload: Payload, path: Path, fmt: str) -> Path:
    """Write ``payload`` to ``path`` using the writer registered for ``fmt``.

    ``payload`` is a list of rows or a single mapping (a randonneur package). The
    ``json``/``yaml`` writers serialize either; ``parquet`` writes a list of rows as a
    table. Creates parent directories. Returns the path.
    """
    try:
        writer = _WRITERS[fmt]
    except KeyError:
        raise SentierImporterError(f"no writer registered for format {fmt!r}") from None
    path.parent.mkdir(parents=True, exist_ok=True)
    return writer(payload, path)


def _infer_arrow_schema(rows: Rows) -> pa.Schema:
    """Arrow schema of a list of rows.

    Scans *all* rows (not just the first) for the key union and infers each column's type
    from its observed values, so sparse optional columns are not dropped and numeric
    columns (e.g. ``factor_value``) are not forced to string. Precedence per column:
    list > string > float > int > bool (a column seen as both int and float becomes float;
    any string presence wins, since strings can't be coerced to numbers safely).
    """
    order: list[str] = []
    kinds: dict[str, set[str]] = {}
    for row in rows:
        for key, value in row.items():
            if key not in order:
                order.append(key)
                kinds[key] = set()
            if value is None:
                continue
            if isinstance(value, list):
                kinds[key].add("list")
            elif isinstance(value, bool):  # bool before int (bool is a subclass of int)
                kinds[key].add("bool")
            elif isinstance(value, int):
                kinds[key].add("int")
            elif isinstance(value, float):
                kinds[key].add("float")
            else:
                kinds[key].add("str")

    def arrow_type(seen: set[str]) -> "pa.DataType":
        if "list" in seen:
            return pa.list_(pa.string())
        if "str" in seen or not seen:
            return pa.string()
        if "float" in seen:
            return pa.float64()
        if "int" in seen:
            return pa.int64()
        return pa.bool_()

    return pa.schema([(k, arrow_type(kinds[k])) for k in order])


def _write_json(payload: Payload, path: Path) -> Path:
    path.write_bytes(orjson.dumps(payload, option=orjson.OPT_INDENT_2))
    return path


def _write_yaml(payload: Payload, path: Path) -> Path:
    # ``safe_dump`` needs a plain dict, not an arbitrary Mapping subclass.
    data = dict(payload) if isinstance(payload, Mapping) else payload
    path.write_text(yaml.safe_dump(data, sort_keys=True, allow_unicode=True), encoding="utf-8")
    return path


def _write_parquet(payload: Payload, path: Path) -> Path:
    if isinstance(payload, Mapping):
        raise SentierImporterError("the parquet writer takes a list of rows, not a mapping")
    rows = sorted(payload, key=lambda r: r.get("notation") or r.get("iri") or "")
    table = pa.Table.from_pylist(rows, schema=_infer_arrow_schema(rows))
    pq.write_table(table, path, compression="snappy")
    return path


register_writer("json", _write_json)
register_writer("yaml", _write_yaml)
register_writer("parquet", _write_parquet)
