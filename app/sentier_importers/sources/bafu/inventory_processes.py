"""BAFU-2026 -> sentier-inventory ``processes.parquet``, one registry entry per release.

The entry's ``category`` is the source folder (``ecospold.SOURCE_FOLDER``,
``data/bafu-2026-v1/``); ``transform`` emits every dataset and writes the sector
its BAFU category routes to into the ``sector`` column. Rows follow the
sentier-inventory ``schema/process.yaml`` contract exactly, column order included.
"""

from sentier_importers.core.source import Source, TransformStep
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
    """Emit the process table rows of the whole release, sector as a column."""

    def parse(self, raw: RawData) -> Records:
        return ecospold.parse_ecospold_zip(raw)

    def transform(self, records: Records) -> Rows:
        """Compatibility entry point; the pipeline uses ``transform_steps``."""
        return self._map_process_rows(records)

    def transform_steps(self) -> tuple[TransformStep, ...]:
        return (
            TransformStep(
                name="map-process-rows",
                description=("Map BAFU process records to inventory rows with source and sector."),
                function=self._map_process_rows,
                code_dependencies=("app/sentier_importers/sources/bafu/ecospold.py",),
            ),
        )

    def _map_process_rows(self, records: Records) -> Rows:
        rows: Rows = []
        for record in records:
            row: Record = {
                "process_id": record["uuid"],
                "name": record["name"],
                # EcoSpold1: the reference function IS the produced product
                "reference_product": record["name"],
                "reference_unit": record["unit"],
                "reference_amount": record["amount"],
                "location": record["location"],
                "process_type": "unit",
                "sector": ecospold.sector_for(record["category"]),
                "source": ecospold.SOURCE_ID,
                "source_version": ecospold.SOURCE_VERSION,
                "technology": record["technology"],
                "comment": process_comment(record),
            }
            rows.append(row)
        return rows
