"""Deterministic, in-repo dedup — supersedes the deferred ``sentier_mappings`` layer.

A term's identity is its ``iri``. Sources mint IRIs deterministically from a stable
source key via :func:`slugify`, so re-running a source yields identical IRIs. The
``dedup`` stage then guarantees a source introduces no duplicate terms:

- **Layer A (intra-source, always on):** collapse identical same-``iri`` rows, raise on
  conflicting same-``iri`` rows (a transform bug), warn on same-label/different-``iri``.
- **Layer B (against the target vocab, default on for schema-bearing targets):** read the
  target's existing ``data/<category>/*.yaml`` collections at the pinned ref and apply an
  ``on_existing`` policy (``skip`` | ``error`` | ``overwrite``) to colliding IRIs.

No fuzzy matching, no external files — fully deterministic and offline-testable.
"""

import re
import unicodedata

import orjson
import yaml
from loguru import logger
from sentier_importers.core import fetch as fetch_mod
from sentier_importers.core.context import RunContext
from sentier_importers.core.errors import ValidationError
from sentier_importers.core.source import SourceConfig
from sentier_importers.core.targets import Target
from sentier_importers.core.types import Record, Rows

_SLUG_STRIP = re.compile(r"[^a-z0-9]+")


def slugify(value: str) -> str:
    """Return a lowercase, ASCII, hyphen-separated slug — stable and URL-safe."""
    ascii_value = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode("ascii")
    return _SLUG_STRIP.sub("-", ascii_value.lower()).strip("-")


def _normalize_label(label: object) -> str:
    """Normalize a ``pref_label`` for collision comparison (case/space-folded)."""
    return " ".join(str(label).split()).casefold()


def _dedup_intra(rows: Rows) -> Rows:
    """Layer A: collapse identical IRIs, raise on conflicts, warn on label clashes."""
    by_iri: dict[str, Record] = {}
    result: Rows = []
    for row in rows:
        iri = row.get("iri")
        if iri is None:
            result.append(row)  # no identity — nothing to reconcile
            continue
        if iri in by_iri:
            if by_iri[iri] != row:
                raise ValidationError(
                    f"intra-source conflict: iri {iri!r} has differing field values"
                )
            continue  # identical duplicate — collapse
        by_iri[iri] = row
        result.append(row)

    label_to_iri: dict[str, str] = {}
    for row in result:
        label = row.get("pref_label")
        iri = row.get("iri")
        if label is None or iri is None:
            continue
        norm = _normalize_label(label)
        seen = label_to_iri.get(norm)
        if seen is not None and seen != iri:
            logger.warning(
                f"intra-source label collision: {label!r} maps to {seen!r} and {iri!r}; "
                "keeping both for human review"
            )
        else:
            label_to_iri[norm] = iri
    return result


def _existing_index(
    target: Target, category: str, items_key: str, ctx: RunContext
) -> tuple[set[str], dict[str, str]]:
    """Read the target's ``data/<category>`` collections at the pinned ref.

    Lists the directory via the GitHub contents API and fetches each ``*.yaml`` file,
    all through the cached fetcher so CI can run offline against a seeded cache. The
    directory is the ``category`` (the on-disk data folder), while ``items_key`` is the
    plural slot each collection stores its items under — the two differ when they must
    (e.g. category ``elementary-flows`` / items_key ``flows``).
    Returns the set of existing IRIs and a normalized-label → IRI index.
    """
    repo = target.repo.removesuffix(".git").removeprefix("https://github.com/")
    listing_url = (
        f"https://api.github.com/repos/{repo}/contents/data/{category}?ref={target.schema_ref}"
    )
    entries = orjson.loads(fetch_mod.fetch(listing_url, ctx).content)

    iris: set[str] = set()
    labels: dict[str, str] = {}
    for entry in entries:
        name = entry.get("name", "")
        if not name.endswith((".yaml", ".yml")):
            continue
        collection = yaml.safe_load(fetch_mod.fetch(entry["download_url"], ctx).content) or {}
        for item in collection.get(items_key, []) or []:
            iri = item.get("iri")
            if iri is not None:
                iris.add(iri)
            label = item.get("pref_label")
            if label is not None and iri is not None:
                labels.setdefault(_normalize_label(label), iri)
    return iris, labels


def _dedup_against_existing(
    rows: Rows, existing_iris: set[str], existing_labels: dict[str, str], on_existing: str
) -> Rows:
    """Layer B: apply the ``on_existing`` policy to IRIs already in the target."""
    if on_existing not in {"skip", "error", "overwrite"}:
        raise ValidationError(f"unknown on_existing policy: {on_existing!r}")

    result: Rows = []
    for row in rows:
        iri = row.get("iri")
        if iri in existing_iris:
            if on_existing == "skip":
                continue  # idempotent re-import — drop the already-present term
            if on_existing == "error":
                raise ValidationError(f"iri already exists in target vocab: {iri!r}")
            # overwrite — keep the imported version (source is authoritative)
        result.append(row)

        label = row.get("pref_label")
        if label is not None:
            existing_iri = existing_labels.get(_normalize_label(label))
            if existing_iri is not None and existing_iri != iri:
                logger.warning(
                    f"cross-source label collision: {label!r} ({iri!r}) shares a label with "
                    f"existing {existing_iri!r}; not auto-merging — set crosswalk links explicitly"
                )
    return result


def dedup(rows: Rows, config: SourceConfig, target: Target, ctx: RunContext) -> Rows:
    """Run Layer A (always) then Layer B (when configured) and return reconciled rows."""
    rows = _dedup_intra(rows)

    layer_b_active = (
        config.dedup_check_existing
        and target.schema_ref is not None
        and config.collection_items_key is not None
    )
    if layer_b_active:
        existing_iris, existing_labels = _existing_index(
            target, config.category, config.collection_items_key, ctx
        )
        rows = _dedup_against_existing(
            rows, existing_iris, existing_labels, config.dedup_on_existing
        )
    return rows
