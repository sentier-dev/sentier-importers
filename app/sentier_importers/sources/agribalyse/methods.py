"""EF 3.1 as a single Sentier ``LCIAMethod`` term covering its 25 impact categories."""

from sentier_importers.core.source import Source
from sentier_importers.core.types import RawData, Records, Rows
from sentier_importers.sources.agribalyse.ef_common import (
    EF31_SOURCE_IRI,
    METHOD_IRI,
    distinct_method_names,
    impact_iri,
    parse_cf_table,
)


class AgribalyseEfMethodsSource(Source):
    """Emit the one EF 3.1 ``LCIAMethod``, linking its 25 impact categories."""

    def parse(self, raw: RawData) -> Records:
        return parse_cf_table(raw)

    def transform(self, records: Records) -> Rows:
        impacts = [impact_iri(name) for name in distinct_method_names(records)]
        return [
            {
                "iri": METHOD_IRI,
                "pref_label": "Environmental Footprint 3.1",
                "methodology": "European Commission Environmental Footprint (EF) 3.1",
                "impact_categories": impacts,
                "source": EF31_SOURCE_IRI,
                "status": "draft",
            }
        ]
