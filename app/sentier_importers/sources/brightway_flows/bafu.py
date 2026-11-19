"""Brightway Flows links for the ``bafu-2026-v1 -> ef-3.1`` pair: shared logic.

Three sources build on this module (``bafu_sources``): the fifth package of the pair,
the alignment of packages 1 to 4, and the review sidecar. They share one parsed
:class:`State` and the same decisions, so the sidecar always reports exactly what the
two emitting sources did.

A link is used only if its target is an EF 3.1 flow that sentier-vocab lists, it is
not a ``broadMatch``, a long-term source lands on a long-term EF context, and the
target is not a known label defect for that source name (spec 2026-11-19).

"Changes no score" has one definition here, :func:`scores`: a flow scores when it
carries at least one non-zero factor. A flow without CF rows and a flow whose CF rows
are all zero are the same thing to a result, for package 5 and for the alignment.

A link that does change a score is applied only where the project owner decided so,
flow by flow (``decisions``, ``bafu_decisions.yaml``): package 5 ships a scoring target
and the alignment takes a differently scoring one when the decision is ``theirs``.
Without a decision, or with ``ours`` or ``hold``, such a link is left for the review.
"""

from __future__ import annotations

import math
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path

import orjson
from sentier_importers.core import fetch as fetch_mod
from sentier_importers.core.context import RunContext
from sentier_importers.core.errors import ValidationError
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
from sentier_importers.sources.brightway_flows.decisions import (
    DECISIONS_PATH,
    REVIEWED,
    THEIRS,
    FlowDecision,
    load_decisions,
)
from sentier_importers.sources.brightway_flows.factors import Vector, compare, factor_vectors
from sentier_importers.sources.brightway_flows.identifiers import bafu_link
from sentier_importers.sources.randonneur_data.ecoinvent_ef_biosphere import vocab_flow_codes

LIST = "bafu-2026-v1"
_VOCAB_DIR = "ef_vocab"
_COVERAGE, _EF_CFS = "coverage", "ef_cfs"
#: Package name -> registry input name, in the pair's order of precedence.
PACKAGE_INPUTS = {
    CURATED: "curated",
    INFERRED: "inferred",
    MATCHED: "matched",
    NOMENCLATURE: "nomenclature",
}
#: Inputs ``parse`` reads from ``self.inputs`` (``ef_vocab`` is a directory, read apart).
_REQUIRED = (_COVERAGE, *PACKAGE_INPUTS.values(), _EF_CFS)
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
ALIGNED_FACTORLESS = f"target aligned with {RELEASE} (neither flow carries a factor)"
ALIGNED_UNIT = f"target and unit aligned with {RELEASE} (neither flow carries a factor)"
UNCHARACTERISED = "uncharacterised in EF 3.1: no factor in any method"
ALL_ZERO = "all factors are zero in EF 3.1"
#: What the nomenclature package says of a factorless target's unit
#: (``mappings_biosphere_matched.entry_for``).
_CONVENTION_UNIT = (
    "target unit is the EF convention for this dimension "
    "(EF states no reference unit for this flow)"
)
#: Sentences of an entry's comment that describe a target without factors. They turn
#: false when a reviewed decision moves the entry onto a target that scores.
_NO_SCORE_SENTENCES = frozenset({UNCHARACTERISED, ALL_ZERO, _CONVENTION_UNIT})
#: Same wording as the nomenclature package (``mappings_biosphere_matched.entry_for``):
#: said whenever a target's context is left out because the index could not place it.
UNCERTAIN_CONTEXT = (
    "EF context inferred from the Brightway context code, branch/sub-compartment uncertain"
)
#: Brightway Flows context codes of the long-term compartments end with this
#: (``envi-air-lote``, ``envi-wate-lote``).
_LONG_TERM_CODE_SUFFIX = "lote"


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
    #: Reviewed decisions on score-changing links, by BAFU flow code.
    decisions: Mapping[str, FlowDecision]


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


def scores(code: str | None, state: State) -> bool:
    """Whether the EF flow ``code`` carries at least one non-zero factor."""
    return any(state.vectors.get(code, {}).values())


def _taken(source: Mapping, state: State) -> FlowDecision | None:
    """The reviewed decision on ``source`` when it is to take their target."""
    reviewed = state.decisions.get(source.get("code"))
    return reviewed if reviewed is not None and reviewed.decision == THEIRS else None


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
    if _is_long_term(source) and not _target_is_long_term(target, consensus):
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


def _target_record(flow: EfFlow, unit: str | None) -> Record:
    record: Record = {"name": flow.label or flow.name, "code": flow.code}
    if unit:
        record["unit"] = unit
    if flow.context and not flow.context_uncertain:
        record["context"] = list(flow.context)
    return record


def _their_unit(
    source: Mapping, decision: Decision, state: State
) -> tuple[str | None, float | None, str | None]:
    """``(target unit, conversion_factor, withhold reason)`` of a usable link.

    The unit is the EF convention one of their target: the index knows it for a
    characterised flow, the Brightway Flows flow gives it otherwise.
    """
    code = decision.target.code
    unit = state.index.reference_unit(code) or EF_UNIT.get(decision.consensus.unit)
    if unit is None:
        return None, None, "unit_unknown"
    factor, reason = _conversion(source.get("unit") or "", unit, decision.link.multiplier)
    return unit, factor, reason


def _no_score_note(code: str, state: State) -> str:
    """Why the flow ``code``, which does not score, changes no result."""
    return ALL_ZERO if state.vectors.get(code) else UNCHARACTERISED


def _rescaled_note(factor: float, link: Link) -> str:
    return f"amount rescaled by {factor:g} ({link.basis or 'conversion'})"


def package_entry(source: Record, state: State) -> tuple[Record | None, str | None]:
    """The package-5 entry for an unmapped ``source``, or ``(None, reason)``."""
    decision = decide(source, state)
    if decision.reason is not None:
        return None, decision.reason
    flow, link = decision.target, decision.link
    unit, factor, reason = _their_unit(source, decision, state)
    if reason is not None:
        return None, reason
    scoring = scores(flow.code, state)
    reviewed = _taken(source, state) if scoring else None
    if scoring and reviewed is None:
        return None, "characterised_held_for_review"
    comments = [f"{RELEASE}, skos:{link.match}"]
    if not scoring:
        comments.append(_no_score_note(flow.code, state))
    entry: Record = {"source": dict(source), "target": _target_record(flow, unit)}
    if "context" not in entry["target"]:
        comments.append(UNCERTAIN_CONTEXT)
    if factor is not None:
        entry["conversion_factor"] = factor
        comments.append(_rescaled_note(factor, link))
    if reviewed is not None:
        comments.append(f"reviewed {REVIEWED}: {reviewed.reason}")
    entry["comment"] = "; ".join(comments)
    return entry, None


def _their_target(entry: Record, flow: EfFlow) -> Record:
    """Our target record renamed to ``flow``; the context goes when theirs is uncertain."""
    target = dict(entry["target"])
    target["name"] = flow.label or flow.name
    target["code"] = flow.code
    if flow.context and not flow.context_uncertain:
        target["context"] = list(flow.context)
    else:
        target.pop("context", None)
    return target


def _unit_aligned(entry: Record, decision: Decision, state: State) -> Record | None:
    """``entry`` on their target, in their unit and with their conversion, or ``None``
    when their unit is unknown or their conversion is not usable.

    Only for two flows that do not score (spec addition 2026-11-19): the amount is
    then converted otherwise, and no result can tell. The comment is written anew,
    since ours states a unit convention and a rescaling that no longer hold.
    """
    flow = decision.target
    unit, factor, reason = _their_unit(entry["source"], decision, state)
    if reason is not None:
        return None
    target = {**_their_target(entry, flow), "unit": unit}
    comments = [_no_score_note(flow.code, state), ALIGNED_UNIT]
    if factor is not None:
        comments.append(_rescaled_note(factor, decision.link))
    if "context" not in target:
        comments.append(UNCERTAIN_CONTEXT)
    kept = {k: v for k, v in entry.items() if k not in ("conversion_factor", "comment")}
    aligned: Record = {**kept, "target": target}
    if factor is not None:
        aligned["conversion_factor"] = factor
    aligned["comment"] = "; ".join(comments)
    return aligned


def retarget(entry: Record, state: State) -> tuple[Record, str | None]:
    """``entry`` with their EF target when the two flows carry identical factors.

    Returns ``(entry, kind)``: ``retargeted`` when the target changed; ``None`` when
    they have no usable link or name the very code we use; otherwise the entry
    unchanged and why it was left alone (``differ``, ``theirs_zero``, ``ours_zero``,
    ``unit_differs``, ``ours_without_code``).

    Our code is compared as it is, not through ``BrightwayFlows.resolve``: where they
    merged our EF flow into theirs, sentier-methods may still carry other factors on
    the two codes, so the pair goes through the factor comparison like any other.

    Two flows that both carry no factor count as identical (spec 2026-11-19: Brightway
    Flows is the reference wherever no score can change); the comment then says so
    instead of claiming identical factors.

    When the two sides convert the amount differently, the entry is left alone
    (``unit_differs``) if a flow scores: another conversion changes the amount that
    gets characterised. If neither scores it takes their unit and conversion too
    (:func:`_unit_aligned`), provided their conversion is usable.

    Two flows that score differently are aligned only when the flow's reviewed
    decision is ``theirs``; the comment then gives the reason, and loses what it said
    of a factorless target if theirs scores. A decision never overrides a unit
    problem: with another conversion the entry stays ``unit_differs``.
    """
    ours = entry["target"].get("code")
    decision = decide(entry["source"], state)
    if decision.reason is not None:
        return entry, None
    if ours is None:
        return entry, "ours_without_code"
    flow = decision.target
    if ours == flow.code:
        return entry, None
    kind, _ = compare(state.vectors.get(ours, {}), state.vectors.get(flow.code, {}))
    reviewed = None
    if kind != "identical":
        reviewed = _taken(entry["source"], state)
        if reviewed is None:
            return entry, kind
    multiplier = decision.link.multiplier
    theirs = 1.0 if multiplier is None else multiplier
    if not math.isclose(entry.get("conversion_factor", 1.0), theirs, rel_tol=1e-9):
        if scores(ours, state) or scores(flow.code, state):
            return entry, "unit_differs"
        aligned = _unit_aligned(entry, decision, state)
        return (entry, "unit_differs") if aligned is None else (aligned, "retargeted")
    target = _their_target(entry, flow)
    if reviewed is not None:
        note = f"target aligned with {RELEASE} (reviewed {REVIEWED}: {reviewed.reason})"
    else:
        note = ALIGNED if scores(ours, state) else ALIGNED_FACTORLESS
    kept = entry.get("comment")
    if reviewed is not None and kept and scores(flow.code, state):
        kept = "; ".join(p for p in kept.split("; ") if p not in _NO_SCORE_SENTENCES)
    parts = [kept, note]
    if "context" not in target and UNCERTAIN_CONTEXT not in (entry.get("comment") or ""):
        parts.append(UNCERTAIN_CONTEXT)
    comment = "; ".join(part for part in parts if part)
    return {**entry, "target": target, "comment": comment}, "retargeted"


def _order(source: Mapping) -> tuple[str, str, str]:
    return (
        source["name"].lower(),
        "/".join(source.get("context") or []),
        source.get("unit") or "",
    )


def needs_decision(source: Mapping, state: State) -> bool:
    """Whether taking their link for ``source`` would change a score.

    An unmapped flow: their usable link lands on a scoring target. A mapped one: their
    usable target is another flow than ours and scores differently.
    """
    decision = decide(source, state)
    if decision.reason is not None:
        return False
    mapped = state.mapped.get(source["code"])
    if mapped is None:
        _, _, reason = _their_unit(source, decision, state)
        return reason is None and scores(decision.target.code, state)
    ours = mapped[1]["target"].get("code")
    if ours is None or ours == decision.target.code:
        return False
    theirs = state.vectors.get(decision.target.code, {})
    return compare(state.vectors.get(ours, {}), theirs)[0] != "identical"


def _already_taken(source: Mapping, state: State) -> bool:
    """A ``theirs`` decision whose target our package entry already names."""
    mapped = state.mapped.get(source["code"])
    decision = decide(source, state)
    return (
        _taken(source, state) is not None
        and mapped is not None
        and decision.reason is None
        and mapped[1]["target"].get("code") == decision.target.code
    )


def decision_gaps(state: State) -> tuple[list[Record], list[Record]]:
    """``(unneeded, undecided)`` source records, each sorted by name, context, unit.

    ``unneeded``: flows with a decision that needs none (they agree with us, change no
    score, or have no usable link); a ``theirs`` decision the packages already carry
    is not one of them. ``undecided``: flows whose link would change a score and that
    no decision covers, so a new upstream case is seen.
    """
    unneeded: list[Record] = []
    undecided: list[Record] = []
    for source in sorted(state.universe, key=_order):
        decided = source["code"] in state.decisions
        if needs_decision(source, state):
            if not decided:
                undecided.append(source)
        elif decided and not _already_taken(source, state):
            unneeded.append(source)
    return unneeded, undecided


def _check_decisions(decisions: Mapping[str, FlowDecision], universe: tuple[Record, ...]) -> None:
    """Refuse a decision whose flow is gone from coverage or is no longer the flow it
    was made for: a stale decision must fail, not apply to something else."""
    by_code = {source["code"]: source for source in universe}
    for code, reviewed in decisions.items():
        source = by_code.get(code)
        if source is None:
            raise ValidationError(f"decision on {code} ({reviewed.name!r}): not in coverage")
        theirs = (reviewed.name, list(reviewed.context), reviewed.unit)
        ours = (source.get("name"), list(source.get("context") or []), source.get("unit"))
        if theirs != ours:
            raise ValidationError(
                f"decision on {code} differs from its coverage record: {theirs!r} != {ours!r}"
            )


def _mapped(
    packages: Mapping[str, dict], universe: tuple[Record, ...]
) -> dict[str, tuple[str, Record]]:
    """``{source code: (package, entry)}`` over packages 1 to 4.

    The three sources walk different things (package 5 and the review the coverage
    universe, the alignment the package entries), so they report the same decisions
    only if every entry is one coverage row, once. Anything else is refused here.
    """
    by_code = {source["code"]: source for source in universe}
    mapped: dict[str, tuple[str, Record]] = {}
    for package, payload in packages.items():
        for entry in entries_of(payload):
            source = entry["source"]
            code = source.get("code")
            if code in mapped:
                raise ValidationError(
                    f"source {code} ({source.get('name')!r}) is mapped by more than one "
                    f"package: {mapped[code][0]} and {package}"
                )
            if code not in by_code:
                raise ValidationError(
                    f"{package}: source {code} ({source.get('name')!r}) is not in coverage"
                )
            if source != by_code[code]:
                raise ValidationError(
                    f"{package}: source {code} differs from its coverage record: "
                    f"{source!r} != {by_code[code]!r}"
                )
            mapped[code] = (package, entry)
    return mapped


class BafuBrightwayFlowsBase(Source):
    """Fetch and parse every input once into a :class:`State`."""

    #: ``None`` in tests, which feed a synthetic file.
    expected_sha256: str | None = PINNED_SHA256
    #: The reviewed decisions; ``None`` in tests that want none.
    decisions_path: Path | None = DECISIONS_PATH

    def fetch(self, ctx: RunContext) -> RawData:
        """Fetch every named input except ``ef_vocab`` (a directory, read in ``parse``)."""
        self.inputs = {
            name: fetch_mod.fetch(url, ctx)
            for name, url in self.config.inputs.items()
            if name != _VOCAB_DIR
        }
        return fetch_mod.fetch(self.config.fetch_url, ctx)

    def parse(self, raw: RawData) -> Records:
        missing = [name for name in _REQUIRED if name not in self.inputs]
        if missing:
            raise RuntimeError(
                f"inputs {missing} not fetched: the registry block must declare "
                f"{list(_REQUIRED)}"
            )
        bw = load(raw.content, self.expected_sha256)
        vocab_dir = fetch_mod.local_path(self.config.inputs.get(_VOCAB_DIR), _VOCAB_DIR)
        cf_bytes = self.inputs[_EF_CFS].content
        packages = {
            package: orjson.loads(self.inputs[name].content)
            for package, name in PACKAGE_INPUTS.items()
        }
        coverage = orjson.loads(self.inputs[_COVERAGE].content)["coverage"]
        universe = tuple(row["source"] for row in coverage)
        decisions = load_decisions(self.decisions_path) if self.decisions_path else {}
        _check_decisions(decisions, universe)
        state = State(
            bw=bw,
            by_id={item.source_id: item for item in bw.links[LIST]},
            universe=universe,
            packages=packages,
            # the one index that reads ef-context notations: the flows of the
            # Brightway Flows 1.1 part of the ef-3.1 release are reachable through
            # their links here, and stay out of our own name matcher's index
            index=EfFlowIndex.from_bytes(
                cf_bytes, vocab_dir, include_uncharacterised=True, read_ef_context=True
            ),
            vectors=factor_vectors(cf_bytes),
            vocab_codes=vocab_flow_codes(vocab_dir),
            defects=load_label_defects(),
            mapped=_mapped(packages, universe),
            decisions=decisions,
        )
        return [{"state": state}]
