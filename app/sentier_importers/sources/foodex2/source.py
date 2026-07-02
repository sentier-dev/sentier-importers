"""FoodEx2 (EFSA food classification) imported from the EFSA *primary* source.

The catalogue is EFSA's **MTX** (the FoodEx2 matrix), published at
``openefsa/efsa-catalogues`` as ``MTX.ecf`` — a ZIP wrapping a single ``MTX.xml``
(~95 MB). We pin a specific commit (catalogue v17.1) so a re-import is a deliberate,
reviewed bump rather than silent drift. This **replaces** the former third-party
``esfc-glossary`` pull (AGPL, opaque AI mapping). See
``docs/specs/2026-06-23-foodex2-mtx-primary-source.md``.

**Routing.** Each term's ``termType`` attribute decides its Sentier category:

    n           biological source taxa            -> organisms
    r d s c g   commodities & grouping nodes       -> products
    f           facet descriptors                  -> qualifiers

Nothing is dropped — every term lands in exactly one category. A term's IRI is
category-namespaced (``<root>/<category>/foodex2/<code>``), so a cross-reference
(``broader``, ``related``) can point into another category. ``transform`` therefore
builds a global ``code -> category`` index over *all* terms first, so any referenced
code resolves to the right namespace regardless of which slice is being emitted.

**Parity.** The emitted row shape is identical to the previous importer's: the same
slots, derived field-for-field from MTX (see the spec's mapping table). No schema or
downstream change — the rows still feed the Parquet/TTL pipeline unchanged.
"""

import io
import zipfile
from xml.etree import ElementTree as ET

from sentier_importers.core.dedup import slugify
from sentier_importers.core.source import Source
from sentier_importers.core.types import RawData, Record, Records, Rows
from sentier_importers.sources.foodex2.provenance import PROVENANCE_IRI

#: Root of every published Sentier IRI; a category scheme is ``<ROOT>/<category>/``.
IRI_ROOT = "https://vocab.sentier.dev"
#: IRI path segment that namespaces this source's codes.
SOURCE_PREFIX = "foodex2"

#: FoodEx2 ``termType`` -> Sentier category. Unknown types fall back to products.
_TERMTYPE_CATEGORY = {
    "n": "organisms",
    "r": "products",
    "d": "products",
    "s": "products",
    "c": "products",
    "g": "products",
    "f": "qualifiers",
}

#: MTX term ``status`` -> Sentier ``status_enum`` (unknown -> slot omitted).
_STATUS_MAP = {"APPROVED": "draft", "PRELIMINARY": "draft", "DEPRECATED": "deprecated"}

#: Legacy/external identifier attributes -> ``additional_notations`` prefix.
_LEGACY_CODES = (("foodexOldCode", "foodex1"), ("GEMSCode", "gems"), ("matrixCode", "matrix"))

#: The hierarchy whose ``parentCode`` gives the single SKOS ``broader`` (master tree).
_MASTER_HIERARCHY = "MTX"

#: Implicit-attribute codes carrying facet links (``<own>#F01.CODE$F02.CODE...``).
_FACET_ATTRS = ("allFacets", "implicitFacets")


def _text(parent: ET.Element | None, tag: str) -> str | None:
    """Stripped text of ``parent``'s ``tag`` child, or ``None`` if absent/empty."""
    if parent is None:
        return None
    child = parent.find(tag)
    if child is None or child.text is None:
        return None
    text = child.text.strip()
    return text or None


def _facet_value_codes(spec: str) -> list[str]:
    """Parse the facet-link string ``A0CTY#F01.A04TB$F02.A0EME`` -> term codes.

    The leading ``<ownCode>#`` is dropped; each ``Fxx.CODE`` pair contributes ``CODE``.
    """
    body = spec.split("#", 1)[1] if "#" in spec else spec
    codes: list[str] = []
    for segment in body.split("$"):
        if "." in segment:
            codes.append(segment.split(".", 1)[1].strip())
    return codes


def _record_from_term(term: ET.Element) -> Record:
    """Flatten one ``<term>`` element into a compact dict.

    Extracting eagerly lets the caller ``clear()`` the heavy XML subtree immediately,
    keeping memory flat while streaming the ~95 MB document.
    """
    desc = term.find("termDesc")
    attrs: dict[str, list[str]] = {}
    for ia in term.findall("implicitAttributes/implicitAttribute"):
        code = _text(ia, "attributeCode")
        if not code:
            continue
        values = [
            v.text.strip()
            for v in ia.findall("attributeValues/attributeValue")
            if v.text and v.text.strip()
        ]
        if values:
            attrs[code] = values
    parent_code: str | None = None
    for ha in term.findall("hierarchyAssignments/hierarchyAssignment"):
        if _text(ha, "hierarchyCode") == _MASTER_HIERARCHY:
            parent_code = _text(ha, "parentCode")
            break
    return {
        "termCode": _text(desc, "termCode"),
        "termExtendedName": _text(desc, "termExtendedName"),
        "termShortName": _text(desc, "termShortName"),
        "termScopeNote": _text(desc, "termScopeNote"),
        "status": _text(term.find("termVersion"), "status"),
        "parentCode": parent_code,
        "attrs": attrs,
    }


class Foodex2Source(Source):
    """Map EFSA's MTX (FoodEx2) catalogue into Sentier SKOS ``Concept`` rows."""

    def __init__(self, config) -> None:
        super().__init__(config)
        self._category_index: dict[str, str] = {}  # code -> category, built in transform

    # --- fetch/parse ----------------------------------------------------------
    # ``fetch`` is the framework default: a cached download of ``config.fetch_url``
    # (the pinned ``MTX.ecf``). Only ``parse`` is source-specific.

    def parse(self, raw: RawData) -> Records:
        """Unzip the ``.ecf`` and stream every ``<term>`` out of ``MTX.xml``."""
        with zipfile.ZipFile(io.BytesIO(raw.content)) as archive:
            xml_name = next(n for n in archive.namelist() if n.lower().endswith(".xml"))
            with archive.open(xml_name) as handle:
                records: Records = []
                for _event, elem in ET.iterparse(handle, events=("end",)):
                    if elem.tag == "term":
                        records.append(_record_from_term(elem))
                        elem.clear()
                return records

    # --- routing --------------------------------------------------------------
    def classify(self, term: Record) -> str:
        """Sentier category for ``term``, from its ``termType`` attribute."""
        term_type = (term.get("attrs") or {}).get("termType")
        code = term_type[0] if term_type else None
        return _TERMTYPE_CATEGORY.get(code, "products")

    # --- field hooks ----------------------------------------------------------
    def notation(self, term: Record) -> str | None:
        return term.get("termCode") or None

    def pref_label(self, term: Record) -> str:
        return (term.get("termExtendedName") or "").strip()

    def definition(self, term: Record) -> str | None:
        """Scope note with the trailing ``£``-delimited source URLs stripped off."""
        note = term.get("termScopeNote")
        if not note:
            return None
        text = note.split("£", 1)[0].strip()
        return text or None

    def alt_labels(self, term: Record) -> list[str]:
        """Synonyms: short name + common names (A02) + scientific names (A01)."""
        attrs = term.get("attrs") or {}
        labels: list[str] = []
        if short := term.get("termShortName"):
            labels.append(short)
        labels.extend(attrs.get("A02", []))
        labels.extend(attrs.get("A01", []))
        return labels

    def broader(self, term: Record) -> str | None:
        """Parent IRI, from the master-hierarchy ``parentCode`` (``None`` if unresolved)."""
        parent = term.get("parentCode")
        return self.iri_for_code(parent) if parent else None

    def related(self, term: Record) -> list[str]:
        """FoodEx2 facet links -> ``related`` IRIs (drop self-refs and unknown codes)."""
        own_code = self.notation(term)
        attrs = term.get("attrs") or {}
        iris: list[str] = []
        seen: set[str] = set()
        for attr in _FACET_ATTRS:
            for spec in attrs.get(attr, []):
                for code in _facet_value_codes(spec):
                    if not code or code == own_code:
                        continue  # F27 (source-commodity) often points at the term itself
                    iri = self.iri_for_code(code)
                    if iri is not None and iri not in seen:
                        seen.add(iri)
                        iris.append(iri)
        return iris

    def additional_notations(self, term: Record) -> list[str]:
        """Legacy/external identifiers kept verbatim with a namespace prefix."""
        attrs = term.get("attrs") or {}
        notations: list[str] = []
        for field, prefix in _LEGACY_CODES:
            for value in attrs.get(field, []):
                notations.append(f"{prefix}:{value}")
        return notations

    # --- IRI minting ----------------------------------------------------------
    def iri_for_code(self, code: str | None) -> str | None:
        """Category-namespaced IRI for a code, or ``None`` if the code is unknown."""
        category = self._category_index.get(code or "")
        if category is None:
            return None
        return f"{IRI_ROOT}/{category}/{SOURCE_PREFIX}/{code}"

    def make_iri(self, term: Record) -> str:
        """Stable IRI: code-based when available, else a label slug."""
        code = self.notation(term)
        if code and (iri := self.iri_for_code(code)) is not None:
            return iri
        return f"{IRI_ROOT}/{self.classify(term)}/{SOURCE_PREFIX}/{slugify(self.pref_label(term))}"

    # --- transform ------------------------------------------------------------
    @staticmethod
    def _dedupe_keep_order(values: list[str]) -> list[str]:
        seen: set[str] = set()
        out: list[str] = []
        for value in values:
            key = value.casefold()
            if value and key not in seen:
                seen.add(key)
                out.append(value)
        return out

    def transform(self, records: Records) -> Rows:
        """Route each term, then mint a ``Concept``-shaped row for the matching slice.

        Optional slots are emitted only when present so closed-world LinkML validation
        passes. Terms without a ``pref_label`` (schema-required) are skipped.
        """
        self._category_index = {
            code: self.classify(term)
            for term in records
            if (code := self.notation(term)) is not None
        }
        rows: Rows = []
        for term in records:
            if self.classify(term) != self.config.category:
                continue
            label = self.pref_label(term)
            if not label:
                continue

            row: Record = {"iri": self.make_iri(term), "pref_label": label}
            if (definition := self.definition(term)) is not None:
                row["definition"] = definition
            alt = [
                value
                for value in self._dedupe_keep_order(self.alt_labels(term))
                if value.casefold() != label.casefold()
            ]
            if alt:
                row["alt_labels"] = alt
            if (notation := self.notation(term)) is not None:
                row["notation"] = notation
            if extra := self._dedupe_keep_order(self.additional_notations(term)):
                row["additional_notations"] = extra
            if (broader := self.broader(term)) is not None:
                row["broader"] = broader
            if related := self.related(term):
                row["related"] = related
            if status := _STATUS_MAP.get((term.get("status") or "").strip().upper()):
                row["status"] = status
            row["source"] = PROVENANCE_IRI
            rows.append(row)
        return rows
