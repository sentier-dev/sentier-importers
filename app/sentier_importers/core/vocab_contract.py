"""The sentier-vocab contracts (``schemas/term.yaml`` and ``schemas/relation.yaml``)
and the checks a delivery must pass before it is staged.

sentier-vocab stores every term of one source release in ``terms/<release>.parquet``
and every edge of that release in ``relations/<release>.parquet``. The two YAML files
list the columns, the allowed term types (namespace, attribute keys and datatypes)
and the allowed predicates (subject and object types, external IRI prefixes).

``check`` covers what can be decided from one release alone. What needs the whole
tree (an IRI unique across releases, an object that exists in another release) stays
with ``python -m sentier_vocab validate``, the CI gate of the target repo.
"""

import datetime as dt
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from types import MappingProxyType

import yaml
from sentier_importers.core.errors import ValidationError
from sentier_importers.core.types import Rows

ATTRIBUTE_TYPES = frozenset({"string", "date", "double", "uri", "list[string]"})
SOURCE_TYPE = "source"


@dataclass(frozen=True)
class Attribute:
    """One declared key of a term type's ``attributes``."""

    name: str
    type: str = "string"
    enum: tuple[str, ...] | None = None


@dataclass(frozen=True)
class TermType:
    """One allowed value of the ``type`` column."""

    name: str
    namespace: str
    attributes: Mapping[str, Attribute] = field(default_factory=dict)
    retired: bool = False


@dataclass(frozen=True)
class Predicate:
    """One allowed value of the ``predicate`` column."""

    curie: str
    subject: tuple[str, ...]
    object: tuple[str, ...]
    external: tuple[str, ...] = ()
    max_one_per_subject: bool = False


@dataclass(frozen=True)
class Contract:
    """Both contracts, reduced to what the importer checks and writes."""

    term_columns: tuple[str, ...]
    required_term_columns: tuple[str, ...]
    relation_columns: tuple[str, ...]
    required_relation_columns: tuple[str, ...]
    statuses: tuple[str, ...]
    types: Mapping[str, TermType]
    predicates: Mapping[str, Predicate]

    @property
    def sources_namespace(self) -> str:
        """Namespace of the ``source`` type; a release's source term is ``<ns><release>``."""
        return self.types[SOURCE_TYPE].namespace

    def type_for_iri(self, iri: str) -> str | None:
        """The term type whose namespace holds ``iri`` (longest namespace wins), or ``None``."""
        matches = [t for t in self.types.values() if iri.startswith(t.namespace)]
        return max(matches, key=lambda t: len(t.namespace)).name if matches else None


def _tuple(value: object) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, (list, tuple)):
        return tuple(str(v) for v in value)
    return (str(value),)


def _attributes(raw: dict | None, type_name: str) -> Mapping[str, Attribute]:
    attributes = {}
    for name, spec in (raw or {}).items():
        attr_type = spec.get("type", "string")
        if attr_type not in ATTRIBUTE_TYPES:
            raise ValidationError(
                f"term.yaml: type {type_name!r} attribute {name!r} has unknown type {attr_type!r}"
            )
        enum = tuple(spec["enum"]) if "enum" in spec else None
        attributes[name] = Attribute(name=name, type=attr_type, enum=enum)
    return MappingProxyType(attributes)


def _required(columns: dict) -> tuple[str, ...]:
    return tuple(name for name, spec in columns.items() if spec.get("required"))


@lru_cache(maxsize=None)
def load_contract(term_yaml: Path, relation_yaml: Path) -> Contract:
    """Parse the two contract files. Cached per pair of paths."""
    try:
        term = yaml.safe_load(Path(term_yaml).read_text(encoding="utf-8"))
        relation = yaml.safe_load(Path(relation_yaml).read_text(encoding="utf-8"))
        types = {
            name: TermType(
                name=name,
                namespace=spec["namespace"],
                attributes=_attributes(spec.get("attributes"), name),
                retired=bool(spec.get("retired", False)),
            )
            for name, spec in term["types"].items()
        }
        predicates = {
            curie: Predicate(
                curie=curie,
                subject=_tuple(spec["subject"]),
                object=_tuple(spec["object"]),
                external=_tuple(spec.get("external")),
                max_one_per_subject=bool(spec.get("max_one_per_subject", False)),
            )
            for curie, spec in relation["predicates"].items()
        }
        return Contract(
            term_columns=tuple(term["columns"]),
            required_term_columns=_required(term["columns"]),
            relation_columns=tuple(relation["columns"]),
            required_relation_columns=_required(relation["columns"]),
            statuses=tuple(term["columns"]["status"]["enum"]),
            types=MappingProxyType(types),
            predicates=MappingProxyType(predicates),
        )
    except (KeyError, TypeError, AttributeError, yaml.YAMLError) as exc:
        raise ValidationError(
            f"cannot read the vocab contracts {term_yaml} and {relation_yaml}: {exc!r}"
        ) from exc


def _attribute_error(value: object, spec: Attribute) -> str | None:
    if spec.enum is not None and value not in spec.enum:
        return f"attribute {spec.name!r}: {value!r} is not one of {list(spec.enum)}"
    if spec.type == "date":
        try:
            dt.date.fromisoformat(str(value))
        except ValueError:
            return f"attribute {spec.name!r}: {value!r} is not an ISO date"
    elif spec.type == "double":
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return f"attribute {spec.name!r}: {value!r} is not a double"
    elif spec.type == "list[string]":
        if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
            return f"attribute {spec.name!r}: {value!r} is not a list of strings"
    elif not isinstance(value, str):
        return f"attribute {spec.name!r}: {value!r} is not a string"
    return None


def _check_common(
    where: str, row: dict, required: tuple[str, ...], contract: Contract, source: str
) -> list[str]:
    errors = [
        f"{where}: column {name!r} is required" for name in required if row.get(name) in (None, "")
    ]
    status = row.get("status")
    if status is not None and status not in contract.statuses:
        errors.append(f"{where}: status {status!r} is not one of {list(contract.statuses)}")
    if row.get("source") not in (None, "", source):
        errors.append(f"{where}: source must be {source}, found {row['source']!r}")
    return errors


def _check_term(row: dict, contract: Contract, source: str) -> list[str]:
    where = str(row.get("iri", "<no iri>"))
    errors = _check_common(where, row, contract.required_term_columns, contract, source)
    type_name = row.get("type")
    term_type = contract.types.get(type_name)
    if term_type is None:
        return [*errors, f"{where}: type {type_name!r} is not a listed type"]
    if term_type.retired:
        errors.append(f"{where}: type {type_name!r} is retired")
    if not str(row.get("iri", "")).startswith(term_type.namespace):
        errors.append(f"{where}: iri is not in the {type_name} namespace {term_type.namespace}")
    for name, value in (row.get("attributes") or {}).items():
        spec = term_type.attributes.get(name)
        if spec is None:
            errors.append(f"{where}: attribute {name!r} is not declared for type {type_name!r}")
        elif problem := _attribute_error(value, spec):
            errors.append(f"{where}: {problem}")
    return errors


def _accepts(kinds: tuple[str, ...], type_name: str) -> bool:
    return "any" in kinds or type_name in kinds


def _check_relation(
    row: dict, types: dict[str, str], contract: Contract, source: str
) -> list[str]:
    where = f"{row.get('subject')} {row.get('predicate')} {row.get('object')}"
    errors = _check_common(where, row, contract.required_relation_columns, contract, source)
    predicate = contract.predicates.get(row.get("predicate"))
    if predicate is None:
        return [*errors, f"{where}: {row.get('predicate')!r} is not a listed predicate"]
    subject_type = types.get(row.get("subject"))
    if subject_type is None:
        errors.append(f"{where}: subject {row.get('subject')!r} is not a term of this release")
    elif not _accepts(predicate.subject, subject_type):
        errors.append(f"{where}: {predicate.curie} does not accept subject type {subject_type!r}")
    obj = str(row.get("object") or "")
    # A term of another release is typed by its namespace; that it exists is checked
    # by the target repo's own validator, which sees every release.
    object_type = types.get(obj) or contract.type_for_iri(obj)
    if object_type is not None:
        if not _accepts(predicate.object, object_type):
            errors.append(
                f"{where}: {predicate.curie} does not accept object type {object_type!r}"
            )
    elif not obj.startswith(predicate.external):
        errors.append(
            f"{where}: object {obj!r} is neither a term nor an allowed external IRI "
            f"for {predicate.curie}"
        )
    return errors


def _check_cardinality(relations: Rows, contract: Contract) -> list[str]:
    counts: Counter[tuple[str, str]] = Counter()
    for row in relations:
        predicate = contract.predicates.get(row.get("predicate"))
        if predicate is not None and predicate.max_one_per_subject:
            counts[(row["subject"], predicate.curie)] += 1
    return [
        f"{subject}: more than one {curie} edge ({count})"
        for (subject, curie), count in sorted(counts.items())
        if count > 1
    ]


def check(
    terms: Rows, relations: Rows, contract: Contract, *, release: str, complete: bool
) -> list[str]:
    """Every problem in the term and relation rows of ``release``, one line each.

    ``complete`` says the rows are the whole release and not one source's part of
    it; only then must the release hold its own source term.
    """
    source = contract.sources_namespace + release
    errors: list[str] = []
    types: dict[str, str] = {}
    for row in terms:
        errors.extend(_check_term(row, contract, source))
        iri = row.get("iri")
        if iri in types:
            errors.append(f"duplicate iri {iri}")
        elif iri is not None:
            types[iri] = row.get("type")
    for row in relations:
        errors.extend(_check_relation(row, types, contract, source))
    errors.extend(_check_cardinality(relations, contract))
    if complete and types.get(source) != SOURCE_TYPE:
        errors.append(f"release {release!r} does not hold its source term {source}")
    return errors


def validate(
    terms: Rows, relations: Rows, contract: Contract, *, release: str, complete: bool
) -> None:
    """Raise ``ValidationError`` listing every problem ``check`` finds."""
    errors = check(terms, relations, contract, release=release, complete=complete)
    if errors:
        raise ValidationError(f"vocab release {release!r} is not valid:\n" + "\n".join(errors))
