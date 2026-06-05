"""Resolve a target repo's schema file at its pinned git ref via the cached fetcher.

The schema is fetched from GitHub's raw-content host at the target's pinned
``schema_ref`` and cached on disk, so validation is reproducible and offline
tests can pre-seed the cache.
"""

from pathlib import Path

from sentier_importers.core import fetch as fetch_mod
from sentier_importers.core.context import RunContext
from sentier_importers.core.errors import ValidationError
from sentier_importers.core.targets import Target


def _raw_url(target: Target, schema_id: str) -> str:
    """Build the raw.githubusercontent.com URL for the ``schema_id`` file at the pinned ref.

    ``schema_id`` is the schema *file* id (e.g. ``product`` → ``schemas/product.yaml``),
    not a class name — a collection class (``ProductCollection``) and its schema file
    need not share a name.
    """
    repo = target.repo.removesuffix(".git").removeprefix("https://github.com/")
    return (
        f"https://raw.githubusercontent.com/{repo}/"
        f"{target.schema_ref}/schemas/{schema_id}.yaml"
    )


def resolve_schema(target: Target, schema_id: str, ctx: RunContext) -> Path:
    """Fetch ``schema_id`` for ``target`` at its pinned ref and return a local path.

    Raises ``ValidationError`` if the target has no pinned ``schema_ref``.
    """
    if target.schema_ref is None:
        raise ValidationError(f"target {target.name!r} has no pinned schema_ref")
    raw = fetch_mod.fetch(_raw_url(target, schema_id), ctx)
    schema_path = ctx.cache_dir / "schemas" / f"{target.name}-{target.schema_ref}-{schema_id}.yaml"
    schema_path.parent.mkdir(parents=True, exist_ok=True)
    schema_path.write_bytes(raw.content)
    return schema_path
