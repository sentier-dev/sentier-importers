from pathlib import Path

import pytest

from sentier_importers.core import validate as validate_mod
from sentier_importers.core.errors import ValidationError

SCHEMA = Path(__file__).parent / "fixtures" / "widget.yaml"


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
