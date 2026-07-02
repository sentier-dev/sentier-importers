"""Registry of output writers: json | yaml | parquet. The framework never emits
TTL — RDF is an input concern only.

The Parquet writer is the bulk delivery format for large vocab imports (e.g. FoodEx2);
see the cross-repo spec docs/specs/2026-06-17-foodex2-parquet-delivery.md. Its contract:
columns are the item's snake_case slots (scalar -> string, multivalued -> list<string>);
``scheme`` is stored in Arrow key-value metadata, not a column; rows are sorted by
``notation`` (fallback ``iri``) for content-stability. The Arrow schema is explicit —
``from_pylist`` infers from the first row and silently drops optional columns absent there.
"""

from collections.abc import Callable, Mapping
from pathlib import Path

import orjson
import pyarrow as pa
import pyarrow.parquet as pq
import yaml
from linkml_runtime import SchemaView
from sentier_importers.core.errors import SentierImporterError
from sentier_importers.core.types import Payload, Rows

# (payload, path, arrow_schema) -> path. ``arrow_schema`` is only consulted by the
# parquet writer; json/yaml ignore it.
WriterFn = Callable[[Payload, Path, "pa.Schema | None"], Path]

_WRITERS: dict[str, WriterFn] = {}

# Canonical file extension per output format.
EXTENSIONS: dict[str, str] = {"json": ".json", "yaml": ".yaml", "parquet": ".parquet"}

_SCHEME_METADATA_KEY = "scheme"


def register_writer(fmt: str, fn: WriterFn) -> None:
    """Register (or override) a writer for an output ``fmt`` string."""
    _WRITERS[fmt] = fn


def write(payload: Payload, path: Path, fmt: str, arrow_schema: "pa.Schema | None" = None) -> Path:
    """Write ``payload`` to ``path`` using the writer registered for ``fmt``.

    ``payload`` is a list of rows or a single assembled collection mapping. The
    ``json``/``yaml`` writers serialize either. ``parquet`` writes the items as a table
    with ``scheme`` in metadata; pass ``arrow_schema`` (from :func:`arrow_schema_for`) so
    optional columns are never dropped. Creates parent directories. Returns the path.
    """
    try:
        writer = _WRITERS[fmt]
    except KeyError:
        raise SentierImporterError(f"no writer registered for format {fmt!r}") from None
    path.parent.mkdir(parents=True, exist_ok=True)
    return writer(payload, path, arrow_schema)


def arrow_schema_for(schema_path: Path | str, item_class: str) -> pa.Schema:
    """Build an explicit Arrow schema for ``item_class`` from its LinkML slots.

    Scalar slot -> ``string``; ``multivalued`` slot -> ``list<string>``. Column order is
    the schema's slot order. The collection ``scheme`` is not an item slot, so it never
    appears here (it is written as file metadata instead).
    """
    sv = SchemaView(str(schema_path))
    fields = []
    for slot_name in sv.class_slots(item_class):
        slot = sv.induced_slot(slot_name, item_class)
        fields.append((slot_name, pa.list_(pa.string()) if slot.multivalued else pa.string()))
    return pa.schema(fields)


def _infer_arrow_schema(rows: Rows) -> pa.Schema:
    """Fallback schema when no LinkML schema is available (bulk non-vocab targets).

    Scans *all* rows (not just the first) for the key union and detects list columns, so
    sparse optional columns are not dropped the way ``from_pylist`` inference would.
    """
    list_keys: set[str] = set()
    order: list[str] = []
    for row in rows:
        for key, value in row.items():
            if key not in order:
                order.append(key)
            if isinstance(value, list):
                list_keys.add(key)
    return pa.schema(
        [(k, pa.list_(pa.string()) if k in list_keys else pa.string()) for k in order]
    )


def _split_collection(payload: Payload) -> tuple[str | None, Rows]:
    """Return ``(scheme, rows)`` from a collection mapping, or ``(None, rows)`` for a list."""
    if isinstance(payload, Mapping):
        scheme = payload.get(_SCHEME_METADATA_KEY)
        rows = next((v for v in payload.values() if isinstance(v, list)), [])
        return scheme, rows
    return None, payload


def _write_json(payload: Payload, path: Path, arrow_schema: "pa.Schema | None" = None) -> Path:
    path.write_bytes(orjson.dumps(payload, option=orjson.OPT_INDENT_2))
    return path


def _write_yaml(payload: Payload, path: Path, arrow_schema: "pa.Schema | None" = None) -> Path:
    # ``safe_dump`` needs a plain dict, not an arbitrary Mapping subclass.
    data = dict(payload) if isinstance(payload, Mapping) else payload
    path.write_text(yaml.safe_dump(data, sort_keys=True, allow_unicode=True), encoding="utf-8")
    return path


def _write_parquet(payload: Payload, path: Path, arrow_schema: "pa.Schema | None" = None) -> Path:
    scheme, rows = _split_collection(payload)
    rows = sorted(rows, key=lambda r: r.get("notation") or r.get("iri") or "")
    schema = arrow_schema if arrow_schema is not None else _infer_arrow_schema(rows)
    table = pa.Table.from_pylist(rows, schema=schema)
    if scheme is not None:
        table = table.replace_schema_metadata({_SCHEME_METADATA_KEY: scheme})
    pq.write_table(table, path, compression="snappy")
    return path


register_writer("json", _write_json)
register_writer("yaml", _write_yaml)
register_writer("parquet", _write_parquet)
