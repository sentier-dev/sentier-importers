"""EF 3.1 flow-list bibliographic provenance → a single Sentier ``Source`` record.

The harmonised elementary-flow list imported by :mod:`agribalyse.flows` is the
European Commission / JRC Environmental Footprint 3.1 reference package. This
module emits the one ``Source`` term every flow links to via its ``source`` slot,
and pins the URL of the published, license-free flow artifact.
"""

from sentier_importers.core.context import RunContext
from sentier_importers.core.source import Source
from sentier_importers.core.types import RawData, Records, Rows

#: The Source IRI every imported flow links to. Lives under the ``sources`` scheme.
PROVENANCE_IRI = "https://vocab.sentier.dev/sources/ef-3.1"

#: Pinned download URL of the license-free harmonised flow list (see plan Task 0).
HARMONISED_FLOWS_URL = (
    "https://github.com/sentier-dev/sentier-importers/releases/download/"
    "agribalyse-flows-v1/harmonised-flows-simple.json.gz"
)

#: Static bibliographic metadata for the EF 3.1 reference package.
PROVENANCE: dict = {
    "iri": PROVENANCE_IRI,
    "pref_label": "Environmental Footprint 3.1",
    "title": "Environmental Footprint (EF) 3.1 method and reference elementary flows",
    "creators": ["European Commission, Joint Research Centre (JRC)"],
    "publisher": "European Commission, Joint Research Centre",
    "citation": (
        "European Commission, Joint Research Centre. Environmental Footprint (EF) 3.1. "
        "Harmonised elementary-flow list extracted from the EF 3.1 reference package via "
        "dds-agribalyse; nomenclature only, no LCI data."
    ),
    "status": "published",
}


class AgribalyseEf31Provenance(Source):
    """Emit one ``Source`` row describing the EF 3.1 flow list. Needs no fetch input."""

    def fetch(self, ctx: RunContext) -> RawData | None:
        return None  # static metadata; nothing to retrieve

    def parse(self, raw: RawData | None) -> Records:
        return []

    def transform(self, records: Records) -> Rows:
        return [dict(PROVENANCE)]
