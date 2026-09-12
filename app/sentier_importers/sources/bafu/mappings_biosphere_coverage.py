"""Coverage sidecar: one row per bafu-2026-v1 elementary flow, mapped (which bridge, and
for rank 7 which tier and placement) or unmapped (why). Same inputs and computation as
:mod:`mappings_biosphere_matched`; emitted as ``coverage.json`` with the package verb
``coverage`` (non-normative, ignored by sentier-mappings' validator by design).

Rank-3 and rank-6 membership comes straight from ``ParsedInputs.rank3_codes``/
``rank6_codes`` (kept apart there for exactly this reason -- the sibling's own
``excluded`` is their union and cannot tell them apart); rank 7 and the withheld
reasons come from :meth:`BafuEfMatchedSource.outcomes`.
"""

from __future__ import annotations

from sentier_importers.core.types import Record, Records, Rows
from sentier_importers.matching.pipeline import Match
from sentier_importers.sources.bafu.mappings_biosphere_matched import (
    BafuEfMatchedSource,
    ParsedInputs,
    flow_sort_key,
    source_record,
)


class BafuEfCoverageSource(BafuEfMatchedSource):
    """Emit one coverage row per BAFU flow instead of the matched entries."""

    def transform(self, records: Records) -> Rows:
        (record,) = records
        inputs: ParsedInputs = record["inputs"]
        outcomes = {flow.code: outcome for flow, outcome in self.outcomes(records)}
        rows: Rows = []
        for flow in sorted(inputs.bafu, key=flow_sort_key):
            row: Record = {"source": source_record(flow)}
            if flow.code in inputs.rank3_codes or flow.code in inputs.rank6_codes:
                row["status"] = "mapped"
                row["bridge"] = 3 if flow.code in inputs.rank3_codes else 6
            else:
                outcome = outcomes[flow.code]
                if isinstance(outcome, Match):
                    row["status"] = "mapped"
                    row["bridge"] = 7
                    row["tier"] = outcome.tier
                    row["placement"] = outcome.placement
                    if outcome.location is not None:
                        row["location"] = outcome.location
                    if outcome.caveats:
                        row["caveats"] = list(outcome.caveats)
                else:
                    row["status"] = "unmapped"
                    row["reason"] = outcome.reason
                    row["detail"] = outcome.detail
            if flow.name in inputs.cas_conflicts:
                row["cas_conflict"] = list(inputs.cas_conflicts[flow.name])
            rows.append(row)
        return rows
