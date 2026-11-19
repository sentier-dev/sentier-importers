"""The exact EF context of an EF 3.1 flow, and its leaf: the ``sub_compartment``
attribute of the flow's vocab term (spec 2026-11-19-units-on-ef31-flows, decision
on step 3).

A flow term states its coarse ``compartment`` (air, water, soil, ...). Its
``sub_compartment`` is the last level of the flow's EF context path, as JRC spells
it: ``Emissions to water, unspecified (long-term)``, or ``Land occupation`` for the
two-level path ``Land use / Land occupation``. With it, and the unit, a flow's
(label, compartment, sub_compartment, unit) tells apart what EF tells apart.

The path is the exact one, from one of two sources, in this order:

1. the flow's own entry in the EF correspondence of Brightway Flows 1.1.0 (the link
   whose source is the flow, also when it merges the flow into another one);
2. the JRC CF table (``FLOW_class0..2``), for a characterised flow without an entry.

A flow neither source places (an identifier Brightway Flows records as redirected)
takes the path of the flow it is redirected to, by the same two sources: the rule
already used for its unit. Only a flow whose redirect leads to no placed flow has no
``sub_compartment``. The path is never inferred from the flow's ``bw-context`` code,
whose crosswalk to EF contexts is ambiguous. Where
both sources place a flow they must agree: a disagreement is an error listing the
flows.
"""

from __future__ import annotations

import io
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass

import pyarrow as pa
import pyarrow.parquet as pq
from sentier_importers.core.errors import ParseError, ValidationError
from sentier_importers.core.types import RawData, Record
from sentier_importers.sources.brightway_flows.correspondences import (
    EF_LIST,
    PINNED_SHA256,
    BrightwayFlows,
    load,
)

#: Registry input: Brightway Flows 1.1.0 ``harmonised-flows-simple.json.gz``.
BRIGHTWAY_FLOWS = "brightway_flows"
#: Registry input: the JRC CF table.
JRC = "jrc"
#: The term attribute holding the leaf.
SUB_COMPARTMENT = "sub_compartment"
JRC_UUID = "FLOW_uuid"
JRC_CLASSES = ("FLOW_class0", "FLOW_class1", "FLOW_class2")
#: Level separator of a context path in the Brightway Flows file.
_FILE_SEP = "/"
#: Level separator of a context path as this package spells it.
SEP = " / "
#: How many identifiers an error or log line names.
_LISTED = 10


def correspondence_path(context: str) -> str:
    """A context path of the Brightway Flows file (``a/b/c``), ``" / "``-joined."""
    return SEP.join(level.strip() for level in context.split(_FILE_SEP) if level.strip())


def jrc_path(record: Record) -> str:
    """The EF context path of a CF row, ``" / "``-joined, empty levels dropped."""
    return SEP.join(
        str(record[c]).strip() for c in JRC_CLASSES if isinstance(record.get(c), str) and record[c]
    )


def leaf(path: str) -> str:
    """The last level of a context path."""
    return path.split(SEP)[-1]


def _one_path_each(pairs: Iterable[tuple[str, str]], where: str) -> dict[str, str]:
    paths: dict[str, str] = {}
    for identifier, path in pairs:
        if paths.setdefault(identifier, path) != path:
            raise ValidationError(
                f"{identifier!r}: two context paths in {where}, "
                f"{paths[identifier]!r} and {path!r}"
            )
    return paths


def correspondence_paths(bw: BrightwayFlows) -> dict[str, str]:
    """The context path of each EF identifier's own entry in the EF correspondence."""
    pairs = (
        (item.source_id, correspondence_path(item.context)) for item in bw.links.get(EF_LIST, ())
    )
    return _one_path_each(((i, p) for i, p in pairs if p), "the EF correspondence")


def jrc_paths(content: bytes) -> dict[str, str]:
    """The context path of each flow of the JRC CF table (``content``: the parquet)."""
    try:
        records = pq.read_table(io.BytesIO(content), columns=[JRC_UUID, *JRC_CLASSES]).to_pylist()
    except (OSError, ValueError, KeyError, pa.ArrowException) as exc:
        raise ParseError(f"{JRC} is not the JRC CF table: {exc!r}") from exc
    pairs = ((str(r.get(JRC_UUID) or "").strip(), jrc_path(r)) for r in records)
    return _one_path_each(((i, p) for i, p in pairs if i and p), "the JRC CF table")


@dataclass(frozen=True)
class EfContexts:
    """The exact context path of EF flows, by identifier, from both sources.

    ``resolve`` gives the identifier a redirected one stands for
    (``BrightwayFlows.resolve``); without it no redirect is followed.
    """

    correspondence: Mapping[str, str]
    jrc: Mapping[str, str]
    resolve: Callable[[str], str] | None = None

    def __post_init__(self) -> None:
        differing = sorted(
            identifier
            for identifier, path in self.correspondence.items()
            if self.jrc.get(identifier, path) != path
        )
        if differing:
            shown = [(i, self.correspondence[i], self.jrc[i]) for i in differing[:_LISTED]]
            raise ValidationError(
                f"{len(differing)} EF flow(s) have one context path in the EF correspondence "
                f"and another in the JRC CF table: {shown}"
            )

    def _stated(self, identifier: str) -> str | None:
        return self.correspondence.get(identifier) or self.jrc.get(identifier)

    def path(self, identifier: str) -> str | None:
        """The path of the flow itself, else of the flow it is redirected to."""
        own = self._stated(identifier)
        if own is not None or self.resolve is None:
            return own
        target = self.resolve(identifier)
        return self._stated(target) if target != identifier else None

    def leaf(self, identifier: str) -> str | None:
        path = self.path(identifier)
        return leaf(path) if path else None


def brightway_flows(
    inputs: Mapping[str, RawData], expected_sha256: str | None = PINNED_SHA256
) -> BrightwayFlows:
    """The Brightway Flows file of the ``BRIGHTWAY_FLOWS`` registry input."""
    if BRIGHTWAY_FLOWS not in inputs:
        raise ValidationError(
            f"input {BRIGHTWAY_FLOWS!r} not fetched: the registry must declare it"
        )
    return load(inputs[BRIGHTWAY_FLOWS].content, expected_sha256)


def unplaced_message(part: str, unplaced: list[str], total: int) -> str:
    """The log line of a flow part about its flows without a ``sub_compartment``."""
    line = (
        f"{part}: {len(unplaced)} of {total} flows without a {SUB_COMPARTMENT} (no entry in "
        "the EF correspondence, not in the JRC CF table, no redirect to a flow either places)"
    )
    return f"{line}: {unplaced[:_LISTED]}" if unplaced else line
