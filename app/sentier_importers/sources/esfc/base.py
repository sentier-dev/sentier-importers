"""Shared base for importers of the esfc *LCA Food Glossary* export.

The glossary (https://gitlab.com/eaternity/esfc-glossary) publishes one JSON file per
source, each shaped as ``{"metadata": {...}, "terms": [ {term}, ... ]}``. Every term
shares a common envelope (``id``, ``name``, ``description``, ``status``,
``properties``, ``relationships`` ...), so the field-mapping into a Sentier SKOS
``Concept`` row lives here once. Per-source plugins override the small hooks that
differ (synonym fields, category routing).

**Per-term routing.** ``classify(term)`` returns the Sentier *category* a term belongs
to (``products``, ``organisms``, ``qualifiers`` ...). ``transform`` keeps only the terms
whose category equals this source's declared ``config.category`` and mints rows for them.
A source that spans several categories is registered once per category; each block
re-uses the cached fetch and filters to its own slice. Because a term's IRI is
category-namespaced (``<base>/<category>/<source>/<code>``), a cross-reference (``broader``,
``related``) may point into another category — so ``transform`` first builds a global
``code → category`` index from *all* terms, and :meth:`iri_for_code` resolves any code
to the right namespace regardless of which slice is being emitted.

The framework supplies dedup → validate → emit → deliver around the rows produced here.
"""

from sentier_importers.core import parse as parse_mod
from sentier_importers.core.dedup import slugify
from sentier_importers.core.source import Source
from sentier_importers.core.types import Record, Records, Rows

#: Root of every published Sentier IRI; a category scheme is ``<ROOT>/<category>/``.
IRI_ROOT = "https://vocab.sentier.dev"

# esfc ``status`` → Sentier ``status_enum``. Unknown values drop the slot (it is
# optional) rather than guess.
_STATUS_MAP = {"active": "draft", "deprecated": "deprecated"}


class EsfcSource(Source):
    """Map an esfc glossary export into Sentier SKOS ``Concept`` rows.

    Subclasses set :attr:`source_prefix` (the IRI path segment that namespaces this
    source's codes) and override :meth:`classify` plus any field hook that differs.
    """

    #: IRI path segment under the collection scheme, e.g. ``products/<source_prefix>/<code>``.
    source_prefix = "esfc"

    #: IRI of the bibliographic Source (in data/sources) every emitted term links to via
    #: its ``source`` slot. ``None`` leaves provenance implicit in the IRI namespace.
    provenance_iri: str | None = None

    def __init__(self, config) -> None:
        super().__init__(config)
        self._category_index: dict[str, str] = {}  # code → category, built in transform

    def parse(self, raw):
        """Unwrap the ``{metadata, terms: [...]}`` envelope to the list of terms.

        The default JSON parser wraps a top-level object as a single record, so a
        ``terms`` array surfaces as ``[{...whole file...}]`` — flatten it back out.
        """
        records = parse_mod.parse(raw, self.config.fetch_format)
        if len(records) == 1 and isinstance(records[0], dict) and "terms" in records[0]:
            return records[0]["terms"]
        return records

    # --- routing --------------------------------------------------------------
    def classify(self, term: Record) -> str:
        """Return the Sentier category for ``term``. Default: this source's category."""
        return self.config.category

    # --- field hooks (override per source) ------------------------------------
    def notation(self, term: Record) -> str | None:
        """SKOS notation — the source's own stable code (``id``)."""
        code = str(term.get("id") or "").strip()
        return code or None

    def pref_label(self, term: Record) -> str:
        return str(term.get("name") or "").strip()

    def definition(self, term: Record) -> str | None:
        text = str(term.get("description") or "").strip()
        return text or None

    def alt_labels(self, term: Record) -> list[str]:
        """Synonyms. Base contributes a non-redundant ``shortName``; sources add more."""
        short = str(term.get("shortName") or "").strip()
        if short and short.casefold() != self.pref_label(term).casefold():
            return [short]
        return []

    def broader(self, term: Record) -> str | None:
        """Parent IRI, minted from the first declared parent code (same scheme)."""
        parents = (term.get("relationships") or {}).get("parents") or []
        return self.iri_for_code(str(parents[0])) if parents else None

    def related(self, term: Record) -> list[str]:
        rel = (term.get("relationships") or {}).get("related") or []
        return [iri for code in rel if (iri := self.iri_for_code(str(code))) is not None]

    def additional_notations(self, term: Record) -> list[str]:
        """Source-side identifiers / legacy codes to preserve verbatim (prefixed)."""
        return []

    # --- IRI minting ----------------------------------------------------------
    def iri_for_code(self, code: str) -> str | None:
        """Category-namespaced IRI for a source code, or ``None`` if the code is unknown.

        The category comes from the global index built in :meth:`transform`, so a code
        resolves to the right namespace even when referenced from another category's slice.
        """
        category = self._category_index.get(code)
        if category is None:
            return None
        return f"{IRI_ROOT}/{category}/{self.source_prefix}/{code}"

    def make_iri(self, term: Record) -> str:
        """Stable IRI for ``term``: code-based when available, else a label slug."""
        code = self.notation(term)
        if code and (iri := self.iri_for_code(code)) is not None:
            return iri
        category = self.classify(term)
        return f"{IRI_ROOT}/{category}/{self.source_prefix}/{slugify(self.pref_label(term))}"

    # --- transform ------------------------------------------------------------
    def _dedupe_keep_order(self, values: list[str]) -> list[str]:
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
        # Index every code → category first so cross-category references resolve.
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
            # Synonyms equal to the preferred label are redundant — drop them.
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
            if status := _STATUS_MAP.get(str(term.get("status") or "").strip().lower()):
                row["status"] = status
            if self.provenance_iri is not None:
                row["source"] = self.provenance_iri
            rows.append(row)
        return rows
