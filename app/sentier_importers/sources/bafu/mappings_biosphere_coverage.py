"""Coverage sidecar: one row per bafu-2026-v1 elementary flow, mapped (which bridge, and
for rank 7/8 which tier and placement) or unmapped (why). Reports bridges 3, 6, 7 and 8,
in two passes:

1. ``BafuEfMatchedSource.outcomes`` (inherited unchanged) over the characterised-only
   index -- exactly what ``bafu-ef-biosphere-matched`` itself runs -- for every flow
   rank 3/6 leave uncovered. A ``Match`` here is bridge 7.
2. For whatever pass 1 leaves ``Unmatched``, the same matching/decision chain
   (``BafuEfMatchedSource.outcome_for``) run again over a locally-built inclusive
   index/pipeline (``include_uncharacterised=True``). A ``Match`` here onto an
   uncharacterised target is bridge 8; onto a characterised one would mean the
   inclusive index changed a characterised rank-7 outcome -- a ``RuntimeError``, never
   silently reported. An ``Unmatched`` outcome is reported from this second pass, not
   the first: the inclusive index can refine the reason (e.g. a code the CF table has
   no context for at all).

Rank-3 and rank-6 membership comes straight from ``ParsedInputs.rank3_codes``/
``rank6_codes`` (kept apart there for exactly this reason -- the sibling's own
``excluded`` is their union and cannot tell them apart).
"""

from __future__ import annotations

from sentier_importers.core import fetch as fetch_mod
from sentier_importers.core.types import Record, Records, Rows
from sentier_importers.matching.ef_index import EfFlowIndex
from sentier_importers.matching.matchers import load_aliases
from sentier_importers.matching.pipeline import Match, default_pipeline
from sentier_importers.sources.bafu.mappings_biosphere_matched import (
    BafuEfMatchedSource,
    ParsedInputs,
    flow_sort_key,
    source_record,
)

_VOCAB_DIR = "ef_vocab"


class BafuEfCoverageSource(BafuEfMatchedSource):
    """Emit one coverage row per BAFU flow instead of the matched entries."""

    def transform(self, records: Records) -> Rows:
        (record,) = records
        inputs: ParsedInputs = record["inputs"]

        # pass 1: exactly what bafu-ef-biosphere-matched itself computes.
        pass1 = {flow.code: (flow, outcome) for flow, outcome in self.outcomes(records)}
        bridge7: dict[str, Match] = {
            code: outcome for code, (flow, outcome) in pass1.items() if isinstance(outcome, Match)
        }

        # pass 2: the same matching/decision chain, over the inclusive index, only for
        # whatever pass 1 left Unmatched.
        vocab_dir = fetch_mod.local_path(self.config.inputs.get(_VOCAB_DIR), _VOCAB_DIR)
        index2 = EfFlowIndex.from_bytes(
            self.inputs["ef_cfs"].content, vocab_dir, include_uncharacterised=True
        )
        pipeline2 = default_pipeline(index2, load_aliases())
        pass2 = {}
        for code, (flow, outcome) in pass1.items():
            if isinstance(outcome, Match):
                continue
            cas = inputs.cas.get(flow.name)
            outcome2 = self.outcome_for(flow, cas, pipeline2, index2)
            if isinstance(outcome2, Match) and index2.get(outcome2.code).characterised:
                raise RuntimeError(
                    "characterised match reached bridge 8 in the coverage sidecar: "
                    f"{flow.name} -> {outcome2.code}"
                )
            pass2[code] = outcome2

        rows: Rows = []
        for flow in sorted(inputs.bafu, key=flow_sort_key):
            row: Record = {"source": source_record(flow)}
            if flow.code in inputs.rank3_codes or flow.code in inputs.rank6_codes:
                row["status"] = "mapped"
                row["bridge"] = 3 if flow.code in inputs.rank3_codes else 6
            elif flow.code in bridge7:
                outcome = bridge7[flow.code]
                row["status"] = "mapped"
                row["bridge"] = 7
                row["tier"] = outcome.tier
                row["placement"] = outcome.placement
                if outcome.location is not None:
                    row["location"] = outcome.location
                if outcome.caveats:
                    row["caveats"] = list(outcome.caveats)
            else:
                outcome2 = pass2[flow.code]
                if isinstance(outcome2, Match):
                    row["status"] = "mapped"
                    row["bridge"] = 8
                    row["tier"] = outcome2.tier
                    row["placement"] = outcome2.placement
                    row["characterised"] = False
                    if outcome2.location is not None:
                        row["location"] = outcome2.location
                    if outcome2.caveats:
                        row["caveats"] = list(outcome2.caveats)
                else:
                    row["status"] = "unmapped"
                    row["reason"] = outcome2.reason
                    row["detail"] = outcome2.detail
            if flow.name in inputs.cas_conflicts:
                row["cas_conflict"] = list(inputs.cas_conflicts[flow.name])
            rows.append(row)
        return rows
