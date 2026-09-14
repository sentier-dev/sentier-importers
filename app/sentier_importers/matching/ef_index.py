"""EF 3.1 flow index built from public inputs only.

- ``characterization-factors.parquet`` (sentier-methods): which EF flows carry a
  factor, their context path, and their CF vector by ``method_id``. A flow that is
  not in this table has no factor and is not a mapping target.
- ``elementary-flows/*.parquet`` (sentier-vocab, rows with the ef-3.1 source):
  preferred label, synonyms (``alt_labels``) and CAS number, keyed by the flow IRI.

The CF vector keeps the global (location-less) factor per method; location-specific
rows (land use, water use, some regionalised categories) are not part of flow identity.

An EF vocab row that carries no factor at all (``characterised=False``) can still be
placed and indexed, opt-in via ``include_uncharacterised``: its context comes not from
the CF table (it has none) but from its ``additional_notations`` ``bw-context:<code>``
entry, resolved through the ``matching.bw_context`` crosswalk. A code with no EF leaf
(``envi-biot``) or a row with no ``bw-context`` notation at all is skipped, not indexed.

Round 6, decision 2026-09-14 (first cut): sentier-vocab occasionally ships an EF flow
whose factors are correct but whose ``pref_label`` names the wrong substance (a real
EF flow labelled "sodium" is CAS 1120-01-0, sodium hexadecyl sulphate, a surfactant,
not the inorganic sodium ion). The curated ``label_defects.yaml`` table
(``load_label_defects``) lists every known such defect by CAS.

Round 6, decision 2026-09-14 (second cut, superseded by the third below for how a
characterised flow is actually named, but not for *why*): a sentier-vocab
``pref_label`` is itself a derived label and is not authoritative; the CF table's own
``flow_name`` column is the JRC name and is authoritative for any flow it
characterises.

Round 6, decision 2026-09-14 (third cut): ``EfFlow`` splits the matching key from the
display name so a curated defect entry or a same-bucket collision never has to touch
one to fix the other. ``name`` is always the CF table's own JRC spelling for a
characterised flow -- grouped CASE-INSENSITIVELY across that flow's own CF-table rows
(``_pick_jrc_name`` picks the winning spelling by total row count, ties broken
alphabetically on the lowercased spelling, then picks a literal casing to display:
the vocab pref_label's own casing when it matches case-insensitively, else a
non-all-lowercase variant, else the alphabetically first; ``EfFlowIndex.
multi_name_codes`` counts codes whose rows disagree on more than just case) -- never
the vocab ``pref_label``. ``label`` (what an emitted entry actually shows, e.g.
``mappings_biosphere_matched.entry_for``) is the vocab pref_label when it differs
from ``name`` in more than casing and was kept as a synonym; otherwise (the label is
absent, matches ``name`` already, is listed in ``label_defects.yaml`` for that CAS, or
collides case-insensitively with the name of a DIFFERENT flow -- characterised or
not -- sharing the same bucket, the same grouping ``by_name``/``by_synonym`` use, so
this collision check is not fooled by two colliding flows sitting on different
leaves) ``label`` falls back to ``name`` and the vocab label is dropped instead of
added as a synonym (``EfFlowIndex.suppressed_vocab_synonyms`` counts these drops) --
so the synonym tier can never recreate a collision either construction resolves. An
*uncharacterised* flow (opt-in via ``include_uncharacterised``) carries no CF row at
all, so both ``name`` and ``label`` come from the vocab pref_label, still passed
through ``label_defects.yaml`` exactly as before (see ``load_label_defects``);
``EfFlowIndex.relabelled_count`` counts only these.
"""

from __future__ import annotations

import io
import re
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path

import pyarrow.parquet as pq
import yaml
from sentier_importers.core.errors import ParseError
from sentier_importers.matching.bw_context import context_for
from sentier_importers.matching.compartments import bucket_of_ef_context, leaf_of

EF_SOURCE = "https://vocab.sentier.dev/sources/ef-3.1"
_CF_COLUMNS = ["method_id", "flow", "flow_name", "factor_value", "flow_context", "location"]
_VOCAB_COLUMNS = [
    "iri",
    "pref_label",
    "alt_labels",
    "cas_number",
    "source",
    "additional_notations",
]
_SIGNIFICANT_DIGITS = 12

#: EF 3.1 / ILCD reference-unit convention, keyed by the one method that fixes a
#: flow's unit; checked in this order because a flow may carry other methods too
#: (e.g. a fossil resource also has a climate-change factor, still reported in MJ).
_MJ_METHOD = "ef-3.1:resource-use-fossils"
_KBQ_METHOD = "ef-3.1:ionising-radiation-human-health"
_M3_METHOD = "ef-3.1:water-use"
_LAND_METHOD = "ef-3.1:land-use"

#: An uncharacterised vocab row's context comes only from the ``bw-context`` crosswalk
#: (``matching.bw_context.BW_CONTEXT_PATH``), and that crosswalk has no code at all for
#: EF's ``Non-renewable energy resources from ground`` leaf or for any renewable-energy
#: resource branch (its resource codes only ever reach the element/material leaves --
#: ``reso-grou``'s own leaf is literally "...element resources...", see
#: ``bw_context.py``). So a BAFU/EF resource flow that is actually an ENERGY resource by
#: name (an ecoinvent-style "Energy, <form>, converted", a "Primary Energy ..." label,
#: an oil-sand or pit-methane flow) can never land on the right branch through this
#: crosswalk, no matter which code placed it -- it is always on the wrong (element or
#: material) leaf. Such a row is marked ``context_uncertain`` unconditionally, not just
#: when its code happens to be one of ``bw_context.AMBIGUOUS_CODES``; also imported by
#: ``mappings_biosphere_matched.BafuEfMatchedSource.entry_for`` (decision (f)(2),
#: 2026-09-13), which -- rather than withhold a nomenclature-package match onto one of
#: these -- omits ``target["context"]`` entirely from the emitted entry and discloses
#: that the EF context is not recoverable from the source context code in the comment
#: instead.
UNCERTAIN_RESOURCE_NAME = re.compile(r"^(Energy|Primary Energy|Oil Sand|Pit Methane)\b", re.I)


def normalise_cas(cas: str | None) -> str | None:
    """``007440-50-8`` -> ``7440-50-8``; blank -> None."""
    if cas is None:
        return None
    text = str(cas).strip()
    if not text:
        return None
    return re.sub(r"^0+(?=\d)", "", text)


LABEL_DEFECTS_PATH = Path(__file__).with_name("label_defects.yaml")
_LABEL_DEFECT_FIELDS = {"cas", "wrong_label", "true_name", "note"}


@dataclass(frozen=True)
class LabelDefect:
    """A known sentier-vocab ``pref_label`` defect on one EF flow, keyed by CAS.

    ``wrong_label`` is the incorrect ``pref_label`` an EF flow with this CAS
    currently carries; ``true_name`` is the label it must be corrected to;
    ``note`` records why (see ``label_defects.yaml``).
    """

    wrong_label: str
    true_name: str
    note: str


def load_label_defects(path: Path = LABEL_DEFECTS_PATH) -> dict[str, LabelDefect]:
    """Load the curated label-defect table, keyed by normalised CAS.

    Each entry is a ``{cas, wrong_label, true_name, note}`` mapping. Raises
    ``ParseError``, naming ``path``, when the top-level ``label_defects`` key is
    missing, ``None``, or not a list, or when an entry is not a mapping with exactly
    those four keys, all non-empty strings.
    """
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    entries = data.get("label_defects") if isinstance(data, dict) else None
    if not isinstance(entries, list):
        raise ParseError(f"{path}: missing or malformed top-level 'label_defects' list")
    defects: dict[str, LabelDefect] = {}
    for entry in entries:
        if not isinstance(entry, dict) or set(entry) != _LABEL_DEFECT_FIELDS:
            fields = sorted(_LABEL_DEFECT_FIELDS)
            raise ParseError(f"{path}: label defect {entry!r} must have exactly {fields!r}")
        values = {k: entry[k] for k in _LABEL_DEFECT_FIELDS}
        if not all(isinstance(v, str) and v.strip() for v in values.values()):
            raise ParseError(f"{path}: label defect {entry!r} has a blank or non-string field")
        cas = normalise_cas(values["cas"])
        defects[cas] = LabelDefect(
            wrong_label=values["wrong_label"].strip(),
            true_name=values["true_name"].strip(),
            note=values["note"].strip(),
        )
    return defects


def _relabel(
    name: str, cas: str | None, synonyms: tuple[str, ...], defects: Mapping[str, LabelDefect]
) -> tuple[str, tuple[str, ...], bool]:
    """Apply a matching ``LabelDefect`` to one flow's ``name``/``synonyms``, if any.

    Returns ``(name, synonyms, True)`` when ``cas`` is listed in ``defects`` and
    ``name`` equals that defect's ``wrong_label`` (case/whitespace-insensitive):
    ``name`` becomes the defect's ``true_name``, and the wrong label is dropped from
    ``synonyms`` (case-insensitively) so it can never resurface via the synonym tier.
    Returns the inputs unchanged, with ``False``, otherwise.
    """
    if cas is None:
        return name, synonyms, False
    defect = defects.get(cas)
    if defect is None or name.strip().lower() != defect.wrong_label.strip().lower():
        return name, synonyms, False
    wrong = defect.wrong_label.strip().lower()
    return (
        defect.true_name,
        tuple(s for s in synonyms if s.strip().lower() != wrong),
        True,
    )


def _pick_jrc_name(groups: Mapping[str, Mapping[str, int]], vocab_label: str) -> str:
    """Choose one characterised flow's JRC name from its own CF-table rows.

    ``groups`` maps each distinct ``flow_name`` spelling seen across this flow's own
    CF-table rows, grouped CASE-INSENSITIVELY (lowercased spelling -> {actual
    spelling: row count}) -- almost always a single group of one spelling,
    occasionally more groups when JRC's own data disagrees with itself on more than
    just case (round 6, decision 2026-09-14, third cut: ``EfFlowIndex.multi_name_codes``
    counts codes with more than one such group).

    The winning group is the one with the most total rows across all its case
    variants, ties broken by the lowercased spelling itself (alphabetically first).
    Within that group, the literal casing to display is then chosen deliberately,
    never by row count (a preference between casings, not a tie-break for content):
    the vocab pref_label's own casing, when it matches this group case-insensitively
    (JRC and vocab already agree in substance, so the nicer of the two spellings
    wins with no risk -- every comparison anywhere in this module is
    case-insensitive, so this changes only what is displayed); failing that, a
    variant that is not all-lowercase (JRC's raw data is overwhelmingly all-lowercase,
    so any variant that escaped that convention is more likely to be a deliberate
    acronym or proper-noun spelling); failing that, the alphabetically first variant.
    """
    totals = {lower: sum(variants.values()) for lower, variants in groups.items()}
    top = max(totals.values())
    winning_lower = sorted(lower for lower, total in totals.items() if total == top)[0]
    variants = groups[winning_lower]
    if vocab_label and vocab_label.strip().lower() == winning_lower:
        return vocab_label.strip()
    non_lower = sorted(v for v in variants if v != v.lower())
    if non_lower:
        return non_lower[0]
    return sorted(variants)[0]


def _code(iri: str) -> str:
    return str(iri).rsplit("/", 1)[-1]


@dataclass(frozen=True)
class EfFlow:
    code: str
    #: The matching key: matchers, ``by_name``/``by_synonym``/``by_cas`` and every
    #: identity/collision check key off this. For a characterised flow this is the
    #: CF table's own (JRC) name, never the vocab pref_label (round 6, decision
    #: 2026-09-14, third cut); for an uncharacterised flow (no CF row) it is the
    #: vocab pref_label, same as ``label`` below.
    name: str
    context: tuple[str, ...]
    synonyms: tuple[str, ...] = ()
    cas: str | None = None
    #: ``False`` for a flow indexed only via ``include_uncharacterised`` (no CF
    #: vector at all -- ``vector``/``identity`` are ``{}``/``()`` and
    #: ``reference_unit`` is ``None`` for it).
    characterised: bool = True
    #: ``True`` when this flow's context was resolved through one of
    #: ``bw_context.AMBIGUOUS_CODES`` -- the majority leaf was used, but a real
    #: minority of flows under that code sit elsewhere. Always ``False`` for a
    #: characterised flow.
    context_uncertain: bool = False
    #: The DISPLAY name: what an emitted entry actually shows a human (``entry_for``
    #: reads this, never ``name``, for its ``target["name"]``). For a characterised
    #: flow this is the vocab pref_label when it was kept as a synonym (round 6,
    #: decision 2026-09-14, third cut: ``EfFlowIndex.from_tables`` decouples the
    #: matching key from the display name so a curated defect or a same-bucket
    #: collision never has to touch matching at all to fix display, or vice versa);
    #: when the vocab label was instead suppressed (defect-listed or colliding), or
    #: for an uncharacterised flow (no vocab/JRC split to make), this defaults to
    #: ``name`` -- an empty/omitted ``label`` at construction always falls back to
    #: ``name`` (``__post_init__``), so every existing caller that builds an
    #: ``EfFlow`` without naming ``label`` explicitly keeps displaying ``name``,
    #: exactly as before this field existed.
    label: str = ""

    def __post_init__(self) -> None:
        if not self.label:
            object.__setattr__(self, "label", self.name)

    @property
    def context_path(self) -> str:
        return " / ".join(self.context)

    @property
    def leaf(self) -> str:
        return leaf_of(self.context_path)

    @property
    def bucket(self) -> str | None:
        return bucket_of_ef_context(self.context_path)


class EfFlowIndex:
    """Lookup index over EF 3.1 flows: by default only those that carry at least one
    characterization factor, plus uncharacterised ones too when built with
    ``include_uncharacterised=True`` (see ``from_tables``).

    Every lookup's ``bucket`` argument must be a value produced by
    ``compartments.bucket_of_bafu_category`` or ``compartments.bucket_of_ef_context``
    (``"resource"``, not ``"resources"``); a bucket these functions would not produce
    simply matches nothing (``[]``), it is never an error. ``by_name``, ``by_synonym``
    and ``by_cas`` each return a fresh, code-sorted list; ``vector`` returns a fresh
    dict; ``identity`` returns a tuple sorted by ``method_id``. Callers may hold onto
    or mutate any of these results without affecting the index.

    ``by_name_any_bucket`` is the one un-bucketed lookup: every flow whose label
    matches, across every bucket at once, code-sorted like the rest. Round 5, decision
    2026-09-14: it exists only for the nomenclature package's name-only alignment
    (``mappings_biosphere_matched._name_only_match``), which deliberately looks past
    the bucket a source flow's own category would restrict it to.
    """

    def __init__(
        self,
        flows: Iterable[EfFlow],
        vectors: dict[str, dict[str, float]],
        *,
        includes_uncharacterised: bool = False,
        relabelled_count: int = 0,
        multi_name_codes: int = 0,
        suppressed_vocab_synonyms: int = 0,
    ) -> None:
        #: Mirrors the ``include_uncharacterised`` flag this index was built with (see
        #: ``from_tables``) -- not whether any uncharacterised flow actually ended up
        #: indexed, just what the index was asked to include. ``MatchPipeline`` reads
        #: this to phrase a ``no_ef_flow`` detail honestly: "with a factor" only makes
        #: sense to say when the search really was restricted to factor-bearing flows.
        self.includes_uncharacterised = includes_uncharacterised
        #: How many *uncharacterised* flows ``from_tables`` renamed via
        #: ``load_label_defects`` (round 6, decision 2026-09-14). A characterised
        #: flow's name always comes from the CF table now, never from a defect entry,
        #: so this only ever counts uncharacterised hits (see the module docstring's
        #: second cut). ``0`` for an index built directly from pre-built ``EfFlow``
        #: objects (this constructor never relabels anything itself).
        self.relabelled_count = relabelled_count
        #: How many characterised flow codes carried more than one case-insensitively
        #: distinct ``flow_name`` spelling across their own CF-table rows (round 6,
        #: decision 2026-09-14); the spelling is chosen by ``_pick_jrc_name``. Zero on
        #: the real EF 3.1 table.
        self.multi_name_codes = multi_name_codes
        #: How many characterised flows had their vocab ``pref_label`` dropped
        #: instead of added as a synonym (round 6, decision 2026-09-14): either
        #: ``label_defects.yaml`` lists it for that CAS, or it collides
        #: case-insensitively with the name of a different flow -- characterised or
        #: not -- sharing the same bucket (the same grouping ``by_name``/
        #: ``by_synonym`` themselves use).
        self.suppressed_vocab_synonyms = suppressed_vocab_synonyms
        self._flows: dict[str, EfFlow] = {}
        self._vectors: dict[str, dict[str, float]] = {code: dict(v) for code, v in vectors.items()}
        by_name: dict[tuple[str, str | None], list[EfFlow]] = {}
        by_name_any_bucket: dict[str, list[EfFlow]] = {}
        by_synonym: dict[tuple[str, str | None], list[EfFlow]] = {}
        by_cas: dict[tuple[str, str | None], list[EfFlow]] = {}
        for flow in flows:
            self._flows[flow.code] = flow
            bucket = flow.bucket
            name_key = flow.name.strip().lower()
            by_name.setdefault((name_key, bucket), []).append(flow)
            by_name_any_bucket.setdefault(name_key, []).append(flow)
            for synonym in flow.synonyms:
                by_synonym.setdefault((synonym.strip().lower(), bucket), []).append(flow)
            if flow.cas is not None:
                by_cas.setdefault((flow.cas, bucket), []).append(flow)

        def _sorted(
            index: dict[tuple[str, str | None], list[EfFlow]],
        ) -> dict[tuple[str, str | None], list[EfFlow]]:
            return {key: sorted(items, key=lambda f: f.code) for key, items in index.items()}

        self._by_name = _sorted(by_name)
        self._by_name_any_bucket = {
            key: sorted(items, key=lambda f: f.code) for key, items in by_name_any_bucket.items()
        }
        self._by_synonym = _sorted(by_synonym)
        self._by_cas = _sorted(by_cas)
        self._sorted_flows: tuple[EfFlow, ...] = tuple(
            sorted(self._flows.values(), key=lambda f: f.code)
        )

    @classmethod
    def from_tables(
        cls,
        cf_rows: Iterable[dict],
        vocab_rows: Iterable[dict],
        *,
        include_uncharacterised: bool = False,
        label_defects: Mapping[str, LabelDefect] | None = None,
    ) -> "EfFlowIndex":
        """Build an index from CF table rows and vocab shard rows already loaded in memory.

        When ``include_uncharacterised`` is ``True``, every EF vocab row whose code
        carries no CF-table context is also indexed (``EfFlow.characterised=False``),
        placed via ``bw_context.context_for`` on its ``additional_notations``. A row
        whose ``bw-context`` code has no EF leaf, or that carries no ``bw-context``
        notation at all, is skipped. Default ``False`` reproduces the exact previous
        behaviour (characterised flows only).

        A characterised flow's ``name`` (the matching key) is always the CF table's
        own JRC spelling (round 6, decision 2026-09-14, third cut -- see the module
        docstring), grouped case-insensitively across that flow's own CF-table rows
        (``EfFlowIndex.multi_name_codes`` counts codes whose rows disagree on more
        than just case; ``_pick_jrc_name`` picks both the winning spelling and its
        displayed casing). The vocab ``pref_label``, when its CONTENT differs from
        the JRC name (not just its casing), becomes ``EfFlow.label`` (what an emitted
        entry actually shows, see ``EfFlow.label``) and an extra synonym, UNLESS
        ``label_defects`` (the curated CAS -> wrong pref_label -> true name table,
        ``load_label_defects``) lists it for that CAS, or it collides
        (case-insensitively) with the name of a different flow -- characterised or
        not -- sharing the same bucket (the same grouping ``by_name``/``by_synonym``
        themselves use, so "the synonym tier can never recreate the collision" holds
        by construction, not just for same-leaf collisions). Either way it is
        dropped, not added, ``label`` falls back to the JRC name too, and
        ``EfFlowIndex.suppressed_vocab_synonyms`` counts how many times this fires.

        An *uncharacterised* flow carries no CF row, so both its matching key
        (``name``) and its display (``label``, defaulting to ``name``) come from the
        vocab pref_label, passed through ``label_defects`` exactly as before --
        ``EfFlowIndex.relabelled_count`` counts only these.

        ``label_defects`` defaults to ``None``, which loads the shipped
        ``label_defects.yaml`` (pass ``{}`` to disable it entirely, e.g. in a test
        that wants the raw vocab label to survive as a synonym unconditionally).
        """
        if label_defects is None:
            label_defects = load_label_defects()
        labels = {_code(r["iri"]): r for r in vocab_rows if (r.get("source") or "") == EF_SOURCE}
        contexts: dict[str, str] = {}
        #: code -> lowercased spelling -> {actual spelling: row count}
        jrc_name_groups: dict[str, dict[str, dict[str, int]]] = {}
        vectors: dict[str, dict[str, float]] = {}
        for r in cf_rows:
            try:
                code = _code(r["flow"])
                context = str(r.get("flow_context") or "")
                if context:
                    # the CF table carries one context and one location-less factor
                    # per (flow, method); the first context seen for a flow wins.
                    contexts.setdefault(code, context)
                flow_name = str(r.get("flow_name") or "").strip()
                if flow_name:
                    group = jrc_name_groups.setdefault(code, {}).setdefault(flow_name.lower(), {})
                    group[flow_name] = group.get(flow_name, 0) + 1
                if r.get("location"):
                    continue  # location-specific factor: not part of flow identity
                # factors are rounded so CF vectors compare equal across the parquet
                # round trip; this rounding is what CF-identity disambiguation relies on
                vectors.setdefault(code, {})[r["method_id"]] = float(
                    "%.*g" % (_SIGNIFICANT_DIGITS, float(r["factor_value"]))
                )
            except (KeyError, TypeError, ValueError) as exc:
                raise ParseError(f"CF row for flow {r.get('flow')!r}: {exc}") from exc
        # case-insensitively distinct spellings only (a code whose rows disagree
        # solely on case is not "multi-name": _pick_jrc_name resolves that silently)
        multi_name_codes = sum(1 for groups in jrc_name_groups.values() if len(groups) > 1)

        # pass 1: every flow's identity -- characterised (from the CF table) and, if
        # requested, uncharacterised (from the bw-context crosswalk) -- gathered
        # before any vocab-label synonym decision, so that decision can see every
        # OTHER flow the final index will hold, keyed by (bucket, lowercased name)
        # exactly as EfFlowIndex.__init__ itself keys by_name/by_synonym.
        core: dict[str, dict] = {}
        for code, context in contexts.items():
            label_row = labels.get(code) or {}
            vocab_label = (label_row.get("pref_label") or "").strip()
            groups = jrc_name_groups.get(code)
            name = _pick_jrc_name(groups, vocab_label) if groups else vocab_label
            context_tuple = tuple(p.strip() for p in context.split("/") if p.strip())
            core[code] = {
                "name": name,
                "vocab_label": vocab_label,
                "cas": normalise_cas(label_row.get("cas_number")),
                "alt": tuple(str(s) for s in (label_row.get("alt_labels") or [])),
                "context": context_tuple,
                "bucket": bucket_of_ef_context(" / ".join(context_tuple)),
                "characterised": True,
            }
        relabelled = 0
        if include_uncharacterised:
            for code, row in labels.items():
                if code in contexts:
                    continue  # already indexed as a characterised flow
                path, uncertain = context_for(row.get("additional_notations") or [])
                if path is None:
                    continue  # no EF leaf for this code, or no bw-context notation at all
                name = (row.get("pref_label") or "").strip()
                cas = normalise_cas(row.get("cas_number"))
                alt = tuple(str(s) for s in (row.get("alt_labels") or []))
                name, alt, hit = _relabel(name, cas, alt, label_defects)
                relabelled += hit
                context_tuple = tuple(p.strip() for p in path.split("/") if p.strip())
                bucket = bucket_of_ef_context(" / ".join(context_tuple))
                if bucket == "resource" and UNCERTAIN_RESOURCE_NAME.match(name):
                    # the crosswalk cannot reach an energy resource leaf at all (see
                    # UNCERTAIN_RESOURCE_NAME) -- this placement is wrong regardless
                    # of which code produced it.
                    uncertain = True
                core[code] = {
                    "name": name,
                    "vocab_label": "",  # no separate JRC alternative to weigh against
                    "cas": cas,
                    "alt": alt,
                    "context": context_tuple,
                    "bucket": bucket,
                    "characterised": False,
                    "context_uncertain": uncertain,
                }

        # pass 2: (bucket, lowercased name) -> every code the FINAL index would find
        # there via by_name -- built from every flow pass 1 gathered, characterised
        # or not, since that is exactly what by_name/by_synonym themselves see.
        bucket_name_index: dict[tuple[str | None, str], set[str]] = {}
        for code, v in core.items():
            bucket_name_index.setdefault((v["bucket"], v["name"].strip().lower()), set()).add(code)

        suppressed_vocab_synonyms = 0
        flows = []
        for code, v in core.items():
            name = v["name"]
            synonyms = v["alt"]
            if not v["characterised"]:
                flows.append(
                    EfFlow(
                        code=code,
                        name=name,
                        context=v["context"],
                        synonyms=synonyms,
                        cas=v["cas"],
                        characterised=False,
                        context_uncertain=v["context_uncertain"],
                    )
                )
                continue
            vocab_label = v["vocab_label"]
            cas = v["cas"]
            label = name
            defect = label_defects.get(cas) if cas is not None else None
            if defect is not None:
                wrong = defect.wrong_label.strip().lower()
                synonyms = tuple(s for s in synonyms if s.strip().lower() != wrong)
            if vocab_label and vocab_label.strip().lower() != name.strip().lower():
                is_defect = defect is not None and vocab_label.strip().lower() == (
                    defect.wrong_label.strip().lower()
                )
                collides = bool(
                    bucket_name_index.get((v["bucket"], vocab_label.strip().lower()), set())
                    - {code}
                )
                if is_defect or collides:
                    suppressed_vocab_synonyms += 1
                else:
                    synonyms = synonyms + (vocab_label,)
                    label = vocab_label
            flows.append(
                EfFlow(
                    code=code,
                    name=name,
                    context=v["context"],
                    synonyms=synonyms,
                    cas=cas,
                    label=label,
                )
            )
        # a characterised flow only exists once it has a context; drop any vector
        # accumulated for a context-less CF row so it cannot outlive the flow it
        # would have belonged to
        return cls(
            flows,
            {code: v for code, v in vectors.items() if code in contexts},
            includes_uncharacterised=include_uncharacterised,
            relabelled_count=relabelled,
            multi_name_codes=multi_name_codes,
            suppressed_vocab_synonyms=suppressed_vocab_synonyms,
        )

    @classmethod
    def from_files(
        cls,
        cf_parquet: Path,
        vocab_dir: Path,
        *,
        include_uncharacterised: bool = False,
        label_defects: Mapping[str, LabelDefect] | None = None,
    ) -> "EfFlowIndex":
        """Build an index by reading the CF parquet file and every vocab shard parquet file."""
        cf_rows = pq.read_table(cf_parquet, columns=_CF_COLUMNS).to_pylist()
        return cls.from_tables(
            cf_rows,
            cls._read_vocab_dir(vocab_dir),
            include_uncharacterised=include_uncharacterised,
            label_defects=label_defects,
        )

    @classmethod
    def from_bytes(
        cls,
        cf_parquet_bytes: bytes,
        vocab_dir: Path,
        *,
        include_uncharacterised: bool = False,
        label_defects: Mapping[str, LabelDefect] | None = None,
    ) -> "EfFlowIndex":
        """Build an index from the CF parquet file's raw bytes (e.g. already fetched
        through the content-addressed cache, so the cache/offline contract holds) and
        every vocab shard parquet file read from ``vocab_dir`` on disk.
        """
        cf_rows = pq.read_table(io.BytesIO(cf_parquet_bytes), columns=_CF_COLUMNS).to_pylist()
        return cls.from_tables(
            cf_rows,
            cls._read_vocab_dir(vocab_dir),
            include_uncharacterised=include_uncharacterised,
            label_defects=label_defects,
        )

    @staticmethod
    def _read_vocab_dir(vocab_dir: Path) -> list[dict]:
        vocab_rows = []
        for path in sorted(Path(vocab_dir).glob("*.parquet")):
            vocab_rows += pq.read_table(path, columns=_VOCAB_COLUMNS).to_pylist()
        return vocab_rows

    def get(self, code: str) -> EfFlow | None:
        """Return the flow for ``code``, or ``None`` if it is not indexed."""
        return self._flows.get(code)

    def vector(self, code: str) -> dict[str, float]:
        """Return a copy of the CF vector (``method_id`` -> factor) for ``code``, or ``{}``."""
        return dict(self._vectors.get(code, {}))

    def identity(self, code: str) -> tuple[tuple[str, float], ...]:
        """Hashable CF-identity key: the vector's ``(method_id, factor)`` pairs, sorted."""
        return tuple(sorted(self._vectors.get(code, {}).items()))

    def reference_unit(self, code: str) -> str | None:
        """The EF 3.1 reference unit for ``code``, inferred from method membership.

        Returns ``None`` when ``code`` is an uncharacterised flow
        (``EfFlow.characterised=False``): it carries no CF vector at all, so there is
        no method membership to infer a unit from. An unknown ``code`` (not indexed
        at all) is unaffected by this and still defaults to ``"kilogram"`` below, same
        as before.

        The CF table (``characterization-factors.parquet``) carries no flow-unit
        column at all -- only a ``factor_value`` per ``(flow, method)`` -- so a
        flow's physical unit is not data to look up but a convention to apply: EF
        3.1 / ILCD fixes one reference unit per impact-category family, and every
        flow that family characterises is reported in it. A resource-use-fossils
        flow is in megajoule, an ionising-radiation-human-health flow in kBq, a
        water-use flow in cubic meter, a land-use flow in m2 (or m2*a when its EF
        context leaf is ``land occupation``, not ``land transformation``); everything
        else -- the overwhelming majority, all substance emissions and non-energy/
        water/land resources -- is in kilogram.

        The land-use unit is keyed on the EF context *leaf*, not the flow name: EF
        occupation flows are named ``Arable``, ``Pasture/meadow`` and so on, never
        anything starting with "occupation" -- biosphere-1-curated's published payload uses
        ``m2*a`` for exactly the flows whose leaf is ``land occupation``.
        """
        flow = self.get(code)
        if flow is not None and not flow.characterised:
            return None
        methods = set(self.vector(code))
        if _MJ_METHOD in methods:
            return "megajoule"
        if _KBQ_METHOD in methods:
            return "kBq"
        if _M3_METHOD in methods:
            return "cubic meter"
        if _LAND_METHOD in methods:
            return "m2*a" if flow is not None and flow.leaf == "land occupation" else "m2"
        return "kilogram"

    def by_name(self, name: str, bucket: str | None) -> list[EfFlow]:
        """Flows in ``bucket`` whose label matches ``name`` (case- and whitespace-insensitive)."""
        return list(self._by_name.get((name.strip().lower(), bucket), []))

    def by_name_any_bucket(self, name: str) -> list[EfFlow]:
        """Every indexed flow whose label matches ``name`` (case/whitespace-insensitive),
        in ANY bucket, code-sorted.

        Unlike ``by_name``, not scoped to one bucket: used only by the nomenclature
        package's round-5 name-only alignment (``mappings_biosphere_matched.
        _name_only_match``), which recovers an EF namesake living in a different
        bucket than the source flow's own category would place it in (e.g. a resource
        extraction whose only EF namesake sits among soil-emission leaves).
        """
        return list(self._by_name_any_bucket.get(name.strip().lower(), []))

    def by_synonym(self, name: str, bucket: str | None) -> list[EfFlow]:
        """Flows in ``bucket`` whose synonym matches ``name`` (case/whitespace-insensitive)."""
        return list(self._by_synonym.get((name.strip().lower(), bucket), []))

    def by_cas(self, cas: str | None, bucket: str | None) -> list[EfFlow]:
        """Flows in ``bucket`` whose CAS matches ``cas``, normalised; ``[]`` for ``None``."""
        normalised = normalise_cas(cas)
        if normalised is None:
            return []
        return list(self._by_cas.get((normalised, bucket), []))

    def __len__(self) -> int:
        return len(self._flows)

    def __iter__(self) -> Iterator[EfFlow]:
        """Iterate all indexed flows, sorted by code."""
        return iter(self._sorted_flows)
