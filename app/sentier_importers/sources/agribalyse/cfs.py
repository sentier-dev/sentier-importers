"""EF 3.1 characterization factors for sentier-methods (one row per CF).

Keyed to the vocab flow IRIs (``flows/<FLOW_uuid>``) and the EF methods table
(``method_id``). CF values are JRC-public and uncapped; no ecoinvent involvement.
"""

from sentier_importers.core.source import Source
from sentier_importers.core.types import RawData, Records, Rows
from sentier_importers.sources.agribalyse.ef_common import (
    flow_iri,
    method_id,
    parse_cf_table,
    unit_for,
)


class AgribalyseEfCfsSource(Source):
    """Emit ``characterization-factors.parquet`` rows (method+impact+flow -> value)."""

    def parse(self, raw: RawData) -> Records:
        return parse_cf_table(raw)

    def transform(self, records: Records) -> Rows:
        rows: Rows = []
        seen: set[tuple] = set()
        for rec in records:
            uuid = rec.get("flow_uuid")
            impact = rec.get("method_name") or ""
            value = rec.get("cf")
            if not uuid or not impact or value is None:
                continue
            location = rec.get("location")
            key = (impact, uuid, location)
            if key in seen:
                continue  # keep distinct (impact, flow, location) — preserves regional CFs
            seen.add(key)
            row = {
                "method_id": method_id(impact),
                "impact_category": impact,
                "flow": flow_iri(uuid),
                "flow_name": rec.get("flow_name") or "",
                "factor_value": float(value),
                "unit": unit_for(impact),
            }
            if rec.get("flow_context"):
                row["flow_context"] = rec["flow_context"]
            if location:
                row["location"] = location
            rows.append(row)
        return rows
