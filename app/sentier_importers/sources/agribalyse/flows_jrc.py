"""EF 3.1 flows the harmonised list lacks, from the JRC characterization-factor table.

The ``ef-3.1`` release's flows come from Brightway's harmonised flow list, a
de-duplicated consensus list (79,267 flows). sentier-methods keys its factors on the
JRC nomenclature (89,070 flows), so 13,774 characterised flows had no term: mostly
long-term water emissions and the soil duplicates the consensus list drops. This
part of the release emits exactly those, from the JRC CF table the ``lcia-methods``
and ``impact-categories`` parts already read, so that a factor key, a mappings
target and a vocab term are one IRI (spec 2026-11-10-ef31-flows-missing-from-vocab).

- One row per flow UUID the harmonised part does not emit (its ``source == "EF 3.1"``
  flows, read from the same file through the ``harmonised`` input).
- Label: the JRC name; when the table spells one flow in two casings, the cased one.
- ``compartment`` from the context path. No ``bw-context`` notation: that code is the
  harmonised list's own identifier, and ``matching.bw_context`` derives its crosswalk
  from the rows that carry it; a code computed here from that crosswalk would feed it
  its own output.
- No CAS, formula or unit: the CF table carries none.
"""

import gzip
import io
import json
from collections import OrderedDict

import pyarrow.parquet as pq
from sentier_importers.core.errors import ParseError, ValidationError
from sentier_importers.core.source import Source
from sentier_importers.core.types import RawData, Record, Records, Rows
from sentier_importers.sources.agribalyse.flows import FLOWS_SCHEME, is_emitted
from sentier_importers.sources.agribalyse.provenance import PROVENANCE_IRI

#: Registry input: the harmonised flow list, whose EF 3.1 flows this part never emits.
HARMONISED = "harmonised"
_COLUMNS = ["FLOW_uuid", "FLOW_name", "FLOW_class0", "FLOW_class1", "FLOW_class2"]
_CLASSES = ("FLOW_class0", "FLOW_class1", "FLOW_class2")
_SEP = " / "
#: Second path level -> compartment for emissions; the first level decides the rest.
_EMISSION_COMPARTMENT = {
    "Emissions to air": "air",
    "Emissions to water": "water",
    "Emissions to soil": "soil",
}
_TOP_COMPARTMENT = {"Resources": "natural resource", "Land use": "land use"}


def context_path(record: Record) -> str:
    """The EF context path of a CF row, ``" / "``-joined, empty levels dropped."""
    return _SEP.join(
        str(record[c]).strip() for c in _CLASSES if isinstance(record.get(c), str) and record[c]
    )


def compartment_for_path(path: str) -> str | None:
    """Coarse compartment (the vocab attribute) for an EF context path, or ``None``."""
    levels = path.split(_SEP)
    if levels[0] == "Emissions" and len(levels) > 1:
        return _EMISSION_COMPARTMENT.get(levels[1])
    return _TOP_COMPARTMENT.get(levels[0])


def _label(names: list[str]) -> str:
    """The JRC spelling of a flow name: of several casings, the one with capitals."""
    cased = sorted(n for n in names if n != n.lower())
    return cased[0] if cased else names[0]


def _harmonised_identifiers(raw: bytes) -> frozenset[str]:
    """Identifiers the harmonised part emits (the rule is ``flows.is_emitted``)."""
    try:
        flows = json.loads(gzip.decompress(raw))["flows"]
    except (OSError, EOFError, ValueError, KeyError, TypeError) as exc:
        raise ParseError(f"{HARMONISED} is not the gzipped harmonised flow list: {exc!r}") from exc
    if not isinstance(flows, list) or not all(isinstance(f, dict) for f in flows):
        raise ParseError(f"{HARMONISED}: 'flows' is not a list of flow records")
    identifiers = frozenset(str(f["identifier"]) for f in flows if is_emitted(f))
    if not identifiers:
        # Nothing to exclude would re-emit every flow of the harmonised part.
        raise ParseError(f"{HARMONISED}: no EF 3.1 flow found in the harmonised flow list")
    return identifiers


class JrcFlowsSource(Source):
    """Emit the characterised EF 3.1 flows the harmonised list does not carry."""

    def parse(self, raw: RawData) -> Records:
        try:
            return pq.read_table(io.BytesIO(raw.content), columns=_COLUMNS).to_pylist()
        except (OSError, ValueError, KeyError) as exc:
            raise ParseError(f"{raw.source_url} is not the JRC CF table: {exc!r}") from exc

    def transform(self, records: Records) -> Rows:
        if HARMONISED not in self.inputs:
            raise ValidationError(
                f"input {HARMONISED!r} not fetched: the registry must declare it"
            )
        skip = _harmonised_identifiers(self.inputs[HARMONISED].content)
        names: OrderedDict[str, list[str]] = OrderedDict()
        paths: dict[str, str] = {}
        for record in records:
            uuid = str(record.get("FLOW_uuid") or "").strip()
            name = str(record.get("FLOW_name") or "").strip()
            path = context_path(record)
            if not uuid or not name or not path:
                raise ValidationError(f"CF row without a flow uuid, name or context: {record!r}")
            if uuid in skip:
                continue
            if paths.setdefault(uuid, path) != path:
                raise ValidationError(f"{uuid}: two context paths, {paths[uuid]!r} and {path!r}")
            variants = names.setdefault(uuid, [])
            if name not in variants:
                variants.append(name)

        rows: Rows = []
        for uuid, variants in names.items():
            row: Record = {"iri": f"{FLOWS_SCHEME}{uuid}", "pref_label": _label(variants)}
            if (compartment := compartment_for_path(paths[uuid])) is not None:
                row["compartment"] = compartment
            row["source"] = PROVENANCE_IRI
            row["status"] = "draft"
            rows.append(row)
        return rows
