"""The two inputs the EF 3.1 flow parts read exact EF context paths from, in small."""

import io

import pyarrow as pa
import pyarrow.parquet as pq
from sentier_importers.core.types import RawData

from tests.sources.brightway_flows_fixture import document, gz, link

JRC_COLUMNS = ["FLOW_uuid", "FLOW_name", "FLOW_class0", "FLOW_class1", "FLOW_class2"]


def entry(identifier, context, target=None, unit="KiloGM"):
    """An EF flow's entry in the EF correspondence of Brightway Flows."""
    return link(
        "ef/3.1", identifier, target or identifier, match="exactMatch", context=context, unit=unit
    )


def bw_bytes(ef_links=(), flows=(), redirects=()) -> bytes:
    return gz(document(flows, {"ef-3.1": list(ef_links)}, redirects=redirects))


def jrc_bytes(rows) -> bytes:
    """A JRC CF table of ``(uuid, name, class0, class1, class2)`` rows."""
    table = pa.table(
        {c: pa.array([r[i] for r in rows], pa.string()) for i, c in enumerate(JRC_COLUMNS)}
    )
    buffer = io.BytesIO()
    pq.write_table(table, buffer)
    return buffer.getvalue()


def raw(content: bytes, name: str = "x") -> RawData:
    return RawData(content=content, source_url=f"memory://{name}")
