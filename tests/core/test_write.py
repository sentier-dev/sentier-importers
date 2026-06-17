import orjson
import pyarrow as pa
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


def test_write_parquet_collection_stores_scheme_in_metadata(tmp_path):
    out = write_mod.write(COLLECTION, tmp_path / "c.parquet", "parquet")
    table = pq.read_table(out)
    assert table.schema.metadata[b"scheme"] == b"https://vocab.sentier.dev/products/"
    assert {r["id"] for r in table.to_pylist()} == {"1", "2"}


def test_write_parquet_sorts_rows_by_notation(tmp_path):
    rows = [{"iri": "x/B", "notation": "B"}, {"iri": "x/A", "notation": "A"}]
    out = write_mod.write({"scheme": "x/", "items": rows}, tmp_path / "s.parquet", "parquet")
    assert [r["notation"] for r in pq.read_table(out).to_pylist()] == ["A", "B"]


def test_explicit_arrow_schema_preserves_optional_column_absent_in_first_row(tmp_path):
    # first row lacks alt_labels -> from_pylist inference would drop the column entirely
    rows = [
        {"iri": "x/1", "notation": "1", "pref_label": "a"},
        {"iri": "x/2", "notation": "2", "pref_label": "b", "alt_labels": ["b2"]},
    ]
    schema = pa.schema(
        [
            ("iri", pa.string()),
            ("notation", pa.string()),
            ("pref_label", pa.string()),
            ("alt_labels", pa.list_(pa.string())),
        ]
    )
    out = write_mod.write(
        {"scheme": "x/", "items": rows}, tmp_path / "e.parquet", "parquet", schema
    )
    back = {r["iri"]: r for r in pq.read_table(out).to_pylist()}
    assert back["x/2"]["alt_labels"] == ["b2"]
    assert "alt_labels" in pq.read_table(out).column_names


def test_infer_arrow_schema_detects_list_columns_across_all_rows():
    rows = [{"iri": "x/1"}, {"iri": "x/2", "related": ["x/1"]}]
    schema = write_mod._infer_arrow_schema(rows)
    assert dict(zip(schema.names, schema.types))["related"] == pa.list_(pa.string())


def test_arrow_schema_for_reads_linkml_slots(tmp_path):
    schema_file = tmp_path / "mini.yaml"
    schema_file.write_text(
        "id: https://example.org/mini\n"
        "name: mini\n"
        "prefixes: {linkml: https://w3id.org/linkml/}\n"
        "default_range: string\n"
        "imports: [linkml:types]\n"
        "classes:\n"
        "  Item:\n"
        "    attributes:\n"
        "      iri: {identifier: true}\n"
        "      pref_label: {}\n"
        "      alt_labels: {multivalued: true}\n"
    )
    schema = write_mod.arrow_schema_for(schema_file, "Item")
    fields = dict(zip(schema.names, schema.types))
    assert fields["pref_label"] == pa.string()
    assert fields["alt_labels"] == pa.list_(pa.string())
