"""EF 3.1 flow index built from public inputs only.

- ``characterization-factors.parquet`` (sentier-methods): which EF flows carry a
  factor, their context path, and their CF vector by ``method_id``. A flow that is
  not in this table has no factor and is not a mapping target.
- ``terms/*.parquet`` (sentier-vocab, ``elementary-flow`` terms with the ef-3.1 source):
  preferred label, synonyms (``alt_labels``) and CAS number, keyed by the flow IRI.

The CF vector keeps the global (location-less) factor per method; location-specific
rows (land use, water use, some regionalised categories) are not part of flow identity.

An EF vocab row that carries no factor at all (``characterised=False``) can still be
placed and indexed, opt-in via ``include_uncharacterised``. Its context comes not
from the CF table (it has none) but from the vocab term itself, by the first of
three sources (2026-11-19):

1. its ``ef-context:<path>`` notation, the exact EF context path the Brightway Flows
   1.1 part of the ef-3.1 release writes on the flows it adds;
2. its ``sub_compartment`` attribute, the leaf of its exact EF context, which every
   flow term of the release states. The index needs the path: it is the one path
   that ends in that leaf among the contexts of the CF table, the ``ef-context``
   notations and ``EF_LEAF_PATH`` (the nine leaves neither shows). A leaf a
   factorless flow states that has no path, or two, is an error at index build
   listing the flows; so is a flow whose two exact sources name different leaves;
3. only for a term that has neither (an older vocab checkout, a test fixture): its
   ``bw-context:<code>`` notation through the ``matching.bw_context`` crosswalk,
   marked ``context_uncertain`` where that code is ambiguous or the name is an
   energy resource the crosswalk cannot place (``UNCERTAIN_RESOURCE_NAME``).

A placement by 1 or 2 is exact and never uncertain. A row none of the three places
(a code with no EF leaf such as ``envi-biot``, or no notation at all) is skipped.
The crosswalk had been the only source for a flow with a ``bw-context``: it put 30
targets of the nomenclature package on "water, unspecified" that EF keeps on the
long-term leaf, and every energy resource on a material or element leaf.

A characterised flow keeps its CF-table context. Where its vocab term states a
``sub_compartment`` the two must name the same leaf: a disagreement is an error at
index build, with the list (none on the real table of 2026-11-19).

There is no ``read_ef_context`` flag any more (it existed for a day, 2026-11-19, to
hide the Brightway Flows 1.1 flows from our own name matcher until it had a unit
gate and a provisional fallback): every source reads all terms the same way.

Units (2026-11-19, spec units-on-ef31-flows): the ef-3.1 release states the unit of
every flow term as a ``qudt:hasUnit`` relation. ``from_files``/``from_bytes`` read
those relations from the ``relations`` folder beside the ``terms`` folder they are
given (no registry input of its own; ``relations_dir`` names another folder), and
each ``EfFlow`` carries its ``stated_unit`` in the spelling of the mapping packages
(``sources.brightway_flows.units.UNIT_SPELLING``). A checkout without that folder,
or without such relations, states no unit and everything works as before.
``reference_unit`` (inference from method membership) stays as the cross-check: a
characterised flow whose stated unit differs from the inferred one is an error at
index build, with the list (none on the real table: 89,070 characterised flows
agree). ``EfFlowIndex.unit`` is the one accessor for "the unit of this EF flow":
inferred when characterised, stated otherwise.

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
import json
import re
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path

import pyarrow.parquet as pq
import yaml
from sentier_importers.core.errors import ParseError
from sentier_importers.matching.bw_context import context_for, ef_context_for
from sentier_importers.matching.compartments import bucket_of_ef_context, leaf_of
from sentier_importers.sources.brightway_flows.units import unit_spelling

EF_SOURCE = "https://vocab.sentier.dev/sources/ef-3.1"
_CF_COLUMNS = ["method_id", "flow", "flow_name", "factor_value", "flow_context", "location"]
#: Columns read from a sentier-vocab term file; ``cas_number`` and ``sub_compartment``
#: are keys of the JSON ``attributes`` column, lifted out of it by ``_read_vocab_dir``.
_VOCAB_COLUMNS = [
    "iri",
    "pref_label",
    "alt_labels",
    "source",
    "additional_notations",
    "attributes",
]
_FLOW_TYPE = "elementary-flow"
#: The relation that states a flow term's unit, its columns, and the folder of
#: sentier-vocab's relation layer, a sibling of the ``terms`` folder.
_UNIT_PREDICATE = "qudt:hasUnit"
_RELATION_COLUMNS = ["subject", "object"]
_RELATIONS_DIR = "relations"
_SIGNIFICANT_DIGITS = 12

#: EF 3.1 / ILCD reference-unit convention, keyed by the one method that fixes a
#: flow's unit; checked in this order because a flow may carry other methods too
#: (e.g. a fossil resource also has a climate-change factor, still reported in MJ).
_MJ_METHOD = "ef-3.1:resource-use-fossils"
_KBQ_METHOD = "ef-3.1:ionising-radiation-human-health"
_M3_METHOD = "ef-3.1:water-use"
_LAND_METHOD = "ef-3.1:land-use"

#: An uncharacterised vocab row with neither an ``ef-context`` notation nor a
#: ``sub_compartment`` gets its context from the ``bw-context`` crosswalk alone
#: (``matching.bw_context.BW_CONTEXT_PATH``), and that crosswalk has no code at all for
#: EF's ``Non-renewable energy resources from ground`` leaf or for any renewable-energy
#: resource branch (its resource codes only ever reach the element/material leaves --
#: ``reso-grou``'s own leaf is literally "...element resources...", see
#: ``bw_context.py``). So a BAFU/EF resource flow that is actually an ENERGY resource by
#: name (an ecoinvent-style "Energy, <form>, converted", a "Primary Energy ..." label,
#: an oil-sand or pit-methane flow) can never land on the right branch through this
#: crosswalk, no matter which code placed it -- it is always on the wrong (element or
#: material) leaf. Such a row is marked ``context_uncertain`` unconditionally, not just
#: when its code happens to be one of ``bw_context.AMBIGUOUS_CODES``. A row placed
#: exactly (``ef-context`` or ``sub_compartment``) is not concerned: its leaf is not
#: inferred. Also imported by
#: ``mappings_biosphere_matched.BafuEfMatchedSource.entry_for`` (decision (f)(2),
#: 2026-09-13), which -- rather than withhold a nomenclature-package match onto one of
#: these -- omits ``target["context"]`` entirely from the emitted entry and discloses
#: that the EF context is not recoverable from the source context code in the comment
#: instead, when the flow's context is uncertain.
UNCERTAIN_RESOURCE_NAME = re.compile(r"^(Energy|Primary Energy|Oil Sand|Pit Methane)\b", re.I)

_SEP = " / "
#: EF context leaf -> its path, for the leaves of factorless flows that no
#: characterised flow sits on and no ``ef-context`` notation spells out: the index
#: cannot learn their path from its own inputs. From the EF correspondence of
#: Brightway Flows 1.1.0, the source of the ``sub_compartment`` attribute itself
#: (``sources.agribalyse.ef_context``), read 2026-11-19; 34 flows in all. A leaf the
#: index does learn elsewhere must agree with this table, or it has two paths.
EF_LEAF_PATH: dict[str, str] = {
    "Renewable energy resources from air": (
        "Resources / Resources from air / Renewable energy resources from air"
    ),
    "Renewable element resources from air": (
        "Resources / Resources from air / Renewable element resources from air"
    ),
    "Renewable energy resources from biosphere": (
        "Resources / Resources from biosphere / Renewable energy resources from biosphere"
    ),
    "Renewable material resources from biosphere": (
        "Resources / Resources from biosphere / Renewable material resources from biosphere"
    ),
    "Renewable energy resources from ground": (
        "Resources / Resources from ground / Renewable energy resources from ground"
    ),
    "Renewable material resources from ground": (
        "Resources / Resources from ground / Renewable material resources from ground"
    ),
    "Renewable energy resources from water": (
        "Resources / Resources from water / Renewable energy resources from water"
    ),
    "Non-renewable element resources from water": (
        "Resources / Resources from water / Non-renewable element resources from water"
    ),
    # not under "Emissions to soil": EF keeps industrial soil as a compartment of its
    # own, which no method characterises and no BAFU sub-compartment reaches
    "Other emissions to industrial soil": (
        "Emissions / Emissions to industrial soil / Other emissions to industrial soil"
    ),
}


def _levels(path: str) -> tuple[str, ...]:
    return tuple(level.strip() for level in path.split("/") if level.strip())


def _leaf_paths(paths: Iterable[str]) -> dict[str, set[str]]:
    """Leaf -> every distinct path among ``paths`` and ``EF_LEAF_PATH`` that ends in it."""
    by_leaf: dict[str, set[str]] = {}
    for path in (*paths, *EF_LEAF_PATH.values()):
        levels = _levels(path)
        if levels:
            by_leaf.setdefault(levels[-1], set()).add(_SEP.join(levels))
    return by_leaf


def _vocab_leaf(row: Mapping) -> str:
    """The ``sub_compartment`` a vocab row states, stripped; ``""`` when it states none."""
    return str(row.get("sub_compartment") or "").strip()


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


def _stated_units(unit_rows: Iterable[Mapping], ef_iris: set[str]) -> dict[str, str]:
    """Flow code -> stated unit spelling, for the ``unit_rows`` whose subject is one
    of ``ef_iris`` (the relation layer also states the units of other releases).

    Raises ``ParseError`` when one flow has two different units, and lets
    ``unit_spelling``'s own error through for a unit IRI without a spelling.
    """
    units: dict[str, str] = {}
    for row in unit_rows:
        subject = str(row["subject"])
        if subject not in ef_iris:
            continue
        code = _code(subject)
        unit = unit_spelling(str(row["object"]))
        if units.setdefault(code, unit) != unit:
            raise ParseError(f"EF flow {code}: two units stated, {units[code]} and {unit}")
    return units


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
    #: characterised flow and for a flow placed by an exact ``ef-context`` notation.
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
    #: The unit the ef-3.1 release states for this flow (its ``qudt:hasUnit``
    #: relation), in the spelling of the mapping packages (``kilogram``, ``kBq``,
    #: ``cubic meter``, ``megajoule``, ``m2``, ``m2*a``, ``m3*a``, ...). ``None`` when
    #: the index was built without unit relations, or the flow has none. For a
    #: characterised flow it equals ``EfFlowIndex.reference_unit`` (checked at index
    #: build); for a factorless flow it is the only unit known.
    stated_unit: str | None = None

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
        unit_rows: Iterable[Mapping] | None = None,
    ) -> "EfFlowIndex":
        """Build an index from CF table rows and vocab flow rows already loaded in memory.

        ``unit_rows`` (default ``None``: no flow states a unit) are ``qudt:hasUnit``
        relation rows, ``subject`` the flow IRI and ``object`` the unit IRI. Only
        those whose subject is an EF vocab row are read, each into
        ``EfFlow.stated_unit``; two different units on one flow, or a unit without
        a spelling (``brightway_flows.units.unit_spelling``), is an error. With any
        stated unit present, ``_check_stated_units`` then runs.

        When ``include_uncharacterised`` is ``True``, every EF vocab row whose code
        carries no CF-table context is also indexed (``EfFlow.characterised=False``),
        placed by the first of its ``ef-context`` notation, its ``sub_compartment``
        attribute (both exact, never uncertain) and, for a row with neither, its
        ``bw-context`` notation through the crosswalk (module docstring). A row none
        of them places is skipped. A ``sub_compartment`` leaf without exactly one
        path, or one that contradicts the row's own ``ef-context``, is an error
        listing the flows. Default ``False``: characterised flows only.

        A characterised flow keeps its CF-table context; a vocab ``sub_compartment``
        that names another leaf is an error listing the flows, whatever
        ``include_uncharacterised`` is.

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
        stated_units = _stated_units(unit_rows or (), {str(r["iri"]) for r in labels.values()})
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
        # requested, uncharacterised (from ef-context or the bw-context crosswalk) -- gathered
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
        leaf_disagreements = [
            f"{code} ({v['name']}): CF table {v['context'][-1]}, "
            f"vocab {_vocab_leaf(labels[code])}"
            for code, v in sorted(core.items())
            if code in labels and _vocab_leaf(labels[code]) not in ("", v["context"][-1])
        ]
        if leaf_disagreements:
            raise ParseError(
                f"{len(leaf_disagreements)} characterised EF flows have a CF-table context "
                "leaf other than the sub_compartment of their vocab term:\n"
                + "\n".join(leaf_disagreements)
            )
        relabelled = 0
        if include_uncharacterised:
            leaf_paths = _leaf_paths(
                [
                    *contexts.values(),
                    *filter(
                        None,
                        (
                            ef_context_for(r.get("additional_notations") or [])
                            for r in labels.values()
                        ),
                    ),
                ]
            )
            #: leaf -> the factorless flows that state it, for a leaf without one path
            unplaceable: dict[str, list[str]] = {}
            for code, row in labels.items():
                if code in contexts:
                    continue  # already indexed as a characterised flow
                name = (row.get("pref_label") or "").strip()
                notations = list(row.get("additional_notations") or [])
                path, leaf = ef_context_for(notations), _vocab_leaf(row)
                uncertain, exact = False, True
                if path is not None:
                    if leaf and leaf != _levels(path)[-1]:
                        raise ParseError(
                            f"EF flow {code} ({name}): its ef-context notation {path!r} "
                            f"and its sub_compartment {leaf!r} name different leaves"
                        )
                elif leaf:
                    paths = leaf_paths.get(leaf, set())
                    if len(paths) != 1:
                        unplaceable.setdefault(leaf, []).append(f"{code} ({name})")
                        continue
                    (path,) = paths
                else:
                    # neither exact source: the crosswalk, as before 2026-11-19
                    path, uncertain = context_for(notations)
                    exact = False
                if path is None:
                    continue  # no EF leaf for this code, or no notation at all
                cas = normalise_cas(row.get("cas_number"))
                alt = tuple(str(s) for s in (row.get("alt_labels") or []))
                name, alt, hit = _relabel(name, cas, alt, label_defects)
                relabelled += hit
                context_tuple = _levels(path)
                bucket = bucket_of_ef_context(" / ".join(context_tuple))
                if not exact and bucket == "resource" and UNCERTAIN_RESOURCE_NAME.match(name):
                    # the crosswalk cannot reach an energy resource leaf at all (see
                    # UNCERTAIN_RESOURCE_NAME) -- this placement is wrong regardless
                    # of which code produced it. An exact placement (ef-context or
                    # sub_compartment) is not a crosswalk placement and is left alone.
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
            if unplaceable:
                lines = []
                for leaf, flows in sorted(unplaceable.items()):
                    paths = sorted(leaf_paths.get(leaf, set()))
                    found = (
                        f"{len(paths)} EF context paths ({'; '.join(paths)})"
                        if paths
                        else "no EF context path"
                    )
                    lines.append(f"{leaf!r}: {found}, stated by {', '.join(sorted(flows))}")
                raise ParseError(
                    "factorless EF flows state a sub_compartment that is not the leaf of "
                    "exactly one EF context path (ef_index.EF_LEAF_PATH):\n" + "\n".join(lines)
                )

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
                        stated_unit=stated_units.get(code),
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
                    stated_unit=stated_units.get(code),
                )
            )
        # a characterised flow only exists once it has a context; drop any vector
        # accumulated for a context-less CF row so it cannot outlive the flow it
        # would have belonged to
        index = cls(
            flows,
            {code: v for code, v in vectors.items() if code in contexts},
            includes_uncharacterised=include_uncharacterised,
            relabelled_count=relabelled,
            multi_name_codes=multi_name_codes,
            suppressed_vocab_synonyms=suppressed_vocab_synonyms,
        )
        index._check_stated_units()
        return index

    @classmethod
    def from_files(
        cls,
        cf_parquet: Path,
        vocab_dir: Path,
        *,
        include_uncharacterised: bool = False,
        label_defects: Mapping[str, LabelDefect] | None = None,
        relations_dir: Path | None = None,
    ) -> "EfFlowIndex":
        """Build an index by reading the CF parquet file, every vocab term file and
        the unit relations beside them (``_read_unit_relations``)."""
        cf_rows = pq.read_table(cf_parquet, columns=_CF_COLUMNS).to_pylist()
        return cls.from_tables(
            cf_rows,
            cls._read_vocab_dir(vocab_dir),
            include_uncharacterised=include_uncharacterised,
            label_defects=label_defects,
            unit_rows=cls._read_unit_relations(vocab_dir, relations_dir),
        )

    @classmethod
    def from_bytes(
        cls,
        cf_parquet_bytes: bytes,
        vocab_dir: Path,
        *,
        include_uncharacterised: bool = False,
        label_defects: Mapping[str, LabelDefect] | None = None,
        relations_dir: Path | None = None,
    ) -> "EfFlowIndex":
        """Build an index from the CF parquet file's raw bytes (e.g. already fetched
        through the content-addressed cache, so the cache/offline contract holds),
        every vocab term file read from ``vocab_dir`` on disk and the unit relations
        beside them (``_read_unit_relations``).
        """
        cf_rows = pq.read_table(io.BytesIO(cf_parquet_bytes), columns=_CF_COLUMNS).to_pylist()
        return cls.from_tables(
            cf_rows,
            cls._read_vocab_dir(vocab_dir),
            include_uncharacterised=include_uncharacterised,
            label_defects=label_defects,
            unit_rows=cls._read_unit_relations(vocab_dir, relations_dir),
        )

    @staticmethod
    def _read_unit_relations(vocab_dir: Path, relations_dir: Path | None = None) -> list[dict]:
        """The ``qudt:hasUnit`` relations of every file in sentier-vocab's
        ``relations/`` folder, as ``subject``/``object`` rows.

        The folder is ``relations_dir`` when given, else the ``relations`` sibling of
        ``vocab_dir`` (sentier-vocab keeps ``terms/`` and ``relations/`` side by
        side, so the registry needs no input for it). A folder that does not exist
        (an older checkout, a test fixture) yields no row: no flow states a unit.
        """
        folder = (
            Path(relations_dir)
            if relations_dir is not None
            else Path(vocab_dir).parent / _RELATIONS_DIR
        )
        if not folder.is_dir():
            return []
        rows: list[dict] = []
        for path in sorted(folder.glob("*.parquet")):
            table = pq.read_table(
                path, columns=_RELATION_COLUMNS, filters=[("predicate", "==", _UNIT_PREDICATE)]
            )
            rows.extend(table.to_pylist())
        return rows

    @staticmethod
    def _read_vocab_dir(vocab_dir: Path) -> list[dict]:
        """The elementary-flow terms of every file in sentier-vocab's ``terms/`` folder,
        as flat rows with ``cas_number`` and ``sub_compartment`` (both keys of the
        ``attributes`` column) beside the common columns."""
        vocab_rows = []
        for path in sorted(Path(vocab_dir).glob("*.parquet")):
            table = pq.read_table(
                path, columns=_VOCAB_COLUMNS, filters=[("type", "==", _FLOW_TYPE)]
            )
            for row in table.to_pylist():
                attributes = json.loads(row.pop("attributes") or "{}")
                row["cas_number"] = attributes.get("cas_number")
                row["sub_compartment"] = attributes.get("sub_compartment")
                vocab_rows.append(row)
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

    def unit(self, code: str) -> str | None:
        """The unit of the EF flow ``code``: the inferred ``reference_unit`` when it
        is characterised, else the unit it states (``EfFlow.stated_unit``), else
        ``None``.

        The two agree on every characterised flow that states one
        (``_check_stated_units``), so the inferred unit is kept there and an index
        built without unit relations answers exactly as before.
        """
        flow = self.get(code)
        if flow is not None and not flow.characterised:
            return flow.stated_unit
        return self.reference_unit(code)

    def _check_stated_units(self) -> None:
        """Raise ``ParseError`` listing every characterised flow whose stated unit
        differs from the inferred ``reference_unit``.

        Run by ``from_tables``. A flow that states no unit is skipped (so is the
        whole check when none does), and so is a factorless flow: nothing is
        inferred for it. On the real ef-3.1 release (2026-11-19) the 89,070
        characterised flows agree, so a disagreement means the vocab relations or
        the method-membership convention changed and must be looked at.
        """
        disagreements = [
            f"{flow.code} ({flow.name}, {flow.context[-1] if flow.context else 'no context'}): "
            f"stated {flow.stated_unit}, inferred {self.reference_unit(flow.code)} "
            f"(methods: {', '.join(sorted(self._vectors.get(flow.code, {}))) or 'none'})"
            for flow in self._sorted_flows
            if flow.characterised
            and flow.stated_unit is not None
            and flow.stated_unit != self.reference_unit(flow.code)
        ]
        if disagreements:
            raise ParseError(
                f"{len(disagreements)} characterised EF flows state a unit other than the "
                "one inferred from their methods:\n" + "\n".join(disagreements)
            )

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
