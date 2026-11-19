"""Registry module of the four ``bafu-ef-align-*`` blocks."""

from sentier_importers.sources.brightway_flows.bafu_sources import BafuBrightwayFlowsAlignSource


class BafuBrightwayFlowsAlign(BafuBrightwayFlowsAlignSource):
    """The package named by ``emit_filename``, targets aligned."""
