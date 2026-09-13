"""bafu-2026-v1 -> EF 3.1 nomenclature bridge for uncharacterised targets (rank 8).

For every BAFU-2026 v1 elementary flow that ranks 3, 6 and 7 leave uncovered, run the
same ``matching.pipeline.default_pipeline`` matching/decision chain rank 7 uses, but
over the EF flow index built with ``include_uncharacterised=True``
(``matching.ef_index.EfFlowIndex``): EF 3.1 vocab rows that exist in sentier-vocab but
carry no characterization factor in any EF 3.1 method, placed via the Brightway
context crosswalk (``matching.bw_context``) rather than the CF table. Every entry this
source emits asserts a nomenclature alignment only -- impact is zero by construction,
since the target carries no factor at all -- and is ranked 8, below 3, 6 and 7.

Requires the sibling rank-7 payload as the ``rank7`` input: rank 8 must skip exactly
what rank 7 itself mapped (``ParsedInputs.rank7_codes``), not just what rank 3 and
rank 6 map, or the two sources could both claim the same flow.
"""

from __future__ import annotations

from sentier_importers.core.types import Records, Rows
from sentier_importers.matching.pipeline import Match
from sentier_importers.sources.bafu.mappings_biosphere_matched import BafuEfMatchedSource


class BafuEfNomenclatureSource(BafuEfMatchedSource):
    """Emit one entry per BAFU flow that resolves onto an uncharacterised EF target."""

    include_uncharacterised = True

    def transform(self, records: Records) -> Rows:
        """Emit one entry per BAFU flow ``outcomes`` resolves to an uncharacterised ``Match``.

        A ``Match`` onto a *characterised* target reaching this source would mean the
        inclusive index changed a characterised rank-7 outcome -- it must never
        happen, so it is a loud ``RuntimeError`` naming the flow rather than a silently
        dropped or duplicated entry.
        """
        (record,) = records
        index = record["inputs"].index
        rows: Rows = []
        for flow, outcome in self.outcomes(records):
            if not isinstance(outcome, Match):
                continue
            target = index.get(outcome.code)
            if target.characterised:
                raise RuntimeError(
                    f"characterised match reached the nomenclature source: "
                    f"{flow.name} -> {outcome.code}"
                )
            rows.append(self.entry_for(flow, outcome, index))
        return rows
