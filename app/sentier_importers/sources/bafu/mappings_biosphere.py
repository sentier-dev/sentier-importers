"""BAFU-2026 elementary flows → EF 3.1 CF keys, as a randonneur package.

Source: ``dds-carbonminds-data/registry/mappings_biosphere_ef.parquet``, the
de-bridged crosswalk. carbonminds resolves BAFU flows to whatever code the EF
v3.1 factor sits on, which for 98.7% of matched flows is an
``ecoinvent-3.9.1-biosphere`` code; the de-bridger re-expresses each link as the
EF flow carrying the identical factor, so nothing ecoinvent-shaped reaches this
public repo. See ``docs/specs/2026-08-06-bafu-ef-debridged-mappings.md``.

Each entry asserts one thing: *this BAFU flow receives this EF characterization
factor*. The upstream parquet is already filtered to shippable rows — flows
whose target carries no CF, or for which no CF-compatible EF flow exists, are
withheld there with a reason rather than guessed at by name here.

Two defects in the sibling ``agribalyse-3.2__ef-3.1`` package are deliberately
not repeated: stringified ``"nan"`` field values, and name-only targets. A
target without a ``code`` cannot be resolved to a factor, so it is not shipped.
"""

from __future__ import annotations

import io

import pyarrow.parquet as pq
from sentier_importers.core.source import Source
from sentier_importers.core.types import RawData, Record, Records, Rows
from sentier_importers.sources.bafu.ecospold import flow_id

#: Only ``ef`` targets exist in the de-bridged table; asserted rather than
#: filtered, because anything else means the upstream build is wrong.
_EF_DB = "ef"

#: pandas writes missing strings as the literal "nan" through parquet; every
#: field is screened so no entry ever carries it (the agribalyse package ships
#: 1,093 entries with ``"unit": "nan"``).
_NULLISH = {"", "nan", "none", "<na>"}

_COLUMNS = [
    "source_name",
    "source_category",
    "source_subcategory",
    "source_unit",
    "target_code",
    "target_name",
    "target_unit",
    "target_categories",
    "unit_conversion",
    "tier",
    "candidate_count",
    "cf_equivalent",
    "target_db",
]

_TIER_COMMENT = {
    "T1": "already an EF target",
    "T2": "exact CF twin via ecoinvent",
    "T3": "EF target agrees on every current factor and adds more",
}


class BafuBiosphereMappingsSource(Source):
    """Map the de-bridged registry table into randonneur ``replace`` entries."""

    def parse(self, raw: RawData) -> Records:
        table = pq.read_table(io.BytesIO(raw.content), columns=_COLUMNS)
        data = table.to_pydict()
        return [
            {column: data[column][i] for column in _COLUMNS}
            for i in range(len(data["source_name"]))
        ]

    def transform(self, records: Records) -> Rows:
        rows: Rows = []
        for record in records:
            entry = self._entry(record)
            if entry is not None:
                rows.append(entry)
        return rows

    # -- internals ---------------------------------------------------------

    def _entry(self, record: Record) -> Record | None:
        name = self._clean(record.get("source_name"))
        code = self._clean(record.get("target_code"))
        if not name or not code or record.get("target_db") != _EF_DB:
            return None

        category = self._clean(record.get("source_category"))
        subcategory = self._clean(record.get("source_subcategory"))

        source: Record = {
            "name": name,
            # the id sentier-vocab mints for this flow, so the bridge joins to
            # the published IRI rather than to a bare string
            "code": flow_id(name, category, subcategory),
        }
        if unit := self._clean(record.get("source_unit")):
            source["unit"] = unit
        if context := [c for c in (category, subcategory) if c]:
            source["context"] = context

        target: Record = {"name": self._clean(record.get("target_name")), "code": code}
        if unit := self._clean(record.get("target_unit")):
            target["unit"] = unit
        if categories := [self._clean(c) for c in (record.get("target_categories") or [])]:
            target["context"] = [c for c in categories if c]
        if not target["name"]:
            del target["name"]

        entry: Record = {"source": source, "target": target}
        conversion = record.get("unit_conversion")
        if conversion is not None and float(conversion) != 1.0:
            entry["conversion_factor"] = float(conversion)

        tier = self._clean(record.get("tier"))
        note = _TIER_COMMENT.get(tier, tier)
        candidates = record.get("candidate_count") or 0
        entry["comment"] = (
            f"debridge {tier}: {note}; {candidates} CF-equivalent candidate"
            f"{'' if candidates == 1 else 's'}"
        )
        return entry

    @staticmethod
    def _clean(value) -> str:
        """Normalise a parquet cell to a trimmed string, or ``""`` if nullish."""
        if value is None:
            return ""
        text = str(value).strip()
        return "" if text.lower() in _NULLISH else text
