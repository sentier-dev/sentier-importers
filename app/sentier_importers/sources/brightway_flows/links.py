"""What a Brightway Flows link is worth, whatever the pair it serves.

A link is used only if its target is an EF 3.1 flow that sentier-vocab lists, it is
not a ``broadMatch``, a long-term source lands on a long-term EF context, and the
target is not a known label defect for that source name (spec 2026-11-19).

"Changes no score" has one definition here, :func:`scores`: a flow scores when it
carries at least one non-zero factor. A flow without CF rows and a flow whose CF rows
are all zero are the same thing to a result.

The functions take the link as an argument and read the rest from a state
(:class:`LinkState`); finding the link of a source flow is the business of each pair
(``bafu``, ``agribalyse``), as is what it does with a usable link.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol

from sentier_importers.core.types import Record
from sentier_importers.matching.ef_index import EfFlow, EfFlowIndex, LabelDefect
from sentier_importers.sources.brightway_flows.correspondences import (
    RELEASE,
    BrightwayFlows,
    ConsensusFlow,
    Link,
)
from sentier_importers.sources.brightway_flows.factors import Vector

#: Brightway Flows unit symbol -> the EF convention spelling our packages use, for
#: targets that carry no factor (a characterised target takes ``reference_unit``).
EF_UNIT = {
    "kg": "kilogram",
    "kBq": "kBq",
    "m3": "cubic meter",
    "MJ": "megajoule",
    "m2": "m2",
    "m2.a": "m2*a",
    "m3.a": "m3*a",
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
    "m3y": ("volume-time", 1.0),
    "m3*a": ("volume-time", 1.0),
    "mj": ("energy", 1.0),
    "megajoule": ("energy", 1.0),
    "kwh": ("energy", 3.6),
}
UNCHARACTERISED = "uncharacterised in EF 3.1: no factor in any method"
ALL_ZERO = "all factors are zero in EF 3.1"
#: Same wording as the nomenclature package (``mappings_biosphere_matched.entry_for``):
#: said whenever a target's context is left out because the index could not place it.
UNCERTAIN_CONTEXT = (
    "EF context inferred from the Brightway context code, branch/sub-compartment uncertain"
)
#: Brightway Flows context codes of the long-term compartments end with this
#: (``envi-air-lote``, ``envi-wate-lote``).
_LONG_TERM_CODE_SUFFIX = "lote"


class LinkState(Protocol):
    """What the decisions read from the parsed inputs of a pair."""

    bw: BrightwayFlows
    index: EfFlowIndex
    vectors: Mapping[str, Vector]
    vocab_codes: frozenset[str]
    defects: Mapping[str, LabelDefect]


@dataclass(frozen=True)
class Decision:
    link: Link | None
    consensus: ConsensusFlow | None
    target: EfFlow | None
    reason: str | None


def _is_long_term(source: Mapping) -> bool:
    return any("long-term" in part for part in source.get("context") or [])


def _target_is_long_term(target: EfFlow, consensus: ConsensusFlow) -> bool:
    """Whether the EF target sits on a long-term context.

    The index leaf says so, unless the index itself marks that context uncertain
    (an uncharacterised flow placed through an ambiguous ``bw-context`` code, which
    folds long-term water into ``water, unspecified``): then the context code
    Brightway Flows gives the flow decides.
    """
    if target.context_uncertain:
        return consensus.context_code.endswith(_LONG_TERM_CODE_SUFFIX)
    return "long-term" in target.leaf


def scores(code: str | None, state: LinkState) -> bool:
    """Whether the EF flow ``code`` carries at least one non-zero factor."""
    return any(state.vectors.get(code, {}).values())


def decide(source: Mapping, link: Link | None, state: LinkState) -> Decision:
    """Whether ``link``, the Brightway Flows link of ``source``, gives it a usable EF
    target, and if not, why. ``source`` is read for its ``name`` and ``context``."""
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
    if _is_long_term(source) and not _target_is_long_term(target, consensus):
        return Decision(link, consensus, target, "long_term_onto_immediate")
    defect = state.defects.get(target.cas) if target.cas else None
    if defect and source["name"].strip().lower() == defect.wrong_label.strip().lower():
        return Decision(link, consensus, target, "label_defect")
    return Decision(link, consensus, target, None)


def conversion(
    source_unit: str, target_unit: str, multiplier: float | None
) -> tuple[float | None, str | None]:
    """``(conversion_factor, withhold reason)`` for one unit pair and their multiplier."""
    source = _DIMENSION.get(source_unit.lower())
    target = _DIMENSION.get(target_unit.lower())
    if source is None or target is None:
        return None, "unit_unknown"
    if multiplier is not None and not (math.isfinite(multiplier) and multiplier > 0):
        return None, "unit_mismatch"
    if source[0] != target[0]:
        return (multiplier, None) if multiplier is not None else (None, "unit_mismatch")
    ratio = source[1] / target[1]
    if multiplier is None:
        return (None, None) if math.isclose(ratio, 1.0) else (None, "unit_mismatch")
    if not math.isclose(multiplier, ratio, rel_tol=1e-9):
        return None, "unit_mismatch"
    return (None, None) if math.isclose(multiplier, 1.0) else (multiplier, None)


def target_record(flow: EfFlow, unit: str | None) -> Record:
    record: Record = {"name": flow.label or flow.name, "code": flow.code}
    if unit:
        record["unit"] = unit
    if flow.context and not flow.context_uncertain:
        record["context"] = list(flow.context)
    return record


def their_unit(
    source: Mapping, decision: Decision, state: LinkState
) -> tuple[str | None, float | None, str | None]:
    """``(target unit, conversion_factor, withhold reason)`` of a usable link.

    The unit is the EF convention one of their target: the index knows it for a
    characterised flow, the Brightway Flows flow gives it otherwise.
    """
    code = decision.target.code
    unit = state.index.reference_unit(code) or EF_UNIT.get(decision.consensus.unit)
    if unit is None:
        return None, None, "unit_unknown"
    factor, reason = conversion(source.get("unit") or "", unit, decision.link.multiplier)
    return unit, factor, reason


def no_score_note(code: str, state: LinkState) -> str:
    """Why the flow ``code``, which does not score, changes no result."""
    return ALL_ZERO if state.vectors.get(code) else UNCHARACTERISED


def rescaled_note(factor: float, link: Link) -> str:
    return f"amount rescaled by {factor:g} ({link.basis or 'conversion'})"


def link_entry(
    source: Mapping, decision: Decision, unit: str, factor: float | None, state: LinkState
) -> Record:
    """The package entry of a usable link: ``source`` onto their EF target in ``unit``.

    The comment names the release and the match, says when the target changes no
    score and why, when its context is left out, and how the amount is rescaled.
    """
    flow, link = decision.target, decision.link
    comments = [f"{RELEASE}, skos:{link.match}"]
    if not scores(flow.code, state):
        comments.append(no_score_note(flow.code, state))
    entry: Record = {"source": dict(source), "target": target_record(flow, unit)}
    if "context" not in entry["target"]:
        comments.append(UNCERTAIN_CONTEXT)
    if factor is not None:
        entry["conversion_factor"] = factor
        comments.append(rescaled_note(factor, link))
    entry["comment"] = "; ".join(comments)
    return entry


def order(source: Mapping) -> tuple[str, str, str]:
    """Sort key of a source record: name, context, unit."""
    return (
        source["name"].lower(),
        "/".join(source.get("context") or []),
        source.get("unit") or "",
    )


def their_record(decision: Decision) -> Record | None:
    """What Brightway Flows links a source to, for a review row."""
    if decision.link is None:
        return None
    record: Record = {"code": decision.link.target_id, "match": decision.link.match}
    if decision.consensus is not None:
        # the live flow, which a redirected link target is not
        record["code"] = decision.consensus.identifier
        record["name"] = decision.consensus.label
        record["context"] = decision.consensus.context_code
        record["in_ef"] = decision.consensus.is_ef
    return record
