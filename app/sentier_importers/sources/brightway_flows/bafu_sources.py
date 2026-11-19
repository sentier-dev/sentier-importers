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
  ``retarget_withheld`` (no factor differs, but the two sides convert the amount
  differently, or our entry names no target code to compare).
"""

from __future__ import annotations

from sentier_importers.core.errors import ValidationError
from sentier_importers.core.types import Record, Records, Rows
from sentier_importers.sources.brightway_flows.bafu import (
    BafuBrightwayFlowsBase,
    Decision,
    State,
    decide,
    entries_of,
    package_entry,
    retarget,
)
from sentier_importers.sources.brightway_flows.factors import compare

#: ``retarget`` kinds that mean the two EF flows score differently; the other kinds
#: (``unit_differs``, ``ours_without_code``) hold a retarget back although no factor
#: is known to differ.
_FACTOR_KINDS = frozenset({"differ", "theirs_zero", "ours_zero"})


def _sort_key(row: Record) -> tuple[str, str, str]:
    source = row["source"]
    return (
        source["name"].lower(),
        "/".join(source.get("context") or []),
        source.get("unit") or "",
    )


def _theirs(decision: Decision) -> Record | None:
    if decision.link is None:
        return None
    record: Record = {"code": decision.link.target_id, "match": decision.link.match}
    if decision.consensus is not None:
        # the live flow, which a redirected link target is not
        record["code"] = decision.consensus.identifier
        record["name"] = decision.consensus.label
        record["context"] = decision.consensus.context_code
        record["in_ef"] = decision.consensus.is_ef
    return record


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
        row["brightway_flows"] = _theirs(decision)
        return row
