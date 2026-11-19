"""Registry module of ``agribalyse-ef-brightway-flows``."""

from sentier_importers.core.types import Records, Rows
from sentier_importers.sources.brightway_flows.agribalyse import (
    AgribalyseBrightwayFlowsBase,
    package_rows,
)


class AgribalyseBrightwayFlowsPackage(AgribalyseBrightwayFlowsBase):
    """``biosphere-1-brightway-flows.json``: the flows of the export that Brightway
    Flows links to an EF 3.1 flow."""

    def transform(self, records: Records) -> Rows:
        return package_rows(records[0]["state"])
