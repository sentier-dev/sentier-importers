"""Import the EF 3.1 harmonised elementary flows into ``sentier_vocab``.

Reads the license-free ``harmonised-flows-simple.json.gz`` (EF 3.1 = JRC public,
nomenclature only) and maps each ``source == "EF 3.1"`` flow to an ``ElementaryFlow``
SKOS row. Flows tagged ``ecoinvent algorithm addition`` are excluded pending review.

The list gives a flow's context as a ``bw-context`` code only. The ``sub_compartment``
attribute is the leaf of the flow's exact EF context, read from the two inputs
``brightway_flows`` and ``jrc`` (see ``ef_context``); a flow neither places has none
and is counted in a log line.
"""

import gzip
import json
import re
from collections.abc import Iterable

from loguru import logger
from sentier_importers.core.errors import ValidationError
from sentier_importers.core.source import Source
from sentier_importers.core.types import RawData, Record, Records, Rows
from sentier_importers.sources.agribalyse import ef_context
from sentier_importers.sources.agribalyse.ef_context import JRC, SUB_COMPARTMENT, EfContexts
from sentier_importers.sources.agribalyse.provenance import PROVENANCE_IRI
from sentier_importers.sources.brightway_flows.correspondences import PINNED_SHA256

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


def alt_labels(raw_labels: Iterable[str], label: str) -> list[str]:
    """Alternative labels of a term: HTML stripped, de-duplicated without regard to
    case, and never the preferred ``label`` itself. Order is kept."""
    out: list[str] = []
    seen: set[str] = {label.casefold()}
    for raw_label in raw_labels:
        cleaned = strip_html(raw_label)
        key = cleaned.casefold()
        if cleaned and key not in seen:
            seen.add(key)
            out.append(cleaned)
    return out


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
    """Crosswalk IRIs from exactMatch concept associations (source + target @ids).

    These point at Brightway flow IRIs. A correspondence to another identifier space
    is a sentier-mappings bridge (``ef-3.1__brightway``), so the vocab rows emitted by
    :class:`AgribalyseFlowsSource` do not carry them.
    """
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


def is_emitted(flow: Record) -> bool:
    """Whether :class:`AgribalyseFlowsSource` emits a term for ``flow``.

    Ecoinvent algorithm additions are excluded (see plan §guardrails), and so is a flow
    without a label or an identifier. ``flows_jrc`` uses the same rule to know which
    flows this part already covers, so the two parts of the release cannot drift apart.
    """
    return bool(
        flow.get("source") == ALLOWED_SOURCE
        and (flow.get("prefLabel") or "").strip()
        and flow.get("identifier")
    )


class AgribalyseFlowsSource(Source):
    """Map the EF 3.1 harmonised flow list into elementary-flow term rows.

    The compartment is an attribute of the term; every flow of the release is
    emitted by this one source.
    """

    #: Pin of the ``brightway_flows`` input; ``None`` in tests, which feed a synthetic file.
    expected_sha256: str | None = PINNED_SHA256

    def parse(self, raw: RawData) -> Records:
        """Gunzip the ``.json.gz`` payload and return its ``flows`` list."""
        data = json.loads(gzip.decompress(raw.content))
        return list(data["flows"])

    def _contexts(self) -> EfContexts:
        """The exact EF context paths, from the two context inputs."""
        bw = ef_context.brightway_flows(self.inputs, self.expected_sha256)
        if JRC not in self.inputs:
            raise ValidationError(f"input {JRC!r} not fetched: the registry must declare it")
        return EfContexts(
            correspondence=ef_context.correspondence_paths(bw),
            jrc=ef_context.jrc_paths(self.inputs[JRC].content),
        )

    def transform(self, records: Records) -> Rows:
        contexts = self._contexts()
        unplaced: list[str] = []
        rows: Rows = []
        for flow in records:
            if not is_emitted(flow):
                continue
            label = (flow.get("prefLabel") or "").strip()
            identifier = flow.get("identifier")

            row: Record = {"iri": f"{FLOWS_SCHEME}{identifier}", "pref_label": label}

            if alt := alt_labels(flow.get("altLabel") or [], label):
                row["alt_labels"] = alt

            if (definition := _definition(flow)) is not None:
                row["definition"] = definition
            if (cas := cas_number(flow)) is not None:
                row["cas_number"] = cas
            if (chem := formula(flow)) is not None:
                row["formula"] = chem
            if (compartment := compartment_for_context(flow.get("context_iri"))) is not None:
                row["compartment"] = compartment
            if (sub_compartment := contexts.leaf(str(identifier))) is not None:
                row[SUB_COMPARTMENT] = sub_compartment
            else:
                unplaced.append(str(identifier))
            if notations := extra_notations(flow):
                row["additional_notations"] = notations
            row["source"] = PROVENANCE_IRI
            row["status"] = "draft"
            rows.append(row)
        logger.log(
            "WARNING" if unplaced else "INFO",
            ef_context.unplaced_message(self.config.name, unplaced, len(rows)),
        )
        return rows
