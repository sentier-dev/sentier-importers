"""Import the Agribalyse 3.2 process catalog into ``sentier_vocab`` (processes).

Each CIQUAL product has one associated LCI process (its "LCI Name"). We describe those
processes as ``Process`` terms — **nomenclature only** (name, code, type; no geography IRI
and no exchanges, since the exchange amounts are the licensed LCI). One process per AGB code,
linked to its product term via ``related``.
"""

from sentier_importers.core.source import Source
from sentier_importers.core.types import RawData, Record, Records, Rows
from sentier_importers.sources.agribalyse.agribalyse_provenance import AGRIBALYSE_PROVENANCE_IRI
from sentier_importers.sources.agribalyse.products import parse_reference_synthese, product_iri

#: Base of every published Sentier process IRI.
PROCESSES_SCHEME = "https://vocab.sentier.dev/processes/"
#: IRI path segment namespacing this source's codes.
SOURCE_PREFIX = "agribalyse"


def process_iri(agb_code: str) -> str:
    """IRI for an Agribalyse process, keyed on its AGB code."""
    return f"{PROCESSES_SCHEME}{SOURCE_PREFIX}/{agb_code}"


class AgribalyseProcessesSource(Source):
    """Map each Agribalyse product's LCI process into a ``Process`` term."""

    def parse(self, raw: RawData) -> Records:
        return parse_reference_synthese(raw)

    def transform(self, records: Records) -> Rows:
        rows: Rows = []
        seen: set[str] = set()
        for rec in records:
            agb = rec.get("agb") or ""
            lci = rec.get("lci_name") or ""
            name_fr = rec.get("name_fr") or ""
            label = lci or name_fr  # prefer the English LCI process name
            if not agb or not label:
                continue
            iri = process_iri(agb)
            if iri in seen:
                continue
            seen.add(iri)

            row: Record = {
                "iri": iri,
                "pref_label": label,
                "notation": rec.get("ciqual") or agb,
                "process_type": "lci_result",
                "related": [product_iri(agb)],
                "additional_notations": [f"agb:{agb}"],
                "source": AGRIBALYSE_PROVENANCE_IRI,
                "status": "draft",
            }
            if name_fr and name_fr.casefold() != label.casefold():
                row["alt_labels"] = [name_fr]
            rows.append(row)
        return rows
