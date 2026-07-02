"""FoodEx2 bibliographic provenance → a single Sentier ``Source`` record.

Registered as its own ``foodex2-source`` block (category ``sources``) so re-running the
importer regenerates ``data/sources/foodex2.yaml`` rather than it being hand-maintained.
The IRI is shared with :mod:`sentier_importers.sources.foodex2.source`, which stamps it
onto every product's ``source`` slot.
"""

from sentier_importers.core.context import RunContext
from sentier_importers.core.source import Source
from sentier_importers.core.types import RawData, Records, Rows

#: The Source IRI products link to. Lives under the ``sources`` ConceptScheme.
PROVENANCE_IRI = "https://vocab.sentier.dev/sources/foodex2"

# --- Pinned EFSA primary source (the MTX = FoodEx2 catalogue) -------------------
# We import directly from EFSA's official distribution (openefsa/efsa-catalogues),
# NOT a third-party repackaging. The catalogue is pinned to a specific commit so a
# re-import is a deliberate, reviewed bump (see the 2026-06-23 MTX-primary-source spec).
#: Imported MTX catalogue version (``<catalogueVersion><version>``), validFrom 2026-04-28.
CATALOGUE_VERSION = "17.1"
#: Pinned commit of ``openefsa/efsa-catalogues`` holding that version.
CATALOGUE_COMMIT = "242ead56e40a29002a66d63fc64c6a497151af4b"
#: The pinned download URL for ``MTX.ecf`` (a ZIP wrapping ``MTX.xml``).
MTX_URL = (
    "https://raw.githubusercontent.com/openefsa/efsa-catalogues/"
    f"{CATALOGUE_COMMIT}/ecf_catalogues/MTX.ecf"
)

#: Static bibliographic metadata for the FoodEx2 (revision 2) publication.
PROVENANCE: dict = {
    "iri": PROVENANCE_IRI,
    "pref_label": "FoodEx2",
    "title": "The food classification and description system FoodEx2 (revision 2)",
    "creators": ["European Food Safety Authority (EFSA)"],
    "publisher": "European Food Safety Authority",
    "identifier": "10.2903/sp.efsa.2015.EN-804",
    "citation": (
        "EFSA, 2015. The food classification and description system FoodEx2 (revision 2). "
        "EFSA Supporting Publication 2015:EN-804. doi:10.2903/sp.efsa.2015.EN-804. "
        f"Imported from the EFSA MTX catalogue v{CATALOGUE_VERSION} "
        f"(openefsa/efsa-catalogues @ {CATALOGUE_COMMIT[:12]})."
    ),
    "status": "published",
}


class Foodex2Provenance(Source):
    """Emit one ``Source`` row describing the FoodEx2 dataset. Needs no fetch input."""

    def fetch(self, ctx: RunContext) -> RawData | None:
        return None  # static metadata; nothing to retrieve

    def parse(self, raw: RawData | None) -> Records:
        return []

    def transform(self, records: Records) -> Rows:
        return [dict(PROVENANCE)]
