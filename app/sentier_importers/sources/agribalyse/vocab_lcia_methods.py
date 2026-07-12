"""Describe EF 3.1 as a Sentier ``LCIAMethod`` vocab term (the descriptive layer).

The numeric CF data lives in sentier-methods; here we mint the canonical method term and
link its impact categories. Reads the EF CF parquet only to enumerate the categories.
"""

from sentier_importers.core.source import Source
from sentier_importers.core.types import RawData, Records, Rows
from sentier_importers.sources.agribalyse.ef_common import (
    EF31_SOURCE_IRI,
    VOCAB_METHOD_IRI,
    distinct_method_names,
    parse_cf_table,
    vocab_impact_iri,
)


class AgribalyseEfLciaMethodSource(Source):
    """Emit the single EF 3.1 ``LCIAMethod`` term, linking its impact categories."""

    def parse(self, raw: RawData) -> Records:
        return parse_cf_table(raw)

    def transform(self, records: Records) -> Rows:
        impacts = [vocab_impact_iri(name) for name in distinct_method_names(records)]
        return [
            {
                "iri": VOCAB_METHOD_IRI,
                "pref_label": "Environmental Footprint 3.1",
                "methodology": "European Commission Environmental Footprint (EF) 3.1",
                "impact_categories": impacts,
                "source": EF31_SOURCE_IRI,
                "status": "draft",
            }
        ]
