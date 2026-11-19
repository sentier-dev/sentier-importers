"""EF 3.1 flows only Brightway Flows 1.1.0 lists, as ``sentier_vocab`` terms.

The ``ef-3.1`` release takes its flows from the earlier edition of the harmonised
list (``agribalyse.flows``) and from the JRC CF table (``agribalyse.flows_jrc``).
Brightway Flows 1.1.0 lists EF 3.1 flows neither part has (878 at 2026-11-19,
nomenclature only, no factor), and a link of theirs onto one of them cannot be a
sentier-mappings target without a term. This third part of the release emits
exactly those (spec 2026-11-19-ef31-flows-from-brightway-flows-1.1).

- One row per EF flow of the 1.1.0 list whose UUID neither existing part emits: the
  ``harmonised`` input excludes by the harmonised part's own rule
  (``flows.is_emitted``), the ``jrc`` input by every ``FLOW_uuid`` of the CF table.
- Label, alt labels, CAS number, formula and ``ec:`` notations as the harmonised part.
  The JRC flow name is an alt label when it differs from the list's label.
- ``compartment`` and an ``ef-context:<path>`` notation from the flow's own entry in
  the file's EF correspondence, which gives the exact EF context path. A flow without
  such an entry is an error, never a row without context.
- No ``bw-context`` notation: the 1.1.0 context codes do not mean what the codes of
  the earlier edition mean (``envi-air-grle-ru10pesq``), and ``matching.bw_context``
  would misread them. ``matching.ef_index`` places these flows from ``ef-context``.
"""

from __future__ import annotations

import io
from collections.abc import Mapping

import pyarrow as pa
import pyarrow.parquet as pq
from sentier_importers.core.errors import ParseError, ValidationError
from sentier_importers.core.source import Source
from sentier_importers.core.types import RawData, Record, Records, Rows
from sentier_importers.matching.bw_context import EF_CONTEXT_PREFIX
from sentier_importers.sources.agribalyse.flows import FLOWS_SCHEME, alt_labels
from sentier_importers.sources.agribalyse.flows_jrc import (
    _harmonised_identifiers,
    compartment_for_path,
)
from sentier_importers.sources.agribalyse.provenance import PROVENANCE_IRI
from sentier_importers.sources.brightway_flows.correspondences import (
    EF_LIST,
    PINNED_SHA256,
    RELEASE,
    BrightwayFlows,
    ConsensusFlow,
    Link,
    load,
)

#: Registry input: the earlier edition of the harmonised list (the harmonised part).
HARMONISED = "harmonised"
#: Registry input: the JRC CF table, whose flows the harmonised and JRC parts cover.
JRC = "jrc"
_JRC_UUID = "FLOW_uuid"
#: Level separator of a context path in the file, and in an ``ef-context`` notation.
_FILE_SEP = "/"
_SEP = " / "


def _jrc_identifiers(raw: RawData) -> frozenset[str]:
    """Every flow UUID of the JRC CF table: the two existing parts cover them all."""
    try:
        column = pq.read_table(io.BytesIO(raw.content), columns=[_JRC_UUID]).column(_JRC_UUID)
    except (OSError, ValueError, KeyError, pa.ArrowException) as exc:
        raise ParseError(f"{JRC} is not the JRC CF table: {exc!r}") from exc
    identifiers = frozenset(str(v).strip() for v in column.to_pylist() if v)
    if not identifiers:
        # Nothing to exclude would re-emit every flow of the JRC part.
        raise ParseError(f"{JRC}: no flow uuid found in the JRC CF table")
    return identifiers


def context_path(context: str) -> str:
    """An EF context path of the file (``a/b/c``) in the ``" / "``-joined spelling."""
    return _SEP.join(level.strip() for level in context.split(_FILE_SEP) if level.strip())


def _own_links(bw: BrightwayFlows) -> Mapping[str, Link]:
    """Each EF flow's own entry in the EF correspondence, keyed by its identifier."""
    own: dict[str, Link] = {}
    for item in bw.links.get(EF_LIST, ()):
        if item.source_id != item.target_id:
            continue  # an EF flow merged into another one
        known = own.setdefault(item.source_id, item)
        if context_path(known.context) != context_path(item.context):
            raise ValidationError(
                f"{item.source_id}: two context paths in the EF correspondence, "
                f"{known.context!r} and {item.context!r}"
            )
    return own


def _row(flow: ConsensusFlow, label: str, link: Link) -> Record:
    path = context_path(link.context)
    if not path:
        raise ValidationError(f"{flow.identifier}: empty context path in the EF correspondence")
    row: Record = {"iri": f"{FLOWS_SCHEME}{flow.identifier}", "pref_label": label}
    if alt := alt_labels([*flow.alt_labels, link.label], label):
        row["alt_labels"] = alt
    if cas := next((c for c in flow.cas_numbers if c), None):
        row["cas_number"] = cas
    if flow.formula:
        row["formula"] = flow.formula
    if (compartment := compartment_for_path(path)) is not None:
        row["compartment"] = compartment
    row["additional_notations"] = [
        *(f"ec:{ec}" for ec in flow.ec_numbers if ec),
        f"{EF_CONTEXT_PREFIX}{path}",
    ]
    row["source"] = PROVENANCE_IRI
    row["status"] = "draft"
    return row


class BrightwayFlowsVocabSource(Source):
    """Emit the EF 3.1 flows of Brightway Flows 1.1.0 the release does not have yet."""

    #: ``None`` in tests, which feed a synthetic file.
    expected_sha256: str | None = PINNED_SHA256

    def parse(self, raw: RawData) -> Records:
        return [{"bw": load(raw.content, self.expected_sha256)}]

    def _excluded(self) -> frozenset[str]:
        """Identifiers the two existing parts of the release emit."""
        for name in (HARMONISED, JRC):
            if name not in self.inputs:
                raise ValidationError(f"input {name!r} not fetched: the registry must declare it")
        return _harmonised_identifiers(self.inputs[HARMONISED].content) | _jrc_identifiers(
            self.inputs[JRC]
        )

    def transform(self, records: Records) -> Rows:
        bw: BrightwayFlows = records[0]["bw"]
        skip = self._excluded()
        own = _own_links(bw)
        new = [
            (flow, flow.label.strip())
            for flow in bw.flows.values()
            if flow.is_ef and flow.identifier not in skip and flow.label.strip()
        ]
        orphans = sorted(flow.identifier for flow, _ in new if flow.identifier not in own)
        if orphans:
            raise ValidationError(
                f"{len(orphans)} EF flow(s) of {RELEASE} have no entry of their own in the "
                f"EF correspondence, so no EF context: {orphans[:5]}"
            )
        rows = [_row(flow, label, own[flow.identifier]) for flow, label in new]
        return sorted(rows, key=lambda row: row["iri"])
