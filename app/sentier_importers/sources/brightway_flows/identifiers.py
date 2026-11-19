"""Brightway Flows' identifier for a BAFU or an Agribalyse flow, computed from our
own fields.

Reimplemented from the recipe their source documents (one ``uuid5`` call); no code
of theirs is copied. The identifier is used for the join only and is never emitted.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping

from sentier_importers.sources.agribalyse.simapro_flows import FlowRecord
from sentier_importers.sources.brightway_flows.correspondences import Link

BAFU_NAMESPACE = uuid.uuid5(uuid.NAMESPACE_URL, "https://vocab.brightway.one/bafu/")
AGRIBALYSE_NAMESPACE = uuid.uuid5(uuid.NAMESPACE_URL, "https://vocab.brightway.one/agribalyse/")
#: The Brightway Flows correspondence that holds the Agribalyse links.
AGRIBALYSE_LIST = "agribalyse-3.2"


def bafu_candidates(name: str, category: str, subcategory: str, unit: str) -> tuple[str, ...]:
    """The identifier of a plain flow, then the one of a regionalised flow whose place
    is the part of the name after the last ``", "``."""
    candidates = [str(uuid.uuid5(BAFU_NAMESPACE, "|".join((name, category, subcategory, unit))))]
    head, separator, place = name.rpartition(", ")
    if separator:
        seed = "|".join((head, category, subcategory, unit, place))
        candidates.append(str(uuid.uuid5(BAFU_NAMESPACE, seed)))
    return tuple(candidates)


def bafu_link(source: Mapping, by_id: Mapping[str, Link]) -> Link | None:
    """The Brightway Flows link of one of our BAFU ``source`` records, or ``None``."""
    context = list(source.get("context") or [])
    category = context[0] if context else ""
    subcategory = context[1] if len(context) > 1 else ""
    for candidate in bafu_candidates(
        source["name"], category, subcategory, source.get("unit") or ""
    ):
        if candidate in by_id:
            return by_id[candidate]
    return None


def agribalyse_id(name: str, compartment: str, subcompartment: str, unit: str) -> str:
    """The identifier of an Agribalyse flow, from the fields of the SimaPro export as
    they are spelled there (an absent sub-compartment is the empty string)."""
    seed = "|".join((name, compartment, subcompartment, unit))
    return str(uuid.uuid5(AGRIBALYSE_NAMESPACE, seed))


def agribalyse_link(flow: FlowRecord, by_id: Mapping[str, Link]) -> Link | None:
    """The Brightway Flows link of one Agribalyse ``flow``, or ``None``."""
    return by_id.get(agribalyse_id(flow.name, flow.compartment, flow.subcompartment, flow.unit))
