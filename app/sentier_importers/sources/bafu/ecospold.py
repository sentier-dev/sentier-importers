"""Shared EcoSpold1 reader + helpers for the BAFU-2026 v1 source family.

The zip holds one ``process_<uuid>.xml`` per dataset; the dataset-level
``number`` attribute is an export-wide key (every technosphere input's
``number`` resolves to a dataset), so linking is exact and needs no name
matching.

Every source in this family is opt-in (``enabled: false``) and run locally
against the downloaded export.
"""

import hashlib
import io
import math
import re
import uuid
import xml.etree.ElementTree as ET  # noqa: N817 - stdlib convention
import zipfile

from sentier_importers.core.errors import ParseError
from sentier_importers.core.types import RawData, Record, Records

#: Required source quotation for the 2026 release.
CITATION = "Life Cycle Inventory database of the Swiss Federal Administration, BAFU:2026"

#: Fixed namespace for deterministic elementary-flow ids (public recipe: locally
#: regenerated data is bit-identical across users).
BAFU_FLOW_NS = uuid.uuid5(uuid.NAMESPACE_URL, "https://vocab.sentier.dev/flows/bafu/")

#: Sector folder -> BAFU ``referenceFunction`` categories (covers all 59 observed).
SECTORS: dict[str, list[str]] = {
    "01-agriculture": ["agricultural", "food industry"],
    "02-electricity": [
        "electricity",
        "electricity by fuel",
        "photovoltaic",
        "wind power",
        "power plants",
        "impoundment",
    ],
    "03-chemicals": ["chemicals"],
    "04-transport": ["transport systems"],
    "05-energy": [
        "natural gas",
        "heat",
        "fuels",
        "oil",
        "heating",
        "heat pumps",
        "biomass",
        "compressed air",
        "pipeline",
        "energy supply, kbob recommendation",
    ],
    "06-waste": [
        "waste management",
        "landfill",
        "construction waste",
        "electronics waste",
        "wastewater treatment",
        "waste",
        "transport waste",
        "incineration",
        "nuclear waste",
        "underground deposit",
        "recycling",
        "landfarming",
    ],
    "07-construction": [
        "construction materials",
        "construction",
        "construction processes",
        "building components",
        "building processes",
        "flooring",
        "insulation materials",
        "ceramics",
    ],
    "08-materials": [
        "wood",
        "metals",
        "minerals",
        "paper+ board",
        "plastics",
        "glass",
        "textiles",
        "cardboard",
    ],
    "09-electronics": ["electronics", "computers & network", "mechanical"],
    "10-building-services": ["ventilation", "water", "private consumption", "Others"],
    "99-obsolete": [
        "material, obsolete",
        "energy, obsolete",
        "processing, obsolete",
        "waste treatment, obsolete",
    ],
}

_CATEGORY_TO_SECTOR: dict[str, str] = {
    category: sector for sector, categories in SECTORS.items() for category in categories
}

_FILENAME_RE = re.compile(r"process_(.+)\.xml$")

#: Parsed-records memo, keyed by content digest — 22 inventory slices + 2 vocab
#: slices reuse one 37 MB parse when run in a single process.
_PARSE_CACHE: dict[str, Records] = {}


def sector_for(category: str) -> str:
    """Sector folder for a BAFU category; raises ``ParseError`` on unknown categories."""
    try:
        return _CATEGORY_TO_SECTOR[category]
    except KeyError:
        raise ParseError(f"BAFU category not in the sector map: {category!r}") from None


def flow_id(name: str, category: str | None, subcategory: str | None) -> str:
    """Deterministic elementary-flow id over the (name, category, subCategory) triple."""
    return str(uuid.uuid5(BAFU_FLOW_NS, f"{name}|{category or ''}|{subcategory or ''}"))


def bw_uncertainty(
    amount: float, uncertainty_type: str | None, sd95: float | None
) -> tuple[int | None, float | None, float | None]:
    """EcoSpold1 uncertainty -> Brightway ``(uncertainty_type, loc, scale)``.

    EcoSpold 1 (lognormal, ``standardDeviation95`` = square of the geometric SD)
    maps to Brightway 2 with ``loc = ln(|amount|)``, ``scale = ln(√SD95)``;
    EcoSpold 2 (normal) maps to Brightway 3 with ``loc = amount``,
    ``scale = SD95 / 2``. Anything else — including a missing/non-positive SD95
    or a zero amount for the lognormal case — is undefined (all ``None``).
    """
    if sd95 is None or sd95 <= 0:
        return None, None, None
    if uncertainty_type == "1" and amount != 0:
        return 2, math.log(abs(amount)), math.log(math.sqrt(sd95))
    if uncertainty_type == "2":
        return 3, amount, sd95 / 2
    return None, None, None


def _is_obsolete(name: str, category: str, subcategory: str) -> bool:
    return name.startswith("xx") or "obsolete" in category or "notMaintained" in subcategory


def _parse_exchange(elem: ET.Element) -> Record:
    input_group = elem.find("inputGroup")
    output_group = elem.find("outputGroup")
    group, code = ("input", input_group) if input_group is not None else ("output", output_group)
    sd95 = elem.get("standardDeviation95")
    return {
        "name": elem.get("name") or "",
        "category": elem.get("category"),
        "subcategory": elem.get("subCategory"),
        "cas": elem.get("CASNumber"),
        "location": elem.get("location"),
        "unit": elem.get("unit") or "",
        "amount": float(elem.get("meanValue") or 0.0),
        "number": elem.get("number") or "",
        "group": group,
        "group_code": int(code.text) if code is not None and code.text else None,
        "uncertainty_type": elem.get("uncertaintyType"),
        "sd95": float(sd95) if sd95 else None,
    }


def _parse_dataset(xml_name: str, handle) -> Record:
    root = ET.parse(handle).getroot()
    dataset = root.find("dataset")
    if dataset is None:
        raise ParseError(f"no <dataset> element in {xml_name}")
    ref = dataset.find("metaInformation/processInformation/referenceFunction")
    if ref is None:
        raise ParseError(f"no <referenceFunction> element in {xml_name}")
    geography = dataset.find("metaInformation/processInformation/geography")
    technology = dataset.find("metaInformation/processInformation/technology")
    match = _FILENAME_RE.search(xml_name)
    if match is None:
        raise ParseError(f"unexpected EcoSpold file name: {xml_name}")

    name = ref.get("name") or ""
    category = ref.get("category") or ""
    subcategory = ref.get("subCategory") or ""
    return {
        "uuid": match.group(1),
        "number": dataset.get("number") or "",
        "name": name,
        "category": category,
        "subcategory": subcategory,
        "unit": ref.get("unit") or "",
        "amount": float(ref.get("amount") or 1.0),
        "location": geography.get("location") if geography is not None else None,
        "technology": technology.get("text") if technology is not None else None,
        "infrastructure": (ref.get("infrastructureProcess") or "false") == "true",
        "obsolete": _is_obsolete(name, category, subcategory),
        "general_comment": ref.get("generalComment") or "",
        "exchanges": [_parse_exchange(ex) for ex in dataset.findall("flowData/exchange")],
    }


def parse_ecospold_zip(raw: RawData) -> Records:
    """One record per ``process_<uuid>.xml`` dataset in the zip. Memoized by content."""
    digest = hashlib.sha256(raw.content).hexdigest()
    if digest not in _PARSE_CACHE:
        records: Records = []
        with zipfile.ZipFile(io.BytesIO(raw.content)) as archive:
            for xml_name in archive.namelist():
                if not xml_name.endswith(".xml"):
                    continue
                with archive.open(xml_name) as handle:
                    records.append(_parse_dataset(xml_name, handle))
        _PARSE_CACHE[digest] = records
    return _PARSE_CACHE[digest]
