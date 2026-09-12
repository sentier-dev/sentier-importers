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
from sentier_importers.matching.ef_index import EfFlowIndex, normalise_cas
from sentier_importers.matching.matchers import load_aliases
from sentier_importers.matching.pipeline import Match, Unmatched, default_pipeline
from sentier_importers.sources.bafu.ecospold import parse_ecospold_zip
from sentier_importers.sources.eaternity.bridge import BafuFlow, BafuFlowIndex

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

#: Physical dimension per unit spelling (BAFU and EF spellings both included), used by
#: ``conversion`` to tell a safe same-dimension unit respelling from an unsafe
#: cross-dimension pair. ``Nm3`` (normal cubic meter, some BAFU gas flows) is volume
#: like ``m3``: still the wrong dimension for an energy or mass EF reference unit.
_DIMENSION: dict[str, str] = {
    "kg": "mass",
    "kilogram": "mass",
    "Bq": "activity",
    "kBq": "activity",
    "m3": "volume",
    "Nm3": "volume",
    "cubic meter": "volume",
    "m2": "area",
    "m2a": "area-time",
    "m2*a": "area-time",
    "MJ": "energy",
    "megajoule": "energy",
    "kWh": "energy",
}

#: The few same-dimension unit pairs whose physical scale actually differs: an SI
#: activity prefix (BAFU sometimes reports becquerel where EF's reference unit is
#: kilobecquerel) and the historical kWh/MJ energy pair. Every other same-dimension
#: pair (kg/kilogram, m3/cubic meter, kBq/kBq, m2, m2*a, MJ/megajoule) is the same
#: physical scale and needs no factor (1.0) -- see ``conversion``. The reverse
#: direction, kBq -> Bq, never occurs: EF's reference unit for ionising radiation is
#: always kBq (``EfFlowIndex.reference_unit``), never Bq.
_SCALED: dict[tuple[str, str], float] = {("Bq", "kBq"): 0.001, ("kWh", "megajoule"): 3.6}


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
    """Normalised CAS number per substance NAME over every biosphere exchange (group 4).

    A BAFU flow name is shared across sub-compartments (``Methanol`` to air and to
    water, say); its CAS number is a property of the substance, not the placement, so
    it is collected per name rather than per (name, category, subcategory, unit).
    Every value is compared after ``normalise_cas`` (leading-zero padding, e.g.
    ``000067-56-1`` vs ``67-56-1``, must not read as two different substances), and a
    blank or missing CAS is ignored rather than counted as a value. A name whose
    exchanges disagree on (normalised) CAS gets no CAS at all -- returned instead in
    the second mapping, keyed by name, so a caller can see what was withheld and why.
    """
    per_name: dict[str, set[str]] = {}
    for record in records:
        for exchange in record.get("exchanges", []):
            if exchange.get("group_code") != _BIOSPHERE_GROUP:
                continue
            cas = normalise_cas(exchange.get("cas"))
            if cas is None:
                continue
            per_name.setdefault(exchange["name"], set()).add(cas)
    cas = {name: next(iter(values)) for name, values in per_name.items() if len(values) == 1}
    conflicts = {name: values for name, values in per_name.items() if len(values) > 1}
    return cas, conflicts


def conversion(bafu_unit: str, ef_unit: str) -> float | None:
    """Fixed multiplier from ``bafu_unit`` onto EF's ``ef_unit``, or ``None`` when unsafe.

    The same unit spelling is trivially 1.0. Otherwise a conversion is only ever safe
    within one physical dimension (see ``_DIMENSION``): most BAFU/EF spelling pairs
    within a dimension are the same physical scale and need no factor at all (1.0,
    e.g. ``kg``/``kilogram``, ``m3``/``cubic meter``, ``m2*a``/``m2a``); the handful
    that are not (an SI activity prefix, the historical kWh/MJ energy pair) have one
    fixed factor in ``_SCALED``. A cross-dimension pair (e.g. a volume-denominated
    BAFU flow onto an EF flow whose reference unit is energy or mass), or either unit
    outside ``_DIMENSION`` entirely, returns ``None`` -- there is no general fixed
    conversion between different physical quantities, so the caller must withhold the
    flow rather than assert a fabricated factor.

    The one true mass -> volume conversion this system asserts (a BAFU kilogram water
    flow onto an EF water-use cubic-meter flow, using water's density) is deliberately
    NOT handled here: it depends on the EF flow's characterisation method, which this
    function -- unit strings only -- cannot see. It is applied as a special case at
    the call site instead (see ``_unit_factor``).
    """
    if bafu_unit == ef_unit:
        return 1.0
    if (bafu_unit, ef_unit) in _SCALED:
        return _SCALED[(bafu_unit, ef_unit)]
    dimension = _DIMENSION.get(bafu_unit)
    if dimension is None or dimension != _DIMENSION.get(ef_unit):
        return None
    return 1.0


def _unit_factor(flow: BafuFlow, match: Match, index: EfFlowIndex) -> float | None:
    """The multiplier from ``flow.unit`` onto ``match``'s EF reference unit, or
    ``None`` when no fixed conversion exists and the flow must be withheld.
    """
    if flow.unit == "kg" and set(index.vector(match.code)) == {_WATER_USE}:
        # water is the only substance with a fixed mass -> volume factor (density);
        # this depends on the target's characterisation method, so it cannot live in
        # the generic, method-blind ``conversion`` table above.
        return 0.001
    return conversion(flow.unit, index.reference_unit(match.code))


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


def _decide(flow: BafuFlow, outcome: Match | Unmatched, index: EfFlowIndex) -> Match | Unmatched:
    """Apply every plan decision to one pipeline outcome, ``Match`` included.

    Order:

    1. freshwater-deprivation-only (decision 5) overrides even a real ``Match``: a
       BAFU ``Water, salt`` or ``Water, fossil`` flow can share a name with the
       freshwater flow the pipeline would otherwise happily match, and that match
       would still assert the wrong factor;
    2. a ``Match`` onto an ocean-discharge flow (``emissions to water`` / ``ocean``)
       whose EF target is water-use-only is likewise never right (decision 3): EF's
       water-use method characterises freshwater withdrawal and (return-flow) release,
       never a sea-water discharge, so the same-named freshwater flow the unspecified
       fallback would otherwise offer must not be taken here. This must run before the
       unit check below: a kg-denominated ocean flow would otherwise pass the water
       density special case and be wrongly emitted;
    3. a real ``Match`` with no fixed unit conversion onto the EF flow's reference
       unit (``EfFlowIndex.reference_unit``) is withheld as ``unit_mismatch`` (decision
       1) rather than emitted with a fabricated factor;
    4. everything else only ever refines an ``Unmatched`` (see ``classify_unmatched``).
    """
    if flow.name.startswith(_NON_FRESHWATER):
        return Unmatched("no_ef_flow", "EF water use characterises freshwater deprivation only")
    if isinstance(outcome, Match):
        if (
            flow.category == "emissions to water"
            and flow.subcategory == "ocean"
            and set(index.vector(outcome.code)) == {_WATER_USE}
        ):
            return Unmatched("no_ef_flow", "EF water use has no sea-water discharge flow")
        if _unit_factor(flow, outcome, index) is None:
            ef_flow = index.get(outcome.code)
            name = ef_flow.name if ef_flow is not None else outcome.code
            ef_unit = index.reference_unit(outcome.code)
            return Unmatched(
                "unit_mismatch",
                f"BAFU unit {flow.unit} vs EF reference unit {ef_unit} for {name}; "
                "no fixed conversion",
            )
        return outcome
    return classify_unmatched(flow, outcome)


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
        """One randonneur ``replace`` entry asserting ``flow`` resolves to ``match``.

        The target unit is always the EF flow's own reference unit
        (``EfFlowIndex.reference_unit``) -- never a respelling of the BAFU unit -- and
        ``conversion_factor`` is set only when that factor is not 1.0.
        """
        ef_flow = index.get(match.code)
        source: Record = {"name": flow.name, "code": flow.code}
        if flow.unit:
            source["unit"] = flow.unit
        if flow.context:
            source["context"] = flow.context

        ef_unit = index.reference_unit(match.code)
        # ``outcomes`` withholds a genuine mismatch before an entry is ever built for
        # it; a ``None`` here only guards a direct call against a bad (match, index)
        # pair and is never expected to fire on the emitted path.
        factor = _unit_factor(flow, match, index)
        if factor is None:
            factor = 1.0

        target: Record = {"code": match.code}
        if ef_flow is not None and ef_flow.name:
            target["name"] = ef_flow.name
        target["unit"] = ef_unit
        if ef_flow is not None and ef_flow.context:
            target["context"] = list(ef_flow.context)
        if match.location:
            target["location"] = match.location

        entry: Record = {"source": source, "target": target}
        if factor != 1.0:
            entry["conversion_factor"] = factor
        if match.caveats:
            entry["comment"] = "; ".join(match.caveats)
        return entry

    def outcomes(self, records: Records) -> list[tuple[BafuFlow, Match | Unmatched]]:
        """Every non-excluded BAFU flow with its final outcome, sorted for determinism.

        A flow rank 3 or rank 6 already maps is skipped entirely -- not just its
        entry withheld -- since those bridges keep precedence and this source's job
        is only to fill the gap they leave.
        """
        (record,) = records
        flows = sorted(record["bafu"], key=lambda f: (f.name, f.category, f.subcategory, f.unit))
        result: list[tuple[BafuFlow, Match | Unmatched]] = []
        for flow in flows:
            if flow.code in record["excluded"]:
                continue
            cas = record["cas"].get(flow.name)
            match = record["pipeline"].match(flow, cas)
            result.append((flow, _decide(flow, match, record["index"])))
        return result

    def transform(self, records: Records) -> Rows:
        """Emit one entry per BAFU flow ``outcomes`` resolves to a ``Match``."""
        (record,) = records
        index = record["index"]
        return [
            self.entry_for(flow, outcome, index)
            for flow, outcome in self.outcomes(records)
            if isinstance(outcome, Match)
        ]
