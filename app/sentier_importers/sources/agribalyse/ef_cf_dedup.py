"""Deterministic resolution of duplicate global-level CF rows in the EF 3.1 JRC table.

For 182 (flow, method) pairs at the global level (``LCIAMethod_location`` empty), the
JRC ``EF-LCIAMethod_CF(EF-v3.1)`` table lists TWO rows with different ``CF EF3.1``
values instead of one: 171 Land use flows and 11 Water use flows. Naive first-row-wins
dedup (the previous behavior of :mod:`sentier_importers.sources.agribalyse.cfs`) kept
whichever value happened to sort first in the parquet, so mass-balanced pairs such as
land ``from X`` / ``to X`` transformation flows, or water intake / return flows, ended
up asymmetric in ``sentier-methods/data/01-ef-3.1/characterization-factors.parquet``.

Resolution rules (deterministic, applied in this order):

1. Detect every (flow uuid, method) group of *global* rows (``LCIAMethod_location``
   empty or ``None``) with 2+ distinct ``CF EF3.1`` values; only these groups are
   touched. Country-level rows are never inspected or altered.
2. **Water use**: keep the value in the "42.95 family" (``|value|`` closest to 42.95,
   i.e. one of 42.95 / -42.95 / -42.955) -- the AWARE world-average default EF 3.1
   applies to an unknown location. This is also what makes a flow's intake and return
   values cancel (e.g. ``Water to Cooling`` +42.95 against ``Water`` emission -42.95).
3. **Land use**: keep the value that makes the flow's ``from X`` / ``to X``
   transformation partner symmetric (``|from| == |to|``, within :data:`_ARBITER_RTOL`
   -- never exact float equality). A partner is either a single deterministic value
   (a non-ambiguous flow) -- in which case we keep whichever of our two candidates
   matches its absolute value -- or itself an ambiguous duplicate group, in which case
   we compare candidate sets. When both of our candidates could be symmetric (the
   partner is an equally ambiguous duplicate carrying the same two ``|value|``s) or no
   partner exists at all (``Land occupation`` flows have no ``from``/``to``
   counterpart), the SimaPro "EF 3.1 adapted" export
   (``dds-agribalyse/source/simapro-EF31-adapted-cfs.parquet``, a local reference input
   that is never copied into sentier-methods) arbitrates. It is an *equality* arbiter,
   not a nearest-value guess: its ``Occupation, X`` / ``Transformation, from X`` /
   ``Transformation, to X`` rows are normalized onto our ``X`` / ``from X`` / ``to X``
   flow names (see :func:`_land_index_keys`) and compared case-insensitively; a
   candidate only "hits" when it equals a SimaPro value within :data:`_ARBITER_RTOL`
   (scaled by the candidate's own magnitude). Exactly one hit resolves the group;
   zero hits (no matching name) or two hits (both candidates happen to equal some
   SimaPro value -- normalization matched the wrong class) both fall through:

   - *primary* normalization (rule ``"simapro"`` when it resolves the group) --
     reused wholesale from
     :data:`sentier_importers.matching.matchers.LAND_CLASS_SYNONYMS` (the same
     ecoinvent-style land-class spellings ``LandUseMatcher`` already reconciles for
     BAFU), a SimaPro-only ``pasture, man made`` -> ``pasture/meadow`` rename (its own
     land vocabulary splits "man made" from the JRC/BAFU-shared "meadow" naming),
     dropping a trailing SimaPro ``(non-use)`` qualifier EF's own names never carry,
     and singular/plural spelling (``crop``/``crops``, ``margin``/``margins``,
     ``wetland``/``wetlands``);
   - *fallback* normalization (rule ``"simapro-fallback"``) -- dropping a bare
     ``natural``/``sclerophyllous``/``unspecified`` qualifier segment SimaPro carries
     but EF's coarser class does not (e.g. EF's bare ``forest`` matches SimaPro's
     ``forest, unspecified``), tried only once the primary forms above fail to match
     -- so a class EF *does* qualify with ``natural`` (e.g. ``unspecified, natural``)
     still matches on its own name first -- plus one narrow alias for EF's
     ``grassland, not used`` (no dedicated SimaPro row; shares both candidate values
     with bare ``grassland``). A dropped-segment candidate is discarded outright when
     its *un-dropped* name is itself a distinct global flow in the same method (e.g.
     ``forest`` can never resolve from ``forest, natural``'s SimaPro evidence --
     ``forest, natural`` is EF's own class, not a filler-qualified spelling of
     ``forest`` -- but *can* resolve from ``forest, unspecified``'s, since EF has no
     ``forest, unspecified`` class of its own).

   SimaPro's own land-use nomenclature still does not cover every JRC flow name (it
   groups some ecoinvent land classes differently, e.g. by crop type rather than
   irrigation regime), so a number of groups still fall through. If the arbiter is
   absent (the file could not be fetched, or could not be parsed) or has no
   single-hit entry, the larger ``|value|`` is kept and the flow is recorded in the
   report with rule ``"larger"``; either way a warning is logged.
4. No other method is expected to hit this ambiguity (verified empirically against the
   full 319k-row JRC table: every one of the 182 ambiguous groups is Water use or Land
   use), but should one appear in a future refresh of the source file, it falls back to
   rule 3's last resort (larger ``|value|``, logged) rather than silently keeping an
   arbitrary row.

:func:`resolve_global_duplicates` returns both the resolved value per group (for the CF
transform to filter rows on) and a full report of every resolved (flow, method) pair, in
the ``flow_name | method | kept | dropped | rule`` shape this module's own tests and the
import's ad hoc diagnostics use -- the importer framework has no import-report facility
of its own, so nothing else reads ``report``; a summary is also logged at INFO level.
"""

from __future__ import annotations

import io
import math
from dataclasses import dataclass, field
from typing import Any

import pyarrow.parquet as pq
from loguru import logger
from sentier_importers.core.types import RawData, Records
from sentier_importers.matching.matchers import LAND_CLASS_SYNONYMS

#: Name of the named ``registry.yaml`` input carrying the SimaPro arbiter file.
SIMAPRO_INPUT = "simapro"

#: Methods with global-level duplicate CF rows in the JRC EF 3.1 table.
WATER_METHOD = "Water use"
LAND_METHOD = "Land use"

#: AWARE world-average-default anchor for Water use global CFs (unknown location).
_WATER_FAMILY_ANCHOR = 42.95

#: Relative tolerance for "the same CF value" comparisons: the SimaPro arbiter's
#: equality check and the land from/to symmetry check. Never exact float equality --
#: both sides may have gone through independent parsing/rounding -- but tight enough
#: that it only absorbs floating-point noise, not genuinely different values.
_ARBITER_RTOL = 1e-9

_FROM_PREFIX = "from "
_TO_PREFIX = "to "

#: SimaPro "EF 3.1 adapted" export column layout (read directly; never emitted).
_SIMAPRO_METHOD_COL = "simapro_method"
_SIMAPRO_NAME_COL = "name"
_SIMAPRO_CF_COL = "cf"
_SIMAPRO_LAND_METHOD = "Land use"
_SIMAPRO_OCCUPATION_PREFIX = "occupation, "
_SIMAPRO_TRANSFORM_FROM_PREFIX = "transformation, from "
_SIMAPRO_TRANSFORM_TO_PREFIX = "transformation, to "
#: A SimaPro row named ``..., <ISO2>`` / ``..., RER`` / ... is a per-region variant of
#: a class already carried at the global level; skipped entirely, since the resolver
#: never looks anything up but a *global* JRC class name (rule 1) and generating name
#: variants for ~250 region rows per class would be wasted work.
_SIMAPRO_REGION_SUFFIXES = frozenset(
    {"RAF", "RAS", "RER", "RLA", "RNA", "RME", "UCTE", "CENTREL", "NORDEL", "UN-OCEANIA"}
)

#: SimaPro land-class segment renames :data:`LAND_CLASS_SYNONYMS` doesn't carry
#: because BAFU's own (much smaller) land-flow vocabulary never needed them. A
#: *sequence* of leading segments to replace, not a single one, since SimaPro splits
#: "man made" from "pasture" where JRC/BAFU keep them merged as ``pasture/meadow``.
_SIMAPRO_LAND_PREFIX_ALIASES: dict[tuple[str, ...], tuple[str, ...]] = {
    ("pasture", "man made"): ("pasture/meadow",),
}
#: A qualifier SimaPro's land vocabulary carries as its own segment, with no bearing
#: on EF's coarser class -- dropped as a fallback candidate only (see module
#: docstring rule 3), and only when the un-dropped name is not itself one of EF's own
#: global flows (see :func:`_resolve_land`'s ``known_land_names`` guard).
_DROPPABLE_LAND_SEGMENTS = frozenset({"natural", "sclerophyllous", "unspecified"})
#: EF's ``grassland, not used`` has no dedicated SimaPro ``Occupation`` row (and no
#: ``Transformation, from`` row either -- only ``to``), sharing both candidate values
#: with bare ``grassland`` in the JRC table, so SimaPro's evidence for ``grassland``
#: is a good fallback stand-in -- explicit and reviewed, so (unlike a dropped segment)
#: it is never guarded against ``grassland`` being EF's own distinct class too.
#: Direction-agnostic: applied after the ``from``/``to`` prefix has been split off, so
#: it fires the same way for all three forms.
_SIMAPRO_LAND_FALLBACK_ALIASES: dict[str, tuple[str, ...]] = {
    "grassland": ("grassland, not used",),
}


def _is_close(candidate: float, reference: float) -> bool:
    """Whether ``candidate`` equals ``reference`` within :data:`_ARBITER_RTOL`,
    scaled by the candidate's own magnitude (so a near-zero candidate does not
    spuriously match everything). Used for both the SimaPro arbiter's equality
    check and the land from/to symmetry check -- never exact float equality."""
    return abs(candidate - reference) <= _ARBITER_RTOL * max(1.0, abs(candidate))


@dataclass(frozen=True)
class DuplicateResolution:
    """Result of resolving global-level CF duplicates.

    ``kept`` maps ``(method_name, flow_uuid)`` to the CF value to keep; any row in that
    group whose value differs from the kept one must be dropped by the caller.
    ``report`` lists one entry per resolved group -- ``flow_name``, ``method``,
    ``kept``, ``dropped``, ``rule`` (one of ``"water"``, ``"symmetric"``,
    ``"simapro"``, ``"simapro-fallback"``, ``"larger"``) -- for this module's own
    tests and ad hoc diagnostics; nothing in the import pipeline reads it back (see
    module docstring), so a full breakdown is also logged at INFO level.
    """

    kept: dict[tuple[str, str], float] = field(default_factory=dict)
    report: list[dict[str, Any]] = field(default_factory=list)


@dataclass(frozen=True)
class SimaproLandIndex:
    """The SimaPro land-use arbiter, keyed by normalized, lowercased JRC-style name.

    ``primary`` holds the high-confidence normalizations (module docstring rule 3);
    ``fallback`` the lower-confidence ones, each CF paired with the *donor* name the
    candidate key was derived from (``None`` for an explicit, reviewed alias that
    needs no guard) -- see :meth:`fallback_values`.
    """

    primary: dict[str, list[float]] = field(default_factory=dict)
    fallback: dict[str, list[tuple[float, str | None]]] = field(default_factory=dict)

    def primary_values(self, name: str) -> list[float] | None:
        """SimaPro CF values carried directly (high confidence) for ``name``."""
        return self.primary.get(name)

    def fallback_values(self, name: str, known_land_names: frozenset[str]) -> list[float] | None:
        """SimaPro CF values inferred (lower confidence) for ``name``, excluding any
        entry whose donor is itself a distinct global flow in ``known_land_names``
        (e.g. ``forest`` must never borrow ``forest, natural``'s evidence)."""
        entries = self.fallback.get(name)
        if not entries:
            return None
        allowed = [cf for cf, donor in entries if donor is None or donor not in known_land_names]
        return allowed or None


def parse_simapro_index(raw: RawData | None) -> SimaproLandIndex | None:
    """Build the SimaPro land-use arbiter index from the adapted-export rows.

    Returns ``None`` when ``raw`` is ``None`` (arbiter file unavailable -- the caller
    already logged the fallback warning when the fetch failed) or when the content
    cannot be parsed as the expected parquet (a malformed arbiter file must not crash
    the import; a warning is logged and the caller falls back the same way).
    """
    if raw is None:
        return None
    try:
        table = pq.read_table(
            io.BytesIO(raw.content),
            columns=[_SIMAPRO_METHOD_COL, _SIMAPRO_NAME_COL, _SIMAPRO_CF_COL],
        ).to_pydict()
    except Exception as exc:
        logger.warning(
            f"SimaPro EF 3.1 arbiter file could not be parsed ({exc}); falling back to "
            "larger-|value| for ambiguous EF 3.1 land-use global CF duplicates"
        )
        return None

    primary: dict[str, list[float]] = {}
    fallback: dict[str, list[tuple[float, str | None]]] = {}
    for method, name, cf in zip(
        table[_SIMAPRO_METHOD_COL], table[_SIMAPRO_NAME_COL], table[_SIMAPRO_CF_COL]
    ):
        if method != _SIMAPRO_LAND_METHOD or not name:
            continue
        stripped = _strip_simapro_prefix(name)
        if _is_region_variant(stripped):
            continue
        primary_keys, fallback_keys = _land_index_keys(stripped)
        for key in primary_keys:
            primary.setdefault(key, []).append(cf)
        for key, donor in fallback_keys.items():
            fallback.setdefault(key, []).append((cf, donor))
    return SimaproLandIndex(primary=primary, fallback=fallback)


def _strip_simapro_prefix(name: str) -> str:
    """Map SimaPro's ``Occupation, ``/``Transformation, from|to `` prefix onto the
    JRC ``X``/``from X``/``to X`` naming convention (case preserved for now)."""
    low = name.lower()
    if low.startswith(_SIMAPRO_OCCUPATION_PREFIX):
        return name[len("Occupation, ") :]
    if low.startswith(_SIMAPRO_TRANSFORM_FROM_PREFIX):
        return _FROM_PREFIX + name[len("Transformation, from ") :]
    if low.startswith(_SIMAPRO_TRANSFORM_TO_PREFIX):
        return _TO_PREFIX + name[len("Transformation, to ") :]
    return name


def _is_region_variant(stripped: str) -> bool:
    last_segment = stripped.rsplit(",", 1)[-1].strip()
    return last_segment in _SIMAPRO_REGION_SUFFIXES or (
        len(last_segment) == 2 and last_segment.isupper() and last_segment.isalpha()
    )


def _split_direction(name: str) -> tuple[str, str]:
    """``("from ", "arable")`` for ``"from arable"``; ``("", "arable")`` when bare."""
    for prefix in (_FROM_PREFIX, _TO_PREFIX):
        if name.startswith(prefix):
            return prefix, name[len(prefix) :]
    return "", name


def _apply_prefix_alias(segments: list[str]) -> list[str]:
    for prefix, replacement in _SIMAPRO_LAND_PREFIX_ALIASES.items():
        n = len(prefix)
        if tuple(segments[:n]) == prefix:
            return list(replacement) + segments[n:]
    return segments


def _pluralize_variants(name: str) -> set[str]:
    """``name`` with the trailing word of one comma/``/``-separated part pluralized
    (``permanent crop`` -> ``permanent crops``, ``field margin/hedgerow`` ->
    ``field margins/hedgerow``), one part at a time -- covers every mismatch this
    module has seen without guessing at a full inflection engine. Callers wanting
    every part pluralized together (``field margins/hedgerows``) should expand this
    to a fixed point (see :func:`_expand_pluralizations`) rather than call it once."""
    segments = name.split(", ")
    variants = set()
    for seg_i, segment in enumerate(segments):
        parts = segment.split("/")
        for part_i, part in enumerate(parts):
            words = part.split(" ")
            last = words[-1]
            if not last or last.endswith("s"):
                continue
            new_part = "/".join(
                parts[:part_i] + [" ".join(words[:-1] + [last + "s"])] + parts[part_i + 1 :]
            )
            variants.add(", ".join(segments[:seg_i] + [new_part] + segments[seg_i + 1 :]))
    return variants


def _expand_pluralizations(names: set[str]) -> set[str]:
    """Fixed-point closure of :func:`_pluralize_variants`: pluralizing one part at a
    time, repeated, reaches a name needing more than one part pluralized (``field
    margin/hedgerow`` -> ``field margins/hedgerow`` -> ``field margins/hedgerows``)."""
    expanded = set(names)
    frontier = expanded
    while True:
        added = set()
        for name in frontier:
            added |= _pluralize_variants(name)
        added -= expanded
        if not added:
            return expanded
        expanded |= added
        frontier = added


def _land_index_keys(stripped: str) -> tuple[set[str], dict[str, str | None]]:
    """Every JRC-style key this stripped-and-prefixed SimaPro land name (``X`` /
    ``from X`` / ``to X``) should be registered under.

    Returns ``(primary, fallback)``: ``primary`` is a set of high-confidence keys;
    ``fallback`` maps each lower-confidence key to its donor name (the pre-drop name
    it was derived from), or ``None`` for an explicit alias that needs no donor
    guard -- see :data:`SimaproLandIndex`. Both are lowercased (comparisons are
    case-insensitive) and carry the original ``from``/``to`` direction, if any.
    """
    direction, rest = _split_direction(stripped.strip())
    without_non_use = rest.replace(" (non-use)", "").strip().rstrip(",").strip()

    primary: set[str] = set()
    for variant in {rest, without_non_use}:
        if not variant:
            continue
        segments = [s.strip() for s in variant.split(",") if s.strip()]
        if not segments:
            continue
        segments = _apply_prefix_alias(segments)
        segments = [LAND_CLASS_SYNONYMS.get(seg, seg) for seg in segments]
        primary.add(", ".join(segments))
    primary = _expand_pluralizations(primary)
    primary.discard("")

    # Fallback candidates always take a back seat to a primary hit at lookup time
    # (see SimaproLandIndex.fallback_values / _resolve_land), so a fallback key that
    # happens to coincide with a primary one is harmless noise, not a correctness
    # concern -- no need to guard against it here too.
    fallback: dict[str, str | None] = {}
    for name in primary:
        segments = [s.strip() for s in name.split(",")]
        if len(segments) <= 1:
            continue
        # Drop one droppable segment at a time (never all at once): "unspecified"
        # and "natural" are both fillers in general, but each is meaningful as the
        # *other* segment's companion (``unspecified, natural`` keeps ``unspecified``
        # when ``natural`` is dropped, ``forest, unspecified`` keeps ``forest``).
        for i, segment in enumerate(segments):
            if segment not in _DROPPABLE_LAND_SEGMENTS:
                continue
            key = ", ".join(segments[:i] + segments[i + 1 :])
            fallback.setdefault(key, name)  # donor = the pre-drop name, guarded
    # The direction (stripped above, re-applied below) carries through unchanged, so
    # this alias fires the same way whether the row was bare, ``from``, or ``to``:
    # EF's own "grassland, not used" transformation flows only cover one direction.
    for name in list(primary) + list(fallback):
        for alias_key in _SIMAPRO_LAND_FALLBACK_ALIASES.get(name, ()):
            fallback[alias_key] = None  # explicit alias: unguarded, overrides any donor

    return (
        {(direction + name).lower() for name in primary if name},
        {
            (direction + key).lower(): ((direction + donor).lower() if donor is not None else None)
            for key, donor in fallback.items()
            if key
        },
    )


def _land_partner_name(name: str) -> str | None:
    """The opposite-direction flow name for a land transformation flow, or ``None``
    when ``name`` carries no ``from``/``to`` direction (e.g. a Land occupation flow)."""
    if name.startswith(_FROM_PREFIX):
        return _TO_PREFIX + name[len(_FROM_PREFIX) :]
    if name.startswith(_TO_PREFIX):
        return _FROM_PREFIX + name[len(_TO_PREFIX) :]
    return None


def _resolve_water(candidates: list[float]) -> tuple[float, str]:
    """Rule 2: keep the 42.95-family value (AWARE world-average default)."""
    kept = min(candidates, key=lambda v: abs(abs(v) - _WATER_FAMILY_ANCHOR))
    return kept, "water"


#: JRC rounds one member of the 42.95 family to three decimals (``Water`` emitted to
#: water, unspecified: -42.955) while every intake and the fresh-water return carry
#: 42.95 / -42.95. Left as published, a turbine or cooling intake netted against that
#: return leaves -0.005 per m3 and hydropower comes out with negative water use. The
#: family is harmonised to |42.95| exactly on global rows (decision 2026-09-14:
#: "consistent 42.95 / -42.95 for water"); anything farther from the anchor than this
#: tolerance is not a rounding artefact and is left alone.
_WATER_FAMILY_TOLERANCE = 0.01


def harmonise_water_family(value: float) -> float:
    """``-42.955`` -> ``-42.95``; values outside the rounding tolerance unchanged."""
    if abs(abs(value) - _WATER_FAMILY_ANCHOR) <= _WATER_FAMILY_TOLERANCE:
        return math.copysign(_WATER_FAMILY_ANCHOR, value)
    return value


def _arbiter_hits(candidates: list[float], sp_values: list[float]) -> list[float]:
    """Candidates equal (within :data:`_ARBITER_RTOL`) to some SimaPro value."""
    return [c for c in candidates if any(_is_close(c, sv) for sv in sp_values)]


def _resolve_land(
    name: str,
    candidates: list[float],
    singles: dict[str, float],
    dup_values: dict[str, list[float]],
    simapro_index: SimaproLandIndex | None,
    known_land_names: frozenset[str],
) -> tuple[float, str]:
    """Rule 3: symmetric ``from``/``to`` partner first, SimaPro arbiter (primary then
    fallback tier), then larger ``|value|`` as the last resort (always logged)."""
    partner_name = _land_partner_name(name)
    if partner_name is not None:
        if partner_name in singles:
            partner_value = singles[partner_name]
            matches = [c for c in candidates if _is_close(abs(c), abs(partner_value))]
            if len(matches) == 1:
                return matches[0], "symmetric"
        elif partner_name in dup_values:
            partner_abs = [abs(v) for v in dup_values[partner_name]]
            matches = [c for c in candidates if any(_is_close(abs(c), pa) for pa in partner_abs)]
            if len(matches) == 1:
                return matches[0], "symmetric"
    if simapro_index is not None:
        key = name.strip().lower()
        primary_values = simapro_index.primary_values(key)
        if primary_values:
            hits = _arbiter_hits(candidates, primary_values)
            if len(hits) == 1:
                return hits[0], "simapro"
        fallback_values = simapro_index.fallback_values(key, known_land_names)
        if fallback_values:
            hits = _arbiter_hits(candidates, fallback_values)
            if len(hits) == 1:
                return hits[0], "simapro-fallback"
    kept = max(candidates, key=abs)
    logger.warning(
        f"EF 3.1 global CF duplicate for land-use flow {name!r} left undecided by symmetry "
        f"and the SimaPro arbiter ({candidates}); keeping the larger |value| ({kept})"
    )
    return kept, "larger"


def resolve_global_duplicates(
    records: Records, simapro_index: SimaproLandIndex | None
) -> DuplicateResolution:
    """Resolve every ambiguous (flow uuid, method) global CF group in ``records``.

    ``records`` are :func:`~sentier_importers.sources.agribalyse.ef_common.parse_cf_table`
    records (one per JRC row: ``flow_uuid``, ``flow_name``, ``method_name``, ``cf``,
    ``location``). Only rows with an empty/``None`` ``location`` are considered;
    country-level rows are left untouched by construction.
    """
    values: dict[tuple[str, str], list[float]] = {}
    names: dict[tuple[str, str], str] = {}
    for rec in records:
        if rec.get("location"):
            continue
        method = rec.get("method_name") or ""
        uuid = rec.get("flow_uuid")
        value = rec.get("cf")
        if not uuid or not method or value is None:
            continue
        key = (method, uuid)
        values.setdefault(key, []).append(float(value))
        names[key] = rec.get("flow_name") or ""

    dup_keys = [key for key, vals in values.items() if len(set(vals)) > 1]
    if not dup_keys:
        return DuplicateResolution()

    # Per-method name indices for the land partner lookup: a single deterministic
    # value for non-ambiguous flows, or the raw candidate list for ambiguous ones.
    singles_by_method: dict[str, dict[str, float]] = {}
    dup_values_by_method: dict[str, dict[str, list[float]]] = {}
    for key, vals in values.items():
        method = key[0]
        name = names[key]
        if len(set(vals)) == 1:
            singles_by_method.setdefault(method, {})[name] = vals[0]
        else:
            dup_values_by_method.setdefault(method, {})[name] = vals

    land_singles = singles_by_method.get(LAND_METHOD, {})
    land_dup_values = dup_values_by_method.get(LAND_METHOD, {})
    known_land_names = frozenset(n.lower() for n in (*land_singles, *land_dup_values))

    kept: dict[tuple[str, str], float] = {}
    report: list[dict[str, Any]] = []
    for key in dup_keys:
        method = key[0]
        name = names[key]
        candidates = sorted(set(values[key]))
        if method == WATER_METHOD:
            value, rule = _resolve_water(candidates)
        elif method == LAND_METHOD:
            value, rule = _resolve_land(
                name, candidates, land_singles, land_dup_values, simapro_index, known_land_names
            )
        else:
            value = max(candidates, key=abs)
            rule = "larger"
            logger.warning(
                f"unhandled EF 3.1 global CF duplicate for method {method!r}, flow "
                f"{name!r} ({candidates}); keeping the larger |value| ({value})"
            )
        kept[key] = value
        dropped = next((c for c in candidates if c != value), None)
        report.append(
            {
                "flow_name": name,
                "method": method,
                "kept": value,
                "dropped": dropped,
                "rule": rule,
            }
        )

    rule_counts: dict[str, int] = {}
    for entry in report:
        rule_counts[entry["rule"]] = rule_counts.get(entry["rule"], 0) + 1
    logger.info(f"resolved {len(report)} EF 3.1 global CF duplicates: {rule_counts}")
    return DuplicateResolution(kept=kept, report=report)
