"""Shard a large vocab-collection parquet into several sub-limit parquet files.

sentier-vocab commits data as parquet directly (no git-LFS, no release assets) and its
``check-added-large-files`` guard caps a single file at 3 MB. A bulk import that exceeds
that (e.g. the 79k EF elementary-flows list, ~13 MB zstd) is split here into contiguous
shards, each under the limit. The vocab builder merges every parquet in a category dir,
so N shards load identically to one file — no data is dropped.

Each shard preserves the collection ``scheme`` in the parquet's Arrow key-value metadata
(the vocab loader reads it from there), and rows keep their original order.

Usage:
    python scripts/shard_collection.py INPUT.parquet OUTDIR STEM [--limit-bytes N]

Writes ``OUTDIR/STEM-01.parquet`` … keeping every shard file strictly under the limit.
"""

from __future__ import annotations

import argparse
import io
import math
import sys
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

#: Default per-shard ceiling. 2.5 MB leaves margin under sentier-vocab's 3 MB guard.
DEFAULT_LIMIT = 2_500_000
_COMPRESSION = "zstd"
_LEVEL = 9


def _write(table: pa.Table, path: Path, scheme: bytes | None) -> int:
    if scheme is not None:
        table = table.replace_schema_metadata({b"scheme": scheme})
    pq.write_table(table, path, compression=_COMPRESSION, compression_level=_LEVEL)
    return path.stat().st_size


def shard(input_path: Path, outdir: Path, stem: str, limit: int) -> list[Path]:
    table = pq.read_table(input_path)
    scheme = (table.schema.metadata or {}).get(b"scheme")
    outdir.mkdir(parents=True, exist_ok=True)

    # Baseline full-file size to estimate the shard count, then grow the count until
    # every shard is under the limit (heavy outlier rows can make a chunk overshoot).
    buf = io.BytesIO()
    pq.write_table(table, buf, compression=_COMPRESSION, compression_level=_LEVEL)
    total = len(buf.getvalue())
    n = max(1, math.ceil(total / limit))

    while True:
        rows_per = math.ceil(table.num_rows / n)
        shards: list[Path] = []
        oversized = False
        for i in range(n):
            chunk = table.slice(i * rows_per, rows_per)
            if chunk.num_rows == 0:
                continue
            path = outdir / f"{stem}-{i + 1:02d}.parquet"
            size = _write(chunk, path, scheme)
            shards.append(path)
            if size > limit:
                oversized = True
        if not oversized:
            return shards
        for path in shards:  # discard and retry with more shards
            path.unlink()
        n += 1


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("input")
    ap.add_argument("outdir")
    ap.add_argument("stem")
    ap.add_argument("--limit-bytes", type=int, default=DEFAULT_LIMIT)
    args = ap.parse_args()
    shards = shard(Path(args.input), Path(args.outdir), args.stem, args.limit_bytes)
    total_rows = sum(pq.read_metadata(p).num_rows for p in shards)
    print(f"wrote {len(shards)} shard(s), {total_rows} rows total:")
    for p in shards:
        print(f"  {p}  ({p.stat().st_size / 1e6:.2f} MB, {pq.read_metadata(p).num_rows} rows)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
