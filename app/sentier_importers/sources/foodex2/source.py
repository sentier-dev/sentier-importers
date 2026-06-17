"""FoodEx2 (EFSA food classification) → multiple Sentier categories.

FoodEx2 is one file but six top-level "lists", which are different *kinds* of concept.
We route each term to the right Sentier category by its master-hierarchy list (the first
two ``hierarchyCode`` segments, ``Z0001.000X``):

    Food (Z0001.0001), Feed (Z0001.0002), Groups for hierarchies (Z0001.0005) → products
    Natural sources (Z0001.0003)                                              → organisms
    Facets (Z0001.0006), Non-food matrices (Z0001.0004)                       → qualifiers

Nothing is dropped — every term lands in some category. Source-specific field mapping:
- synonyms ← ``properties.commonNames`` (list) + ``properties.scientificNames`` (string);
- parentage ← ``properties.hierarchyCode`` (NOT ``relationships.parents``, which is empty);
- ``related`` ← ``properties.facets`` + ``implicitFacets`` (FoodEx2's descriptor links,
  resolved to whatever category each target lives in — these cross categories);
- ``additional_notations`` ← legacy/external codes (FoodEx1, GEMS, matrix) kept verbatim;
- the stable code is the FoodEx2 code (``id``, e.g. ``A000A``), used as ``notation``.
"""

from sentier_importers.core.types import Record, Records, Rows
from sentier_importers.sources.esfc.base import EsfcSource
from sentier_importers.sources.foodex2.provenance import PROVENANCE_IRI

# FoodEx2 master-hierarchy list (Z0001.000X) → Sentier category.
_LIST_CATEGORY = {
    "Z0001.0001": "products",  # Food
    "Z0001.0002": "products",  # Feed
    "Z0001.0003": "organisms",  # Natural sources (source taxa)
    "Z0001.0004": "qualifiers",  # Non-food matrices
    "Z0001.0005": "products",  # Groups for hierarchies (aggregation nodes)
    "Z0001.0006": "qualifiers",  # Facets (descriptors)
}

# Legacy/external identifier fields → notation prefix.
_LEGACY_CODES = (("foodexOldCode", "foodex1"), ("GEMSCode", "gems"), ("matrixCode", "matrix"))


class Foodex2Source(EsfcSource):
    """Normalize the esfc FoodEx2 export, routed across products/organisms/qualifiers."""

    source_prefix = "foodex2"
    provenance_iri = PROVENANCE_IRI

    def __init__(self, config) -> None:
        super().__init__(config)
        self._code_by_hierarchy: dict[str, str] = {}

    def classify(self, term: Record) -> str:
        hierarchy_code = self._hierarchy_code(term)
        if not hierarchy_code:
            return "products"
        list_prefix = ".".join(hierarchy_code.split(".")[:2])
        return _LIST_CATEGORY.get(list_prefix, "products")  # root (Z0001) → products

    @staticmethod
    def _hierarchy_code(term: Record) -> str | None:
        code = str((term.get("properties") or {}).get("hierarchyCode") or "").strip()
        return code or None

    def transform(self, records: Records) -> Rows:
        # Index hierarchyCode -> term id so broader() can resolve parents across the set.
        self._code_by_hierarchy = {
            hc: term["id"]
            for term in records
            if (hc := self._hierarchy_code(term)) and term.get("id")
        }
        return super().transform(records)

    def broader(self, term: Record) -> str | None:
        """Parent IRI, derived by truncating the term's ``hierarchyCode`` by one level."""
        hierarchy_code = self._hierarchy_code(term)
        if not hierarchy_code or "." not in hierarchy_code:
            return None  # root node
        parent_code = hierarchy_code.rsplit(".", 1)[0]
        parent_id = self._code_by_hierarchy.get(parent_code)
        return self.iri_for_code(parent_id) if parent_id else None

    def related(self, term: Record) -> list[str]:
        """FoodEx2 facet links → ``related`` IRIs (drop self-references and unknown codes)."""
        own_code = self.notation(term)
        properties = term.get("properties") or {}
        iris: list[str] = []
        seen: set[str] = set()
        for facet_key in ("facets", "implicitFacets"):
            facet_map = properties.get(facet_key) or {}
            if not isinstance(facet_map, dict):
                continue
            for value in facet_map.values():
                code = str(value).strip()
                if not code or code == own_code:
                    continue  # F27 etc. often points at the term itself
                iri = self.iri_for_code(code)
                if iri is not None and iri not in seen:
                    seen.add(iri)
                    iris.append(iri)
        return iris

    def additional_notations(self, term: Record) -> list[str]:
        metadata = term.get("metadata") or {}
        notations: list[str] = []
        for field, prefix in _LEGACY_CODES:
            value = str(metadata.get(field) or "").strip()
            if value:
                notations.append(f"{prefix}:{value}")
        return notations

    def alt_labels(self, term: Record) -> list[str]:
        labels = super().alt_labels(term)
        properties = term.get("properties") or {}

        common = properties.get("commonNames") or []
        if isinstance(common, str):
            common = [common]
        labels.extend(str(name).strip() for name in common if str(name).strip())

        scientific = str(properties.get("scientificNames") or "").strip()
        if scientific:
            labels.append(scientific)
        return labels
