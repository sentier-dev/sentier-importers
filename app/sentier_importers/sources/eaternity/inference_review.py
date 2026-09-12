"""Review sidecar for the inferred ``bafu-2026-v1 -> ef-3.1`` bridge.

Same inputs and computation as :mod:`mappings_biosphere`; emits the pairs that were
withheld, one row per BAFU flow (or unresolved Eaternity target) with a ``reason``:
``target_unresolved``, ``uncharacterised``, ``compartment_mismatch``,
``no_cf_compatible_ef_flow``, ``superset_candidates_disagree``. Emitted as
``inference_review.json`` with the package verb ``review`` — a non-normative sidecar
in the shape of the other ``*_review.json`` files in sentier-mappings, invisible to
its CI validator by design.
"""

from __future__ import annotations

from sentier_importers.core.types import Records, Rows
from sentier_importers.sources.eaternity.mappings_biosphere import EaternityInferredBafuEfSource


class EaternityInferenceReviewSource(EaternityInferredBafuEfSource):
    """Emit the withheld pairs instead of the entries."""

    def transform(self, records: Records) -> Rows:
        return self.infer(records).review
