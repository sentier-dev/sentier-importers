"""The three Brightway Flows sources of the ``bafu-2026-v1 -> ef-3.1`` pair.

- ``BafuBrightwayFlowsPackageSource``: ``biosphere-5-brightway-flows.json``, the flows
  packages 1 to 4 leave unmapped that Brightway Flows links to an EF 3.1 flow.
- ``BafuBrightwayFlowsAlignSource``: one of packages 1 to 4 (named by the registry
  block's ``emit_filename``) re-emitted with our target replaced by theirs wherever
  the two EF flows carry identical factors. Run it last, after any regeneration of
  that package.
- ``BafuBrightwayFlowsReviewSource``: ``brightway_flows_review.json``, everything the
  other two did not apply, with the reason. Its ``kind`` is ``new_link_withheld``
  (an unmapped flow whose link package 5 does not take), ``theirs_not_usable`` (a
  mapped flow whose link fails a rule and whose target is not the one we use),
  ``score_changing_retarget`` (their target scores differently from ours) or
  ``retarget_withheld`` (the two flows score the same, but the two sides convert
  the amount differently, or our entry names no target code to compare; two flows
  that do not score are aligned whatever the conversion, so they are not listed).

  A row whose flow has a reviewed decision (``bafu_decisions.yaml``) carries it as
  ``decision`` and ``decision_reason``: ``ours`` and ``hold`` are why the row is
  there to stay; ``theirs`` is on a row only when it could not be applied. A
  score-changing case without a decision is listed as before, and logged.
"""

from __future__ import annotations

import logging

from sentier_importers.core.errors import ValidationError
from sentier_importers.core.types import Record, Records, Rows
from sentier_importers.sources.brightway_flows.bafu import (
    BafuBrightwayFlowsBase,
    State,
    decide,
    decision_gaps,
    entries_of,
    package_entry,
    retarget,
)
from sentier_importers.sources.brightway_flows.factors import compare
from sentier_importers.sources.brightway_flows.links import order, their_record

#: ``retarget`` kinds that mean the two EF flows score differently; the other kinds
#: (``unit_differs``, ``ours_without_code``) hold a retarget back although no factor
#: is known to differ.
_FACTOR_KINDS = frozenset({"differ", "theirs_zero", "ours_zero"})

logger = logging.getLogger(__name__)


def _named(sources: list[Record]) -> str:
    return "; ".join(
        f"{s['name']} ({'/'.join(s.get('context') or [])}, {s.get('unit')})" for s in sources
    )


def _sort_key(row: Record) -> tuple[str, str, str]:
    return order(row["source"])


class BafuBrightwayFlowsPackageSource(BafuBrightwayFlowsBase):
    def transform(self, records: Records) -> Rows:
        state: State = records[0]["state"]
        rows: Rows = []
        for source in state.universe:
            if source["code"] in state.mapped:
                continue
            entry, _ = package_entry(source, state)
            if entry is not None:
                rows.append(entry)
        return sorted(rows, key=_sort_key)


class BafuBrightwayFlowsAlignSource(BafuBrightwayFlowsBase):
    def transform(self, records: Records) -> Rows:
        state: State = records[0]["state"]
        name = self.config.emit_filename
        if name not in state.packages:
            raise ValidationError(
                f"emit_filename {name!r} is not one of the packages to align: "
                f"{sorted(state.packages)}"
            )
        package = state.packages[name]
        return [retarget(entry, state)[0] for entry in entries_of(package)]


class BafuBrightwayFlowsReviewSource(BafuBrightwayFlowsBase):
    def transform(self, records: Records) -> Rows:
        state: State = records[0]["state"]
        unneeded, undecided = decision_gaps(state)
        if unneeded:
            logger.warning(
                f"{self.config.name}: {len(unneeded)} decision(s) not needed, the link "
                f"changes no score or is not usable: {_named(unneeded)}"
            )
        if undecided:
            logger.warning(
                f"{self.config.name}: {len(undecided)} score-changing case(s) without a "
                f"decision: {_named(undecided)}"
            )
        rows: Rows = []
        for source in state.universe:
            row = self._row(source, state)
            if row is not None:
                rows.append(row)
        return sorted(rows, key=_sort_key)

    @staticmethod
    def _row(source: Record, state: State) -> Record | None:
        decision = decide(source, state)
        if decision.link is None:
            return None
        row: Record = {"source": dict(source)}
        mapped = state.mapped.get(source["code"])
        if mapped is None:
            entry, reason = package_entry(source, state)
            if entry is not None:
                return None
            row.update(kind="new_link_withheld", reason=reason, ours=None)
            if decision.target is not None:
                _, methods = compare({}, state.vectors.get(decision.target.code, {}))
                row["methods"] = list(methods)
        else:
            package, entry = mapped
            row.update(package=package, ours=entry["target"])
            if decision.reason is not None:
                theirs = decision.consensus.identifier if decision.consensus else None
                if theirs is not None and theirs == entry["target"].get("code"):
                    return None  # unusable, but it names our own target: nothing to decide
                row.update(kind="theirs_not_usable", reason=decision.reason)
            else:
                _, kind = retarget(entry, state)
                if kind in (None, "retargeted"):
                    return None
                ours = state.vectors.get(entry["target"].get("code"), {})
                _, methods = compare(ours, state.vectors.get(decision.target.code, {}))
                if kind in _FACTOR_KINDS:
                    row.update(kind="score_changing_retarget", factors=kind)
                else:
                    row.update(kind="retarget_withheld", reason=kind)
                row["methods"] = list(methods)
        reviewed = state.decisions.get(source["code"])
        if reviewed is not None:
            row.update(decision=reviewed.decision, decision_reason=reviewed.reason)
        row["brightway_flows"] = their_record(decision)
        return row
