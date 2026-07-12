"""Agribalyse 3.2 → EF 3.1 biosphere crosswalk as a randonneur mapping package.

Source: the built ``registry/mappings_biosphere.parquet``. Emits a randonneur ``replace``
package for the ``02-agribalyse-3.2__ef-3.1`` bridge in ``sentier-mappings``.

Scope guardrail (2026-07-10 decision — ignore ecoinvent nomenclature):
- keep only ``target_db in {"ef", "biosphere3"}`` (no ecoinvent-biosphere targets exist);
- drop any row whose ``provenance`` mentions ``ecoinvent``;
- drop the ~1.56M ``harmonised-flows-simple`` identity rows (already shipped as the flow
  vocabulary). What remains: the hand-authored curated + LLM-reviewed AGB→EF crosswalks.
"""

import io

import pyarrow.parquet as pq
from sentier_importers.core.source import Source
from sentier_importers.core.types import RawData, Record, Records, Rows

#: target_db values that are license-free (EF = JRC, biosphere3 = open Brightway).
_ALLOWED_TARGET_DB = {"ef", "biosphere3"}
#: provenance dropped entirely (bulk identity layer already in the vocab).
_DROP_PROVENANCE = {"harmonised-flows-simple"}
#: provenance substring that marks an ecoinvent-derived mapping (excluded).
_ECOINVENT = "ecoinvent"

_COLUMNS = [
    "source_name",
    "source_unit",
    "source_context",
    "source_cas",
    "target_db",
    "target_code",
    "target_name",
    "target_unit",
    "unit_conversion",
    "priority_tier",
    "provenance",
]


class AgribalyseBiosphereMappingsSource(Source):
    """Map the biosphere registry table into randonneur ``replace`` entries."""

    def parse(self, raw: RawData) -> Records:
        table = pq.read_table(io.BytesIO(raw.content), columns=_COLUMNS)
        data = table.to_pydict()
        length = len(data["source_name"])
        return [{col: data[col][i] for col in _COLUMNS} for i in range(length)]

    def _shippable(self, rec: Record) -> bool:
        provenance = rec.get("provenance") or ""
        return (
            rec.get("target_db") in _ALLOWED_TARGET_DB
            and _ECOINVENT not in provenance
            and provenance not in _DROP_PROVENANCE
        )

    def transform(self, records: Records) -> Rows:
        rows: Rows = []
        for rec in records:
            if not self._shippable(rec):
                continue
            source_name = (rec.get("source_name") or "").strip()
            target_name = (rec.get("target_name") or "").strip()
            if not source_name or not target_name:
                continue

            source: Record = {"name": source_name}
            if unit := (rec.get("source_unit") or "").strip():
                source["unit"] = unit
            if context := [c for c in (rec.get("source_context") or []) if c]:
                source["context"] = context
            if cas := rec.get("source_cas"):
                source["cas_number"] = cas

            target: Record = {"name": target_name}
            if t_unit := (rec.get("target_unit") or "").strip():
                target["unit"] = t_unit
            if code := rec.get("target_code"):
                target["code"] = code

            entry: Record = {"source": source, "target": target}
            conversion = rec.get("unit_conversion")
            if conversion is not None and conversion != 1.0:
                entry["conversion_factor"] = float(conversion)
            provenance = rec.get("provenance") or ""
            tier = rec.get("priority_tier")
            entry["comment"] = f"{provenance} (priority tier {tier})"
            rows.append(entry)
        return rows
