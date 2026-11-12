"""Resolve opt-in, reusable source declarations without inventing missing facts."""

from copy import deepcopy
from datetime import date
from urllib.parse import urlsplit

from sentier_importers.core.errors import RegistryError

TEXT = {
    "source",
    "source_version",
    "title",
    "publisher",
    "date",
    "identifier",
    "citation",
    "source_url",
    "license",
    "license_url",
    "format",
    "format_version",
}
REQUIRED = {"source", "source_version", "title", "publisher", "license", "citation"}


def resolve_provenance(entry, catalog):
    references = entry.get("provenance")
    if references is None:
        return {}  # Existing sources adopt the new contract incrementally.
    roles = {"source", *(entry.get("inputs") or {})}
    if not isinstance(references, dict) or set(references) != roles:
        raise RegistryError("provenance must declare source and every named input role")
    if not isinstance(catalog, dict):
        raise RegistryError("source_datasets must be a mapping")
    resolved = {}
    for role, reference in references.items():
        if not isinstance(reference, str) or reference not in catalog:
            raise RegistryError(f"Unknown provenance declaration for input {role!r}")
        metadata = deepcopy(catalog[reference])
        if not isinstance(metadata, dict) or not REQUIRED <= metadata.keys():
            raise RegistryError(f"Incomplete source declaration {reference!r}")
        if metadata.keys() - (TEXT | {"creators", "coverage"}):
            raise RegistryError(f"Unknown fields in source declaration {reference!r}")
        for key in TEXT & metadata.keys():
            value = metadata[key]
            if value is not None and (not isinstance(value, str) or not value.strip()):
                raise RegistryError(f"Source declaration {key!r} must be text or null")
        if not metadata["source"] or not metadata["title"]:
            raise RegistryError("Source declarations need a nonempty source ID and title")
        creators = metadata.get("creators")
        if creators is not None and (
            not isinstance(creators, list)
            or any(not isinstance(item, str) or not item.strip() for item in creators)
        ):
            raise RegistryError("Source creators must be a list of names or null")
        coverage = metadata.get("coverage")
        if coverage is not None and not isinstance(coverage, str):
            raise RegistryError("Source coverage must be descriptive text or null")
        for key in ("source_url", "license_url", "identifier"):
            value = metadata.get(key)
            if value is None:
                continue
            url = urlsplit(value)
            if (
                url.scheme not in {"https", "http", "urn", "doi"}
                or (url.scheme in {"http", "https"} and not url.netloc)
                or url.username
                or url.password
                or url.query
            ):
                raise RegistryError(f"Source {key} must be a public reference without credentials")
        if metadata.get("date") is not None:
            try:
                date.fromisoformat(metadata["date"])
            except ValueError as exc:
                raise RegistryError("Source date must be an ISO publication date or null") from exc
        resolved[role] = metadata
    return resolved
