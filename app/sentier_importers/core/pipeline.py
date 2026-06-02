"""Pipeline driver: run a source through fetch → parse → transform → validate →
emit → deliver."""

from pathlib import Path

from sentier_importers.core import deliver as deliver_mod
from sentier_importers.core import schema_provider
from sentier_importers.core import validate as validate_mod
from sentier_importers.core import write as write_mod
from sentier_importers.core.context import RunContext
from sentier_importers.core.source import Source
from sentier_importers.core.targets import Target, get_target
from sentier_importers.core.types import Rows


def _produce_rows(source: Source, ctx: RunContext) -> tuple[Rows, Target]:
    """Run fetch → parse → transform → validate; return rows and the resolved target."""
    config = source.config
    target = get_target(config.target)
    rows = source.transform(source.parse(source.fetch(ctx)))

    schema_path = None
    if config.validate_against is not None:
        schema_path = schema_provider.resolve_schema(target, config.validate_against, ctx)
    validate_mod.validate(rows, target.validator, schema_path, config.validate_against)
    return rows, target


def validate_source(source: Source, ctx: RunContext) -> int:
    """Run the pipeline up to and including validation; return the row count."""
    rows, _ = _produce_rows(source, ctx)
    return len(rows)


def run_source(source: Source, ctx: RunContext) -> Path:
    """Run the full pipeline. Emits to ``output_dir``; delivers a PR unless dry-run."""
    config = source.config
    rows, target = _produce_rows(source, ctx)

    out_path = (
        ctx.output_dir
        / config.target
        / config.category
        / f"{config.name}{write_mod.EXTENSIONS[config.output_format]}"
    )
    write_mod.write(rows, out_path, config.output_format)

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
