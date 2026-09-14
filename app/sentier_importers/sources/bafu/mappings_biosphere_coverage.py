"""Coverage sidecar: one row per bafu-2026-v1 elementary flow, mapped (which package,
and for the matched/nomenclature packages which tier and placement) or unmapped (why).
Reports all four packages (biosphere-1-curated, biosphere-2-inferred,
biosphere-3-matched, biosphere-4-nomenclature), in two passes:

1. ``BafuEfMatchedSource.outcomes`` (inherited unchanged) over the characterised-only
   index -- exactly what ``bafu-ef-biosphere-matched`` itself runs -- for every flow
   the curated/inferred packages leave uncovered. A ``Match`` here is biosphere-3-matched.
2. For whatever pass 1 leaves ``Unmatched``, the same matching/decision chain
   (``mappings_biosphere_matched.outcome_for``) run again over the inclusive
   index/pipeline (``include_uncharacterised=True``) this source's own ``parse``
   override attaches to ``ParsedInputs`` (``inclusive_index``/``inclusive_pipeline``).
   A ``Match`` here onto an uncharacterised target is biosphere-4-nomenclature; onto a
   characterised one would mean the inclusive index changed a characterised
   biosphere-3-matched outcome -- a ``RuntimeError``, never silently reported. An
   ``Unmatched`` outcome is reported from this second pass, not the first: the
   inclusive index can refine the reason (e.g. a code the CF table has no context for
   at all). Round 5, decision 2026-09-14: ``outcome_for``'s own ``_decide`` step may
   also resolve a plain ``no_ef_flow`` here into a name-only alignment
   (``mappings_biosphere_matched._name_only_match``) -- reported the same as any other
   biosphere-4-nomenclature ``Match``, with ``tier="name-only"`` and
   ``placement="name_only"``; this source needs no code of its own for it.

``transform`` itself is pure (records in, rows out): it never touches ``self.inputs``
or ``self.config`` -- everything both passes need was already built in ``parse``.

Curated and inferred membership comes straight from ``ParsedInputs.curated_codes``/
``inferred_codes`` (kept apart there for exactly this reason -- the sibling's own
``excluded`` is their union and cannot tell them apart).

Round 6, decision 2026-09-14: ``parse`` logs the inclusive index's own naming
bookkeeping (``EfFlowIndex.multi_name_codes``/``suppressed_vocab_synonyms``/
``relabelled_count``) once, here, since this is the one place a full pipeline run
builds the fullest (inclusive) index -- these counters are not otherwise surfaced
anywhere in the emitted rows themselves.
"""

from __future__ import annotations

from dataclasses import replace

from loguru import logger
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
from sentier_importers.sources.bafu.packages import CURATED, INFERRED, MATCHED, NOMENCLATURE

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
        # round 6, decision 2026-09-14: this is the one place the fullest (inclusive)
        # index gets built for a full pipeline run, so it is where its own naming
        # bookkeeping is worth surfacing -- these counters are not otherwise logged
        # or emitted anywhere in the output rows themselves.
        logger.info(
            "EF index naming: {} multi-spelling code(s), {} vocab label(s) "
            "suppressed as a synonym, {} uncharacterised flow(s) relabelled",
            inclusive_index.multi_name_codes,
            inclusive_index.suppressed_vocab_synonyms,
            inclusive_index.relabelled_count,
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
        matched: dict[str, Match] = {
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
                    f"characterised match reached {NOMENCLATURE} in the coverage sidecar: "
                    f"{flow.name} -> {outcome2.code}; check that the matched input is "
                    f"the current {MATCHED} payload"
                )
            pass2[code] = outcome2

        rows: Rows = []
        for flow in sorted(inputs.bafu, key=flow_sort_key):
            row: Record = {"source": source_record(flow)}
            if flow.code in inputs.curated_codes or flow.code in inputs.inferred_codes:
                row["status"] = "mapped"
                row["package"] = CURATED if flow.code in inputs.curated_codes else INFERRED
            elif flow.code in matched:
                outcome = matched[flow.code]
                row["status"] = "mapped"
                row["package"] = MATCHED
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
                    row["package"] = NOMENCLATURE
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
