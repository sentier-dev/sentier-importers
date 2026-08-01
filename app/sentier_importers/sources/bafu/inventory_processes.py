"""BAFU-2026 -> sentier-inventory ``processes.parquet``, one registry entry per sector.

Each entry's ``category`` is the sector folder (``NN-<sector>``); ``transform``
keeps only the datasets whose BAFU category routes there. Rows follow the
sentier-inventory ``schema/process.yaml`` contract exactly.
"""

from sentier_importers.core.source import Source
from sentier_importers.core.types import RawData, Record, Records, Rows
from sentier_importers.sources.bafu import ecospold


def process_comment(record: Record) -> str:
    """Provenance comment: BAFU category path, obsolete marker, source citation."""
    parts = [f"BAFU category: {record['category']} / {record['subcategory']}"]
    if record["obsolete"]:
        parts.append("obsolete/not maintained in BAFU:2026")
    parts.append(f"Source: {ecospold.CITATION}")
    return ". ".join(parts)


class BafuInventoryProcessesSource(Source):
    """Emit the process table rows for one sector folder."""

    def parse(self, raw: RawData) -> Records:
        return ecospold.parse_ecospold_zip(raw)

    def transform(self, records: Records) -> Rows:
        rows: Rows = []
        for record in records:
            if ecospold.sector_for(record["category"]) != self.config.category:
                continue
            row: Record = {
                "process_id": record["uuid"],
                "name": record["name"],
                # EcoSpold1: the reference function IS the produced product
                "reference_product": record["name"],
                "reference_unit": record["unit"],
                "reference_amount": record["amount"],
                "location": record["location"],
                "process_type": "unit",
                "technology": record["technology"],
                "comment": process_comment(record),
            }
            rows.append(row)
        return rows
