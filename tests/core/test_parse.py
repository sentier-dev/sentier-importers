import pytest

from sentier_importers.core import parse as parse_mod
from sentier_importers.core.errors import ParseError
from sentier_importers.core.types import RawData


def _raw(text: str) -> RawData:
    return RawData(content=text.encode("utf-8"), source_url="file:///x")


def test_parse_csv():
    records = parse_mod.parse(_raw("id,name\n1,alpha\n2,beta\n"), "csv")
    assert records == [{"id": "1", "name": "alpha"}, {"id": "2", "name": "beta"}]


def test_parse_json_array():
    records = parse_mod.parse(_raw('[{"a": 1}, {"a": 2}]'), "json")
    assert records == [{"a": 1}, {"a": 2}]


def test_parse_json_object_wrapped():
    records = parse_mod.parse(_raw('{"a": 1}'), "json")
    assert records == [{"a": 1}]


def test_parse_yaml():
    records = parse_mod.parse(_raw("- a: 1\n- a: 2\n"), "yaml")
    assert records == [{"a": 1}, {"a": 2}]


def test_parse_ttl():
    ttl = "@prefix ex: <http://example.org/> .\n" "ex:s ex:p ex:o .\n"
    records = parse_mod.parse(_raw(ttl), "ttl")
    assert records == [
        {
            "subject": "http://example.org/s",
            "predicate": "http://example.org/p",
            "object": "http://example.org/o",
        }
    ]


def test_unknown_format_raises():
    with pytest.raises(ParseError):
        parse_mod.parse(_raw("x"), "toml")


def test_register_parser_extends_registry():
    parse_mod.register_parser("upper", lambda raw: [{"v": raw.content.decode().upper()}])
    assert parse_mod.parse(_raw("hi"), "upper") == [{"v": "HI"}]
