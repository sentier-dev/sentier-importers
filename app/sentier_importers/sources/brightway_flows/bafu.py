"""Brightway Flows links for the ``bafu-2026-v1 -> ef-3.1`` pair: shared logic.

Three sources build on this module (``bafu_sources``): the fifth package of the pair,
the alignment of packages 1 to 4, and the review sidecar. They share one parsed
:class:`State` and the same decisions, so the sidecar always reports exactly what the
two emitting sources did.

A link is used only if its target is an EF 3.1 flow that sentier-vocab lists, it is
not a ``broadMatch``, a long-term source lands on a long-term EF context, and the
target is not a known label defect for that source name (spec 2026-11-19).
"""

from __future__ import annotations

import math
from collections.abc import Iterator, Mapping
from dataclasses import dataclass

import orjson
from sentier_importers.core import fetch as fetch_mod
from sentier_importers.core.context import RunContext
from sentier_importers.core.source import Source
from sentier_importers.core.types import RawData, Record, Records
from sentier_importers.matching.ef_index import (
    EfFlow,
    EfFlowIndex,
    LabelDefect,
    load_label_defects,
)
from sentier_importers.sources.bafu.packages import CURATED, INFERRED, MATCHED, NOMENCLATURE
from sentier_importers.sources.brightway_flows.correspondences import (
    PINNED_SHA256,
    RELEASE,
    BrightwayFlows,
    ConsensusFlow,
    Link,
    load,
)
from sentier_importers.sources.brightway_flows.factors import Vector, compare, factor_vectors
from sentier_importers.sources.brightway_flows.identifiers import bafu_link
from sentier_importers.sources.randonneur_data.ecoinvent_ef_biosphere import vocab_flow_codes

LIST = "bafu-2026-v1"
_VOCAB_DIR = "ef_vocab"
#: Package name -> registry input name, in the pair's order of precedence.
PACKAGE_INPUTS = {
    CURATED: "curated",
    INFERRED: "inferred",
    MATCHED: "matched",
    NOMENCLATURE: "nomenclature",
}
#: BAFU flow codes whose characterised link Laurenz accepted after the grouped review.
#: Empty until then: a characterised link changes a score and is held back.
ACCEPTED_CHARACTERISED: frozenset[str] = frozenset()
#: Brightway Flows unit symbol -> the EF convention spelling our packages use, for
#: targets that carry no factor (a characterised target takes ``reference_unit``).
EF_UNIT = {
    "kg": "kilogram",
    "kBq": "kBq",
    "m3": "cubic meter",
    "MJ": "megajoule",
    "m2": "m2",
    "m2.a": "m2*a",
}
#: Unit spelling (lowercased) -> (dimension, scale); the subset sentier-mappings'
#: validator knows, so an entry built here passes its unit check.
_DIMENSION = {
    "kg": ("mass", 1.0),
    "kilogram": ("mass", 1.0),
    "bq": ("activity", 1.0),
    "kbq": ("activity", 1e3),
    "m3": ("volume", 1.0),
    "cubic meter": ("volume", 1.0),
    "nm3": ("volume", 1.0),
    "m2": ("area", 1.0),
    "m2a": ("area-time", 1.0),
    "m2*a": ("area-time", 1.0),
    "mj": ("energy", 1.0),
    "megajoule": ("energy", 1.0),
    "kwh": ("energy", 3.6),
}
ALIGNED = f"target aligned with {RELEASE} (identical factors)"
UNCHARACTERISED = "uncharacterised in EF 3.1: no factor in any method"


@dataclass(frozen=True)
class State:
    bw: BrightwayFlows
    by_id: Mapping[str, Link]
    universe: tuple[Record, ...]
    packages: Mapping[str, dict]
    index: EfFlowIndex
    vectors: Mapping[str, Vector]
    vocab_codes: frozenset[str]
    defects: Mapping[str, LabelDefect]
    mapped: Mapping[str, tuple[str, Record]]


@dataclass(frozen=True)
class Decision:
    link: Link | None
    consensus: ConsensusFlow | None
    target: EfFlow | None
    reason: str | None


def entries_of(package: Mapping) -> Iterator[Record]:
    if package.get("update"):
        raise ValueError(f"package {package.get('name')!r} has update entries: not supported")
    yield from package.get("replace", [])


def _is_long_term(source: Mapping) -> bool:
    return any("long-term" in part for part in source.get("context") or [])


def decide(source: Mapping, state: State) -> Decision:
    """Whether Brightway Flows gives ``source`` a usable EF target, and if not, why."""
    link = bafu_link(source, state.by_id)
    if link is None:
        return Decision(None, None, None, "no_link")
    target_id = state.bw.resolve(link.target_id)
    consensus = state.bw.flows.get(target_id)
    if consensus is None or not consensus.is_ef:
        return Decision(link, consensus, None, "no_ef_target")
    if target_id not in state.vocab_codes:
        return Decision(link, consensus, None, "target_not_in_vocab")
    target = state.index.get(target_id)
    if target is None:
        return Decision(link, consensus, None, "target_not_indexed")
    if link.match == "broadMatch":
        return Decision(link, consensus, target, "broad_match")
    if _is_long_term(source) and "long-term" not in target.leaf:
        return Decision(link, consensus, target, "long_term_onto_immediate")
    defect = state.defects.get(target.cas) if target.cas else None
    if defect and source["name"].strip().lower() == defect.wrong_label.strip().lower():
        return Decision(link, consensus, target, "label_defect")
    return Decision(link, consensus, target, None)


def _conversion(
    source_unit: str, target_unit: str, multiplier: float | None
) -> tuple[float | None, str | None]:
    """``(conversion_factor, withhold reason)`` for one unit pair and their multiplier."""
    source = _DIMENSION.get(source_unit.lower())
    target = _DIMENSION.get(target_unit.lower())
    if source is None or target is None:
        return None, "unit_unknown"
    if source[0] != target[0]:
        return (multiplier, None) if multiplier else (None, "unit_mismatch")
    ratio = source[1] / target[1]
    if multiplier is None:
        return (None, None) if math.isclose(ratio, 1.0) else (None, "unit_mismatch")
    if not math.isclose(multiplier, ratio, rel_tol=1e-9):
        return None, "unit_mismatch"
    return (None, None) if math.isclose(multiplier, 1.0) else (multiplier, None)


def _target_record(flow: EfFlow, unit: str | None) -> Record:
    record: Record = {"name": flow.label or flow.name, "code": flow.code}
    if unit:
        record["unit"] = unit
    if flow.context and not flow.context_uncertain:
        record["context"] = list(flow.context)
    return record


def package_entry(source: Record, state: State) -> tuple[Record | None, str | None]:
    """The package-5 entry for an unmapped ``source``, or ``(None, reason)``."""
    decision = decide(source, state)
    if decision.reason is not None:
        return None, decision.reason
    flow, link = decision.target, decision.link
    unit = state.index.reference_unit(flow.code) or EF_UNIT.get(decision.consensus.unit)
    if unit is None:
        return None, "unit_unknown"
    factor, reason = _conversion(source.get("unit") or "", unit, link.multiplier)
    if reason is not None:
        return None, reason
    if flow.characterised and source["code"] not in ACCEPTED_CHARACTERISED:
        return None, "characterised_held_for_review"
    comments = [f"{RELEASE}, skos:{link.match}"]
    if not flow.characterised:
        comments.append(UNCHARACTERISED)
    entry: Record = {"source": dict(source), "target": _target_record(flow, unit)}
    if factor is not None:
        entry["conversion_factor"] = factor
        comments.append(f"amount rescaled by {factor:g} ({link.basis or 'conversion'})")
    entry["comment"] = "; ".join(comments)
    return entry, None


def retarget(entry: Record, state: State) -> tuple[Record, str | None]:
    """``entry`` with their EF target when the two flows carry identical factors.

    Returns ``(entry, kind)``: ``retargeted`` when the target changed; ``None`` when
    they have no usable link or agree with us; otherwise the entry unchanged and why
    it was left alone (``differ``, ``theirs_zero``, ``ours_zero``, ``both_zero``,
    ``unit_differs``). ``both_zero``: neither flow carries a factor, so nothing shows
    that the two are the same flow; equal factors are the evidence a retarget needs.
    """
    ours = entry["target"].get("code")
    decision = decide(entry["source"], state)
    if decision.reason is not None or ours is None:
        return entry, None
    flow = decision.target
    if state.bw.resolve(ours) == flow.code:
        return entry, None
    our_vector = state.vectors.get(ours, {})
    kind, _ = compare(our_vector, state.vectors.get(flow.code, {}))
    if kind != "identical":
        return entry, kind
    if not any(our_vector.values()):
        return entry, "both_zero"
    if not math.isclose(
        entry.get("conversion_factor", 1.0), decision.link.multiplier or 1.0, rel_tol=1e-9
    ):
        return entry, "unit_differs"
    target = dict(entry["target"])
    target["name"] = flow.label or flow.name
    target["code"] = flow.code
    if flow.context and not flow.context_uncertain:
        target["context"] = list(flow.context)
    else:
        target.pop("context", None)
    comment = "; ".join(part for part in (entry.get("comment"), ALIGNED) if part)
    return {**entry, "target": target, "comment": comment}, "retargeted"


class BafuBrightwayFlowsBase(Source):
    """Fetch and parse every input once into a :class:`State`."""

    #: ``None`` in tests, which feed a synthetic file.
    expected_sha256: str | None = PINNED_SHA256

    def fetch(self, ctx: RunContext) -> RawData:
        """Fetch every named input except ``ef_vocab`` (a directory, read in ``parse``)."""
        self.inputs = {
            name: fetch_mod.fetch(url, ctx)
            for name, url in self.config.inputs.items()
            if name != _VOCAB_DIR
        }
        return fetch_mod.fetch(self.config.fetch_url, ctx)

    def parse(self, raw: RawData) -> Records:
        bw = load(raw.content, self.expected_sha256)
        vocab_dir = fetch_mod.local_path(self.config.inputs.get(_VOCAB_DIR), _VOCAB_DIR)
        cf_bytes = self.inputs["ef_cfs"].content
        packages = {
            package: orjson.loads(self.inputs[name].content)
            for package, name in PACKAGE_INPUTS.items()
        }
        mapped: dict[str, tuple[str, Record]] = {}
        for package, payload in packages.items():
            for entry in entries_of(payload):
                mapped.setdefault(entry["source"]["code"], (package, entry))
        coverage = orjson.loads(self.inputs["coverage"].content)["coverage"]
        state = State(
            bw=bw,
            by_id={item.source_id: item for item in bw.links[LIST]},
            universe=tuple(row["source"] for row in coverage),
            packages=packages,
            index=EfFlowIndex.from_bytes(cf_bytes, vocab_dir, include_uncharacterised=True),
            vectors=factor_vectors(cf_bytes),
            vocab_codes=vocab_flow_codes(vocab_dir),
            defects=load_label_defects(),
            mapped=mapped,
        )
        return [{"state": state}]
