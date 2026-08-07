"""Validate a staged BAFU-2026 regeneration.

Checks, over ``--output-dir`` (default ``output/bafu-2026``):
  1. dataset count across sector processes files == expected (11,947 for v1);
  2. no duplicate process ids; every exchange's process_id has a process row;
  3. zero unresolved technosphere flows (every technosphere ``flow`` is a process id);
  4. every biosphere ``flow`` id exists in the staged vocab flow terms;
  5. exchange enum values within the sentier-inventory schema enums;
  6. required columns non-null; per-file size < 50 MB;
  7. every sector metadata.json validates against schema/metadata.schema.json
     (when a sentier-inventory checkout is given via --inventory-repo);
  8. optional LCIA-workbook coverage: every (name, location) in BAFU's own
     results workbook appears in the staged processes (encoding-normalized).

Exit code 0 = all checks pass.
"""

import argparse
import json
import sys
import zipfile
from pathlib import Path

import pyarrow.parquet as pq

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))

from sentier_importers.sources.bafu.ecospold import SECTORS  # noqa: E402

FLOW_TYPES = {"production", "technosphere", "biosphere"}
DIRECTIONS = {"input", "output"}
PROCESS_TYPES = {"unit", "system", "lci_result"}
MAX_FILE_MB = 50

_FAILURES: list[str] = []


def check(ok: bool, message: str) -> None:
    print(("PASS " if ok else "FAIL ") + message)
    if not ok:
        _FAILURES.append(message)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", default="output/bafu-2026")
    parser.add_argument("--expected-datasets", type=int, default=11947)
    parser.add_argument("--inventory-repo", default=None)
    parser.add_argument("--lcia-workbook", default=None)
    args = parser.parse_args()

    out = Path(args.output_dir)
    inventory = out / "sentier_inventory"

    processes = []
    exchanges = []
    for sector in SECTORS:
        for name, acc in (("processes", processes), ("exchanges", exchanges)):
            path = inventory / sector / f"{name}.parquet"
            if path.exists():
                acc.append(pq.read_table(path))
                size_mb = path.stat().st_size / 1e6
                check(size_mb < MAX_FILE_MB, f"{sector}/{name}.parquet {size_mb:.1f} MB < 50 MB")

    import pyarrow as pa

    # permissive: a sector where e.g. every uncertainty value is null infers a
    # different column type than one with values; unify instead of failing
    proc = pa.concat_tables(processes, promote_options="permissive")
    exch = pa.concat_tables(exchanges, promote_options="permissive")

    ids = proc.column("process_id").to_pylist()
    check(
        len(ids) == args.expected_datasets,
        f"process count {len(ids)} == {args.expected_datasets}",
    )
    id_set = set(ids)
    check(len(id_set) == len(ids), "process ids unique")

    for column in ("name", "reference_product", "reference_unit", "location"):
        nulls = proc.column(column).null_count
        check(nulls == 0, f"processes.{column} non-null ({nulls} nulls)")
    check(
        set(proc.column("process_type").to_pylist()) <= PROCESS_TYPES,
        "process_type within enum",
    )

    check(
        set(exch.column("flow_type").to_pylist()) <= FLOW_TYPES
        and set(exch.column("direction").to_pylist()) <= DIRECTIONS,
        "exchange enums within schema",
    )
    check(
        set(exch.column("process_id").to_pylist()) <= id_set,
        "every exchange belongs to a staged process",
    )

    flows = exch.to_pylist()
    tech_unresolved = sum(
        1
        for r in flows
        if r["flow_type"] in ("production", "technosphere") and r["flow"] not in id_set
    )
    check(
        tech_unresolved == 0,
        f"technosphere/production flows resolved ({tech_unresolved} dangling)",
    )

    vocab_flow_files = sorted((out / "sentier_vocab" / "elementary-flows").glob("*.parquet"))
    if vocab_flow_files:
        vocab_ids: set[str] = set()
        for path in vocab_flow_files:
            vocab_ids.update(pq.read_table(path).column("notation").to_pylist())
        bio_missing = {
            r["flow"]
            for r in flows
            if r["flow_type"] == "biosphere" and r["flow"] not in vocab_ids
        }
        check(not bio_missing, f"biosphere flows all in vocab terms ({len(bio_missing)} missing)")
    else:
        check(False, "staged vocab elementary-flows parquet found")

    if args.inventory_repo:
        import jsonschema

        schema = json.loads(
            (Path(args.inventory_repo) / "schema" / "metadata.schema.json").read_text()
        )
        for sector in SECTORS:
            meta_path = inventory / sector / "metadata.json"
            if meta_path.exists():
                jsonschema.validate(json.loads(meta_path.read_text()), schema)
        check(True, "sector metadata.json files validate")

    if args.lcia_workbook:
        import pandas as pd

        wb_path = args.lcia_workbook
        if wb_path.endswith(".zip"):
            with zipfile.ZipFile(wb_path) as z:
                inner = next(n for n in z.namelist() if n.endswith(".xlsx"))
                wb_path = z.extract(inner, Path(args.output_dir) / "_lcia")
        df = pd.ExcelFile(wb_path).parse(sheet_name=1, header=1)

        def fix(text: str) -> str:
            # undo the workbook's (possibly repeated) UTF-8-read-as-cp1252 mojibake;
            # some rows need latin-1 (chars in the 0x80-0x9f control range)
            for _ in range(3):
                try:
                    text = text.encode("cp1252").decode("utf-8")
                except (UnicodeEncodeError, UnicodeDecodeError):
                    try:
                        text = text.encode("latin-1").decode("utf-8")
                    except (UnicodeEncodeError, UnicodeDecodeError):
                        break
            return text

        wanted = {fix(p) for p in df["Product"].dropna().astype(str)}
        have = {
            f"{name} - {location}"
            for name, location in zip(
                proc.column("name").to_pylist(), proc.column("location").to_pylist()
            )
        }

        def matches(product: str) -> bool:
            if product in have:
                return True
            # the workbook relabels some export locations: "US-ERCOT" for the bare
            # grid region "ERCOT", "RER without CH" for "Europe without Switzerland"
            name, sep, location = product.rpartition(" - ")
            if not sep:
                return False
            if location.startswith("US-") and f"{name} - {location[3:]}" in have:
                return True
            return location == "RER without CH" and f"{name} - Europe without Switzerland" in have

        missing = {p for p in wanted if not matches(p)}
        check(not missing, f"LCIA workbook coverage ({len(missing)} of {len(wanted)} missing)")

    print()
    if _FAILURES:
        print(f"{len(_FAILURES)} check(s) FAILED")
        raise SystemExit(1)
    print("all checks passed")


if __name__ == "__main__":
    main()
