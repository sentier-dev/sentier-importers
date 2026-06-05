"""Registry of input parsers keyed by format. RDF/TTL parsing lives here, on the
input side — the framework reads external TTL but never emits it."""

import csv
import datetime
import io
from collections.abc import Callable
from typing import Any

import openpyxl
import orjson
import yaml
from rdflib import Graph
from sentier_importers.core.errors import ParseError
from sentier_importers.core.types import RawData, Records

ParserFn = Callable[[RawData], Records]

_PARSERS: dict[str, ParserFn] = {}


def register_parser(fmt: str, fn: ParserFn) -> None:
    """Register (or override) a parser for an input ``fmt`` string."""
    _PARSERS[fmt] = fn


def parse(raw: RawData, fmt: str) -> Records:
    """Parse ``raw`` bytes into records using the parser registered for ``fmt``."""
    try:
        parser = _PARSERS[fmt]
    except KeyError:
        raise ParseError(f"no parser registered for format {fmt!r}") from None
    return parser(raw)


def _parse_csv(raw: RawData) -> Records:
    reader = csv.DictReader(io.StringIO(raw.content.decode("utf-8")))
    return [dict(row) for row in reader]


def _parse_json(raw: RawData) -> Records:
    data = orjson.loads(raw.content)
    if isinstance(data, dict):
        return [data]
    if isinstance(data, list):
        return data
    raise ParseError("JSON root must be an object or array")


def _parse_yaml(raw: RawData) -> Records:
    data = yaml.safe_load(raw.content)
    if isinstance(data, dict):
        return [data]
    if isinstance(data, list):
        return data
    raise ParseError("YAML root must be a mapping or sequence")


def _parse_ttl(raw: RawData) -> Records:
    graph = Graph()
    graph.parse(data=raw.content, format="turtle")
    return [{"subject": str(s), "predicate": str(p), "object": str(o)} for s, p, o in graph]


def _normalize_cell(value: Any) -> Any:
    """Make a cell value JSON-serializable; dates become ISO-8601 strings."""
    if isinstance(value, (datetime.datetime, datetime.date)):
        return value.isoformat()
    return value


def _parse_xlsx(raw: RawData) -> Records:
    """Parse the active worksheet of an OOXML workbook into records.

    The first non-empty row is the header; each later non-empty row becomes one
    record. ``None`` cells are omitted (not emitted as empty strings) and
    ``date``/``datetime`` cells are normalized to ISO-8601 strings. Sources with
    multi-sheet or multi-row-header layouts override ``parse()`` in their plugin.
    """
    workbook = openpyxl.load_workbook(io.BytesIO(raw.content), read_only=True, data_only=True)
    worksheet = workbook.active
    if worksheet is None:
        raise ParseError("workbook has no active worksheet")

    rows = worksheet.iter_rows(values_only=True)
    header: list[str] | None = None
    for cells in rows:
        if all(cell is None for cell in cells):
            continue  # skip leading blank rows when locating the header
        header = [str(cell).strip() for cell in cells]
        break
    if header is None:
        raise ParseError("workbook has no header row")
    if len(set(header)) != len(header):
        raise ParseError(f"duplicate header names: {header}")

    records: Records = []
    for cells in rows:
        if all(cell is None for cell in cells):
            continue
        record = {
            key: _normalize_cell(cell) for key, cell in zip(header, cells) if cell is not None
        }
        records.append(record)
    return records


register_parser("csv", _parse_csv)
register_parser("json", _parse_json)
register_parser("yaml", _parse_yaml)
register_parser("ttl", _parse_ttl)
register_parser("rdf", _parse_ttl)
register_parser("xlsx", _parse_xlsx)
register_parser("xls", _parse_xlsx)
