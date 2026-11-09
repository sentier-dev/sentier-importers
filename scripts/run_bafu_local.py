"""Run the full BAFU-2026 local regeneration: every ``bafu-*`` source, one process.

Stages all vocab + inventory artifacts under ``--output-dir`` (dry-run —
nothing is pushed anywhere): the vocab release as ``sentier_vocab/terms/`` and
``sentier_vocab/relations/``, the inventory as ``sentier_inventory/bafu-2026-v1/``
with the source folder's ``metadata.json`` beside its two parquet files. It
optionally places that folder into a local sentier-inventory checkout as
``data/bafu-2026-v1/``, replacing it whole. To place the vocab release into a
local sentier-vocab checkout, run
``sentier-importers run --release bafu-2026 --deliver-local ../sentier-vocab``.
Running all sources in one process lets them share a single parse of the 37 MB
EcoSpold zip (see the memo in ``sources/bafu/ecospold.py``).

Usage:
    uv run python scripts/run_bafu_local.py \\
        [--output-dir output/bafu-2026] [--cache-dir cache] \\
        [--data-root ~/dds] [--schema-dir ../sentier-vocab/schemas] \\
        [--inventory-clone ../sentier-inventory]
"""

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))

import pyarrow.compute as pc  # noqa: E402
import pyarrow.parquet as pq  # noqa: E402
from sentier_importers.core import deliver as deliver_mod  # noqa: E402
from sentier_importers.core import pipeline, registry  # noqa: E402
from sentier_importers.core.context import RunContext  # noqa: E402
from sentier_importers.core.targets import VOCAB_CONTRACT, get_target  # noqa: E402
from sentier_importers.sources.bafu import ecospold  # noqa: E402
from sentier_importers.sources.bafu.provenance import (  # noqa: E402
    BAFU_PROVENANCE,
    BAFU_PROVENANCE_IRI,
)

#: sentier-inventory schema contract this script emits (``sector`` column, source folders).
INVENTORY_SCHEMA_VERSION = "0.3.0"


def _distinct(table, column: str) -> list[str]:
    return sorted(pc.drop_null(table.column(column).combine_chunks()).unique().to_pylist())


def _assert_single_source(processes) -> None:
    """One folder holds one release of one source: every row must say so."""
    for column, expected in (
        ("source", ecospold.SOURCE_ID),
        ("source_version", ecospold.SOURCE_VERSION),
    ):
        seen = _distinct(processes, column)
        if seen != [expected]:
            raise ValueError(f"staged processes.{column} must be [{expected!r}], found {seen}")


def source_metadata(folder: Path) -> dict:
    """``metadata.json`` content for the staged source folder.

    ``sectors`` and ``row_counts`` are read from the staged parquet; the bibliographic
    keys come from ``sources/bafu/provenance.py`` and ``ecospold``. Raises
    ``FileNotFoundError`` without a ``processes.parquet`` and ``ValueError`` when the
    rows do not all carry this source and version.
    """
    processes = pq.read_table(
        folder / "processes.parquet", columns=["sector", "source", "source_version"]
    )
    _assert_single_source(processes)
    row_counts = {"processes.parquet": processes.num_rows}
    exchanges = folder / "exchanges.parquet"
    if exchanges.exists():
        row_counts["exchanges.parquet"] = pq.read_metadata(exchanges).num_rows
    return {
        "source": ecospold.SOURCE_ID,
        "source_version": ecospold.SOURCE_VERSION,
        "title": BAFU_PROVENANCE["title"],
        "publisher": BAFU_PROVENANCE["publisher"],
        "license": ecospold.LICENSE_SUMMARY,
        "citation": BAFU_PROVENANCE["citation"],
        "source_url": None,
        "provenance": BAFU_PROVENANCE_IRI,
        "retrieved": None,
        "schema_version": INVENTORY_SCHEMA_VERSION,
        "sectors": _distinct(processes, "sector"),
        "row_counts": row_counts,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", default="output/bafu-2026")
    parser.add_argument("--cache-dir", default="cache")
    parser.add_argument(
        "--data-root",
        default=None,
        help="Directory the registry's file:// inputs are relative to "
        "(default: $SENTIER_DATA_ROOT, else the parent of this checkout).",
    )
    parser.add_argument(
        "--schema-dir",
        default=None,
        help="Local sentier-vocab schemas dir holding term.yaml and relation.yaml "
        "(default: ../sentier-vocab/schemas when present).",
    )
    parser.add_argument(
        "--inventory-clone",
        default=None,
        help="Also place the inventory folder (parquet + metadata.json) into this "
        "sentier-inventory checkout as data/%s/." % ecospold.SOURCE_FOLDER,
    )
    args = parser.parse_args()

    schema_dir = Path(args.schema_dir) if args.schema_dir else None
    if schema_dir is None:
        default = Path(__file__).resolve().parents[2] / "sentier-vocab" / "schemas"
        schema_dir = default if default.is_dir() else None

    data_root = registry.resolve_data_root(Path(args.data_root) if args.data_root else None)
    ctx = RunContext(
        cache_dir=Path(args.cache_dir),
        output_dir=Path(args.output_dir),
        dry_run=True,
        schema_dir=schema_dir,
        data_root=data_root,
    )

    configs = [c for c in registry.load_registry(data_root=data_root) if c.name.startswith("bafu")]
    vocab = [c for c in configs if get_target(c.target).validator == VOCAB_CONTRACT]
    for config in configs:
        if config in vocab:
            continue  # a vocab source is one part of the release staged below
        start = time.monotonic()
        out = pipeline.run_source(registry.load_source(config), ctx)
        print(f"{config.name:36} -> {out}  ({time.monotonic() - start:.1f}s)", flush=True)

    start = time.monotonic()
    release_files = pipeline.run_release(
        ecospold.SOURCE_ID, [registry.load_source(c) for c in vocab], ctx
    )
    for file in release_files:
        print(f"{'vocab release ' + ecospold.SOURCE_ID:36} -> {file}", flush=True)
    print(f"{'':36}    ({time.monotonic() - start:.1f}s)", flush=True)

    folder = ctx.output_dir / "sentier_inventory" / ecospold.SOURCE_FOLDER
    metadata = source_metadata(folder)
    (folder / "metadata.json").write_text(
        json.dumps(metadata, indent=2, ensure_ascii=False) + "\n"
    )
    print(f"metadata.json {ecospold.SOURCE_FOLDER}: {metadata['row_counts']}")

    if args.inventory_clone:
        clone = Path(args.inventory_clone)
        files = sorted(folder.glob("*.parquet")) + [folder / "metadata.json"]
        deliver_mod.deliver_local(
            files, get_target("sentier_inventory"), category=ecospold.SOURCE_FOLDER, root=clone
        )
        print(f"placed data/{ecospold.SOURCE_FOLDER}/ into {clone}")


if __name__ == "__main__":
    main()
