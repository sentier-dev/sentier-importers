"""The 25 EF 3.1 impact categories as Sentier ``ImpactCategory`` terms."""

from sentier_importers.core.source import Source
from sentier_importers.core.types import RawData, Records, Rows
from sentier_importers.sources.agribalyse.ef_common import (
    EF31_SOURCE_IRI,
    distinct_method_names,
    impact_iri,
    parse_cf_table,
)


class AgribalyseEfImpactCategoriesSource(Source):
    """Emit one ``ImpactCategory`` per distinct EF 3.1 method name."""

    def parse(self, raw: RawData) -> Records:
        return parse_cf_table(raw)

    def transform(self, records: Records) -> Rows:
        return [
            {
                "iri": impact_iri(name),
                "pref_label": name,
                "source": EF31_SOURCE_IRI,
                "status": "draft",
            }
            for name in distinct_method_names(records)
        ]
