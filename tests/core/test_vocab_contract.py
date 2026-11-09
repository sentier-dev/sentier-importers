from pathlib import Path

import pytest
from sentier_importers.core import vocab_contract
from sentier_importers.core.errors import ValidationError

FIXTURES = Path(__file__).parent / "fixtures" / "vocab"
FLOWS = "https://vocab.sentier.dev/flows/"
SOURCES = "https://vocab.sentier.dev/sources/"
RELEASE = "demo-1"
SOURCE = SOURCES + RELEASE


@pytest.fixture(scope="module")
def contract():
    return vocab_contract.load_contract(FIXTURES / "term.yaml", FIXTURES / "relation.yaml")


def _flow(identifier="a", **extra):
    row = {
        "iri": FLOWS + identifier,
        "type": "elementary-flow",
        "pref_label": f"Flow {identifier}",
        "source": SOURCE,
        "status": "draft",
    }
    row.update(extra)
    return row


def _source_term():
    return {
        "iri": SOURCE,
        "type": "source",
        "pref_label": "Demo",
        "source": SOURCE,
        "status": "published",
    }


def _edge(subject, predicate, obj):
    return {
        "subject": subject,
        "predicate": predicate,
        "object": obj,
        "source": SOURCE,
        "status": "draft",
    }


def _check(contract, terms, relations=(), complete=False):
    return vocab_contract.check(
        list(terms), list(relations), contract, release=RELEASE, complete=complete
    )


def test_load_contract_reads_types_and_predicates(contract):
    assert contract.types["elementary-flow"].namespace == FLOWS
    assert "cas_number" in contract.types["elementary-flow"].attributes
    assert contract.types["characterization-factor"].retired
    assert contract.predicates["qudt:hasUnit"].max_one_per_subject
    assert contract.predicates["ont:geography"].external == ("https://sws.geonames.org/",)
    assert contract.statuses == ("draft", "published", "deprecated")
    assert contract.sources_namespace == SOURCES


def test_type_for_iri_prefers_the_longest_namespace(contract):
    assert contract.type_for_iri("https://vocab.sentier.dev/units/group/mass") == "unit-group"
    assert contract.type_for_iri("https://vocab.sentier.dev/units/kg") == "unit"
    assert contract.type_for_iri("http://qudt.org/vocab/unit/KiloGM") is None


def test_valid_part_has_no_errors(contract):
    terms = [_flow("a", attributes={"compartment": "air", "cas_number": "000124-38-9"})]
    assert _check(contract, terms) == []


def test_required_columns_and_status(contract):
    row = _flow("a", status="final")
    del row["pref_label"]
    errors = _check(contract, [row])
    assert any("'pref_label' is required" in e for e in errors)
    assert any("status 'final'" in e for e in errors)


def test_unknown_and_retired_types(contract):
    unknown = _flow("a", type="widget")
    retired = {
        "iri": "https://vocab.sentier.dev/characterization-factors/x",
        "type": "characterization-factor",
        "pref_label": "x",
        "source": SOURCE,
    }
    errors = _check(contract, [unknown, retired])
    assert any("'widget' is not a listed type" in e for e in errors)
    assert any("'characterization-factor' is retired" in e for e in errors)


def test_iri_must_sit_in_the_type_namespace(contract):
    errors = _check(contract, [_flow(iri="https://vocab.sentier.dev/products/a")])
    assert any("not in the elementary-flow namespace" in e for e in errors)


def test_undeclared_attribute_and_wrong_datatype(contract):
    process = {
        "iri": "https://vocab.sentier.dev/processes/p",
        "type": "process",
        "pref_label": "p",
        "source": SOURCE,
        "attributes": {"valid_from": "yesterday", "process_type": "batch", "colour": "red"},
    }
    errors = _check(contract, [process])
    assert any("'valid_from'" in e and "ISO date" in e for e in errors)
    assert any("'process_type'" in e and "not one of" in e for e in errors)
    assert any("'colour' is not declared for type 'process'" in e for e in errors)


def test_list_attribute_must_be_a_list_of_strings(contract):
    source = dict(_source_term(), attributes={"creators": "EFSA"})
    assert any("list of strings" in e for e in _check(contract, [source]))


def test_duplicate_iri_in_the_release(contract):
    assert any("duplicate iri" in e for e in _check(contract, [_flow("a"), _flow("a")]))


def test_every_row_carries_the_release_source(contract):
    errors = _check(contract, [_flow("a", source=SOURCES + "other")])
    assert any("source must be" in e and SOURCE in e for e in errors)


def test_complete_release_must_hold_its_source_term(contract):
    assert any("source term" in e for e in _check(contract, [_flow("a")], complete=True))
    assert _check(contract, [_flow("a"), _source_term()], complete=True) == []


def test_relation_predicate_subject_and_object_types(contract):
    method = {
        "iri": "https://vocab.sentier.dev/lcia-methods/m",
        "type": "lcia-method",
        "pref_label": "m",
        "source": SOURCE,
    }
    terms = [_flow("a"), method]
    relations = [
        _edge(FLOWS + "a", "ont:likes", FLOWS + "a"),
        _edge(FLOWS + "a", "ont:method", method["iri"]),
        _edge(method["iri"], "ont:impactCategory", FLOWS + "a"),
        _edge(FLOWS + "missing", "skos:related", FLOWS + "a"),
    ]
    errors = _check(contract, terms, relations)
    assert any("'ont:likes' is not a listed predicate" in e for e in errors)
    assert any("ont:method does not accept subject type 'elementary-flow'" in e for e in errors)
    assert any(
        "ont:impactCategory does not accept object type 'elementary-flow'" in e for e in errors
    )
    assert any("subject" in e and "is not a term of this release" in e for e in errors)


def test_object_outside_the_release_is_typed_by_its_namespace(contract):
    terms = [_flow("a")]
    ok = _edge(FLOWS + "a", "skos:broader", "https://vocab.sentier.dev/organisms/x")
    unit = _edge(FLOWS + "a", "qudt:hasUnit", "http://qudt.org/vocab/unit/KiloGM")
    assert _check(contract, terms, [ok, unit]) == []
    wrong = _edge(FLOWS + "a", "qudt:hasUnit", "https://vocab.sentier.dev/organisms/x")
    assert any(
        "does not accept object type 'organism'" in e for e in _check(contract, terms, [wrong])
    )


def test_external_object_needs_an_allowed_prefix(contract):
    edge = _edge(FLOWS + "a", "skos:exactMatch", "https://vocab.brightway.one/x")
    errors = _check(contract, [_flow("a")], [edge])
    assert any("neither a term nor an allowed external IRI" in e for e in errors)


def test_at_most_one_unit_edge_per_subject(contract):
    edges = [
        _edge(FLOWS + "a", "qudt:hasUnit", "http://qudt.org/vocab/unit/BQ"),
        _edge(FLOWS + "a", "qudt:hasUnit", "http://qudt.org/vocab/unit/KiloBQ"),
    ]
    assert any("more than one qudt:hasUnit" in e for e in _check(contract, [_flow("a")], edges))


def test_validate_raises_with_every_problem(contract):
    with pytest.raises(ValidationError) as excinfo:
        vocab_contract.validate(
            [_flow("a", status="final"), _flow("a")], [], contract, release=RELEASE, complete=False
        )
    message = str(excinfo.value)
    assert "status 'final'" in message and "duplicate iri" in message
