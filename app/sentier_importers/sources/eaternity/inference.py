"""Compose rank 4 (biosphere3 -> Eaternity) with CF identity into bafu-2026-v1 -> ef-3.1.

For every rank-4 entry the Eaternity target is placed at BAFU sub-compartment level
(:mod:`bridge`), and the biosphere3 source is resolved to its EF twin
(:mod:`cf_identity`). The composition asserts one thing per entry: *this BAFU flow
receives this EF characterization factor*. Flows the rank-3 bridge already maps are
skipped, so the emitted package only fills gaps and rank 3 keeps precedence by
construction; everything that cannot be asserted goes to the review sidecar with a
reason rather than being guessed at. That includes a BAFU flow reached by several
biosphere3 partners that resolve to different EF flows.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from sentier_importers.core.types import Record, Rows
from sentier_importers.sources.eaternity.bridge import BafuFlow, BafuFlowIndex, resolve
from sentier_importers.sources.eaternity.cf_identity import (
    CfVectors,
    EfLabels,
    Twin,
    Withheld,
    find_twin,
)

#: BAFU unit -> the spelling the rank-3 bridge uses on the EF side. EF ionising-
#: radiation factors are per kBq, so a Bq source lands on a kBq target (with the
#: ``conversion_factor`` the resolver supplies). Unknown units keep the BAFU spelling.
EF_UNIT: dict[str, str] = {
    "kg": "kilogram",
    "Bq": "kBq",
    "kBq": "kBq",
    "m2": "m2",
    "m2a": "m2*a",
    "m3": "cubic meter",
    "MJ": "megajoule",
}

#: Comments are caveats. An exact twin in the matching sub-compartment needs none.
_T3_COMMENT = (
    "the EF flow is characterised in impact categories beyond those this flow currently receives"
)


@dataclass(frozen=True)
class Inputs:
    rank4: dict
    rank3_codes: frozenset[str]
    bafu: BafuFlowIndex
    vectors: CfVectors
    labels: EfLabels


@dataclass
class Inference:
    entries: Rows = field(default_factory=list)
    review: Rows = field(default_factory=list)
    skipped_in_rank3: int = 0


def _entries(package: dict) -> list[Record]:
    return [e for verb in ("replace", "update") for e in package.get(verb, [])]


def _source_dict(flow: BafuFlow) -> Record:
    source: Record = {"name": flow.name, "code": flow.code}
    if flow.unit:
        source["unit"] = flow.unit
    if flow.context:
        source["context"] = flow.context
    return source


def entry_for(flow: BafuFlow, twin: Twin, factor: float, labels: EfLabels) -> Record:
    target: Record = {"code": twin.code}
    if name := labels.name(twin.code):
        target["name"] = name
    if flow.unit:
        target["unit"] = EF_UNIT.get(flow.unit, flow.unit)
    if context := labels.context(twin.code):
        target["context"] = context
    entry: Record = {"source": _source_dict(flow), "target": target}
    if factor != 1.0:
        entry["conversion_factor"] = factor
    caveats = []
    if twin.tier == "T3":
        caveats.append(_T3_COMMENT)
    if not twin.sub_matched and context:
        caveats.append(
            "no CF-identical EF flow in the matching sub-compartment; target is "
            f"{context[-1]!r}"
        )
    if caveats:
        entry["comment"] = "; ".join(caveats)
    return entry


def _review(source: Record, reason: str, detail: str) -> Record:
    return {"source": source, "reason": reason, "detail": detail}


def infer(inputs: Inputs) -> Inference:
    """Entries for BAFU flows rank 3 lacks, plus a review row per withheld pair."""
    reached: dict[BafuFlow, dict[str, float]] = {}
    result = Inference()
    for entry in _entries(inputs.rank4):
        target, b3 = entry["target"], entry["source"]
        root = (target.get("context") or [""])[0]
        resolutions = resolve(
            inputs.bafu, target["name"], root, target.get("unit") or "", b3.get("context") or []
        )
        if not resolutions:
            result.review.append(
                _review(
                    {k: target[k] for k in ("name", "code", "unit", "context") if k in target},
                    "target_unresolved",
                    "Eaternity flow does not resolve to a bafu-2026-v1 flow at the biosphere3 "
                    "sub-compartment (extension flow, other compartment, or unknown context)",
                )
            )
            continue
        for resolution in resolutions:
            reached.setdefault(resolution.flow, {})[b3["code"]] = resolution.conversion_factor

    for flow in sorted(reached, key=lambda f: (f.name, f.category, f.subcategory, f.unit)):
        if flow.code in inputs.rank3_codes:
            result.skipped_in_rank3 += 1
            continue
        outcomes = {
            b3_code: find_twin(inputs.vectors, inputs.labels, b3_code, flow)
            for b3_code in sorted(reached[flow])
        }
        twins = {code: out for code, out in outcomes.items() if isinstance(out, Twin)}
        withheld = [out for out in outcomes.values() if isinstance(out, Withheld)]
        if len({twin.code for twin in twins.values()}) > 1:
            # several biosphere3 partners reach this flow and assert different EF flows:
            # picking one would be a silent decision, so none is made
            picks = sorted(twin.code for twin in twins.values())
            result.review.append(
                _review(
                    _source_dict(flow),
                    "partners_disagree",
                    f"{len(twins)} biosphere3 partners resolve to different EF flows: "
                    + ", ".join(picks),
                )
            )
        elif twins:
            b3_code, twin = next(iter(twins.items()))
            result.entries.append(entry_for(flow, twin, reached[flow][b3_code], inputs.labels))
        else:
            # report the most informative reason: anything but "uncharacterised" first
            chosen = sorted(withheld, key=lambda w: w.reason == "uncharacterised")[0]
            result.review.append(_review(_source_dict(flow), chosen.reason, chosen.detail))
    return result
