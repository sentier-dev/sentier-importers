"""Matchers: one lookup strategy each, from a BAFU flow to EF candidate flows.

A matcher never places (sub-compartment) or disambiguates (several substances); it
returns every EF flow in the BAFU flow's compartment bucket that its key finds, sorted
by code. Placement and the choice among candidates are the pipeline's job.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
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


@dataclass(frozen=True)
class Candidate:
    """One EF flow a matcher proposes, tagged with provenance and any carried caveat."""

    flow: EfFlow
    location: str | None = None  # ISO-2 code carried from the source name
    region: str | None = None  # non-ISO region token carried from the source name
    tier: str | None = None  # the matcher (tier) that produced this candidate
    caveat: str | None = None  # a known caveat about this match, if any


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


class AliasMatcher:
    """Matches on a curated BAFU-name -> EF-preferred-label table, bucket-scoped.

    Accepts either plain ``str`` targets or ``Alias`` values; a plain string is wrapped
    into a target-only ``Alias``. When the matched alias carries a caveat, every
    candidate it produces carries that caveat too.
    """

    tier = "alias"

    def __init__(self, aliases: dict[str, str | Alias]) -> None:
        """Build the matcher from ``aliases`` (arbitrary case/whitespace keys)."""
        self._aliases = {
            k.strip().lower(): v if isinstance(v, Alias) else Alias(target=v)
            for k, v in aliases.items()
        }

    def candidates(self, flow: BafuFlow, cas: str | None, index: EfFlowIndex) -> list[Candidate]:
        """Return EF flows whose label equals the alias table's target for ``flow.name``."""
        alias = self._aliases.get(flow.name.strip().lower())
        if alias is None:
            return []
        found = _lookup(index.by_name(alias.target, _bucket(flow)), self.tier)
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

    ``inner`` must be name-keyed matchers only (``ExactNameMatcher``, ``SynonymMatcher``,
    ``QualifierMatcher``, ``AliasMatcher``): a ``CasMatcher`` inside would ignore the
    stripped stem and match on ``cas`` again, defeating the point of stripping.
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
