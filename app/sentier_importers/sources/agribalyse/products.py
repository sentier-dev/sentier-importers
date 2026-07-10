"""Import Agribalyse 3.2 (CIQUAL) products into ``sentier_vocab``.

Source: the ADEME *reference synthese* workbook (``AGRIBALYSE3.2_reference_synthese_raw``),
published as French open data (Licence Ouverte / Etalab). We import only the product
**nomenclature** — CIQUAL/AGB codes, French names, and the food-group hierarchy — never
the impact scores or any LCI amounts.

The raw parquet carries a two-row preamble; the real column header is at row index 2 and
data starts at row 3 (see the ship manifest). A three-level SKOS hierarchy is emitted:
food group → sub-group → product, linked by ``broader``.
"""

import io

import pyarrow.parquet as pq
from sentier_importers.core.dedup import slugify
from sentier_importers.core.source import Source
from sentier_importers.core.types import RawData, Record, Records, Rows
from sentier_importers.sources.agribalyse.agribalyse_provenance import AGRIBALYSE_PROVENANCE_IRI

#: Base of every published Sentier product IRI.
PRODUCTS_SCHEME = "https://vocab.sentier.dev/products/"
#: IRI path segment namespacing this source's codes.
SOURCE_PREFIX = "agribalyse"

#: Normalized header label -> record field (header cells contain embedded newlines).
_FIELDS = {
    "Code AGB": "agb",
    "Code CIQUAL": "ciqual",
    "Groupe d'aliment": "group",
    "Sous-groupe d'aliment": "subgroup",
    "Nom du Produit en Français": "name_fr",
    "LCI Name": "lci_name",
}
#: Row index of the real column header inside the raw parquet.
_HEADER_ROW = 2
#: First data row.
_DATA_START = 3


def _norm(value: object) -> str:
    """Collapse whitespace (headers embed newlines) and strip."""
    return " ".join(str(value).split()) if value is not None else ""


def group_iri(name: str) -> str:
    """IRI for a food-group grouping term."""
    return f"{PRODUCTS_SCHEME}{SOURCE_PREFIX}/group/{slugify(name)}"


def subgroup_iri(name: str) -> str:
    """IRI for a food-subgroup grouping term."""
    return f"{PRODUCTS_SCHEME}{SOURCE_PREFIX}/subgroup/{slugify(name)}"


def product_iri(agb_code: str) -> str:
    """IRI for a product, keyed on its AGB code."""
    return f"{PRODUCTS_SCHEME}{SOURCE_PREFIX}/{agb_code}"


class AgribalyseProductsSource(Source):
    """Map the Agribalyse reference synthese into ``Product`` SKOS rows."""

    def parse(self, raw: RawData) -> Records:
        """Read the parquet, take the header from row 2, and stream data rows."""
        table = pq.read_table(io.BytesIO(raw.content))
        data = table.to_pydict()
        columns = list(data.keys())
        length = len(data[columns[0]]) if columns else 0
        label_to_col = {
            _norm(data[col][_HEADER_ROW]): col for col in columns if _HEADER_ROW < length
        }
        records: Records = []
        for i in range(_DATA_START, length):
            record = {
                field: _norm(data[label_to_col[label]][i])
                for label, field in _FIELDS.items()
                if label in label_to_col
            }
            records.append(record)
        return records

    def transform(self, records: Records) -> Rows:
        rows: Rows = []
        seen_iris: set[str] = set()

        def add(row: Record) -> None:
            if row["iri"] not in seen_iris:
                seen_iris.add(row["iri"])
                rows.append(row)

        for rec in records:
            group = rec.get("group") or ""
            subgroup = rec.get("subgroup") or ""
            agb = rec.get("agb") or ""
            name = rec.get("name_fr") or ""
            if not agb or not name:
                continue

            if group:
                add(
                    {
                        "iri": group_iri(group),
                        "pref_label": group,
                        "source": AGRIBALYSE_PROVENANCE_IRI,
                        "status": "draft",
                    }
                )
            if subgroup:
                sub_row: Record = {
                    "iri": subgroup_iri(subgroup),
                    "pref_label": subgroup,
                    "source": AGRIBALYSE_PROVENANCE_IRI,
                    "status": "draft",
                }
                if group:
                    sub_row["broader"] = group_iri(group)
                add(sub_row)

            product: Record = {
                "iri": product_iri(agb),
                "pref_label": name,
                "notation": rec.get("ciqual") or agb,
                "source": AGRIBALYSE_PROVENANCE_IRI,
                "status": "draft",
            }
            lci = rec.get("lci_name") or ""
            if lci and lci.casefold() != name.casefold():
                product["alt_labels"] = [lci]
            extra = [f"agb:{agb}"]
            if rec.get("ciqual") and rec["ciqual"] != agb:
                extra.append(f"ciqual:{rec['ciqual']}")
            product["additional_notations"] = extra
            if subgroup:
                product["broader"] = subgroup_iri(subgroup)
            add(product)
        return rows
