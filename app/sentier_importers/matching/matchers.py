"""Matchers: one lookup strategy each, from a BAFU flow to EF candidate flows.

A matcher never places (sub-compartment) or disambiguates (several substances); it
returns every EF flow in the BAFU flow's compartment bucket that its key finds, sorted
by code. Placement and the choice among candidates are the pipeline's job.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import yaml
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
    """One EF flow a matcher proposes, plus any region token carried from the source name."""

    flow: EfFlow
    location: str | None = None  # ISO-2 code carried from the source name
    region: str | None = None  # non-ISO region token carried from the source name


class Matcher(Protocol):
    """A single matching strategy over one BAFU flow."""

    tier: str

    def candidates(self, flow: BafuFlow, cas: str | None, index: EfFlowIndex) -> list[Candidate]:
        """Return every EF flow this strategy finds for ``flow``, sorted by code."""
        ...


def _wrap(flows: Sequence[EfFlow]) -> list[Candidate]:
    return [Candidate(f) for f in sorted(flows, key=lambda f: f.code)]


class ExactNameMatcher:
    """Matches on the BAFU flow name against the EF preferred label, bucket-scoped."""

    tier = "name"

    def candidates(self, flow: BafuFlow, cas: str | None, index: EfFlowIndex) -> list[Candidate]:
        """Return EF flows whose preferred label equals ``flow.name`` in ``flow``'s bucket."""
        bucket = bucket_of_bafu_category(flow.category)
        return _wrap(index.by_name(flow.name, bucket))


class SynonymMatcher:
    """Matches on the BAFU flow name against EF synonyms (``alt_labels``), bucket-scoped."""

    tier = "synonym"

    def candidates(self, flow: BafuFlow, cas: str | None, index: EfFlowIndex) -> list[Candidate]:
        """Return EF flows whose synonym equals ``flow.name`` in ``flow``'s bucket."""
        bucket = bucket_of_bafu_category(flow.category)
        return _wrap(index.by_synonym(flow.name, bucket))


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
        bucket = bucket_of_bafu_category(flow.category)
        return _wrap(index.by_cas(cas, bucket))


class QualifierMatcher:
    """Rewrites a BAFU comma-qualifier (``, biogenic`` / ``, fossil`` / ...) to EF spelling.

    EF spells qualifiers in parentheses (``Carbon dioxide (biogenic)``), calls the
    biogenic class ``biogenic`` (never BAFU's ``non-fossil``), and land transformation
    ``land use change`` (never BAFU's ``land transformation``).
    """

    tier = "qualifier"

    def candidates(self, flow: BafuFlow, cas: str | None, index: EfFlowIndex) -> list[Candidate]:
        """Return EF flows whose label is ``flow.name`` rewritten to EF qualifier spelling."""
        match = _QUALIFIER_RE.match(flow.name)
        if match is None:
            return []
        stem = match.group("stem")
        qualifier = match.group("q").lower()
        qualifier = _QUALIFIER_EF.get(qualifier, qualifier)
        bucket = bucket_of_bafu_category(flow.category)
        return _wrap(index.by_name(f"{stem} ({qualifier})", bucket))


class AliasMatcher:
    """Matches on a curated BAFU-name -> EF-preferred-label table, bucket-scoped."""

    tier = "alias"

    def __init__(self, aliases: dict[str, str]) -> None:
        """Build the matcher from ``aliases`` (arbitrary case/whitespace keys and values)."""
        self._aliases = {k.strip().lower(): v for k, v in aliases.items()}

    def candidates(self, flow: BafuFlow, cas: str | None, index: EfFlowIndex) -> list[Candidate]:
        """Return EF flows whose label equals the alias table's target for ``flow.name``."""
        target = self._aliases.get(flow.name.strip().lower())
        if target is None:
            return []
        bucket = bucket_of_bafu_category(flow.category)
        return _wrap(index.by_name(target, bucket))


class RegionStripMatcher:
    """Strips a trailing region token from the flow name and retries with inner matchers.

    BAFU sometimes bakes a region into the flow name (``Water, KR``, ``Water, Europe``,
    ``Water, river, CH``). This matcher recognises a trailing comma-separated region
    token (an ISO-3166-1 alpha-2 code, or a small set of named non-ISO regions such as
    ``Europe``/``RER``/``GLO``), strips it, runs ``inner`` matchers in turn against the
    stem name, and re-wraps the first non-empty result with the stripped token recorded
    as ``location`` (ISO-2) or ``region`` (anything else). No token, no match: ``[]``.
    """

    tier = "region"

    def __init__(self, inner: Sequence[Matcher]) -> None:
        """Build the matcher from an ordered sequence of inner matchers to retry with."""
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
                return [Candidate(c.flow, location=location, region=region) for c in found]
        return []


def load_aliases(path: Path = ALIASES_PATH) -> dict[str, str]:
    """Load the curated alias table, keyed by lowercase, stripped BAFU flow name."""
    data = yaml.safe_load(path.read_text())
    return {str(k).strip().lower(): str(v) for k, v in data["aliases"].items()}
