"""Describe the 25 EF 3.1 impact categories as Sentier ``ImpactCategory`` vocab terms."""

from sentier_importers.core.source import Source
from sentier_importers.core.types import RawData, Records, Rows
from sentier_importers.sources.agribalyse.ef_common import (
    EF31_SOURCE_IRI,
    VOCAB_METHOD_IRI,
    distinct_method_names,
    parse_cf_table,
    vocab_impact_iri,
)


class AgribalyseEfImpactCategoriesSource(Source):
    """Emit one ``ImpactCategory`` term per distinct EF 3.1 method name."""

    def parse(self, raw: RawData) -> Records:
        return parse_cf_table(raw)

    def transform(self, records: Records) -> Rows:
        return [
            {
                "iri": vocab_impact_iri(name),
                "pref_label": name,
                "method": VOCAB_METHOD_IRI,
                "source": EF31_SOURCE_IRI,
                "status": "draft",
            }
            for name in distinct_method_names(records)
        ]
