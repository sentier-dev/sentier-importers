from pathlib import Path

import pytest
from sentier_importers.core import validate as validate_mod
from sentier_importers.core.errors import ValidationError

SCHEMA = Path(__file__).parent / "fixtures" / "widget.yaml"
COLLECTION_SCHEMA = Path(__file__).parent / "fixtures" / "product_collection.yaml"


def _collection(products):
    return {"scheme": "https://vocab.sentier.dev/products/", "products": products}


def test_none_validator_accepts_anything():
    # Should not raise regardless of content / missing schema.
    validate_mod.validate([{"whatever": 1}], "none", None, None)


def test_linkml_accepts_valid_rows():
    rows = [{"id": "1", "label": "Alpha"}, {"id": "2", "label": "Beta"}]
    validate_mod.validate(rows, "linkml", SCHEMA, "Widget")


def test_linkml_rejects_missing_required_slot():
    rows = [{"id": "1"}]  # missing required "label"
    with pytest.raises(ValidationError):
        validate_mod.validate(rows, "linkml", SCHEMA, "Widget")


def test_linkml_requires_schema_path():
    with pytest.raises(ValidationError):
        validate_mod.validate([{"id": "1", "label": "x"}], "linkml", None, "Widget")


def test_unknown_validator_raises():
    with pytest.raises(ValidationError):
        validate_mod.validate([], "bogus", None, None)


def test_linkml_collection_accepts_valid_collection():
    collection = _collection(
        [
            {"iri": "https://vocab.sentier.dev/products/a", "pref_label": "A"},
            {"iri": "https://vocab.sentier.dev/products/b", "pref_label": "B"},
        ]
    )
    validate_mod.validate(collection, "linkml_collection", COLLECTION_SCHEMA, "ProductCollection")


def test_linkml_collection_rejects_missing_scheme():
    collection = {"products": [{"iri": "x", "pref_label": "A"}]}  # no scheme
    with pytest.raises(ValidationError):
        validate_mod.validate(
            collection, "linkml_collection", COLLECTION_SCHEMA, "ProductCollection"
        )


def test_linkml_collection_rejects_item_missing_pref_label():
    collection = _collection([{"iri": "https://vocab.sentier.dev/products/a"}])  # no pref_label
    with pytest.raises(ValidationError):
        validate_mod.validate(
            collection, "linkml_collection", COLLECTION_SCHEMA, "ProductCollection"
        )


def test_linkml_collection_requires_schema_path():
    with pytest.raises(ValidationError):
        validate_mod.validate(_collection([]), "linkml_collection", None, "ProductCollection")
