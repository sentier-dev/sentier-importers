import json
from pathlib import Path

import pyarrow.parquet as pq
import pytest
from sentier_importers.core import vocab_contract, vocab_layers
from sentier_importers.core.errors import ValidationError

FIXTURES = Path(__file__).parent / "fixtures" / "vocab"
FLOWS = "https://vocab.sentier.dev/flows/"
SOURCE = "https://vocab.sentier.dev/sources/demo-1"


@pytest.fixture(scope="module")
def contract():
    return vocab_contract.load_contract(FIXTURES / "term.yaml", FIXTURES / "relation.yaml")


def _layers(rows, term_type, contract):
    return vocab_layers.to_layers(rows, term_type=term_type, release="demo-1", contract=contract)


def test_literal_slots_become_attributes(contract):
    rows = [
        {
            "iri": FLOWS + "a",
            "pref_label": "Carbon dioxide",
            "alt_labels": ["CO2"],
            "notation": "a",
            "compartment": "air",
            "cas_number": "000124-38-9",
            "source": SOURCE,
            "status": "draft",
        }
    ]
    terms, relations = _layers(rows, "elementary-flow", contract)
    assert terms == [
        {
            "iri": FLOWS + "a",
            "type": "elementary-flow",
            "pref_label": "Carbon dioxide",
            "alt_labels": ["CO2"],
            "notation": "a",
            "source": SOURCE,
            "status": "draft",
            "attributes": {"compartment": "air", "cas_number": "000124-38-9"},
        }
    ]
    assert relations == []


def test_iri_valued_slots_become_relations(contract):
    method = "https://vocab.sentier.dev/lcia-methods/ef-3.1"
    category = "https://vocab.sentier.dev/impact-categories/ef-3.1/acidification"
    rows = [
        {
            "iri": method,
            "pref_label": "EF 3.1",
            "methodology": "EF",
            "impact_categories": [category],
            "source": SOURCE,
            "status": "draft",
        }
    ]
    terms, relations = _layers(rows, "lcia-method", contract)
    assert terms[0]["attributes"] == {"methodology": "EF"}
    assert "impact_categories" not in terms[0]
    assert relations == [
        {
            "subject": method,
            "predicate": "ont:impactCategory",
            "object": category,
            "source": SOURCE,
            "status": "draft",
        }
    ]


def test_scalar_and_list_relation_slots(contract):
    product = "https://vocab.sentier.dev/products/"
    rows = [
        {"iri": product + "a", "pref_label": "a", "broader": product + "g", "related": []},
        {"iri": product + "b", "pref_label": "b", "related": [product + "a", product + "g"]},
    ]
    terms, relations = _layers(rows, "product", contract)
    assert [(r["subject"], r["predicate"], r["object"]) for r in relations] == [
        (product + "a", "skos:broader", product + "g"),
        (product + "b", "skos:related", product + "a"),
        (product + "b", "skos:related", product + "g"),
    ]
    assert all(set(t) <= set(vocab_layers.TERM_SCHEMA.names) for t in terms)


def test_missing_source_and_status_default_to_the_release(contract):
    terms, _ = _layers([{"iri": FLOWS + "a", "pref_label": "a"}], "elementary-flow", contract)
    assert terms[0]["source"] == SOURCE
    assert terms[0]["status"] == "draft"


def test_a_source_term_is_delivered_by_its_own_release(contract):
    rows = [{"iri": SOURCE, "pref_label": "Demo", "title": "Demo", "status": "published"}]
    terms, _ = _layers(rows, "source", contract)
    assert terms[0]["source"] == SOURCE
    assert terms[0]["attributes"] == {"title": "Demo"}


def test_another_source_term_in_a_release_is_delivered_by_that_release(contract):
    other = "https://vocab.sentier.dev/sources/ipcc-ar6"
    terms, _ = _layers([{"iri": other, "pref_label": "IPCC AR6"}], "source", contract)
    assert terms[0]["source"] == SOURCE


def test_rows_name_their_own_type_when_the_source_mixes_types(contract):
    rows = [
        {"type": "unit", "iri": "https://vocab.sentier.dev/units/unit/M2-YR", "pref_label": "m2a"},
        {
            "type": "unit-group",
            "iri": "https://vocab.sentier.dev/units/group/mass",
            "pref_label": "Units of mass",
            "reference_unit": "http://qudt.org/vocab/unit/KiloGM",
            "units": ["http://qudt.org/vocab/unit/GM", "http://qudt.org/vocab/unit/KiloGM"],
            "quantity_kind": "http://qudt.org/vocab/quantitykind/Mass",
        },
    ]
    terms, relations = _layers(rows, vocab_layers.ROW_TYPE, contract)
    assert [t["type"] for t in terms] == ["unit", "unit-group"]
    assert sorted((r["predicate"], r["object"].rsplit("/", 1)[-1]) for r in relations) == [
        ("ont:hasUnit", "GM"),
        ("ont:hasUnit", "KiloGM"),
        ("qudt:hasQuantityKind", "Mass"),
        ("qudt:hasUnit", "KiloGM"),
    ]
    assert vocab_contract.check(terms, relations, contract, release="demo-1", complete=False) == []


def test_a_mixed_source_row_without_a_type_is_an_error(contract):
    with pytest.raises(ValidationError, match="names no type"):
        _layers([{"iri": FLOWS + "a", "pref_label": "a"}], vocab_layers.ROW_TYPE, contract)


def test_a_row_type_is_refused_when_the_source_has_one_type(contract):
    rows = [{"type": "process", "iri": FLOWS + "a", "pref_label": "a"}]
    with pytest.raises(ValidationError, match="'type'"):
        _layers(rows, "elementary-flow", contract)


def test_flow_unit_becomes_a_unit_relation(contract):
    rows = [{"iri": FLOWS + "a", "pref_label": "a", "unit": "http://qudt.org/vocab/unit/KiloGM"}]
    terms, relations = _layers(rows, "elementary-flow", contract)
    assert "unit" not in terms[0]
    assert [(r["predicate"], r["object"]) for r in relations] == [
        ("qudt:hasUnit", "http://qudt.org/vocab/unit/KiloGM")
    ]


def test_null_and_empty_values_are_dropped(contract):
    rows = [{"iri": FLOWS + "a", "pref_label": "a", "definition": None, "cas_number": None}]
    terms, _ = _layers(rows, "elementary-flow", contract)
    assert "definition" not in terms[0] and "attributes" not in terms[0]


def test_unknown_slot_is_an_error(contract):
    rows = [{"iri": FLOWS + "a", "pref_label": "a", "colour": "red"}]
    with pytest.raises(ValidationError, match="'colour'"):
        _layers(rows, "elementary-flow", contract)


def test_unknown_term_type_is_an_error(contract):
    with pytest.raises(ValidationError, match="'widget'"):
        _layers([], "widget", contract)


def test_release_of_strips_the_shard_suffix():
    assert vocab_layers.release_of("ef-3.1-03.parquet") == "ef-3.1"
    assert vocab_layers.release_of(Path("terms/bafu-2026.parquet")) == "bafu-2026"


def _term(i, **extra):
    row = {
        "iri": f"{FLOWS}{i:06d}",
        "type": "elementary-flow",
        "pref_label": f"flow {i}",
        "source": SOURCE,
        "status": "draft",
    }
    row.update(extra)
    return row


def test_write_layers_uses_the_vocab_file_layout(tmp_path):
    terms = [_term(2, attributes={"compartment": "air"}), _term(1, alt_labels=["one"])]
    relations = [
        {
            "subject": terms[0]["iri"],
            "predicate": "skos:related",
            "object": terms[1]["iri"],
            "source": SOURCE,
            "status": "draft",
        }
    ]
    written = vocab_layers.write_layers(terms, relations, tmp_path, "demo-1")
    assert written == {
        "terms": [tmp_path / "terms" / "demo-1.parquet"],
        "relations": [tmp_path / "relations" / "demo-1.parquet"],
    }
    table = pq.read_table(written["terms"][0])
    assert table.schema.names == vocab_layers.TERM_SCHEMA.names
    rows = table.to_pylist()
    assert [r["iri"] for r in rows] == sorted(t["iri"] for t in terms)
    assert rows[0]["alt_labels"] == ["one"] and rows[0]["attributes"] is None
    assert json.loads(rows[1]["attributes"]) == {"compartment": "air"}
    edges = pq.read_table(written["relations"][0])
    assert edges.schema.names == vocab_layers.RELATION_SCHEMA.names


def test_write_layers_without_relations_writes_no_relation_file(tmp_path):
    written = vocab_layers.write_layers([_term(1)], [], tmp_path, "demo-1")
    assert written["relations"] == []
    assert not (tmp_path / "relations").exists()


def test_write_layers_replaces_a_previous_run(tmp_path):
    (tmp_path / "terms").mkdir()
    (tmp_path / "terms" / "demo-1-01.parquet").write_bytes(b"old shard")
    (tmp_path / "terms" / "other.parquet").write_bytes(b"another release")
    vocab_layers.write_layers([_term(1)], [], tmp_path, "demo-1")
    assert sorted(p.name for p in (tmp_path / "terms").iterdir()) == [
        "demo-1.parquet",
        "other.parquet",
    ]


def test_write_layers_shards_over_the_size_limit(tmp_path, monkeypatch):
    monkeypatch.setattr(vocab_layers, "MAX_FILE_BYTES", 20_000)
    terms = [_term(i, definition=f"definition {i} " * 5) for i in range(3000)]
    written = vocab_layers.write_layers(terms, [], tmp_path, "demo-1")["terms"]
    assert len(written) > 1
    assert [p.name for p in written][:2] == ["demo-1-01.parquet", "demo-1-02.parquet"]
    assert all(p.stat().st_size <= 20_000 for p in written)
    read = [r["iri"] for p in written for r in pq.read_table(p).to_pylist()]
    assert read == sorted(t["iri"] for t in terms)


def test_write_layers_rejects_unknown_columns(tmp_path):
    with pytest.raises(ValidationError, match="unknown columns"):
        vocab_layers.write_layers([_term(1, compartment="air")], [], tmp_path, "demo-1")


def test_read_iris_skips_the_named_release(tmp_path):
    vocab_layers.write_layers([_term(1), _term(2)], [], tmp_path, "demo-1")
    vocab_layers.write_layers([_term(3)], [], tmp_path, "other")
    assert vocab_layers.read_iris(tmp_path / "terms", skip_release="demo-1") == {f"{FLOWS}000003"}
    assert vocab_layers.read_iris(tmp_path / "missing", skip_release="demo-1") == set()
