"""BAFU-2026 -> sentier-inventory ``exchanges.parquet``, one registry entry per sector.

Flow ids: the production flow is the process's own UUID; technosphere
inputs resolve through the export-wide ``number -> uuid`` map (exact — 0
unresolved of 114,369 in the real export); biosphere flows get the deterministic
uuid5 shared with the ``bafu-elementary-flows`` vocab terms, so inventory and
vocabulary stay joinable by construction.
"""

from sentier_importers.core.source import Source
from sentier_importers.core.types import RawData, Record, Records, Rows
from sentier_importers.sources.bafu import ecospold


def _flow_type(group: str, group_code: int | None) -> str:
    if group == "output" and group_code == 0:
        return "production"
    if group_code == 4:  # inputGroup 4 = FromNature, outputGroup 4 = ToNature
        return "biosphere"
    return "technosphere"


class BafuInventoryExchangesSource(Source):
    """Emit the exchange table rows for one sector folder."""

    def parse(self, raw: RawData) -> Records:
        return ecospold.parse_ecospold_zip(raw)

    def transform(self, records: Records) -> Rows:
        number_to_uuid = {r["number"]: r["uuid"] for r in records}
        rows: Rows = []
        for record in records:
            if ecospold.sector_for(record["category"]) != self.config.category:
                continue
            for exchange in record["exchanges"]:
                flow_type = _flow_type(exchange["group"], exchange["group_code"])
                if flow_type == "production":
                    flow = record["uuid"]
                elif flow_type == "biosphere":
                    flow = ecospold.flow_id(
                        exchange["name"], exchange["category"], exchange["subcategory"]
                    )
                else:
                    # exact by construction; an unmapped number stays as-is for triage
                    flow = number_to_uuid.get(exchange["number"], exchange["number"])
                utype, loc, scale = ecospold.bw_uncertainty(
                    exchange["amount"], exchange["uncertainty_type"], exchange["sd95"]
                )
                row: Record = {
                    "process_id": record["uuid"],
                    "flow": flow,
                    "flow_name": exchange["name"],
                    "flow_type": flow_type,
                    "direction": exchange["group"],
                    "amount": exchange["amount"],
                    "unit": exchange["unit"],
                    "location": exchange["location"],
                    "uncertainty_type": utype,
                    "loc": loc,
                    "scale": scale,
                    "minimum": None,  # not present in the EcoSpold1 export
                    "maximum": None,
                }
                rows.append(row)
        return rows
