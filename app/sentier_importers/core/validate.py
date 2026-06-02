"""Validator-strategy registry. ``none`` is a no-op for bulk-data targets; ``linkml``
validates each row against a named class in a target repo's LinkML schema."""

from collections.abc import Callable
from pathlib import Path

from linkml.validator import Validator
from linkml.validator.plugins import JsonschemaValidationPlugin
from linkml_runtime import SchemaView

from sentier_importers.core.errors import ValidationError
from sentier_importers.core.types import Rows

# (rows, schema_path, schema_id) -> None; raises ValidationError on failure.
ValidatorFn = Callable[[Rows, Path | None, str | None], None]

_VALIDATORS: dict[str, ValidatorFn] = {}


def register_validator(name: str, fn: ValidatorFn) -> None:
    """Register (or override) a validation strategy."""
    _VALIDATORS[name] = fn


def validate(rows: Rows, validator: str, schema_path: Path | None, schema_id: str | None) -> None:
    """Validate ``rows`` using the named strategy. Raises ``ValidationError`` on failure."""
    try:
        fn = _VALIDATORS[validator]
    except KeyError:
        raise ValidationError(f"no validator registered: {validator!r}") from None
    fn(rows, schema_path, schema_id)


def _validate_none(rows: Rows, schema_path: Path | None, schema_id: str | None) -> None:
    """No-op validator for targets without a schema (bulk-data repos)."""
    return None


def _validate_linkml(rows: Rows, schema_path: Path | None, schema_id: str | None) -> None:
    """Validate each row against the LinkML class ``schema_id`` in ``schema_path``.

    Uses ``JsonschemaValidationPlugin(closed=True)`` so required-slot enforcement is
    active (the default plugin set omits required-field checking in this LinkML version).
    """
    if schema_path is None:
        raise ValidationError("linkml validator requires a schema path")
    if schema_id is None:
        raise ValidationError("linkml validator requires a schema_id (target class name)")
    sv = SchemaView(str(schema_path))
    validator = Validator(sv.schema, validation_plugins=[JsonschemaValidationPlugin(closed=True)])
    problems: list[str] = []
    for index, row in enumerate(rows):
        report = validator.validate(row, schema_id)
        problems.extend(f"row {index}: {result.message}" for result in report.results)
    if problems:
        raise ValidationError("schema validation failed:\n" + "\n".join(problems))


register_validator("none", _validate_none)
register_validator("linkml", _validate_linkml)
