"""Shape rows for sentier-vocab's two parquet layers and write them.

A vocab source's ``transform`` returns one flat row per term, with the slots of its
term type (``compartment``, ``broader``, ``impact_categories``, …). ``to_layers``
splits such rows into the two shapes sentier-vocab stores:

- term rows: the common columns, ``type``, and the literal slots as ``attributes``;
- relation rows: one ``subject, predicate, object`` row per IRI-valued slot.

``write_layers`` writes one release as ``terms/<release>[-NN].parquet`` and
``relations/<release>.parquet``, with the Arrow schemas, row order and shard rule of
sentier-vocab's own ``layers.py`` so a regenerated release diffs cleanly there.
"""

import io
import json
import math
import re
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
from sentier_importers.core.errors import ValidationError
from sentier_importers.core.types import Record, Rows
from sentier_importers.core.vocab_contract import Contract

TERMS_DIR = "terms"
RELATIONS_DIR = "relations"
#: Keeps every delivered file under sentier-vocab's pre-commit size guard (3000 KB).
MAX_FILE_BYTES = 2_400_000
COMPRESSION = "snappy"
DEFAULT_STATUS = "draft"
#: ``term_type`` of a source whose rows are of several types: each row names its own
#: in a ``type`` slot (the hand-curated sentier-core seed).
ROW_TYPE = "per-row"

TERM_SCHEMA = pa.schema(
    [
        ("iri", pa.string()),
        ("type", pa.string()),
        ("pref_label", pa.string()),
        ("alt_labels", pa.list_(pa.string())),
        ("definition", pa.string()),
        ("notation", pa.string()),
        ("additional_notations", pa.list_(pa.string())),
        ("source", pa.string()),
        ("status", pa.string()),
        ("attributes", pa.string()),
    ]
)

RELATION_SCHEMA = pa.schema(
    [
        ("subject", pa.string()),
        ("predicate", pa.string()),
        ("object", pa.string()),
        ("source", pa.string()),
        ("status", pa.string()),
    ]
)

#: Slots every term type may carry whose value is another IRI.
COMMON_RELATION_SLOTS = {
    "broader": "skos:broader",
    "related": "skos:related",
    "exact_match": "skos:exactMatch",
    "close_match": "skos:closeMatch",
}

#: IRI-valued slots of one term type, with the predicate sentier-vocab stores them under.
TYPE_RELATION_SLOTS: dict[str, dict[str, str]] = {
    "elementary-flow": {"unit": "qudt:hasUnit"},
    "flow-property": {
        "default_unit_group": "ont:defaultUnitGroup",
        "default_unit": "qudt:hasUnit",
    },
    "impact-category": {"reference_unit": "qudt:hasUnit", "method": "ont:method"},
    "lcia-method": {"impact_categories": "ont:impactCategory"},
    "model-term": {"quantity_kind": "qudt:hasQuantityKind", "unit": "qudt:hasUnit"},
    "process": {"geography": "ont:geography"},
    "product": {"reference_flow_property": "ont:referenceFlowProperty"},
    "unit-group": {
        "reference_unit": "qudt:hasUnit",
        "units": "ont:hasUnit",
        "quantity_kind": "qudt:hasQuantityKind",
    },
}

_SHARD_SUFFIX = re.compile(r"-\d{2}$")


def release_of(filename: str | Path) -> str:
    """``ef-3.1-03.parquet`` -> ``ef-3.1``; ``bafu-2026.parquet`` -> ``bafu-2026``."""
    return _SHARD_SUFFIX.sub("", Path(filename).stem)


def _present(value: object) -> bool:
    return value is not None and value != [] and value != ""


def _as_list(value: object) -> list:
    return list(value) if isinstance(value, (list, tuple)) else [value]


def _split(row: Record, term_type: str, source: str, contract: Contract) -> tuple[Record, Rows]:
    declared = contract.types[term_type].attributes
    relation_slots = {**COMMON_RELATION_SLOTS, **TYPE_RELATION_SLOTS.get(term_type, {})}
    present = {slot: value for slot, value in row.items() if _present(value)}
    term_source = present.get("source", source)
    status = present.get("status", DEFAULT_STATUS)

    term: Record = {}
    attributes: Record = {}
    relations: Rows = []
    for slot, value in present.items():
        if slot in TERM_SCHEMA.names:
            term[slot] = value
        elif slot in declared:
            attributes[slot] = value
        elif slot in relation_slots:
            relations.extend(
                {
                    "subject": row.get("iri"),
                    "predicate": relation_slots[slot],
                    "object": obj,
                    "source": term_source,
                    "status": status,
                }
                for obj in _as_list(value)
            )
        else:
            raise ValidationError(
                f"{row.get('iri')}: slot {slot!r} is not a term column, not an attribute "
                f"declared for type {term_type!r} and not a relation slot"
            )
    term.update({"type": term_type, "source": term_source, "status": status})
    if attributes:
        term["attributes"] = attributes
    return term, relations


def _type_of(row: Record, term_type: str, contract: Contract) -> str:
    """The term type of ``row``: the source's one type, or the row's own for ``ROW_TYPE``."""
    own = row.get("type")
    if term_type != ROW_TYPE:
        if own not in (None, term_type):
            raise ValidationError(
                f"{row.get('iri')}: row sets 'type' {own!r} but its source emits {term_type!r}"
            )
        return term_type
    if own is None:
        raise ValidationError(f"{row.get('iri')}: row of a mixed-type source names no type")
    if own not in contract.types:
        raise ValidationError(f"{row.get('iri')}: type {own!r} is not listed in the term contract")
    return own


def to_layers(
    rows: Rows, *, term_type: str, release: str, contract: Contract
) -> tuple[Rows, Rows]:
    """Split a source's flat rows into ``(term rows, relation rows)`` for ``release``.

    Every row gets the release's source term as ``source`` unless it names one.
    ``term_type`` is the type of every row, or ``ROW_TYPE`` when each row carries its
    own in a ``type`` slot.
    """
    if term_type != ROW_TYPE and term_type not in contract.types:
        raise ValidationError(f"term type {term_type!r} is not listed in the vocab term contract")
    source = contract.sources_namespace + release
    terms: Rows = []
    relations: Rows = []
    for row in rows:
        term, edges = _split(row, _type_of(row, term_type, contract), source, contract)
        terms.append(term)
        relations.extend(edges)
    return terms, relations


def _check_columns(rows: Rows, schema: pa.Schema) -> None:
    allowed = set(schema.names)
    for row in rows:
        unknown = set(row) - allowed
        if unknown:
            key = row.get("iri") or row.get("subject")
            raise ValidationError(f"row {key!r}: unknown columns {sorted(unknown)}")


def _term_record(row: Record) -> Record:
    record = {name: row.get(name) for name in TERM_SCHEMA.names}
    attributes = row.get("attributes")
    record["attributes"] = (
        json.dumps(attributes, sort_keys=True, ensure_ascii=False) if attributes else None
    )
    return record


def _write_table(records: Rows, schema: pa.Schema, path: Path) -> Path:
    pq.write_table(pa.Table.from_pylist(records, schema=schema), path, compression=COMPRESSION)
    return path


def _write_sharded(records: Rows, schema: pa.Schema, directory: Path, release: str) -> list[Path]:
    directory.mkdir(parents=True, exist_ok=True)
    for stale in directory.glob("*.parquet"):
        if release_of(stale) == release:
            stale.unlink()
    buffer = io.BytesIO()
    pq.write_table(pa.Table.from_pylist(records, schema=schema), buffer, compression=COMPRESSION)
    if buffer.tell() <= MAX_FILE_BYTES:
        return [_write_table(records, schema, directory / f"{release}.parquet")]
    shards = max(2, math.ceil(buffer.tell() / MAX_FILE_BYTES))
    while True:
        size = math.ceil(len(records) / shards)
        paths = [
            _write_table(
                records[index * size : (index + 1) * size],
                schema,
                directory / f"{release}-{index + 1:02d}.parquet",
            )
            for index in range(shards)
        ]
        if all(path.stat().st_size <= MAX_FILE_BYTES for path in paths):
            return paths
        for path in paths:
            path.unlink()
        shards += 1


def write_layers(terms: Rows, relations: Rows, root: Path, release: str) -> dict[str, list[Path]]:
    """Write one release under ``root`` and return ``{layer folder: files}``.

    Terms are sorted by ``(type, iri)``, relations by ``(subject, predicate, object)``.
    Files of an earlier run for the same release are removed first; a release without
    relations has no relation file.
    """
    _check_columns(terms, TERM_SCHEMA)
    _check_columns(relations, RELATION_SCHEMA)
    term_records = [_term_record(r) for r in sorted(terms, key=lambda r: (r["type"], r["iri"]))]
    edges = sorted(relations, key=lambda r: (r["subject"], r["predicate"], r["object"]))
    edge_records = [{name: r.get(name) for name in RELATION_SCHEMA.names} for r in edges]
    written = {
        TERMS_DIR: _write_sharded(term_records, TERM_SCHEMA, Path(root) / TERMS_DIR, release),
        RELATIONS_DIR: [],
    }
    if edge_records:
        written[RELATIONS_DIR] = _write_sharded(
            edge_records, RELATION_SCHEMA, Path(root) / RELATIONS_DIR, release
        )
    return written


def read_iris(terms_dir: Path, *, skip_release: str) -> set[str]:
    """IRIs of every term file in ``terms_dir`` that belongs to another release."""
    iris: set[str] = set()
    for path in sorted(Path(terms_dir).glob("*.parquet")):
        if release_of(path) != skip_release:
            iris.update(pq.read_table(path, columns=["iri"]).column("iri").to_pylist())
    return iris
