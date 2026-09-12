"""bafu-2026-v1 -> EF 3.1 CF keys by public matching (rank 7).

For every BAFU-2026 v1 elementary flow that neither the rank-3 nor the rank-6 bridge
maps, run ``matching.pipeline.default_pipeline`` (name, synonym, qualifier, alias,
region-stripped name, CAS) against the public EF flow index and emit one ``replace``
entry per match. Withheld flows are emitted by the sibling coverage source.

Inputs: the ecoSpold zip (primary), ``rank3`` and ``rank6`` payloads (exclusion),
``ef_cfs`` (sentier-methods CF table) and ``ef_vocab`` (a DIRECTORY of sentier-vocab
elementary-flow shards, read from the local path, not the fetch cache).
"""

from __future__ import annotations

import re
from pathlib import Path

import orjson
from sentier_importers.core import fetch as fetch_mod
from sentier_importers.core.context import RunContext
from sentier_importers.core.source import Source
from sentier_importers.core.types import RawData, Record, Records, Rows
from sentier_importers.matching.ef_index import EfFlowIndex
from sentier_importers.matching.matchers import load_aliases
from sentier_importers.matching.pipeline import Match, Unmatched, default_pipeline
from sentier_importers.sources.bafu.ecospold import parse_ecospold_zip
from sentier_importers.sources.eaternity.bridge import BafuFlow, BafuFlowIndex
from sentier_importers.sources.eaternity.inference import EF_UNIT

_VOCAB_DIR = "ef_vocab"
_REQUIRED = ("rank3", "rank6", "ef_cfs")
_BIOSPHERE_GROUP = 4
_WATER_USE = "ef-3.1:water-use"

#: Ion / oxidation-state markers BAFU bakes into a name: a trailing ``, ion`` /
#: `` ion``, a trailing roman numeral (``II``-``VI``), or a trailing ``+``. EF 3.1
#: does not carry per-species factors for these; collapsing them onto the element
#: would silently assert a factor for the wrong chemical species.
_ION = re.compile(r"(,\s?ion$|\sion$|\s(II|III|IV|V|VI)$|\+$)")

#: EF characterises carbon oxides only with a qualifier (fossil/biogenic/land use
#: change); a bare BAFU name with none can never resolve, no matter the sub-compartment.
_CARBON_OXIDES = {"Carbon dioxide", "Carbon monoxide"}

#: EF's water-use method characterises freshwater deprivation only: a BAFU salt-water
#: or fossil-water resource flow can never receive an EF water factor, even when the
#: pipeline finds a same-named candidate (that candidate is the freshwater flow).
_NON_FRESHWATER = ("Water, salt", "Water, fossil")


def _local_path(url: str | None) -> Path:
    if not url or not url.startswith("file://"):
        raise ValueError(f"{_VOCAB_DIR} must be a local file:// directory, got {url!r}")
    return Path(url[len("file://") :])


def _codes(package: dict) -> set[str]:
    """Source codes named by every ``replace``/``update`` entry in a mapping package."""
    return {
        e["source"]["code"]
        for verb in ("replace", "update")
        for e in package.get(verb, [])
        if e.get("source", {}).get("code")
    }


def substance_cas(records: Records) -> tuple[dict[str, str], dict[str, set[str]]]:
    """CAS number per substance NAME over every biosphere exchange (group 4).

    A BAFU flow name is shared across sub-compartments (``Methanol`` to air and to
    water, say); its CAS number is a property of the substance, not the placement, so
    it is collected per name rather than per (name, category, subcategory, unit). A
    name whose exchanges disagree on CAS gets no CAS at all -- returned instead in the
    second mapping, keyed by name, so a caller can see what was withheld and why.
    """
    per_name: dict[str, set[str]] = {}
    for record in records:
        for exchange in record.get("exchanges", []):
            if exchange.get("group_code") != _BIOSPHERE_GROUP:
                continue
            cas = exchange.get("cas")
            if cas is None:
                continue
            per_name.setdefault(exchange["name"], set()).add(cas)
    cas = {name: next(iter(values)) for name, values in per_name.items() if len(values) == 1}
    conflicts = {name: values for name, values in per_name.items() if len(values) > 1}
    return cas, conflicts


def classify_unmatched(flow: BafuFlow, outcome: Unmatched) -> Unmatched:
    """Apply the plan's decisions to an ``Unmatched`` outcome, most-specific reason first.

    Decision 5 (freshwater deprivation only) applies unconditionally, ahead of
    everything else and regardless of ``outcome.reason``: a BAFU salt-water or
    fossil-water resource flow is never characterised by EF's water-use method.
    Otherwise, a reason other than ``no_ef_flow`` is left alone (it is already the
    most informative reason the pipeline could give); only a plain "nothing matched"
    is refined into ``speciation`` (an ion/oxidation-state name) or
    ``qualifier_missing`` (a bare carbon oxide with no fossil/biogenic/land-use-change
    qualifier).
    """
    if flow.name.startswith(_NON_FRESHWATER):
        return Unmatched("no_ef_flow", "EF water use characterises freshwater deprivation only")
    if outcome.reason != "no_ef_flow":
        return outcome
    if _ION.search(flow.name):
        return Unmatched(
            "speciation",
            f"{flow.name!r} names an ion or oxidation state; not collapsed onto an EF species",
        )
    if flow.name in _CARBON_OXIDES and flow.category == "emissions to air":
        return Unmatched(
            "qualifier_missing",
            "EF characterises carbon oxides only as (fossil), (biogenic) or (land use change)",
        )
    return outcome


def _decide(flow: BafuFlow, outcome: Match | Unmatched) -> Match | Unmatched:
    """Apply the plan's decisions to any pipeline outcome, ``Match`` included.

    Decision 5 must override even a real ``Match``: a BAFU ``Water, salt`` or
    ``Water, fossil`` flow can share a name with the freshwater flow the pipeline
    would otherwise happily match, and that match would still assert the wrong factor.
    Every other decision only ever refines an ``Unmatched`` (see ``classify_unmatched``).
    """
    if flow.name.startswith(_NON_FRESHWATER):
        return Unmatched("no_ef_flow", "EF water use characterises freshwater deprivation only")
    if isinstance(outcome, Unmatched):
        return classify_unmatched(flow, outcome)
    return outcome


class BafuEfMatchedSource(Source):
    """Match every BAFU flow rank 3 and rank 6 leave uncovered against the EF index."""

    def fetch(self, ctx: RunContext) -> RawData:
        """Fetch every named input except ``ef_vocab`` (a directory, read locally in ``parse``)."""
        self.inputs = {
            name: fetch_mod.fetch(url, ctx)
            for name, url in self.config.inputs.items()
            if name != _VOCAB_DIR
        }
        return fetch_mod.fetch(self.config.fetch_url, ctx)

    def parse(self, raw: RawData) -> Records:
        """One record holding the flow universe, CAS table, exclusion set, index and pipeline."""
        missing = [name for name in _REQUIRED if name not in self.inputs]
        if missing:
            raise RuntimeError(
                f"inputs {missing} not fetched: fetch() must run before parse(), and the "
                f"registry entry must declare inputs {list(_REQUIRED)}"
            )
        rank3 = orjson.loads(self.inputs["rank3"].content)
        rank6 = orjson.loads(self.inputs["rank6"].content)
        records = parse_ecospold_zip(raw)
        cas, conflicts = substance_cas(records)
        ef_cfs_path = Path(self.inputs["ef_cfs"].source_url[len("file://") :])
        index = EfFlowIndex.from_files(
            ef_cfs_path, _local_path(self.config.inputs.get(_VOCAB_DIR))
        )
        return [
            {
                "bafu": BafuFlowIndex.from_ecospold(records),
                "cas": cas,
                "cas_conflicts": conflicts,
                "excluded": _codes(rank3) | _codes(rank6),
                "index": index,
                "pipeline": default_pipeline(index, load_aliases()),
            }
        ]

    def entry_for(self, flow: BafuFlow, match: Match, index: EfFlowIndex) -> Record:
        """One randonneur ``replace`` entry asserting ``flow`` resolves to ``match``."""
        ef_flow = index.get(match.code)
        source: Record = {"name": flow.name, "code": flow.code}
        if flow.unit:
            source["unit"] = flow.unit
        if flow.context:
            source["context"] = flow.context

        unit = EF_UNIT.get(flow.unit, flow.unit)
        conversion_factor: float | None = None
        if flow.unit == "Bq":
            conversion_factor = 0.001
        elif flow.unit == "kg" and set(index.vector(match.code)) == {_WATER_USE}:
            # the only factor this EF flow carries is water-use (volume-denominated);
            # a kg-denominated BAFU source is water at (fresh water's) density.
            unit = "cubic meter"
            conversion_factor = 0.001

        target: Record = {"code": match.code}
        if ef_flow is not None and ef_flow.name:
            target["name"] = ef_flow.name
        if unit:
            target["unit"] = unit
        if ef_flow is not None and ef_flow.context:
            target["context"] = list(ef_flow.context)
        if match.location:
            target["location"] = match.location

        entry: Record = {"source": source, "target": target}
        if conversion_factor is not None:
            entry["conversion_factor"] = conversion_factor
        if match.caveats:
            entry["comment"] = "; ".join(match.caveats)
        return entry

    def transform(self, records: Records) -> Rows:
        """Emit one entry per BAFU flow the pipeline resolves, in deterministic order."""
        (record,) = records
        rows: Rows = []
        flows = sorted(record["bafu"], key=lambda f: (f.name, f.category, f.subcategory, f.unit))
        for flow in flows:
            if flow.code in record["excluded"]:
                continue
            cas = record["cas"].get(flow.name)
            outcome = _decide(flow, record["pipeline"].match(flow, cas))
            if isinstance(outcome, Match):
                rows.append(self.entry_for(flow, outcome, record["index"]))
        return rows
