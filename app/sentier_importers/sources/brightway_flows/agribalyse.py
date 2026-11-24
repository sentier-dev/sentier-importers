"""Brightway Flows links for the ``agribalyse-3.2 -> ef-3.1`` pair: shared logic.

Two sources build on this module: the first package of the pair
(``agribalyse_package``) and its review sidecar (``agribalyse_review``). They share
one parsed :class:`State` and the same decisions, so every flow of the export is in
exactly one of the two files.

The source side of an entry is the flow as the SimaPro export spells it: ``name``,
``context`` (the compartment, then the sub-compartment when there is one) and ``unit``.
Brightway Flows normalises its own labels; a consumer sees the raw ones. The link is
found through their identifier recipe on those raw fields (``identifiers``).

A usable link (``links.decide``, then the unit rule) becomes an entry whether or not
its target scores: this package is the main content of the pair, so there is no
decisions file here (plan 2026-11-19, decision 2).
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass

from sentier_importers.core import fetch as fetch_mod
from sentier_importers.core.context import RunContext
from sentier_importers.core.errors import ValidationError
from sentier_importers.core.source import Source
from sentier_importers.core.types import RawData, Record, Records, Rows
from sentier_importers.matching.ef_index import EfFlowIndex, LabelDefect, load_label_defects
from sentier_importers.sources.agribalyse.simapro_flows import FlowRecord, read_flows
from sentier_importers.sources.brightway_flows import links
from sentier_importers.sources.brightway_flows.correspondences import (
    PINNED_SHA256,
    BrightwayFlows,
    Link,
    load,
)
from sentier_importers.sources.brightway_flows.factors import Vector, compare, factor_vectors
from sentier_importers.sources.brightway_flows.identifiers import (
    AGRIBALYSE_LIST,
    agribalyse_id,
    agribalyse_link,
)
from sentier_importers.sources.brightway_flows.links import Decision
from sentier_importers.sources.randonneur_data.ecoinvent_ef_biosphere import vocab_flow_codes

_VOCAB_DIR = "ef_vocab"
_FLOWS, _EF_CFS = "flows", "ef_cfs"
#: Inputs ``parse`` reads from ``self.inputs`` (``ef_vocab`` is a directory, read apart).
_REQUIRED = (_FLOWS, _EF_CFS)
#: ``kind`` of a review row written here: a flow of the export without an entry.
WITHHELD = "withheld"

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class State:
    bw: BrightwayFlows
    by_id: Mapping[str, Link]
    #: Identifiers of the flows Brightway Flows refuses to link (``Final waste flows``).
    excluded: frozenset[str]
    flows: tuple[FlowRecord, ...]
    #: Exchanged names the export gives no substance row, so no unit: not flows.
    dropped: int
    index: EfFlowIndex
    vectors: Mapping[str, Vector]
    vocab_codes: frozenset[str]
    defects: Mapping[str, LabelDefect]


def source_record(flow: FlowRecord) -> Record:
    """The source side of an entry: the raw fields of the export."""
    context = [flow.compartment]
    if flow.subcompartment:
        context.append(flow.subcompartment)
    return {"name": flow.name, "unit": flow.unit, "context": context}


def decide(flow: FlowRecord, state: State) -> Decision:
    """Whether Brightway Flows gives ``flow`` a usable EF target, and if not, why."""
    identifier = agribalyse_id(flow.name, flow.compartment, flow.subcompartment, flow.unit)
    if identifier in state.excluded:
        return Decision(None, None, None, "excluded_by_brightway_flows")
    return links.decide(source_record(flow), agribalyse_link(flow, state.by_id), state)


def package_entry(flow: FlowRecord, state: State) -> tuple[Record | None, str | None]:
    """The package entry of ``flow``, or ``(None, reason)``."""
    decision = decide(flow, state)
    if decision.reason is not None:
        return None, decision.reason
    source = source_record(flow)
    unit, factor, reason = links.their_unit(source, decision, state)
    if reason is not None:
        return None, reason
    return links.link_entry(source, decision, unit, factor, state), None


def _sorted(rows: Rows) -> Rows:
    return sorted(rows, key=lambda row: links.order(row["source"]))


def package_rows(state: State) -> Rows:
    """One entry per flow with a usable link, sorted by name, context and unit."""
    rows: Rows = []
    seen: set[tuple[str, tuple[str, ...]]] = set()
    for flow in state.flows:
        entry, _ = package_entry(flow, state)
        if entry is None:
            continue
        key = (entry["source"]["name"], tuple(entry["source"]["context"]))
        if key in seen:
            raise ValidationError(
                f"two flows share the source key {key!r}: a consumer keyed on name and "
                f"context could not tell them apart"
            )
        seen.add(key)
        rows.append(entry)
    return _sorted(rows)


def _review_row(flow: FlowRecord, state: State) -> Record | None:
    entry, reason = package_entry(flow, state)
    if entry is not None:
        return None
    decision = decide(flow, state)
    row: Record = {"source": source_record(flow), "kind": WITHHELD, "reason": reason}
    if decision.target is not None:
        _, methods = compare({}, state.vectors.get(decision.target.code, {}))
        row["methods"] = list(methods)
    row["brightway_flows"] = links.their_record(decision)
    return row


def review_rows(state: State) -> Rows:
    """One row per flow without an entry: the reason, what Brightway Flows links it
    to (``None`` when it links it to nothing) and, for an EF target, the methods in
    which that target scores."""
    rows = [row for flow in state.flows if (row := _review_row(flow, state)) is not None]
    return _sorted(rows)


class AgribalyseBrightwayFlowsBase(Source):
    """Fetch and parse every input once into a :class:`State`."""

    #: ``None`` in tests, which feed a synthetic file.
    expected_sha256: str | None = PINNED_SHA256

    def fetch(self, ctx: RunContext) -> RawData:
        """Fetch every named input except ``ef_vocab`` (a directory, read in ``parse``)."""
        self.inputs = {
            name: fetch_mod.fetch(url, ctx)
            for name, url in self.config.inputs.items()
            if name != _VOCAB_DIR
        }
        return fetch_mod.fetch(self.config.fetch_url, ctx)

    def parse(self, raw: RawData) -> Records:
        missing = [name for name in _REQUIRED if name not in self.inputs]
        if missing:
            raise RuntimeError(
                f"inputs {missing} not fetched: the registry block must declare "
                f"{list(_REQUIRED)}"
            )
        bw = load(raw.content, self.expected_sha256)
        vocab_dir = fetch_mod.local_path(self.config.inputs.get(_VOCAB_DIR), _VOCAB_DIR)
        cf_bytes = self.inputs[_EF_CFS].content
        flows, dropped = read_flows(self.inputs[_FLOWS].content)
        if dropped:
            logger.warning(
                f"{self.config.name}: {dropped} exchanged name(s) without a substance "
                f"row are not flows"
            )
        state = State(
            bw=bw,
            by_id={item.source_id: item for item in bw.links.get(AGRIBALYSE_LIST, ())},
            excluded=bw.excluded.get(AGRIBALYSE_LIST, frozenset()),
            flows=tuple(flows),
            dropped=dropped,
            # reads ef-context notations, as the BAFU pair does: the flows of the
            # Brightway Flows 1.1 part of the ef-3.1 release are link targets here
            index=EfFlowIndex.from_bytes(cf_bytes, vocab_dir, include_uncharacterised=True),
            vectors=factor_vectors(cf_bytes),
            vocab_codes=vocab_flow_codes(vocab_dir),
            defects=load_label_defects(),
        )
        return [{"state": state}]
