"""EF 3.1 methods table for sentier-methods: one row per (method, impact category)."""

from sentier_importers.core.source import Source
from sentier_importers.core.types import RawData, Records, Rows
from sentier_importers.sources.agribalyse.ef_common import (
    DATASOURCE,
    METHOD_NAME,
    METHOD_SOURCE,
    distinct_method_names,
    method_id,
    parse_cf_table,
    unit_for,
)


class AgribalyseEfMethodsSource(Source):
    """Emit ``methods.parquet`` rows — one per EF 3.1 impact category."""

    def parse(self, raw: RawData) -> Records:
        return parse_cf_table(raw)

    def transform(self, records: Records) -> Rows:
        return [
            {
                "method_id": method_id(name),
                "method_name": METHOD_NAME,
                "impact_category": name,
                "unit": unit_for(name),
                "methodology": "Environmental Footprint",
                "source": METHOD_SOURCE,
                "datasource": DATASOURCE,
            }
            for name in distinct_method_names(records)
        ]
