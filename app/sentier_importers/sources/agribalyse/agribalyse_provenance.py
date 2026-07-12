"""Agribalyse 3.2 bibliographic provenance → a single Sentier ``Source`` record.

Registered as its own ``agribalyse-source`` block (category ``sources``). The IRI is
shared with :mod:`agribalyse.products`, which stamps it onto every product's ``source``
slot. One Source subclass per module (registry contract).
"""

from sentier_importers.core.context import RunContext
from sentier_importers.core.source import Source
from sentier_importers.core.types import RawData, Records, Rows

#: The Source IRI every imported Agribalyse product links to.
AGRIBALYSE_PROVENANCE_IRI = "https://vocab.sentier.dev/sources/agribalyse-3.2"

#: Static bibliographic metadata for the ADEME Agribalyse 3.2 reference release.
AGRIBALYSE_PROVENANCE: dict = {
    "iri": AGRIBALYSE_PROVENANCE_IRI,
    "pref_label": "AGRIBALYSE 3.2",
    "title": "AGRIBALYSE 3.2 — French agricultural and food LCI database",
    "creators": ["ADEME (Agence de la transition écologique)"],
    "publisher": "ADEME",
    "citation": (
        "ADEME. AGRIBALYSE 3.2. Reference synthese (product impact scores) published as "
        "French open data (Licence Ouverte / Etalab) via data.gouv.fr. Product nomenclature "
        "(CIQUAL codes, French names, food groups) imported; no LCI amounts."
    ),
    "status": "published",
}


class AgribalyseProvenance(Source):
    """Emit one ``Source`` row describing the Agribalyse 3.2 dataset. No fetch input."""

    def fetch(self, ctx: RunContext) -> RawData | None:
        return None  # static metadata; nothing to retrieve

    def parse(self, raw: RawData | None) -> Records:
        return []

    def transform(self, records: Records) -> Rows:
        return [dict(AGRIBALYSE_PROVENANCE)]
