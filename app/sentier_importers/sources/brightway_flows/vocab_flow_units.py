"""The unit of every EF 3.1 flow term, as ``qudt:hasUnit`` relations of the
``ef-3.1`` release of ``sentier_vocab`` (spec 2026-11-19-units-on-ef31-flows).

The flow terms of the release come from three parts (``agribalyse.flows``,
``agribalyse.flows_jrc``, ``brightway_flows.vocab_flows``); none of their inputs but
Brightway Flows 1.1.0 states a unit. This part emits no term: one relation per flow,
whichever part emits the flow (``Source.relations_only``).

- The flows are those of the three parts, re-derived from their inputs with their
  own rules (``vocab_flows.covered_identifiers`` and ``vocab_flows.new_flows``). An
  edge on a flow no part emits fails the release, which checks every subject.
- The unit of a flow is the unit of its own entry in the file's EF correspondence
  (the link whose source is the flow), also when that entry merges the flow into
  another one.
- A flow without an entry (an identifier the file records as redirected) takes the
  unit of the flow it is redirected to.
- A flow still without a unit gets no edge and is counted in a log line. A unit is
  never guessed.
- The object follows the rule of ``brightway_flows.units``: the QUDT unit, else a
  ``sentier-core`` unit term.
"""

from __future__ import annotations

from collections.abc import Mapping

from loguru import logger
from sentier_importers.core.errors import ValidationError
from sentier_importers.core.source import Source
from sentier_importers.core.types import RawData, Records, Rows
from sentier_importers.sources.agribalyse.flows import FLOWS_SCHEME
from sentier_importers.sources.brightway_flows.correspondences import (
    EF_LIST,
    PINNED_SHA256,
    BrightwayFlows,
    load,
)
from sentier_importers.sources.brightway_flows.units import unit_iri
from sentier_importers.sources.brightway_flows.vocab_flows import covered_identifiers, new_flows

#: How many identifiers without a unit the log line names.
_LOGGED = 10


def stated_units(bw: BrightwayFlows) -> Mapping[str, str]:
    """The unit code each EF identifier states in the EF correspondence."""
    units: dict[str, str] = {}
    for item in bw.links.get(EF_LIST, ()):
        known = units.setdefault(item.source_id, item.unit)
        if known != item.unit:
            raise ValidationError(
                f"{item.source_id}: two units in the EF correspondence, "
                f"{known!r} and {item.unit!r}"
            )
    return units


def redirected_unit(bw: BrightwayFlows, units: Mapping[str, str], identifier: str) -> str | None:
    """The unit of the flow ``identifier`` is redirected to, or ``None``."""
    target = bw.resolve(identifier)
    return units.get(target) if target != identifier else None


class Ef31FlowUnitsSource(Source):
    """Emit one ``qudt:hasUnit`` relation per EF 3.1 flow term of the release."""

    relations_only = True
    #: ``None`` in tests, which feed a synthetic file.
    expected_sha256: str | None = PINNED_SHA256

    def parse(self, raw: RawData) -> Records:
        return [{"bw": load(raw.content, self.expected_sha256)}]

    def _release_flows(self, bw: BrightwayFlows) -> list[str]:
        """Identifiers of the flow terms the three flow parts of the release emit."""
        covered = covered_identifiers(self.inputs)
        return sorted(covered | {flow.identifier for flow, _ in new_flows(bw, covered)})

    def transform(self, records: Records) -> Rows:
        bw: BrightwayFlows = records[0]["bw"]
        units = stated_units(bw)
        rows: Rows = []
        redirected = 0
        without: list[str] = []
        for identifier in self._release_flows(bw):
            code = units.get(identifier)
            if code is None:
                code = redirected_unit(bw, units, identifier)
                if code is None:
                    without.append(identifier)
                    continue
                redirected += 1
            rows.append({"iri": f"{FLOWS_SCHEME}{identifier}", "unit": unit_iri(code)})
        self._report(len(rows), redirected, without)
        return rows

    def _report(self, edges: int, redirected: int, without: list[str]) -> None:
        message = (
            f"{self.config.name}: {edges} unit edges, {edges - redirected} from the flow's own "
            f"entry in the EF correspondence, {redirected} from the flow it is redirected to; "
            f"{len(without)} flows without a unit (no entry, and no redirect to a flow with one)"
        )
        if without:
            logger.warning(f"{message}: {without[:_LOGGED]}")
        else:
            logger.info(message)
