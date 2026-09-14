"""Matchers: one lookup strategy each, from a BAFU flow to EF candidate flows.

A matcher never places (sub-compartment) or disambiguates (several substances); it
returns every EF flow in the BAFU flow's compartment bucket that its key finds, sorted
by code. Placement and the choice among candidates are the pipeline's job.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Protocol

import yaml
from sentier_importers.core.errors import ParseError
from sentier_importers.matching.compartments import bucket_of_bafu_category
from sentier_importers.matching.ef_index import EfFlow, EfFlowIndex
from sentier_importers.sources.eaternity.bridge import BafuFlow

ALIASES_PATH = Path(__file__).with_name("aliases.yaml")
#: Trailing region tokens BAFU bakes into flow names (``Water, KR``, ``Water, Europe``).
_REGION_RE = re.compile(
    r",\s(?P<token>[A-Z]{2}|Europe|RER|GLO|RoW|OECD|RAF|RAS|RLA|RNA|RME|UCTE|CENTREL|NORDEL)$"
)
_ISO2_RE = re.compile(r"^[A-Z]{2}$")
#: ``Carbon dioxide, biogenic`` -> ``carbon dioxide (biogenic)``; EF spells qualifiers in
#: parentheses, calls the biogenic class ``biogenic`` (never ``non-fossil``) and land
#: transformation ``land use change``.
_QUALIFIER_RE = re.compile(
    r"^(?P<stem>.+), (?P<q>biogenic|fossil|non-fossil|land transformation)$", re.IGNORECASE
)
_QUALIFIER_EF = {"non-fossil": "biogenic", "land transformation": "land use change"}
#: BAFU land-occupation/transformation names: ``Occupation, <class>``,
#: ``Transformation, from <class>`` or ``Transformation, to <class>``; the keyword is
#: matched case-insensitively (``re.IGNORECASE`` covers the whole pattern, but only
#: the keyword varies in case in practice -- BAFU's class spelling is consistent).
_LAND_RE = re.compile(
    r"^(?:occupation, (?P<occ_cls>.+)"
    r"|transformation, from (?P<from_cls>.+)"
    r"|transformation, to (?P<to_cls>.+))$",
    re.IGNORECASE,
)
#: BAFU land-class spellings that differ from EF's, applied to each comma segment of
#: the class independently (e.g. ``annual crop, irrigated`` -> ``arable, irrigated``).
#: Public: also imported by ``sources.agribalyse.ef_cf_dedup`` to normalize the
#: SimaPro "EF 3.1 adapted" export onto the same EF class names, since ecoinvent's
#: land-class vocabulary is the same wherever it is sourced from.
LAND_CLASS_SYNONYMS = {
    "annual crop": "arable",
    "unknown": "unspecified",
    "natural (non-use)": "natural",
    "non-use": "natural",
}
#: ecoinvent v2 ore composite names: a leading capitalised element word, optionally
#: followed by more comma segments, then an ore-grade segment (``x% in y``, or the
#: bare ``in ore``/``in crude ore``), with an optional trailing ``, in ground``. A
#: name whose first segment is not a plain element word (``TiO2, 54% in ilmenite,
#: ...``) does not match: it names a compound, not an element, and the amount would
#: not be "kg of <element>".
_ORE_RE = re.compile(
    r"^(?P<element>[A-Z][a-z]+)(?:, [^,]+)*?, "
    r"(?:[^,]*\d% in [^,]+|in (?:crude )?ore)(?:, in ground)?$"
)
#: The EF leaf ``OreCompositeMatcher`` restricts its candidates to: an ore-composite
#: name always names a non-renewable element resource extracted from the ground.
_ORE_LEAF = "non-renewable element resources from ground"

#: An ion/oxidation-state marker BAFU trails an element or anion name with: a comma or
#: bare ``ion`` (``Arsenic, ion`` / ``Copper ion``), or a trailing roman numeral II-VI
#: (``Calcium II``). Roman numerals are matched case-sensitively (BAFU's own spelling
#: is always upper-case here) -- unlike ``_QUALIFIER_RE``, this pattern carries no
#: ``re.IGNORECASE`` flag. ``IonStripMatcher`` strips the marker and looks the bare
#: stem up directly; decision (d), 2026-09-13.
_ION_STRIP_RE = re.compile(r"^(?P<stem>.+?)(,\s?ion|\sion|\s(II|III|IV|V|VI))$")

#: Species markers EF itself uses in an *EF* flow name (a bracketed roman numeral
#: oxidation state, a bracketed charge like ``(6+)``/``(2+)``/``(2-)``, or the bare
#: word ``ion``): ``IonStripMatcher`` defers to a same-CAS EF flow shaped like this
#: instead of collapsing onto the bare element -- see its own docstring.
_EF_SPECIES_RE = re.compile(r"\((?:i{1,3}|iv|v|vi)\)|\(\d[+-]\)|\bion\b", re.IGNORECASE)

#: The two BAFU carbon-oxide names EF characterises only with a qualifier.
#: ``CarbonOxideMatcher`` rewrites a bare occurrence of either onto an EF qualified
#: spelling; decision (e), 2026-09-13.
_CARBON_OXIDES = ("Carbon dioxide", "Carbon monoxide")
#: BAFU's own name for atmospheric CO2 uptake (a resource-bucket flow, not an
#: emission) -- taken as biogenic uptake; decision (e), 2026-09-13.
_CO2_UPTAKE = "Carbon dioxide, in air"


def _normalise_land_class(raw: str) -> str:
    segments = [seg.strip() for seg in raw.strip().lower().split(",")]
    return ", ".join(LAND_CLASS_SYNONYMS.get(seg, seg) for seg in segments)


@dataclass(frozen=True)
class Candidate:
    """One EF flow a matcher proposes, tagged with provenance and any carried caveat.

    ``subcategory_override``, when set, overrides ``flow.subcategory`` for placement
    (``MatchPipeline.match``) instead of the source's own sub-compartment -- for a
    matcher that knows a flow's true compartment from its NAME rather than from the
    source's sub-compartment column (currently: land use, whose BAFU name encodes
    ``Occupation``/``Transformation`` even when the flow is filed under an unrelated
    resource sub-compartment). Such a matcher must always explain the override with a
    caveat, except when the source's own sub-compartment already agrees with the
    override -- there is nothing to explain in that case.
    """

    flow: EfFlow
    location: str | None = None  # ISO-2 code carried from the source name
    region: str | None = None  # non-ISO region token carried from the source name
    tier: str | None = None  # the matcher (tier) that produced this candidate
    caveat: str | None = None  # a known caveat about this match, if any
    subcategory_override: str | None = None  # placement override; see class docstring


@dataclass(frozen=True)
class Alias:
    """One curated alias table entry: the EF preferred label to match, plus an optional caveat."""

    target: str
    caveat: str | None = None


class Matcher(Protocol):
    """A single matching strategy over one BAFU flow."""

    tier: str

    def candidates(self, flow: BafuFlow, cas: str | None, index: EfFlowIndex) -> list[Candidate]:
        """Return every EF flow this strategy finds for ``flow``, sorted by code."""
        ...


def _bucket(flow: BafuFlow) -> str | None:
    return bucket_of_bafu_category(flow.category)


def _lookup(flows: Sequence[EfFlow], tier: str) -> list[Candidate]:
    return [Candidate(f, tier=tier) for f in sorted(flows, key=lambda f: f.code)]


def _by_name_in_leaf(index: EfFlowIndex, name: str, leaf: str) -> list[EfFlow]:
    """Resource-bucket EF flows named ``name``, restricted to ``leaf`` and code-sorted.

    Shared by ``LandUseMatcher._by_class`` and ``OreCompositeMatcher``: both need a
    same-named EF flow filtered down to one specific leaf family, since ``by_name``
    alone can return same-named flows filed under an unrelated resource leaf (e.g. an
    element and an energy resource both called ``Zinc``).
    """
    found = [f for f in index.by_name(name, "resource") if f.leaf == leaf]
    return sorted(found, key=lambda f: f.code)


class ExactNameMatcher:
    """Matches on the BAFU flow name against the EF preferred label, bucket-scoped."""

    tier = "name"

    def candidates(self, flow: BafuFlow, cas: str | None, index: EfFlowIndex) -> list[Candidate]:
        """Return EF flows whose preferred label equals ``flow.name`` in ``flow``'s bucket."""
        return _lookup(index.by_name(flow.name, _bucket(flow)), self.tier)


class SynonymMatcher:
    """Matches on the BAFU flow name against EF synonyms (``alt_labels``), bucket-scoped."""

    tier = "synonym"

    def candidates(self, flow: BafuFlow, cas: str | None, index: EfFlowIndex) -> list[Candidate]:
        """Return EF flows whose synonym equals ``flow.name`` in ``flow``'s bucket."""
        return _lookup(index.by_synonym(flow.name, _bucket(flow)), self.tier)


class CasMatcher:
    """Matches on a CAS number against the EF flow's CAS, bucket-scoped.

    Needs a CAS number to do anything; returns every flow sharing it (several EF flows,
    e.g. distinct biogenic/fossil/land-use-change carbon dioxide entries, share a CAS
    number -- disambiguating between them is not this matcher's job).
    """

    tier = "cas"

    def candidates(self, flow: BafuFlow, cas: str | None, index: EfFlowIndex) -> list[Candidate]:
        """Return every EF flow in ``flow``'s bucket whose CAS number matches ``cas``."""
        if cas is None:
            return []
        return _lookup(index.by_cas(cas, _bucket(flow)), self.tier)


class QualifierMatcher:
    """Rewrites a BAFU comma-qualifier (``, biogenic`` / ``, fossil`` / ...) to EF spelling.

    EF spells qualifiers in parentheses (``Carbon dioxide (biogenic)``), calls the
    biogenic class ``biogenic`` (never BAFU's ``non-fossil``), and land transformation
    ``land use change`` (never BAFU's ``land transformation``).
    """

    tier = "qualifier"

    def candidates(self, flow: BafuFlow, cas: str | None, index: EfFlowIndex) -> list[Candidate]:
        """Return EF flows whose label is ``flow.name`` rewritten to EF qualifier spelling."""
        match = _QUALIFIER_RE.match(flow.name.strip())
        if match is None:
            return []
        stem = match.group("stem")
        qualifier = match.group("q").lower()
        qualifier = _QUALIFIER_EF.get(qualifier, qualifier)
        return _lookup(index.by_name(f"{stem} ({qualifier})", _bucket(flow)), self.tier)


class CarbonOxideMatcher:
    """Rewrites a bare, unqualified BAFU carbon oxide onto an EF qualified spelling.

    EF characterises ``Carbon dioxide``/``Carbon monoxide`` only with a qualifier
    (fossil/biogenic/land use change); a bare name never matches on its own. Decision
    (e), 2026-09-13: a bare emission (``Carbon dioxide``/``Carbon monoxide`` in the
    air bucket) is taken as fossil; BAFU's own ``Carbon dioxide, in air`` (a resource
    bucket flow, atmospheric CO2 uptake, not an emission) is taken as biogenic. Both
    rewrites carry a caveat naming the decision, so the assumption is never silent.
    """

    tier = "carbon-oxide"

    def candidates(self, flow: BafuFlow, cas: str | None, index: EfFlowIndex) -> list[Candidate]:
        """Return the EF qualified-spelling flows a bare carbon-oxide name rewrites to."""
        name = flow.name.strip()
        bucket = _bucket(flow)
        if bucket == "air" and name in _CARBON_OXIDES:
            target = f"{name} (fossil)"
            caveat = "unqualified carbon oxide taken as fossil (decision 2026-09-13)"
        elif bucket == "resource" and name == _CO2_UPTAKE:
            target = "carbon dioxide (biogenic)"
            caveat = "CO2 uptake from air taken as biogenic (decision 2026-09-13)"
        else:
            return []
        found = _lookup(index.by_name(target, bucket), self.tier)
        return [replace(c, caveat=caveat) for c in found]


class IonStripMatcher:
    """Strips an ion/oxidation-state marker from a BAFU name and matches the bare stem.

    Decision (d), 2026-09-13: an ion-shaped BAFU name (``_ION_STRIP_RE``) whose EF
    counterpart is the plain element/anion flow is emitted, not withheld -- a human
    reviewing the result can see the caveat this matcher attaches to every candidate
    it returns. A name with no ion/oxidation-state marker never matches (``[]``); a
    marker whose stem EF does not carry at all is left to a later tier (this matcher
    only ever collapses onto a same-bucket, same-name EF flow, never invents one).

    One escape, checked before collapsing: when the source carries a CAS number that
    itself names a species-shaped EF flow in this bucket (``_EF_SPECIES_RE``, e.g.
    ``Chromium(6+)``) -- this matcher defers (``[]``) rather than steal that better,
    species-correct match away from ``CasMatcher``, which runs later. Without this
    escape, ``Chromium VI`` (CAS 18540-29-9, no exact/synonym route of its own) would
    collapse onto plain ``Chromium`` here instead of resolving onto EF's own
    ``Chromium(6+)`` flow via CAS -- a real species, silently swapped for a wrong one.
    """

    tier = "ion"

    def candidates(self, flow: BafuFlow, cas: str | None, index: EfFlowIndex) -> list[Candidate]:
        """Return the bare-stem EF flow(s) an ion/oxidation-state-shaped name strips to."""
        match = _ION_STRIP_RE.match(flow.name.strip())
        if match is None:
            return []
        bucket = _bucket(flow)
        if cas is not None and any(
            _EF_SPECIES_RE.search(f.name) for f in index.by_cas(cas, bucket)
        ):
            return []
        stem = match.group("stem")
        found = _lookup(index.by_name(stem, bucket), self.tier)
        caveat = "ion-form source name mapped onto the EF element flow (decision 2026-09-13)"
        return [replace(c, caveat=caveat) for c in found]


class LandUseMatcher:
    """Matches a BAFU land-occupation/transformation name onto its EF 3.1 land class.

    Applies only to resource-bucket flows named ``Occupation, <class>``,
    ``Transformation, from <class>`` or ``Transformation, to <class>``. BAFU spells
    some classes differently from EF (``annual crop`` for EF's ``arable``, ``unknown``
    for EF's ``unspecified``, ...; see ``LAND_CLASS_SYNONYMS``) and files 30 of its
    43 land flows under a resource sub-compartment other than ``land`` (most often
    ``unspecified`` or ``in ground``). Every candidate this matcher returns carries
    ``subcategory_override="land"`` so the pipeline places it on the EF land-use
    context regardless of the BAFU sub-compartment, plus a caveat whenever that
    filing disagrees with ``land`` (see ``Candidate.subcategory_override``).

    When EF has no flow for the exact class, one parent level is dropped (the last
    comma segment) and the lookup retried, with a caveat naming the collapse -- e.g.
    ``Occupation, dump site, benthos`` collapses onto EF's ``Dump Site``. Candidates
    are filtered to the matching EF leaf family too: an ``Occupation`` name is only
    ever satisfied from the ``land occupation`` leaf, a ``from``/``to`` name only
    from ``land transformation`` -- a same-named flow in the other leaf is never
    returned.

    A name that still carries an unstripped trailing region token (``, CH``,
    ``, RER``, ...) is refused outright (``[]``): the one-level-collapse fallback
    above would otherwise mistake the region token for a droppable sub-class segment
    (``Occupation, traffic area, rail network, CH`` would wrongly "collapse" onto
    ``Traffic Area, Rail Network`` before ``RegionStripMatcher`` ever gets a chance to
    strip ``CH`` properly and record it as a location instead). Region-stripping is
    ``RegionStripMatcher``'s job; this matcher only ever sees a clean stem when it
    runs as one of its ``inner`` matchers.
    """

    tier = "landuse"

    def candidates(self, flow: BafuFlow, cas: str | None, index: EfFlowIndex) -> list[Candidate]:
        """Return EF land-use flows matching ``flow``'s occupation/transformation class."""
        if _bucket(flow) != "resource":
            return []
        name = flow.name.strip()
        if _REGION_RE.search(name):
            return []
        match = _LAND_RE.match(name)
        if match is None:
            return []
        if match.group("occ_cls") is not None:
            kind, raw_cls, leaf = "occupation", match.group("occ_cls"), "land occupation"
        elif match.group("from_cls") is not None:
            kind, raw_cls, leaf = "from", match.group("from_cls"), "land transformation"
        else:
            kind, raw_cls, leaf = "to", match.group("to_cls"), "land transformation"

        cls = _normalise_land_class(raw_cls)
        found = self._by_class(index, kind, cls, leaf)
        collapse_caveat = None
        if not found and "," in cls:
            parent = cls.rsplit(",", 1)[0].strip()
            parent_found = self._by_class(index, kind, parent, leaf)
            if parent_found:
                found = parent_found
                collapse_caveat = (
                    f"sub-class {cls!r} collapsed onto EF class {found[0].name!r}; "
                    "EF has no flow for the sub-class"
                )
        if not found:
            return []

        filing_caveat = None
        if flow.subcategory != "land":
            filing_caveat = (
                f"BAFU files this land flow under resources / {flow.subcategory}; "
                "placed on EF land use"
            )
        caveats = [c for c in (filing_caveat, collapse_caveat) if c]
        caveat = "; ".join(caveats) if caveats else None

        return [
            Candidate(f, tier=self.tier, subcategory_override="land", caveat=caveat) for f in found
        ]

    @staticmethod
    def _by_class(index: EfFlowIndex, kind: str, cls: str, leaf: str) -> list[EfFlow]:
        """EF flows named ``cls`` (prefixed ``from``/``to`` outside ``occupation``),
        restricted to ``leaf`` and code-sorted -- the same order the returned
        candidates end up in, so ``found[0]`` (used for the collapse caveat) is
        always the same flow the first returned ``Candidate`` wraps.
        """
        ef_name = cls if kind == "occupation" else f"{kind} {cls}"
        return _by_name_in_leaf(index, ef_name, leaf)


class OreCompositeMatcher:
    """Matches an ecoinvent v2 ore-composite name onto its bare element resource.

    Resource bucket only. BAFU carries several minerals as an ore composite: a
    leading element word, an ore-grade segment (``Zn 0.63%, ..., in ore`` or
    ``0.99% in sulfide, ..., in crude ore``), and sometimes a trailing ``, in
    ground`` (``_ORE_RE``) -- the amount is always kg of the *plain element*, never
    of the ore rock. A name whose leading segment is not a bare capitalised element
    word (e.g. ``TiO2, 54% in ilmenite, ...``) does not match: it names a compound,
    and collapsing it onto the element would silently assert the wrong substance.

    Candidates are restricted to the ``non-renewable element resources from ground``
    EF leaf (``_ORE_LEAF``) -- an ore composite is never anything else -- and every
    candidate carries a caveat naming the composite decomposition, since the BAFU
    amount is not what the ore name on its own would suggest.
    """

    tier = "ore"

    def candidates(self, flow: BafuFlow, cas: str | None, index: EfFlowIndex) -> list[Candidate]:
        """Return the bare-element EF resource flow(s) an ore-composite name decomposes to."""
        if _bucket(flow) != "resource":
            return []
        match = _ORE_RE.match(flow.name.strip())
        if match is None:
            return []
        element = match.group("element")
        found = _by_name_in_leaf(index, element, _ORE_LEAF)
        caveat = (
            f"ore composite of the BAFU-2026 source nomenclature; the amount is kg of {element}"
        )
        return [Candidate(f, tier=self.tier, caveat=caveat) for f in found]


class AliasMatcher:
    """Matches on a curated BAFU-name -> EF-preferred-label table, bucket-scoped.

    Accepts either plain ``str`` targets or ``Alias`` values; a plain string is wrapped
    into a target-only ``Alias``. When the matched alias carries a caveat, every
    candidate it produces carries that caveat too.

    Round 6, decision 2026-09-14 (``ef_index`` naming, third cut): a characterised EF
    flow's matching key (``EfFlow.name``) is now the CF table's own JRC spelling, not
    the vocab pref_label these aliases were curated against -- the vocab label
    survives, when kept, as ``EfFlow.label`` and a synonym (``EfFlowIndex.
    from_tables``). So a target string that still names a flow's pref_label exactly
    (not its JRC name) is looked up by synonym too, but only accepted when that
    tells us something a plain synonym hit alone would not: every matching flow's
    OWN displayed label (``EfFlow.label``, never just any alt_label it happens to
    share the target string with) equals the target, and every one of them is the
    same identity (``(name, cas)`` -- distinct EF rows for one physical substance are
    fine, e.g. genuine duplicates, but two DIFFERENT substances that both merely
    list the target as one alt_label among many are not this alias's business at
    all, e.g. "Water" must never fall through onto "Water Vapour" just because
    "water" appears somewhere in its long synonym list). Falls back to synonym only
    when the name lookup itself finds nothing -- a target that resolves by name is
    never second-guessed by also checking synonyms.
    """

    tier = "alias"

    def __init__(self, aliases: Mapping[str, str | Alias]) -> None:
        """Build the matcher from ``aliases`` (arbitrary case/whitespace keys)."""
        self._aliases = {
            k.strip().lower(): v if isinstance(v, Alias) else Alias(target=v)
            for k, v in aliases.items()
        }

    def candidates(self, flow: BafuFlow, cas: str | None, index: EfFlowIndex) -> list[Candidate]:
        """Return EF flows whose label -- or, failing that, whose OWN displayed
        label via a synonym hit, unambiguously -- equals the alias table's target
        for ``flow.name``."""
        alias = self._aliases.get(flow.name.strip().lower())
        if alias is None:
            return []
        bucket = _bucket(flow)
        matched = index.by_name(alias.target, bucket)
        if not matched:
            target = alias.target.strip().lower()
            hits = [
                f
                for f in index.by_synonym(alias.target, bucket)
                if f.label.strip().lower() == target
            ]
            # identity, not literal string equality: two EF flow codes for the very
            # same substance can pick a differently-cased JRC spelling each (JRC's
            # raw data is not perfectly case-consistent across codes), which must
            # not register as "two different substances" here.
            if len({(f.name.strip().lower(), f.cas) for f in hits}) == 1:
                matched = hits
        found = _lookup(matched, self.tier)
        return [replace(c, caveat=alias.caveat) for c in found]


class RegionStripMatcher:
    """Strips a trailing region token from the flow name and retries with inner matchers.

    BAFU sometimes bakes a region into the flow name (``Water, KR``, ``Water, Europe``,
    ``Water, river, CH``). This matcher recognises a trailing comma-separated region
    token (an ISO-3166-1 alpha-2 code, or a small set of named non-ISO regions such as
    ``Europe``/``RER``/``GLO``), strips it, runs ``inner`` matchers in turn against the
    stem name, and re-wraps the first non-empty result with the stripped token recorded
    as ``location`` (ISO-2) or ``region`` (anything else) and a ``tier`` of
    ``"region/<inner tier>"``. No token, no match: ``[]``.

    ``inner`` must be name-keyed matchers only (``ExactNameMatcher``, ``LandUseMatcher``,
    ``SynonymMatcher``, ``QualifierMatcher``, ``CarbonOxideMatcher``, ``IonStripMatcher``,
    ``AliasMatcher``): a ``CasMatcher`` inside would ignore the stripped stem and match
    on ``cas`` again, defeating the point of stripping. The actual rule is narrower than
    "never reads ``cas``": ``IonStripMatcher`` DOES read ``cas``, but only as a defer
    guard (to check whether a same-CAS EF flow already carries its own species marker,
    see its docstring) -- it never KEYS its lookup on ``cas`` the way ``CasMatcher``
    does, so stripping the region token first still matters for it. ``LandUseMatcher``
    remains name-keyed in the strict sense despite its own internal class lookup -- it
    only ever reads ``flow.name``.
    """

    tier = "region"

    def __init__(self, inner: Sequence[Matcher]) -> None:
        """Build the matcher from an ordered sequence of name-keyed inner matchers."""
        self._inner = list(inner)

    def candidates(self, flow: BafuFlow, cas: str | None, index: EfFlowIndex) -> list[Candidate]:
        """Return candidates found for ``flow``'s name stem, tagged with the stripped region."""
        name = flow.name.rstrip()
        match = _REGION_RE.search(name)
        if match is None:
            return []
        token = match.group("token")
        stem = name[: match.start()]
        stem_flow = BafuFlow(stem, flow.category, flow.subcategory, flow.unit)
        location = token if _ISO2_RE.match(token) else None
        region = token if location is None else None
        for matcher in self._inner:
            found = matcher.candidates(stem_flow, cas, index)
            if found:
                tier = f"region/{matcher.tier}"
                return [replace(c, location=location, region=region, tier=tier) for c in found]
        return []


def _parse_alias_value(key: object, value: object, path: Path) -> Alias:
    if isinstance(value, str) and value.strip():
        return Alias(target=value.strip())
    if isinstance(value, dict):
        if set(value) - {"target", "caveat"}:
            raise ParseError(f"{path}: alias {key!r} has unknown keys {sorted(value)!r}")
        target = value.get("target")
        caveat = value.get("caveat")
        if (
            isinstance(target, str)
            and target.strip()
            and (caveat is None or (isinstance(caveat, str) and caveat.strip()))
        ):
            return Alias(target=target.strip(), caveat=caveat.strip() if caveat else None)
    raise ParseError(f"{path}: alias {key!r} has an invalid value {value!r}")


def load_aliases(path: Path = ALIASES_PATH) -> dict[str, Alias]:
    """Load the curated alias table, keyed by lowercase, stripped BAFU flow name.

    A YAML value is either a bare string (becomes a target-only ``Alias``) or a
    ``{target, caveat}`` mapping. Raises ``ParseError``, naming ``path``, when the
    top-level ``aliases`` key is missing, ``None``, or not a mapping, or when an
    entry's value is neither a non-empty string nor a well-formed ``{target, caveat}``
    mapping (a key with no value at all -- YAML ``null`` -- is exactly such a case).
    """
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    aliases = data.get("aliases") if isinstance(data, dict) else None
    if not isinstance(aliases, dict):
        raise ParseError(f"{path}: missing or malformed top-level 'aliases' mapping")
    return {
        str(key).strip().lower(): _parse_alias_value(key, value, path)
        for key, value in aliases.items()
    }
