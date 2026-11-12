"""Deterministic dedup of a source's rows.

A term's identity is its ``iri``. Sources mint IRIs deterministically from a stable
source key via :func:`slugify`, so re-running a source yields identical IRIs. The
``dedup`` stage then guarantees a source introduces no duplicate terms:

- **Layer A (intra-source, always on):** collapse identical same-``iri`` rows, raise on
  conflicting same-``iri`` rows (a transform bug), warn on same-label/different-``iri``.
- **Layer B (against the target vocab, default on for vocab sources):** read the IRIs
  of every *other* release in the target's ``terms/`` folder at the pinned ref and apply
  an ``on_existing`` policy (``skip`` | ``error``) to colliding IRIs. A release replaces
  its own files whole, so its own previous delivery is never a collision; an IRI that
  another release already delivers can only be skipped or refused, because sentier-vocab
  rejects an IRI that appears in two files.

No fuzzy matching, no external files — fully deterministic and offline-testable.
"""

import io
import re
import unicodedata

import orjson
import pyarrow.parquet as pq
from loguru import logger
from sentier_importers.core import fetch as fetch_mod
from sentier_importers.core import vocab_layers
from sentier_importers.core.context import RunContext
from sentier_importers.core.errors import ValidationError
from sentier_importers.core.source import SourceConfig
from sentier_importers.core.targets import Target
from sentier_importers.core.types import RawData, Record, Rows

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


def _existing_iris(target: Target, release: str, ctx: RunContext) -> set[str]:
    """IRIs the target already holds in the term files of every release but ``release``.

    With ``ctx.schema_dir`` set (the ``schemas/`` folder of a local sentier-vocab
    checkout) the ``terms/`` folder beside it is read. Otherwise the folder is listed
    via the GitHub contents API at the pinned ref and each file fetched, all through
    the cached fetcher so tests can run offline against a seeded cache.
    """
    if ctx.schema_dir is not None:
        if ctx.dependency_observer:
            iris: set[str] = set()
            for path in sorted((ctx.schema_dir.parent / vocab_layers.TERMS_DIR).glob("*.parquet")):
                if vocab_layers.release_of(path) == release:
                    continue
                raw = RawData(content=path.read_bytes(), source_url=f"file://{path}")
                ctx.dependency_observer("existing-vocab", raw.source_url, raw)
                iris.update(
                    pq.read_table(io.BytesIO(raw.content), columns=["iri"])
                    .column("iri")
                    .to_pylist()
                )
            return iris
        return vocab_layers.read_iris(
            ctx.schema_dir.parent / vocab_layers.TERMS_DIR, skip_release=release
        )

    repo = target.repo.removesuffix(".git").removeprefix("https://github.com/")
    listing_url = (
        f"https://api.github.com/repos/{repo}/contents/{vocab_layers.TERMS_DIR}"
        f"?ref={target.schema_ref}"
    )
    listing = fetch_mod.fetch(listing_url, ctx)
    if ctx.dependency_observer:
        ctx.dependency_observer("existing-vocab-index", listing_url, listing)
    entries = orjson.loads(listing.content)

    iris: set[str] = set()
    for entry in entries:
        name = entry.get("name", "")
        if not name.endswith(".parquet") or vocab_layers.release_of(name) == release:
            continue
        raw = fetch_mod.fetch(entry["download_url"], ctx)
        if ctx.dependency_observer:
            ctx.dependency_observer("existing-vocab", entry["download_url"], raw)
        iris.update(
            pq.read_table(io.BytesIO(raw.content), columns=["iri"]).column("iri").to_pylist()
        )
    return iris


def _dedup_against_existing(rows: Rows, existing_iris: set[str], on_existing: str) -> Rows:
    """Layer B: apply the ``on_existing`` policy to IRIs another release already delivers."""
    if on_existing not in {"skip", "error"}:
        raise ValidationError(f"unknown on_existing policy: {on_existing!r}")

    result: Rows = []
    for row in rows:
        iri = row.get("iri")
        if iri in existing_iris:
            if on_existing == "error":
                raise ValidationError(f"iri already exists in target vocab: {iri!r}")
            continue  # skip: the other release keeps the term
        result.append(row)
    return result


def dedup(rows: Rows, config: SourceConfig, target: Target, ctx: RunContext) -> Rows:
    """Run Layer A (always) then Layer B (when configured) and return reconciled rows."""
    rows = _dedup_intra(rows)

    layer_b_active = (
        config.dedup_check_existing
        and target.schema_ref is not None
        and config.term_type is not None
    )
    if layer_b_active:
        existing_iris = _existing_iris(target, config.category, ctx)
        rows = _dedup_against_existing(rows, existing_iris, config.dedup_on_existing)
    return rows
