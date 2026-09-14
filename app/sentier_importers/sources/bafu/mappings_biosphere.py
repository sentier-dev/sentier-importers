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

Decision 2026-09-14: ``EXCLUDED_SOURCE_NAMES`` withholds a handful of BAFU source
flows the upstream crosswalk resolves onto the wrong substance entirely -- carbonminds
pairs BAFU ``Metiram`` with EF ``Zineb``, a different dithiocarbamate fungicide, although
EF 3.1 carries its own ``Metiram`` flow (CAS 9006-42-2). Shipping that row would assert a
factor for the wrong substance, so it is dropped here rather than trusted from upstream;
the matched package then lands Metiram on EF's own flow by name and CAS.
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

#: BAFU source flow names withheld regardless of what the upstream crosswalk
#: resolved them onto (decision 2026-09-14), keyed by lowercased BAFU name. Every
#: entry names the actual EF substance carbonminds paired the flow with, so a
#: reviewer can see at a glance why the row is missing rather than just that it is.
EXCLUDED_SOURCE_NAMES: dict[str, str] = {
    "metiram": (
        "carbonminds pairs Metiram with Zineb, a different dithiocarbamate "
        "fungicide; EF 3.1 has its own Metiram flow, CAS 9006-42-2 (decision 2026-09-14)"
    ),
}

#: Comments are for what a reader could not otherwise tell. An exact match (T1,
#: T2) needs none — the entry says everything. Only T3 carries a caveat: the EF
#: flow is characterised in impact categories beyond the ones this BAFU flow is
#: characterised in today, so adopting it broadens the assessment.
#:
#: Nothing here names the intermediate database the factor was matched through.
#: That is the point of de-bridging, and this repo is public.
_TIER_COMMENT = {
    "T3": (
        "the EF flow is characterised in impact categories beyond those this "
        "flow currently receives"
    ),
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
        if name.lower() in EXCLUDED_SOURCE_NAMES:
            return None

        category = self._clean(record.get("source_category"))
        subcategory = self._clean(record.get("source_subcategory"))
        unit = self._clean(record.get("source_unit"))

        source: Record = {
            "name": name,
            # the id sentier-vocab mints for this flow, so the bridge joins to
            # the published IRI rather than to a bare string
            "code": flow_id(name, category, subcategory, unit),
        }
        if unit:
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

        if note := _TIER_COMMENT.get(self._clean(record.get("tier"))):
            entry["comment"] = note
        return entry

    @staticmethod
    def _clean(value) -> str:
        """Normalise a parquet cell to a trimmed string, or ``""`` if nullish."""
        if value is None:
            return ""
        text = str(value).strip()
        return "" if text.lower() in _NULLISH else text
