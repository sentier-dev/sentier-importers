"""Shared helpers for the EF 3.1 LCIA import (methods, impact categories, CFs).

All three slices read the same JRC ``EF-LCIAMethod_CF(EF-v3.1)`` characterization-factor
parquet. EF 3.1 is a single *method* covering 25 *impact categories*; each CF links a
method + impact category + elementary flow to a numeric factor. CFs are keyed by
``FLOW_uuid`` — the same EF UUID our imported flows use — so they re-key cleanly onto
``flows/<uuid>`` with no ecoinvent involvement.

This module holds no ``Source`` subclass (the registry contract is one Source per module);
the method/impact/CF Source classes live in their own modules and import these helpers.
"""

import io

import pyarrow.parquet as pq
from sentier_importers.core.dedup import slugify
from sentier_importers.core.types import RawData, Records

#: IRI of the single EF 3.1 LCIA method.
METHOD_IRI = "https://vocab.sentier.dev/lcia-methods/ef-3.1"
#: Base of the EF 3.1 impact-category IRIs.
IMPACT_SCHEME = "https://vocab.sentier.dev/impact-categories/"
#: Base of the flow IRIs CFs point at (must match agribalyse.flows).
FLOWS_SCHEME = "https://vocab.sentier.dev/flows/"
#: Base of the CF IRIs.
CF_SCHEME = "https://vocab.sentier.dev/characterization-factors/"
#: EF 3.1 Source provenance IRI (shared with the flow import).
EF31_SOURCE_IRI = "https://vocab.sentier.dev/sources/ef-3.1"

#: Columns read from the EF CF parquet.
_COL_FLOW_UUID = "FLOW_uuid"
_COL_METHOD_NAME = "LCIAMethod_name"
_COL_CF = "CF EF3.1"


def impact_iri(method_name: str) -> str:
    """IRI for an EF impact category (one per distinct ``LCIAMethod_name``)."""
    return f"{IMPACT_SCHEME}ef-3.1/{slugify(method_name)}"


def flow_iri(flow_uuid: str) -> str:
    """Flow IRI a CF points at — matches the agribalyse flow import."""
    return f"{FLOWS_SCHEME}{flow_uuid}"


def cf_iri(method_name: str, flow_uuid: str) -> str:
    """Deterministic CF IRI: method (ef-3.1) + impact-category slug + flow uuid."""
    return f"{CF_SCHEME}ef-3.1_{slugify(method_name)}_{flow_uuid}"


def parse_cf_table(raw: RawData) -> Records:
    """Read the EF CF parquet into compact per-row records (uuid, method, cf)."""
    table = pq.read_table(
        io.BytesIO(raw.content), columns=[_COL_FLOW_UUID, _COL_METHOD_NAME, _COL_CF]
    )
    data = table.to_pydict()
    return [
        {"flow_uuid": u, "method_name": m, "cf": c}
        for u, m, c in zip(
            data[_COL_FLOW_UUID], data[_COL_METHOD_NAME], data[_COL_CF], strict=True
        )
    ]


def distinct_method_names(records: Records) -> list[str]:
    """Ordered distinct ``LCIAMethod_name`` values (the 25 EF impact categories)."""
    seen: dict[str, None] = {}
    for rec in records:
        name = (rec.get("method_name") or "").strip()
        if name and name not in seen:
            seen[name] = None
    return list(seen)
