"""Pipeline driver: run a source through
fetch → parse → transform → dedup → assemble → validate → emit → deliver."""

from pathlib import Path

from sentier_importers.core import dedup as dedup_mod
from sentier_importers.core import deliver as deliver_mod
from sentier_importers.core import schema_provider
from sentier_importers.core import validate as validate_mod
from sentier_importers.core import write as write_mod
from sentier_importers.core.context import RunContext
from sentier_importers.core.errors import ValidationError
from sentier_importers.core.source import Source, SourceConfig
from sentier_importers.core.targets import Target, get_target
from sentier_importers.core.types import Payload, Rows


def _assemble(rows: Rows, config: SourceConfig) -> Payload:
    """Wrap rows into a ``{scheme, <items_key>: [...]}`` collection, or pass through.

    Returns ``rows`` unchanged for bulk/non-collection targets (``collection_class``
    unset); otherwise the assembled collection mapping matching ``data/<category>/*``.
    """
    if config.collection_class is None:
        return rows
    return {"scheme": config.collection_scheme, config.collection_items_key: rows}


def _validate(payload: Payload, config: SourceConfig, target: Target, ctx: RunContext) -> None:
    """Validate the assembled collection (tree-root) or each row (legacy per-item)."""
    if config.collection_class is not None:
        if config.schema_file is None:
            raise ValidationError(
                f"source {config.name!r} sets a collection but no schema_file to resolve"
            )
        schema_path = schema_provider.resolve_schema(target, config.schema_file, ctx)
        validate_mod.validate(payload, "linkml_collection", schema_path, config.collection_class)
    elif config.validate_against is not None:
        schema_path = schema_provider.resolve_schema(target, config.validate_against, ctx)
        validate_mod.validate(payload, target.validator, schema_path, config.validate_against)


def _arrow_schema(config: SourceConfig, target: Target, ctx: RunContext):
    """Explicit Arrow schema for a Parquet collection target, else None.

    Built from the item class (``validate_against``) in the resolved LinkML schema, so
    optional columns are never dropped. The schema fetch is a cache hit (already pulled
    during validation). Non-parquet or non-collection targets need no schema.
    """
    if config.output_format != "parquet" or config.collection_class is None:
        return None
    if config.schema_file is None or config.validate_against is None:
        return None
    schema_path = schema_provider.resolve_schema(target, config.schema_file, ctx)
    return write_mod.arrow_schema_for(schema_path, config.validate_against)


def _produce(source: Source, ctx: RunContext) -> tuple[Rows, Payload, Target]:
    """Run fetch → parse → transform → dedup → assemble → validate.

    Returns the deduped rows, the emit-ready payload, and the resolved target.
    """
    config = source.config
    target = get_target(config.target)
    rows = source.transform(source.parse(source.fetch(ctx)))
    rows = dedup_mod.dedup(rows, config, target, ctx)
    payload = _assemble(rows, config)
    _validate(payload, config, target, ctx)
    return rows, payload, target


def validate_source(source: Source, ctx: RunContext) -> int:
    """Run the pipeline up to and including validation; return the row count."""
    rows, _, _ = _produce(source, ctx)
    return len(rows)


def run_source(source: Source, ctx: RunContext) -> Path:
    """Run the full pipeline. Emits to ``output_dir``; delivers a PR unless dry-run."""
    config = source.config
    _, payload, target = _produce(source, ctx)

    out_path = (
        ctx.output_dir
        / config.target
        / config.category
        / f"{config.name}{write_mod.EXTENSIONS[config.output_format]}"
    )
    arrow_schema = _arrow_schema(config, target, ctx)
    write_mod.write(payload, out_path, config.output_format, arrow_schema)

    if not ctx.dry_run:
        deliver_mod.deliver(
            [out_path],
            target,
            branch=f"import/{config.name}",
            title=f"Import {config.name}",
            body=f"Automated import of {config.name} by sentier_importers.",
            ctx=ctx,
        )
    return out_path
