"""Coverage sidecar: one row per bafu-2026-v1 elementary flow, mapped (which bridge, and
for rank 7 which tier and placement) or unmapped (why). Same inputs and computation as
:mod:`mappings_biosphere_matched`; emitted as ``coverage.json`` with the package verb
``coverage`` (non-normative, ignored by sentier-mappings' validator by design).

Rank-3 and rank-6 membership comes from the two source-code sets read straight off
``rank3``/``rank6`` (rather than the sibling's ``excluded`` union, which cannot tell them
apart); rank 7 and the withheld reasons come from :meth:`BafuEfMatchedSource.outcomes`.
"""

from __future__ import annotations

import orjson
from sentier_importers.core.types import Record, Records, Rows
from sentier_importers.matching.pipeline import Match
from sentier_importers.sources.bafu.mappings_biosphere_matched import (
    BafuEfMatchedSource,
    ParsedInputs,
    codes_of,
)
from sentier_importers.sources.eaternity.bridge import BafuFlow


def _source(flow: BafuFlow) -> Record:
    """The ``source`` sub-record every coverage row carries."""
    return {
        "name": flow.name,
        "code": flow.code,
        "unit": flow.unit,
        "context": flow.context,
    }


class BafuEfCoverageSource(BafuEfMatchedSource):
    """Emit one coverage row per BAFU flow instead of the matched entries."""

    def transform(self, records: Records) -> Rows:
        (record,) = records
        inputs: ParsedInputs = record["inputs"]
        rank3 = codes_of(orjson.loads(self.inputs["rank3"].content))
        rank6 = codes_of(orjson.loads(self.inputs["rank6"].content))
        outcomes = {flow.code: outcome for flow, outcome in self.outcomes(records)}
        rows: Rows = []
        for flow in sorted(inputs.bafu, key=lambda f: (f.name, f.category, f.subcategory, f.unit)):
            row: Record = {"source": _source(flow)}
            if flow.code in rank3 or flow.code in rank6:
                row["status"] = "mapped"
                row["bridge"] = 3 if flow.code in rank3 else 6
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
