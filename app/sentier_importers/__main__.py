"""Command-line entry point: ``python -m sentier_importers <command>``.

Commands:
  list                     print the registry with per-source status
  validate <source>        fetch/parse/transform/validate, no emit or deliver
  run <source> [--deliver] run one source's full pipeline (dry-run unless --deliver)
  run --all [--deliver]    run every enabled source
"""

import argparse
import sys
from pathlib import Path

from platformdirs import user_cache_dir
from sentier_importers.core import pipeline, registry
from sentier_importers.core.context import RunContext
from sentier_importers.core.errors import SentierImporterError

DEFAULT_CACHE_DIR = Path(user_cache_dir("sentier_importers"))
DEFAULT_OUTPUT_DIR = Path("output")


def _ctx(args: argparse.Namespace) -> RunContext:
    return RunContext(
        cache_dir=Path(args.cache_dir) if args.cache_dir else DEFAULT_CACHE_DIR,
        output_dir=Path(args.output_dir) if args.output_dir else DEFAULT_OUTPUT_DIR,
        dry_run=not getattr(args, "deliver", False),
        offline=getattr(args, "offline", False),
    )


def _add_common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--cache-dir", default=None, help="Fetch cache directory.")
    parser.add_argument("--output-dir", default=None, help="Where to stage emitted files.")
    parser.add_argument("--offline", action="store_true", help="Fail on any fetch cache miss.")


def cmd_list(args: argparse.Namespace) -> None:
    for config in registry.load_registry():
        status = "enabled" if config.enabled else "disabled"
        print(f"{config.name:24} -> {config.target}/{config.category}  [{status}]")


def cmd_validate(args: argparse.Namespace) -> None:
    config = registry.get_config(args.source)
    source = registry.load_source(config)
    count = pipeline.validate_source(source, _ctx(args))
    print(f"{config.name}: {count} rows valid")


def cmd_run(args: argparse.Namespace) -> None:
    ctx = _ctx(args)
    if args.all:
        configs = [c for c in registry.load_registry() if c.enabled]
    else:
        if not args.source:
            raise SentierImporterError("run requires a <source> name or --all")
        configs = [registry.get_config(args.source)]
    for config in configs:
        out = pipeline.run_source(registry.load_source(config), ctx)
        print(f"wrote {out}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="sentier_importers")
    sub = parser.add_subparsers(dest="command", required=True)

    lst = sub.add_parser("list", help="List registered sources.")
    lst.set_defaults(func=cmd_list)

    val = sub.add_parser("validate", help="Validate a source without emitting.")
    val.add_argument("source")
    _add_common(val)
    val.set_defaults(func=cmd_validate)

    run = sub.add_parser("run", help="Run a source's full pipeline.")
    run.add_argument("source", nargs="?", default=None)
    run.add_argument("--all", action="store_true", help="Run every enabled source.")
    run.add_argument("--deliver", action="store_true", help="Open a PR (default: dry-run).")
    _add_common(run)
    run.set_defaults(func=cmd_run)

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
