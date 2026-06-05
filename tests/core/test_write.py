import orjson
import pyarrow.parquet as pq
import pytest
import yaml
from sentier_importers.core import write as write_mod
from sentier_importers.core.errors import SentierImporterError

ROWS = [{"id": "1", "label": "Alpha"}, {"id": "2", "label": "Beta"}]


def test_write_json_roundtrip(tmp_path):
    out = write_mod.write(ROWS, tmp_path / "x" / "data.json", "json")
    assert out.exists()
    assert orjson.loads(out.read_bytes()) == ROWS


def test_write_yaml_roundtrip(tmp_path):
    out = write_mod.write(ROWS, tmp_path / "data.yaml", "yaml")
    assert yaml.safe_load(out.read_text()) == ROWS


def test_write_parquet_roundtrip(tmp_path):
    out = write_mod.write(ROWS, tmp_path / "data.parquet", "parquet")
    table = pq.read_table(out)
    assert table.to_pylist() == ROWS


def test_write_creates_parent_dirs(tmp_path):
    out = write_mod.write(ROWS, tmp_path / "a" / "b" / "c.json", "json")
    assert out.exists()


def test_unknown_format_raises(tmp_path):
    with pytest.raises(SentierImporterError):
        write_mod.write(ROWS, tmp_path / "data.xml", "xml")


COLLECTION = {"scheme": "https://vocab.sentier.dev/products/", "products": ROWS}


def test_write_yaml_accepts_mapping(tmp_path):
    out = write_mod.write(COLLECTION, tmp_path / "data.yaml", "yaml")
    loaded = yaml.safe_load(out.read_text())
    assert isinstance(loaded, dict)
    assert loaded == COLLECTION
    assert loaded["scheme"] == "https://vocab.sentier.dev/products/"


def test_write_json_accepts_mapping(tmp_path):
    out = write_mod.write(COLLECTION, tmp_path / "data.json", "json")
    assert orjson.loads(out.read_bytes()) == COLLECTION
