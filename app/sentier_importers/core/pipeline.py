"""Pipeline driver: run a source through
fetch → parse → transform → dedup → assemble → validate → emit → deliver.

A sentier-vocab source is one *part* of a source release: the release is what gets
delivered, as ``terms/<release>[-NN].parquet`` and ``relations/<release>.parquet``.
``run_source`` stages a part for inspection only; ``run_release`` runs every part of
a release, validates the whole against the vocab contracts and delivers it.
"""

from pathlib import Path

from sentier_importers.core import dedup as dedup_mod
from sentier_importers.core import deliver as deliver_mod
from sentier_importers.core import schema_provider, vocab_contract, vocab_layers
from sentier_importers.core import write as write_mod
from sentier_importers.core.context import RunContext
from sentier_importers.core.errors import DeliveryError, FetchError, RegistryError
from sentier_importers.core.registry import DATA_ROOT_VAR
from sentier_importers.core.source import Source, SourceConfig
from sentier_importers.core.targets import VOCAB_CONTRACT, Target, get_target
from sentier_importers.core.types import Payload, Rows

#: Staging folder, under ``<output_dir>/<target>/``, of single vocab parts.
PARTS_DIR = "parts"


def _assemble(rows: Rows, config: SourceConfig) -> Payload:
    """Wrap rows into a randonneur mapping package when ``package_verb`` is set,
    else pass them through."""
    if config.package_verb is not None:
        return {
            "name": config.package_name,
            "version": config.package_version,
            config.package_verb: rows,
        }
    return rows


def _is_vocab(target: Target) -> bool:
    return target.validator == VOCAB_CONTRACT


def _contract(target: Target, ctx: RunContext) -> vocab_contract.Contract:
    """The vocab term and relation contracts at the target's pinned ref."""
    return vocab_contract.load_contract(
        schema_provider.resolve_schema(target, "term", ctx),
        schema_provider.resolve_schema(target, "relation", ctx),
    )


#: What to do about a missing local input, by the source's ``access`` class.
ACCESS_HINTS = {
    "public": "This input is a public sentier-* checkout: clone it beside this repo "
    "or point the data root at it.",
    "licensed": "This input is obtainable from its provider under the provider's "
    "terms; place it at the path above.",
    "private": "This input is a DdS-internal artifact and is not distributed with " "the repo.",
}


def _input_name(config: SourceConfig, url: str | None) -> str:
    """The registry key (``fetch.url`` or an ``inputs`` name) that declares ``url``."""
    if url == config.fetch_url:
        return "fetch.url"
    for name, candidate in config.inputs.items():
        if candidate == url:
            return name
    return "an input"


def _with_input_context(exc: FetchError, config: SourceConfig, ctx: RunContext) -> FetchError:
    """Re-wrap a fetch failure with the source, the registry input, the data root
    that was used, and what the ``access`` class says about obtaining the file.
    The exception type is preserved so the CLI can tell a missing local input
    (warn and skip) from any other fetch failure (error)."""
    root = ctx.data_root if ctx.data_root is not None else "(not set)"
    lines = [
        str(exc),
        f"  input:  {_input_name(config, exc.url)} of source {config.name!r}",
        f"  root:   {root}  (override with --data-root or {DATA_ROOT_VAR})",
        f"  access: {config.access}. {ACCESS_HINTS[config.access]}",
    ]
    return type(exc)("\n".join(lines), url=exc.url)


def _fetch(source: Source, ctx: RunContext):
    try:
        return source.fetch(ctx)
    except FetchError as exc:
        raise _with_input_context(exc, source.config, ctx) from exc


def _rows(source: Source, target: Target, ctx: RunContext) -> Rows:
    """Run fetch → parse → transform → dedup."""
    rows = source.transform(source.parse(_fetch(source, ctx)))
    return dedup_mod.dedup(rows, source.config, target, ctx)


def _vocab_part(source: Source, target: Target, ctx: RunContext) -> tuple[Rows, Rows]:
    """One vocab source's rows as ``(term rows, relation rows)`` of its release."""
    config = source.config
    return vocab_layers.to_layers(
        _rows(source, target, ctx),
        term_type=config.term_type,
        release=config.category,
        contract=_contract(target, ctx),
    )


def validate_source(source: Source, ctx: RunContext) -> int:
    """Run the pipeline up to and including validation; return the row count."""
    config = source.config
    target = get_target(config.target)
    if not _is_vocab(target):
        return len(_rows(source, target, ctx))
    terms, relations = _vocab_part(source, target, ctx)
    vocab_contract.validate(
        terms, relations, _contract(target, ctx), release=config.category, complete=False
    )
    return len(terms)


def _run_vocab_part(source: Source, target: Target, ctx: RunContext) -> Path:
    config = source.config
    if ctx.deliver_local_root is not None or not ctx.dry_run:
        raise DeliveryError(
            f"source {config.name!r} is one part of the vocab release {config.category!r}; "
            f"a release is delivered whole: run --release {config.category}"
        )
    terms, relations = _vocab_part(source, target, ctx)
    vocab_contract.validate(
        terms, relations, _contract(target, ctx), release=config.category, complete=False
    )
    root = ctx.output_dir / config.target / PARTS_DIR / config.name
    return vocab_layers.write_layers(terms, relations, root, config.category)[
        vocab_layers.TERMS_DIR
    ][0]


def run_source(source: Source, ctx: RunContext) -> Path:
    """Run the full pipeline. Emits to ``output_dir``; delivers a PR unless dry-run.

    A vocab source is staged as its two layer files under
    ``<output_dir>/<target>/parts/<source>/`` and is never delivered on its own.
    """
    config = source.config
    target = get_target(config.target)
    if _is_vocab(target):
        return _run_vocab_part(source, target, ctx)

    payload = _assemble(_rows(source, target, ctx), config)
    filename = config.emit_filename or config.name
    out_path = (
        ctx.output_dir
        / config.target
        / config.category
        / f"{filename}{write_mod.EXTENSIONS[config.output_format]}"
    )
    write_mod.write(payload, out_path, config.output_format)

    if ctx.deliver_local_root is not None:
        deliver_mod.deliver_local(
            [out_path], target, category=config.category, root=ctx.deliver_local_root
        )

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


def run_release(release: str, sources: list[Source], ctx: RunContext) -> list[Path]:
    """Run every part of the vocab release ``release`` and deliver it whole.

    ``sources`` are the registry's sources whose ``category`` is ``release``; they
    must share one vocab target. Stages ``terms/<release>[-NN].parquet`` and
    ``relations/<release>.parquet`` under ``<output_dir>/<target>/``, then places
    them into ``ctx.deliver_local_root`` and/or opens a PR, replacing the release's
    previous files. Returns the staged files.
    """
    targets = {source.config.target for source in sources}
    if len(targets) != 1 or any(source.config.category != release for source in sources):
        raise RegistryError(
            f"release {release!r} needs at least one source, all with category "
            f"{release!r} and the same target"
        )
    target = get_target(targets.pop())
    if not _is_vocab(target):
        raise RegistryError(f"target {target.name!r} is not delivered per release")

    terms: Rows = []
    relations: Rows = []
    for source in sources:
        part_terms, part_relations = _vocab_part(source, target, ctx)
        terms.extend(part_terms)
        relations.extend(part_relations)
    vocab_contract.validate(
        terms, relations, _contract(target, ctx), release=release, complete=True
    )
    written = vocab_layers.write_layers(terms, relations, ctx.output_dir / target.name, release)

    if ctx.deliver_local_root is not None:
        deliver_mod.place_release(written, ctx.deliver_local_root, release)
    deliver_mod.deliver_release(
        written,
        target,
        release,
        branch=f"import/{release}",
        title=f"Import {release}",
        body=f"Automated import of the {release} release by sentier_importers.",
        ctx=ctx,
    )
    return [path for files in written.values() for path in files]
