"""Resolve a target repo's contract file at its pinned git ref via the cached fetcher.

A contract (``schemas/term.yaml``, ``schemas/relation.yaml`` of sentier-vocab) is
fetched from GitHub's raw-content host at the target's pinned ``schema_ref`` and
cached on disk, so validation is reproducible and offline tests can pre-seed the cache.
"""

from pathlib import Path

from sentier_importers.core import fetch as fetch_mod
from sentier_importers.core.context import RunContext
from sentier_importers.core.errors import ValidationError
from sentier_importers.core.targets import Target


def _raw_url(target: Target, schema_id: str) -> str:
    """The raw.githubusercontent.com URL of ``schemas/<schema_id>.yaml`` at the pinned ref."""
    repo = target.repo.removesuffix(".git").removeprefix("https://github.com/")
    return f"https://raw.githubusercontent.com/{repo}/{target.schema_ref}/schemas/{schema_id}.yaml"


def resolve_schema(target: Target, schema_id: str, ctx: RunContext) -> Path:
    """Resolve ``schema_id`` for ``target`` and return the local path of the file.

    With ``ctx.schema_dir`` set, read ``<schema_dir>/<schema_id>.yaml`` directly, to
    validate against a contract that is not pushed yet. Otherwise fetch the file from
    the target's pinned ref. Raises ``ValidationError`` on a missing local file or an
    unpinned remote target.
    """
    if ctx.schema_dir is not None:
        local = ctx.schema_dir / f"{schema_id}.yaml"
        if not local.exists():
            raise ValidationError(f"schema {schema_id!r} not found in {ctx.schema_dir}")
        return local

    if target.schema_ref is None:
        raise ValidationError(f"target {target.name!r} has no pinned schema_ref")
    schema_dir = ctx.cache_dir / "schemas" / f"{target.name}-{target.schema_ref}"
    schema_dir.mkdir(parents=True, exist_ok=True)
    path = schema_dir / f"{schema_id}.yaml"
    path.write_bytes(fetch_mod.fetch(_raw_url(target, schema_id), ctx).content)
    return path
