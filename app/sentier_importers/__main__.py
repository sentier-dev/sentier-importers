"""Command-line entry point: ``python -m sentier_importers <command>``.

Commands:
  list                     print the registry with per-source status
  validate <source>        fetch/parse/transform/validate, no emit or deliver
  run <source> [--deliver] run one source's full pipeline (dry-run unless --deliver)
  run --all [--deliver]    run every enabled source
  run --release <release> [--deliver]
                           run every sentier-vocab source of one source release and
                           deliver its terms/ and relations/ files whole
"""

import argparse
import sys
from dataclasses import replace
from pathlib import Path

from platformdirs import user_cache_dir
from sentier_importers.core import inventory_lineage, lineage, pipeline, registry
from sentier_importers.core.context import RunContext
from sentier_importers.core.errors import MissingInputError, SentierImporterError

DEFAULT_CACHE_DIR = Path(user_cache_dir("sentier_importers"))
DEFAULT_OUTPUT_DIR = Path("output")


def _data_root(args: argparse.Namespace) -> Path:
    return registry.resolve_data_root(
        Path(args.data_root) if getattr(args, "data_root", None) else None
    )


def _ctx(args: argparse.Namespace) -> RunContext:
    return RunContext(
        data_root=_data_root(args),
        cache_dir=Path(args.cache_dir) if args.cache_dir else DEFAULT_CACHE_DIR,
        output_dir=Path(args.output_dir) if args.output_dir else DEFAULT_OUTPUT_DIR,
        dry_run=not getattr(args, "deliver", False),
        offline=getattr(args, "offline", False),
        schema_dir=Path(args.schema_dir) if getattr(args, "schema_dir", None) else None,
        deliver_local_root=(
            Path(args.deliver_local) if getattr(args, "deliver_local", None) else None
        ),
        lineage_dir=(Path(args.lineage_dir) if getattr(args, "lineage_dir", None) else None),
        lineage_store=(Path(args.lineage_store) if getattr(args, "lineage_store", None) else None),
        contributor=getattr(args, "contributor", None),
    )


def _add_common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--data-root",
        default=None,
        metavar="DIR",
        help=f"Directory the registry's file:// inputs are relative to (default: "
        f"${registry.DATA_ROOT_VAR}, else the parent of this checkout).",
    )
    parser.add_argument("--cache-dir", default=None, help="Fetch cache directory.")
    parser.add_argument("--output-dir", default=None, help="Where to stage emitted files.")
    parser.add_argument(
        "--lineage-store",
        type=Path,
        help="Historical run archive (default: <output-dir>/lineage/runs); "
        "current bundles stay adjacent.",
    )
    parser.add_argument(
        "--contributor", help="Optional public contributor identity recorded in lineage."
    )
    parser.add_argument("--offline", action="store_true", help="Fail on any fetch cache miss.")
    parser.add_argument(
        "--schema-dir",
        default=None,
        help="Read the vocab contracts (term.yaml, relation.yaml) from this local dir, and "
        "the existing terms from the terms/ folder beside it, instead of the target's "
        "pinned ref.",
    )
    parser.add_argument(
        "--lineage-dir",
        default=None,
        help="Write OpenLineage JSONL here (default: <output-dir>/lineage).",
    )
    parser.add_argument(
        "--input-file",
        type=Path,
        help="Use this local primary input instead of the registry fetch URL (one source only).",
    )
    parser.add_argument(
        "--source-provenance",
        metavar="DECLARATION",
        help="Use a source_datasets declaration for the primary input (one source only).",
    )


def _configured(config, args):
    if getattr(args, "input_file", None) is not None:
        config = replace(config, fetch_url=f"file://{args.input_file.expanduser().resolve()}")
    if getattr(args, "source_provenance", None):
        provenance = {
            **config.input_provenance,
            **registry.source_provenance(args.source_provenance),
        }
        if set(config.inputs) - provenance.keys():
            raise SentierImporterError(
                "Declare provenance for secondary inputs in the registry first"
            )
        config = replace(config, input_provenance=provenance)
    return config


def cmd_list(args: argparse.Namespace) -> None:
    for config in registry.load_registry(data_root=_data_root(args)):
        status = "enabled" if config.enabled else "disabled"
        print(
            f"{config.name:40} -> {config.target}/{config.category}  "
            f"[{status}]  {config.access}"
        )


def _warn_skipped(exc: MissingInputError, name: str) -> None:
    """A licensed or private input is absent on this machine: say so and move on.

    The message already names the input, the root used, and the access class.
    """
    print(f"warning: {exc}\nskipped {name}", file=sys.stderr)


def cmd_validate(args: argparse.Namespace) -> None:
    config = _configured(registry.get_config(args.source, data_root=_data_root(args)), args)
    source = registry.load_source(config)
    ctx = _ctx(args)
    try:
        count = pipeline.validate_source(source, ctx)
    except MissingInputError as exc:
        _warn_skipped(exc, config.name)
        return
    print(f"{config.name}: {count} rows valid")
    print(f"lineage {lineage.lineage_path(config, ctx)}")


def _run_release(release: str, ctx: RunContext) -> None:
    configs = [
        config
        for config in registry.load_registry(data_root=ctx.data_root)
        if config.term_type is not None and config.category == release
    ]
    if not configs:
        raise SentierImporterError(f"no sentier-vocab source has category {release!r}")
    try:
        files = pipeline.run_release(release, [registry.load_source(c) for c in configs], ctx)
    except MissingInputError as exc:
        _warn_skipped(exc, release)
        return
    for file in files:
        print(f"wrote {file}")


def cmd_run(args: argparse.Namespace) -> None:
    ctx = _ctx(args)
    if args.release:
        _run_release(args.release, ctx)
        return
    if args.all:
        if args.input_file or args.source_provenance:
            raise SentierImporterError(
                "--input-file and --source-provenance require a single source"
            )
        configs = [c for c in registry.load_registry(data_root=ctx.data_root) if c.enabled]
    else:
        if not args.source:
            raise SentierImporterError("run requires a <source> name, --all or --release")
        configs = [registry.get_config(args.source, data_root=ctx.data_root)]
    for config in configs:
        config = _configured(config, args)
        try:
            out = pipeline.run_source(registry.load_source(config), ctx)
        except MissingInputError as exc:
            _warn_skipped(exc, config.name)
            continue
        print(f"wrote {out}")
        print(f"lineage {lineage.lineage_path(config, ctx)}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="sentier_importers")
    sub = parser.add_subparsers(dest="command", required=True)

    lst = sub.add_parser("list", help="List registered sources.")
    _add_common(lst)
    lst.set_defaults(func=cmd_list)

    val = sub.add_parser("validate", help="Validate a source without emitting.")
    val.add_argument("source")
    _add_common(val)
    val.set_defaults(func=cmd_validate)

    run = sub.add_parser("run", help="Run a source's full pipeline.")
    run.add_argument("source", nargs="?", default=None)
    run.add_argument("--all", action="store_true", help="Run every enabled source.")
    run.add_argument(
        "--release",
        default=None,
        metavar="RELEASE",
        help="Run every sentier-vocab source of this source release (their registry "
        "category, e.g. bafu-2026) and deliver the release whole.",
    )
    run.add_argument("--deliver", action="store_true", help="Open a PR (default: dry-run).")
    run.add_argument(
        "--deliver-local",
        default=None,
        metavar="PATH",
        help="Copy emitted files into this local target checkout (no git/PR).",
    )
    _add_common(run)
    run.set_defaults(func=cmd_run)

    inspection = sub.add_parser(
        "inspect-lineage", help="Inspect a recorded local artifact in Marquez."
    )
    inventory_lineage.add_arguments(inspection)
    inspection.set_defaults(func=inventory_lineage.command)

    return parser


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    try:
        args.func(args)
    except SentierImporterError as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
