"""Registry of input parsers keyed by format. RDF/TTL parsing lives here, on the
input side — the framework reads external TTL but never emits it."""

import csv
import io
from collections.abc import Callable

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


register_parser("csv", _parse_csv)
register_parser("json", _parse_json)
register_parser("yaml", _parse_yaml)
register_parser("ttl", _parse_ttl)
register_parser("rdf", _parse_ttl)
