"""EF 3.1 characterization factors as Sentier ``CharacterizationFactor`` terms.

Each row links the EF 3.1 method + one impact category + one elementary flow to a numeric
factor. Flows are keyed by ``FLOW_uuid`` → ``flows/<uuid>`` (the agribalyse flow import),
so no ecoinvent nomenclature is involved.
"""

from sentier_importers.core.source import Source
from sentier_importers.core.types import RawData, Records, Rows
from sentier_importers.sources.agribalyse.ef_common import (
    METHOD_IRI,
    cf_iri,
    flow_iri,
    impact_iri,
    parse_cf_table,
)


class AgribalyseEfCfsSource(Source):
    """Emit one ``CharacterizationFactor`` per EF CF row (method+impact+flow → value)."""

    def parse(self, raw: RawData) -> Records:
        return parse_cf_table(raw)

    def transform(self, records: Records) -> Rows:
        rows: Rows = []
        seen: set[str] = set()
        for rec in records:
            uuid = rec.get("flow_uuid")
            method_name = (rec.get("method_name") or "").strip()
            value = rec.get("cf")
            if not uuid or not method_name or value is None:
                continue
            iri = cf_iri(method_name, uuid)
            if iri in seen:
                continue  # collapse duplicate (impact, flow) rows (e.g. regional variants)
            seen.add(iri)
            rows.append(
                {
                    "iri": iri,
                    "pref_label": f"EF 3.1 {method_name} CF for {uuid}",
                    "method": METHOD_IRI,
                    "impact_category": impact_iri(method_name),
                    "flow": flow_iri(uuid),
                    "factor_value": float(value),
                    "status": "draft",
                }
            )
        return rows
