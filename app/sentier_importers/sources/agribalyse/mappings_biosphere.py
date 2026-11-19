"""Agribalyse 3.2 → EF 3.1 biosphere crosswalk as a randonneur mapping package.

Source: the built ``registry/mappings_biosphere.parquet``. Emits a randonneur ``replace``
package for the ``agribalyse-3.2__ef-3.1`` pair in ``sentier-mappings``: its second
package (``biosphere-2-curated``), read by name when the Brightway Flows package has
no entry for a flow. Most entries are keyed by the flow name alone: the registry has
no context and no unit for them, and none is invented here.

Scope guardrail (2026-07-10 decision — ignore ecoinvent nomenclature):
- keep only ``target_db in {"ef", "biosphere3"}`` (no ecoinvent-biosphere targets exist);
- drop any row whose ``provenance`` mentions ``ecoinvent``;
- drop the ~1.56M ``harmonised-flows-simple`` identity rows (already shipped as the flow
  vocabulary). What remains: the hand-authored curated + LLM-reviewed AGB→EF crosswalks.
"""

import io
import json

import pyarrow.parquet as pq
from sentier_importers.core.source import Source
from sentier_importers.core.types import RawData, Record, Records, Rows

#: target_db values that are license-free (EF = JRC, biosphere3 = open Brightway).
_ALLOWED_TARGET_DB = {"ef", "biosphere3"}
#: provenance dropped entirely (bulk identity layer already in the vocab).
_DROP_PROVENANCE = {"harmonised-flows-simple"}
#: provenance substring that marks an ecoinvent-derived mapping (excluded).
_ECOINVENT = "ecoinvent"
#: pandas writes a missing string as the literal "nan" through parquet; every field is
#: screened so no entry carries it (as ``bafu.mappings_biosphere``). 1,082 rows of
#: the registry have the context ``["nan", "nan"]`` and the unit ``"nan"``.
_NULLISH = {"", "nan", "none", "<na>"}

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

#: Optional input: the first package of the pair, whose keys this one must not repeat.
_FIRST_PACKAGE = "brightway_flows"


def _clean(value: object) -> str:
    """A parquet cell as a trimmed string, or ``""`` when it is nullish."""
    if value is None:
        return ""
    text = str(value).strip()
    return "" if text.lower() in _NULLISH else text


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

    def _covered(self) -> frozenset[tuple[str, tuple[str, ...]]]:
        """Source keys ``(name, context)`` the pair's first package already holds.

        Read from the optional ``brightway_flows`` input (the emitted
        ``biosphere-1-brightway-flows.json``). This package is the fallback of that one,
        and the validator of sentier-mappings refuses one key in two packages of a pair.
        """
        package = self.inputs.get(_FIRST_PACKAGE)
        if package is None:
            return frozenset()
        return frozenset(
            (entry["source"]["name"], tuple(entry["source"].get("context") or ()))
            for entry in json.loads(package.content).get("replace", [])
        )

    def transform(self, records: Records) -> Rows:
        covered = self._covered()
        rows: Rows = []
        for rec in records:
            if not self._shippable(rec):
                continue
            source_name = _clean(rec.get("source_name"))
            target_name = _clean(rec.get("target_name"))
            if not source_name or not target_name:
                continue

            source: Record = {"name": source_name}
            if unit := _clean(rec.get("source_unit")):
                source["unit"] = unit
            if context := [c for c in map(_clean, rec.get("source_context") or []) if c]:
                source["context"] = context
            if cas := _clean(rec.get("source_cas")):
                source["cas_number"] = cas
            if (source_name, tuple(source.get("context") or ())) in covered:
                continue

            target: Record = {"name": target_name}
            if t_unit := _clean(rec.get("target_unit")):
                target["unit"] = t_unit
            if code := _clean(rec.get("target_code")):
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
