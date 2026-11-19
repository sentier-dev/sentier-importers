"""Registry module of ``agribalyse-ef-brightway-flows-review``."""

from sentier_importers.core.types import Records, Rows
from sentier_importers.sources.brightway_flows.agribalyse import (
    AgribalyseBrightwayFlowsBase,
    review_rows,
)


class AgribalyseBrightwayFlowsReview(AgribalyseBrightwayFlowsBase):
    """``brightway_flows_review.json``: every flow of the export without an entry in
    the package, with the reason."""

    def transform(self, records: Records) -> Rows:
        return review_rows(records[0]["state"])
