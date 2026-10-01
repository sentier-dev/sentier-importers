"""Run the full BAFU-2026 local regeneration: every ``bafu-*`` source, one process.

Stages all vocab + inventory artifacts under ``--output-dir`` (dry-run —
nothing is pushed anywhere), emits each sector's ``metadata.json`` beside its
parquet, and optionally places the inventory files into a local
sentier-inventory checkout.
Running all sources in one process lets them share a single parse of the 37 MB
EcoSpold zip (see the memo in ``sources/bafu/ecospold.py``).

Usage:
    uv run python scripts/run_bafu_local.py \
        [--output-dir output/bafu-2026] [--cache-dir cache] \
        [--data-root ~/dds] [--schema-dir ../sentier-vocab/schemas] \
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
from sentier_importers.core.targets import get_target  # noqa: E402
from sentier_importers.sources.bafu.ecospold import CITATION, SECTORS  # noqa: E402

#: Human titles for the sector folders' metadata.json.
SECTOR_TITLES = {
    "01-agriculture": "Agriculture",
    "02-electricity": "Electricity",
    "03-chemicals": "Chemicals",
    "04-transport": "Transport",
    "05-energy": "Energy carriers",
    "06-waste": "Waste management",
    "07-construction": "Construction",
    "08-materials": "Materials",
    "09-electronics": "Electronics",
    "10-building-services": "Building services",
    "99-obsolete": "Obsolete (BAFU legacy link targets)",
}


#: sentier-inventory schema contract this script emits (``source`` column + ``sources``).
INVENTORY_SCHEMA_VERSION = "0.2.0"


def staged_sources(folder: Path) -> list[str]:
    """Distinct ``processes.source`` values in a staged folder, sorted; [] without a file.

    Read from the parquet rather than from a constant so the list stays right when a
    second importer stages rows into the same sector folder.
    """
    path = folder / "processes.parquet"
    if not path.exists():
        return []
    column = pq.read_table(path, columns=["source"]).column("source")
    return sorted(pc.drop_null(column.combine_chunks()).unique().to_pylist())


def sector_metadata(sector: str, folder: Path) -> dict:
    """metadata.json content for one staged sector folder, row counts and sources included."""
    row_counts = {}
    for table in ("processes", "exchanges"):
        path = folder / f"{table}.parquet"
        if path.exists():
            row_counts[f"{table}.parquet"] = pq.read_metadata(path).num_rows
    return {
        "sector": sector.split("-", 1)[1],
        "title": SECTOR_TITLES[sector],
        "description": f"Source: {CITATION}.",
        "rank": int(sector.split("-", 1)[0]),
        "schema_version": INVENTORY_SCHEMA_VERSION,
        "sources": staged_sources(folder),
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
    parser.add_argument("--schema-dir", default=None, help="Local sentier-vocab schemas dir.")
    parser.add_argument(
        "--inventory-clone",
        default=None,
        help="Also place inventory files + metadata.json into this sentier-inventory checkout.",
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
    for config in configs:
        start = time.monotonic()
        out = pipeline.run_source(registry.load_source(config), ctx)
        print(f"{config.name:36} -> {out}  ({time.monotonic() - start:.1f}s)", flush=True)

    inventory_root = ctx.output_dir / "sentier_inventory"
    for sector in SECTORS:
        folder = inventory_root / sector
        if not folder.is_dir():
            continue
        metadata = sector_metadata(sector, folder)
        (folder / "metadata.json").write_text(json.dumps(metadata, indent=2) + "\n")
        print(f"metadata.json {sector}: {metadata['row_counts']}")

    if args.inventory_clone:
        clone = Path(args.inventory_clone)
        target = get_target("sentier_inventory")
        for sector in SECTORS:
            folder = inventory_root / sector
            files = sorted(folder.glob("*.parquet")) + [folder / "metadata.json"]
            deliver_mod.deliver_local(
                [f for f in files if f.exists()], target, category=sector, root=clone
            )
        print(f"placed inventory files into {clone}")


if __name__ == "__main__":
    main()
