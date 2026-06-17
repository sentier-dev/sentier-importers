"""Resolve a target repo's schema file at its pinned git ref via the cached fetcher.

The schema is fetched from GitHub's raw-content host at the target's pinned
``schema_ref`` and cached on disk, so validation is reproducible and offline
tests can pre-seed the cache.

LinkML schemas are multi-file: ``product.yaml`` does ``imports: [common]`` and so on.
``SchemaView`` resolves a relative import by name against the loaded file's directory,
so every locally-imported schema is fetched too and written beside the main file under
its bare ``<name>.yaml``. CURIE imports (``linkml:types``) are provided by LinkML itself
and are skipped.
"""

from pathlib import Path

import yaml
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


def _local_imports(content: bytes) -> list[str]:
    """Return the bare (non-CURIE) import names declared in a schema document."""
    document = yaml.safe_load(content) or {}
    imports = document.get("imports") or []
    return [name for name in imports if isinstance(name, str) and ":" not in name]


def _fetch_into(
    target: Target, schema_id: str, schema_dir: Path, seen: set[str], ctx: RunContext
) -> Path:
    """Fetch ``schema_id`` and, recursively, its local imports into ``schema_dir``.

    Returns the path of the fetched file. Each file is written under its bare
    ``<schema_id>.yaml`` so relative imports resolve against ``schema_dir``.
    """
    path = schema_dir / f"{schema_id}.yaml"
    if schema_id in seen:
        return path
    seen.add(schema_id)

    raw = fetch_mod.fetch(_raw_url(target, schema_id), ctx)
    path.write_bytes(raw.content)
    for imported in _local_imports(raw.content):
        _fetch_into(target, imported, schema_dir, seen, ctx)
    return path


def resolve_schema(target: Target, schema_id: str, ctx: RunContext) -> Path:
    """Resolve ``schema_id`` for ``target`` and return the local path of the main file.

    With ``ctx.schema_dir`` set, read ``<schema_dir>/<schema_id>.yaml`` directly (its
    imports already sit beside it) — used to validate against an unpushed local schema.
    Otherwise fetch the schema and its local imports from the target's pinned ref.
    Raises ``ValidationError`` on a missing local file or an unpinned remote target.
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
    return _fetch_into(target, schema_id, schema_dir, set(), ctx)
