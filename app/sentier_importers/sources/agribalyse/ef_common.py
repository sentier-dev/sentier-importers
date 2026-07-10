"""Shared helpers for the EF 3.1 LCIA import into ``sentier_methods``.

The methods (one per impact category) and characterization factors (one per CF) both come
from the JRC ``EF-LCIAMethod_CF(EF-v3.1)`` parquet. They land in
``sentier-methods/data/01-ef-3.1/{methods,characterization-factors}.parquet`` — the data
layer for LCIA methods, keyed by elementary flow (flows link to inventory via mappings).

CF values are JRC-public and uncapped. Flows are referenced by their vocab IRI
(``flows/<FLOW_uuid>``) so the CF table joins cleanly to the imported flow vocabulary.
No ecoinvent nomenclature is involved.

This module holds no ``Source`` subclass (registry contract: one Source per module).
"""

import io

import pyarrow.parquet as pq
from sentier_importers.core.dedup import slugify
from sentier_importers.core.types import RawData, Records

#: Datasource id (matches sentier-methods/data/01-ef-3.1/metadata.json).
DATASOURCE = "ef-3.1"
#: Human-readable method name.
METHOD_NAME = "EF v3.1"
#: Citation stamped on every method row.
METHOD_SOURCE = "European Commission, Joint Research Centre — Environmental Footprint (EF) 3.1"
#: Base of the flow IRIs CFs reference (must match agribalyse.flows).
FLOWS_SCHEME = "https://vocab.sentier.dev/flows/"

#: Vocab term IRIs (the descriptive layer; the numeric CF data lives in sentier-methods).
EF31_SOURCE_IRI = "https://vocab.sentier.dev/sources/ef-3.1"
VOCAB_METHOD_IRI = "https://vocab.sentier.dev/lcia-methods/ef-3.1"
VOCAB_IMPACT_SCHEME = "https://vocab.sentier.dev/impact-categories/"


def vocab_impact_iri(impact_category: str) -> str:
    """Vocab IRI for an EF impact-category term."""
    return f"{VOCAB_IMPACT_SCHEME}ef-3.1/{slugify(impact_category)}"


#: EF CF parquet columns we read.
_COL_FLOW_UUID = "FLOW_uuid"
_COL_FLOW_NAME = "FLOW_name"
_COL_METHOD_NAME = "LCIAMethod_name"
_COL_CF = "CF EF3.1"
_COL_LOCATION = "LCIAMethod_location"
_COL_CLASS = ("FLOW_class0", "FLOW_class1", "FLOW_class2")

#: EF 3.1 reference units per impact category (public JRC values; the CF parquet omits them).
IMPACT_UNITS: dict[str, str] = {
    "Acidification": "mol H+ eq",
    "Climate change": "kg CO2 eq",
    "Climate change-Biogenic": "kg CO2 eq",
    "Climate change-Fossil": "kg CO2 eq",
    "Climate change-Land use and land use change": "kg CO2 eq",
    "EF-particulate Matter": "disease incidence",
    "Eutrophication marine": "kg N eq",
    "Eutrophication, freshwater": "kg P eq",
    "Eutrophication, terrestrial": "mol N eq",
    "Human toxicity, cancer": "CTUh",
    "Human toxicity, cancer_inorganics": "CTUh",
    "Human toxicity, cancer_organics": "CTUh",
    "Human toxicity, non-cancer": "CTUh",
    "Human toxicity, non-cancer_inorganics": "CTUh",
    "Human toxicity, non-cancer_organics": "CTUh",
    "Ionising radiation, human health": "kBq U-235 eq",
    "Land use": "Pt",
    "Ozone depletion": "kg CFC-11 eq",
    "Photochemical ozone formation - human health": "kg NMVOC eq",
    "Resource use, fossils": "MJ",
    "Resource use, minerals and metals": "kg Sb eq",
    "Water use": "m3 world eq deprived",
    "Ecotoxicity, freshwater": "CTUe",
    "Ecotoxicity, freshwater_inorganics": "CTUe",
    "Ecotoxicity, freshwater_organics": "CTUe",
}


def method_id(impact_category: str) -> str:
    """Stable ``<datasource>:<impact_category>`` primary key."""
    return f"{DATASOURCE}:{slugify(impact_category)}"


def flow_iri(flow_uuid: str) -> str:
    """Vocab flow IRI a CF points at — matches the agribalyse flow import."""
    return f"{FLOWS_SCHEME}{flow_uuid}"


def unit_for(impact_category: str) -> str:
    """EF reference unit for an impact category (empty string if unknown)."""
    return IMPACT_UNITS.get(impact_category, "")


def parse_cf_table(raw: RawData) -> Records:
    """Read the EF CF parquet into per-row records (uuid, name, method, cf, ctx, loc)."""
    columns = [
        _COL_FLOW_UUID,
        _COL_FLOW_NAME,
        _COL_METHOD_NAME,
        _COL_CF,
        _COL_LOCATION,
        *_COL_CLASS,
    ]
    data = pq.read_table(io.BytesIO(raw.content), columns=columns).to_pydict()
    records: Records = []
    for i in range(len(data[_COL_FLOW_UUID])):
        context = " / ".join(str(data[c][i]).strip() for c in _COL_CLASS if data[c][i])
        records.append(
            {
                "flow_uuid": data[_COL_FLOW_UUID][i],
                "flow_name": data[_COL_FLOW_NAME][i],
                "method_name": (data[_COL_METHOD_NAME][i] or "").strip(),
                "cf": data[_COL_CF][i],
                "location": data[_COL_LOCATION][i],
                "flow_context": context or None,
            }
        )
    return records


def distinct_method_names(records: Records) -> list[str]:
    """Ordered distinct ``LCIAMethod_name`` values (the EF impact categories)."""
    seen: dict[str, None] = {}
    for rec in records:
        name = rec.get("method_name") or ""
        if name and name not in seen:
            seen[name] = None
    return list(seen)
