"""Reader for Brightway Flows' ``harmonised-flows-simple.json.gz`` (Zenodo 23554066,
version 1.1.0, ODbL 1.0, Chris Mutel).

The file holds a consensus list of elementary flows and, per source list, one link
from each source flow to a flow of that list. 93,700 consensus flows are the EF 3.1
flows under their EF UUID; the rest are flows EF does not have. A link whose target
is an EF flow is a "source flow to EF 3.1 flow" link, which is what sentier-mappings
pairs hold.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import zlib
from collections.abc import Mapping
from dataclasses import dataclass

from sentier_importers.core.errors import ParseError

#: sha256 of the 1.1.0 file. Another file is another release and must be reviewed.
PINNED_SHA256 = "47850016a251ca00d4f806f305293eaa735e3cb59219ea2a27ef7ceb12028271"
RELEASE = "brightway-flows 1.1.0"
EF_LIST = "ef-3.1"
_EF_SOURCE_LABEL = "EF 3.1"
_MATCHES = ("exactMatch", "closeMatch", "broadMatch")


def _tail(iri: str) -> str:
    return iri.rsplit("/", 1)[-1]


@dataclass(frozen=True)
class ConsensusFlow:
    identifier: str
    label: str
    context_code: str
    unit: str
    is_ef: bool


@dataclass(frozen=True)
class Link:
    source_id: str
    label: str
    context: str
    unit: str
    match: str
    target_id: str
    multiplier: float | None = None
    basis: str | None = None


@dataclass(frozen=True)
class BrightwayFlows:
    flows: Mapping[str, ConsensusFlow]
    links: Mapping[str, tuple[Link, ...]]
    redirects: Mapping[str, str]
    ef_alias: Mapping[str, str]

    def resolve(self, identifier: str) -> str:
        """The live consensus identifier for an EF UUID or a consensus identifier:
        follows EF flows merged into another one, then redirects. An identifier
        the file does not know is returned unchanged."""
        current = identifier
        for _ in range(8):
            if current in self.flows:
                return current
            following = self.ef_alias.get(current) or self.redirects.get(current)
            if following is None:
                return current
            current = following
        return current


def _link(assoc: dict) -> Link:
    source = assoc["xkos:sourceConcept"]
    match = next(m for m in _MATCHES if f"skos:{m}" in source)
    multiplier = assoc.get("qudt:conversionMultiplier")
    return Link(
        source_id=_tail(source["@id"]),
        label=source["skos:prefLabel"],
        context=source["context"],
        unit=_tail(source["qudt:hasUnit"]["@id"]),
        match=match,
        target_id=_tail(assoc["xkos:targetConcept"]["@id"]),
        multiplier=float(multiplier) if multiplier is not None else None,
        basis=assoc.get("brightway:conversionBasis"),
    )


def load(content: bytes, expected_sha256: str | None = PINNED_SHA256) -> BrightwayFlows:
    """Parse the gzipped file. ``expected_sha256=None`` skips the release check (tests)."""
    if expected_sha256 is not None:
        digest = hashlib.sha256(content).hexdigest()
        if digest != expected_sha256:
            raise ParseError(
                f"Brightway Flows file has sha256 {digest}, expected {expected_sha256} "
                f"({RELEASE}); a different release needs a review before it is used"
            )
    try:
        doc = json.loads(gzip.decompress(content))
        flows = {
            f["identifier"]: ConsensusFlow(
                identifier=f["identifier"],
                label=f["prefLabel"],
                context_code=_tail(f["context_iri"]),
                unit=f["unit"],
                is_ef=f["source"] == _EF_SOURCE_LABEL,
            )
            for f in doc["flows"]
        }
        redirects = {
            r["identifier"]: r["replaced_by_identifier"]
            for r in doc["redirects"]
            if r.get("replaced_by_identifier")
        }
        links = {
            _tail(c["@id"]): tuple(_link(a) for a in c["xkos:madeOf"])
            for c in doc["correspondences"]
        }
    except (OSError, EOFError, zlib.error, ValueError, KeyError, TypeError, StopIteration) as exc:
        raise ParseError(f"not a Brightway Flows harmonised-flows-simple file: {exc!r}") from exc
    ef_alias = {
        item.source_id: item.target_id
        for item in links.get(EF_LIST, ())
        if item.source_id != item.target_id
    }
    return BrightwayFlows(flows=flows, links=links, redirects=redirects, ef_alias=ef_alias)
