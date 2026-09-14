"""EF 3.1 characterization factors for sentier-methods (one row per CF).

Keyed to the vocab flow IRIs (``flows/<FLOW_uuid>``) and the EF methods table
(``method_id``). CF values are JRC-public and uncapped; no ecoinvent involvement.

The JRC source table lists TWO rows with different values for 182 (flow, method)
pairs at the global level (``LCIAMethod_location`` empty): 171 Land use flows and 11
Water use flows. These are resolved deterministically before emission rather than
kept first-row-wins -- see :mod:`sentier_importers.sources.agribalyse.ef_cf_dedup` for
the full rule (water: the AWARE 42.95-family default; land: symmetric ``from X``/
``to X`` pairs, arbitrated by the SimaPro "EF 3.1 adapted" export when ambiguous or
partnerless, else the larger ``|value|``). Country-level rows are never touched.
"""

from loguru import logger
from sentier_importers.core import fetch as fetch_mod
from sentier_importers.core.context import RunContext
from sentier_importers.core.errors import FetchError
from sentier_importers.core.source import Source
from sentier_importers.core.types import RawData, Records, Rows
from sentier_importers.sources.agribalyse.ef_cf_dedup import (
    SIMAPRO_INPUT,
    WATER_METHOD,
    harmonise_water_family,
    parse_simapro_index,
    resolve_global_duplicates,
)
from sentier_importers.sources.agribalyse.ef_common import (
    flow_iri,
    method_id,
    parse_cf_table,
    unit_for,
)


class AgribalyseEfCfsSource(Source):
    """Emit ``characterization-factors.parquet`` rows (method+impact+flow -> value)."""

    def fetch(self, ctx: RunContext) -> RawData:
        """Fetch the JRC CF table, then the optional SimaPro arbiter (best-effort).

        The arbiter is a local reference input, never emitted. When it cannot be
        fetched (missing file, offline with no cache entry, ...) the import still
        runs: :mod:`ef_cf_dedup` falls back to the larger-``|value|`` rule for any
        land-use duplicate the arbiter would otherwise have resolved.
        """
        raw = fetch_mod.fetch(self.config.fetch_url, ctx)
        simapro_url = self.config.inputs.get(SIMAPRO_INPUT)
        if simapro_url:
            try:
                self.inputs[SIMAPRO_INPUT] = fetch_mod.fetch(simapro_url, ctx)
            except FetchError as exc:
                logger.warning(
                    f"SimaPro EF 3.1 arbiter unavailable ({exc}); falling back to "
                    "larger-|value| for ambiguous EF 3.1 land-use global CF duplicates"
                )
        return raw

    def parse(self, raw: RawData) -> Records:
        return parse_cf_table(raw)

    def transform(self, records: Records) -> Rows:
        simapro_index = parse_simapro_index(self.inputs.get(SIMAPRO_INPUT))
        resolution = resolve_global_duplicates(records, simapro_index)

        rows: Rows = []
        seen: set[tuple] = set()
        for rec in records:
            uuid = rec.get("flow_uuid")
            impact = rec.get("method_name") or ""
            value = rec.get("cf")
            if not uuid or not impact or value is None:
                continue
            location = rec.get("location")
            if not location:
                kept_value = resolution.kept.get((impact, uuid))
                if kept_value is not None and float(value) != kept_value:
                    continue  # dropped duplicate global row, see ef_cf_dedup
                if impact == WATER_METHOD:
                    value = harmonise_water_family(float(value))  # -42.955 -> -42.95
            key = (impact, uuid, location)
            if key in seen:
                continue  # keep distinct (impact, flow, location) — preserves regional CFs
            seen.add(key)
            row = {
                "method_id": method_id(impact),
                "impact_category": impact,
                "flow": flow_iri(uuid),
                "flow_name": rec.get("flow_name") or "",
                "factor_value": float(value),
                "unit": unit_for(impact),
            }
            if rec.get("flow_context"):
                row["flow_context"] = rec["flow_context"]
            if location:
                row["location"] = location
            rows.append(row)
        return rows
