"""Import the EF 3.1 harmonised elementary flows into ``sentier_vocab``.

Reads the license-free ``harmonised-flows-simple.json.gz`` (EF 3.1 = JRC public,
nomenclature only) and maps each ``source == "EF 3.1"`` flow to an ``ElementaryFlow``
SKOS row. Flows tagged ``ecoinvent algorithm addition`` are excluded pending review.
"""

import gzip
import json
import re

from sentier_importers.core.source import Source
from sentier_importers.core.types import RawData, Record, Records, Rows
from sentier_importers.sources.agribalyse.provenance import PROVENANCE_IRI

#: Base of every published Sentier flow IRI.
FLOWS_SCHEME = "https://vocab.sentier.dev/flows/"
#: Only flows from this upstream source are imported (see plan §guardrails).
ALLOWED_SOURCE = "EF 3.1"
#: chemrof property key holding the molecular formula.
_FORMULA_KEY = "https://w3id.org/chemrof/molecular_formula"
#: SKOS mapType fragment marking an exactMatch concept association.
_EXACT_MATCH = "exactMatch"

#: brightway.one context domain -> coarse compartment (when domain fixes it).
_DOMAIN_COMPARTMENT = {"reso": "natural resource", "laus": "land use"}
#: brightway.one emission medium code -> compartment (domain ``envi`` only).
_EMISSION_COMPARTMENT = {"air": "air", "wate": "water", "grou": "soil", "biot": "biota"}

_TAG_RE = re.compile(r"<[^>]+>")


def compartment_for_context(context_iri: str | None) -> str | None:
    """Coarse compartment for a brightway.one flow-context IRI, or ``None``."""
    if not context_iri or "/flow-contexts/" not in context_iri:
        return None
    slug = context_iri.rstrip("/").split("/")[-1]
    parts = slug.split("-")
    domain = parts[0]
    if domain in _DOMAIN_COMPARTMENT:
        return _DOMAIN_COMPARTMENT[domain]
    if domain == "envi" and len(parts) > 1:
        return _EMISSION_COMPARTMENT.get(parts[1])
    return None


def strip_html(text: str) -> str:
    """Remove HTML tags and collapse whitespace from a label."""
    return re.sub(r"\s+", " ", _TAG_RE.sub("", text)).strip()


def cas_number(flow: Record) -> str | None:
    """First CAS number, or ``None``."""
    cas = flow.get("cas_numbers") or []
    return cas[0] if cas else None


def formula(flow: Record) -> str | None:
    """Molecular formula from the chemrof properties, or ``None``.

    The property is usually a scalar but is sometimes a list of candidate formulas
    (isomers/salts); the schema wants a single string, so take the first.
    """
    value = (flow.get("properties") or {}).get(_FORMULA_KEY)
    if isinstance(value, list):
        value = value[0] if value else None
    return value or None


def exact_matches(flow: Record) -> list[str]:
    """Crosswalk IRIs from exactMatch concept associations (source + target @ids)."""
    out: list[str] = []
    seen: set[str] = set()
    for assoc in flow.get("concept_associations") or []:
        map_type = (assoc.get("xkos:mapType") or {}).get("@id", "")
        if not map_type.endswith(_EXACT_MATCH):
            continue
        for key in ("xkos:sourceConcept", "xkos:targetConcept"):
            iri = (assoc.get(key) or {}).get("@id")
            if iri and iri not in seen:
                seen.add(iri)
                out.append(iri)
    return out


def extra_notations(flow: Record) -> list[str]:
    """Preserved source identifiers: EC numbers and the raw brightway context slug."""
    out = [f"ec:{ec}" for ec in (flow.get("ec_numbers") or []) if ec]
    context = flow.get("context_iri")
    if context and "/flow-contexts/" in context:
        out.append(f"bw-context:{context.rstrip('/').split('/')[-1]}")
    return out


def _definition(flow: Record) -> str | None:
    """Definition string, or ``None`` when absent/empty (the file uses ``[]``)."""
    value = flow.get("definition")
    return value.strip() if isinstance(value, str) and value.strip() else None


def compartment_slug(compartment: str) -> str:
    """File-name slug of a compartment (``natural resource`` -> ``natural-resource``)."""
    return re.sub(r"[^a-z0-9]+", "-", compartment.lower()).strip("-")


class AgribalyseFlowsSource(Source):
    """Map the EF 3.1 harmonised flow list into ``ElementaryFlow`` SKOS rows.

    ``emit_filename`` is the compartment slug (``air``, ``natural-resource``, …):
    each registry entry emits only that compartment's flows, so the delivered
    files are named by content, never by source. Without ``emit_filename`` the
    source emits every flow. In a filtered run a flow whose compartment cannot
    be resolved raises — it would otherwise silently vanish from every slice.
    """

    def parse(self, raw: RawData) -> Records:
        """Gunzip the ``.json.gz`` payload and return its ``flows`` list."""
        data = json.loads(gzip.decompress(raw.content))
        return list(data["flows"])

    def transform(self, records: Records) -> Rows:
        slice_slug = self.config.emit_filename
        rows: Rows = []
        for flow in records:
            if flow.get("source") != ALLOWED_SOURCE:
                continue  # exclude ecoinvent algorithm additions (see plan §guardrails)
            label = (flow.get("prefLabel") or "").strip()
            identifier = flow.get("identifier")
            if not label or not identifier:
                continue
            if slice_slug:
                flow_compartment = compartment_for_context(flow.get("context_iri"))
                if flow_compartment is None:
                    raise ValueError(
                        f"flow {identifier!r} has no resolvable compartment "
                        f"(context {flow.get('context_iri')!r}) — it would be dropped "
                        f"from every per-compartment file"
                    )
                if compartment_slug(flow_compartment) != slice_slug:
                    continue

            row: Record = {"iri": f"{FLOWS_SCHEME}{identifier}", "pref_label": label}

            alt: list[str] = []
            seen: set[str] = set()
            for raw_label in flow.get("altLabel") or []:
                cleaned = strip_html(raw_label)
                key = cleaned.casefold()
                if cleaned and key != label.casefold() and key not in seen:
                    seen.add(key)
                    alt.append(cleaned)
            if alt:
                row["alt_labels"] = alt

            if (definition := _definition(flow)) is not None:
                row["definition"] = definition
            if (cas := cas_number(flow)) is not None:
                row["cas_number"] = cas
            if (chem := formula(flow)) is not None:
                row["formula"] = chem
            if (compartment := compartment_for_context(flow.get("context_iri"))) is not None:
                row["compartment"] = compartment
            if notations := extra_notations(flow):
                row["additional_notations"] = notations
            if matches := exact_matches(flow):
                row["exact_match"] = matches
            row["source"] = PROVENANCE_IRI
            row["status"] = "draft"
            rows.append(row)
        return rows
