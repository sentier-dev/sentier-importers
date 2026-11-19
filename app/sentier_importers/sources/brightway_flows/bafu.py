"""Brightway Flows links for the ``bafu-2026-v1 -> ef-3.1`` pair: shared logic.

Three sources build on this module (``bafu_sources``): the fifth package of the pair,
the alignment of packages 1 to 4, and the review sidecar. They share one parsed
:class:`State` and the same decisions, so the sidecar always reports exactly what the
two emitting sources did.

Whether a link is usable, what unit and conversion it gives, and what "changes no
score" means (:func:`links.scores`) are decided in ``links``, which the Agribalyse
pair shares; this module finds the link of a BAFU flow and holds what only this pair
does: the alignment of packages 1 to 4 and the reviewed decisions.

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
from sentier_importers.sources.brightway_flows import links
from sentier_importers.sources.brightway_flows.correspondences import (
    PINNED_SHA256,
    RELEASE,
    BrightwayFlows,
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
from sentier_importers.sources.brightway_flows.links import (
    ALL_ZERO,
    UNCERTAIN_CONTEXT,
    UNCHARACTERISED,
    Decision,
    link_entry,
    no_score_note,
    order,
    rescaled_note,
    scores,
    their_unit,
)
from sentier_importers.sources.randonneur_data.ecoinvent_ef_biosphere import vocab_flow_codes

#: The unit rule, under the name it had while it lived here.
_conversion = links.conversion
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
ALIGNED = f"target aligned with {RELEASE} (identical factors)"
ALIGNED_FACTORLESS = f"target aligned with {RELEASE} (neither flow carries a factor)"
ALIGNED_UNIT = f"target and unit aligned with {RELEASE} (neither flow carries a factor)"
#: What the nomenclature package says of a factorless target's unit
#: (``mappings_biosphere_matched.entry_for``).
_CONVENTION_UNIT = (
    "target unit is the EF convention for this dimension "
    "(EF states no reference unit for this flow)"
)
#: Sentences of an entry's comment that describe a target without factors. They turn
#: false when a reviewed decision moves the entry onto a target that scores.
_NO_SCORE_SENTENCES = frozenset({UNCHARACTERISED, ALL_ZERO, _CONVENTION_UNIT})


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


def entries_of(package: Mapping) -> Iterator[Record]:
    if package.get("update"):
        raise ValueError(f"package {package.get('name')!r} has update entries: not supported")
    yield from package.get("replace", [])


def _taken(source: Mapping, state: State) -> FlowDecision | None:
    """The reviewed decision on ``source`` when it is to take their target."""
    reviewed = state.decisions.get(source.get("code"))
    return reviewed if reviewed is not None and reviewed.decision == THEIRS else None


def decide(source: Mapping, state: State) -> Decision:
    """Whether Brightway Flows gives ``source`` a usable EF target, and if not, why."""
    return links.decide(source, bafu_link(source, state.by_id), state)


def package_entry(source: Record, state: State) -> tuple[Record | None, str | None]:
    """The package-5 entry for an unmapped ``source``, or ``(None, reason)``."""
    decision = decide(source, state)
    if decision.reason is not None:
        return None, decision.reason
    unit, factor, reason = their_unit(source, decision, state)
    if reason is not None:
        return None, reason
    scoring = scores(decision.target.code, state)
    reviewed = _taken(source, state) if scoring else None
    if scoring and reviewed is None:
        return None, "characterised_held_for_review"
    entry = link_entry(source, decision, unit, factor, state)
    if reviewed is not None:
        entry = {**entry, "comment": f"{entry['comment']}; reviewed {REVIEWED}: {reviewed.reason}"}
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
    unit, factor, reason = their_unit(entry["source"], decision, state)
    if reason is not None:
        return None
    target = {**_their_target(entry, flow), "unit": unit}
    comments = [no_score_note(flow.code, state), ALIGNED_UNIT]
    if factor is not None:
        comments.append(rescaled_note(factor, decision.link))
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
        _, _, reason = their_unit(source, decision, state)
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
    for source in sorted(state.universe, key=order):
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
