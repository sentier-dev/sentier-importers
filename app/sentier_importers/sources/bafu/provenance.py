"""BAFU-2026 bibliographic provenance -> a single Sentier ``Source`` record.

Every BAFU vocab term links here via its ``source`` slot.
"""

from sentier_importers.core.context import RunContext
from sentier_importers.core.source import Source
from sentier_importers.core.types import RawData, Records, Rows
from sentier_importers.sources.bafu.ecospold import CITATION

#: The Source IRI every imported BAFU term links to.
BAFU_PROVENANCE_IRI = "https://vocab.sentier.dev/sources/bafu-2026"

#: Static bibliographic metadata for the BAFU:2026 v1 release.
BAFU_PROVENANCE: dict = {
    "iri": BAFU_PROVENANCE_IRI,
    "pref_label": "BAFU:2026",
    "title": "Life Cycle Inventory database of the Swiss Federal Administration, BAFU:2026 (v1)",
    "creators": ["Swiss Federal Office for the Environment (BAFU/FOEN)"],
    "publisher": "Swiss Federal Office for the Environment (BAFU/FOEN)",
    "citation": f"{CITATION}. Version 1, EcoSpold v1 export (11,947 datasets).",
    "status": "published",
}


class BafuProvenance(Source):
    """Emit one ``Source`` row describing the BAFU:2026 database. No fetch input."""

    def fetch(self, ctx: RunContext) -> RawData | None:
        return None  # static metadata; nothing to retrieve

    def parse(self, raw: RawData | None) -> Records:
        return []

    def transform(self, records: Records) -> Rows:
        return [dict(BAFU_PROVENANCE)]
