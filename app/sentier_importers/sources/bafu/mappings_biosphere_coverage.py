"""Coverage sidecar: one row per bafu-2026-v1 elementary flow, mapped (which bridge, and
for rank 7/8 which tier and placement) or unmapped (why). Reports bridges 3, 6, 7 and 8,
in two passes:

1. ``BafuEfMatchedSource.outcomes`` (inherited unchanged) over the characterised-only
   index -- exactly what ``bafu-ef-biosphere-matched`` itself runs -- for every flow
   rank 3/6 leave uncovered. A ``Match`` here is bridge 7.
2. For whatever pass 1 leaves ``Unmatched``, the same matching/decision chain
   (``mappings_biosphere_matched.outcome_for``) run again over the inclusive
   index/pipeline (``include_uncharacterised=True``) this source's own ``parse``
   override attaches to ``ParsedInputs`` (``inclusive_index``/``inclusive_pipeline``).
   A ``Match`` here onto an uncharacterised target is bridge 8; onto a characterised
   one would mean the inclusive index changed a characterised rank-7 outcome -- a
   ``RuntimeError``, never silently reported. An ``Unmatched`` outcome is reported
   from this second pass, not the first: the inclusive index can refine the reason
   (e.g. a code the CF table has no context for at all).

``transform`` itself is pure (records in, rows out): it never touches ``self.inputs``
or ``self.config`` -- everything both passes need was already built in ``parse``.

Rank-3 and rank-6 membership comes straight from ``ParsedInputs.rank3_codes``/
``rank6_codes`` (kept apart there for exactly this reason -- the sibling's own
``excluded`` is their union and cannot tell them apart).
"""

from __future__ import annotations

from dataclasses import replace

from sentier_importers.core import fetch as fetch_mod
from sentier_importers.core.types import RawData, Record, Records, Rows
from sentier_importers.matching.ef_index import EfFlowIndex
from sentier_importers.matching.matchers import load_aliases
from sentier_importers.matching.pipeline import Match, default_pipeline
from sentier_importers.sources.bafu.mappings_biosphere_matched import (
    BafuEfMatchedSource,
    ParsedInputs,
    flow_sort_key,
    outcome_for,
    source_record,
)

_VOCAB_DIR = "ef_vocab"


class BafuEfCoverageSource(BafuEfMatchedSource):
    """Emit one coverage row per BAFU flow instead of the matched entries."""

    def parse(self, raw: RawData) -> Records:
        """``BafuEfMatchedSource.parse`` plus the inclusive index/pipeline pass 2 needs.

        Built here, once, alongside the characterised-only pair the base ``parse``
        already builds, so ``transform`` can stay a pure function of ``records``.
        """
        (record,) = super().parse(raw)
        inputs: ParsedInputs = record["inputs"]
        vocab_dir = fetch_mod.local_path(self.config.inputs.get(_VOCAB_DIR), _VOCAB_DIR)
        inclusive_index = EfFlowIndex.from_bytes(
            self.inputs["ef_cfs"].content, vocab_dir, include_uncharacterised=True
        )
        inclusive_pipeline = default_pipeline(inclusive_index, load_aliases())
        augmented = replace(
            inputs, inclusive_index=inclusive_index, inclusive_pipeline=inclusive_pipeline
        )
        return [{"inputs": augmented}]

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
        pass2 = {}
        for code, (flow, outcome) in pass1.items():
            if isinstance(outcome, Match):
                continue
            cas = inputs.cas.get(flow.name)
            outcome2 = outcome_for(flow, cas, inputs.inclusive_pipeline, inputs.inclusive_index)
            if (
                isinstance(outcome2, Match)
                and inputs.inclusive_index.get(outcome2.code).characterised
            ):
                raise RuntimeError(
                    "characterised match reached bridge 8 in the coverage sidecar: "
                    f"{flow.name} -> {outcome2.code}; check that the rank7 input is "
                    "the current rank-7 payload"
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
