"""bafu-2026-v1 -> EF 3.1 nomenclature bridge for uncharacterised targets (the
nomenclature package, biosphere-4-nomenclature).

For every BAFU-2026 v1 elementary flow that the curated, inferred and matched
packages leave uncovered, run the same ``matching.pipeline.default_pipeline``
matching/decision chain the matched package uses, but over the EF flow index built
with ``include_uncharacterised=True`` (``matching.ef_index.EfFlowIndex``): EF 3.1
vocab rows that exist in sentier-vocab but carry no characterization factor in any EF
3.1 method, placed via the Brightway context crosswalk (``matching.bw_context``)
rather than the CF table. Every entry this source emits asserts a nomenclature
alignment only -- impact is zero by construction, since the target carries no factor
at all -- and applies after the curated, inferred and matched packages.

Requires the sibling matched payload as the ``matched`` input: the nomenclature
package must skip exactly what the matched package itself mapped
(``ParsedInputs.matched_codes``), not just what the curated and inferred packages map,
or the two sources could both claim the same flow.
"""

from __future__ import annotations

from sentier_importers.core.types import Records, Rows
from sentier_importers.matching.pipeline import Match
from sentier_importers.sources.bafu.mappings_biosphere_matched import BafuEfMatchedSource
from sentier_importers.sources.bafu.packages import MATCHED


class BafuEfNomenclatureSource(BafuEfMatchedSource):
    """Emit one entry per BAFU flow that resolves onto an uncharacterised EF target."""

    include_uncharacterised = True

    def transform(self, records: Records) -> Rows:
        """Emit one entry per BAFU flow ``outcomes`` resolves to an uncharacterised ``Match``.

        A ``Match`` onto a *characterised* target reaching this source would mean the
        inclusive index changed a characterised matched-package outcome -- it must
        never happen, so it is a loud ``RuntimeError`` naming the flow rather than a
        silently dropped or duplicated entry.
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
                    f"{flow.name} -> {outcome.code}; check that the matched input is "
                    f"the current {MATCHED} payload"
                )
            rows.append(self.entry_for(flow, outcome, index))
        return rows
