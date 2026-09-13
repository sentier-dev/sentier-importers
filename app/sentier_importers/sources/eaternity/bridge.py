"""Eaternity flat flow -> BAFU-2026 v1 flow: the bridge between the two instances.

Eaternity's BAFU-2026-based instance (datasource ``eaternity-bafu-ext``) collapses
sub-compartments to the root compartment and spells units the Brightway way, so an
Eaternity flow is ``(name, root, unit)``. Our ``bafu-2026-v1`` keys every flow by
``(name, category, subcategory, unit)`` over the v1 ecoSpold exchange universe.

The ecoinvent-biosphere3 -> eaternity-bafu-ext bridge retains the biosphere3 source
sub-compartment on every entry; that sub-compartment is what
places an Eaternity flow at BAFU sub-compartment level. Placement is **strict**:
a biosphere3 ``ocean`` flow resolves only to the BAFU ``ocean`` flow of that
name, never to ``river`` or ``unspecified`` as a fallback. EF factors for sea
water and fresh water differ by orders of magnitude, so a fallback would assert
the wrong factor with a straight face.

Unit twins: BAFU carries radionuclides in both Bq and kBq while Eaternity keeps
only kBq, so one Eaternity kBq flow stands for both BAFU twins, the Bq one with a
``conversion_factor`` of 0.001 (BAFU amount x factor = amount in the target unit).
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from dataclasses import dataclass

from sentier_importers.core.types import Record
from sentier_importers.sources.bafu.ecospold import flow_id

#: Eaternity / biosphere3 root compartment -> BAFU ecoSpold category.
ROOT_TO_CATEGORY: dict[str, str] = {
    "air": "emissions to air",
    "water": "emissions to water",
    "soil": "emissions to soil",
    "natural resource": "resources",
}

#: biosphere3 ``(root, sub-compartment)`` -> BAFU ecoSpold ``subCategory``. A root-only
#: biosphere3 context (``["air"]``) is the ``unspecified`` sub-compartment. Anything
#: not listed resolves to nothing rather than to a guess.
B3_SUB_TO_BAFU: dict[tuple[str, str | None], str] = {
    ("air", None): "unspecified",
    ("air", "urban air close to ground"): "high. pop.",
    ("air", "non-urban air or from high stacks"): "low. pop.",
    ("air", "low population density, long-term"): "low. pop., long-term",
    ("air", "lower stratosphere + upper troposphere"): "stratosphere + troposphere",
    ("air", "indoor"): "indoor",
    ("water", None): "unspecified",
    ("water", "surface water"): "river",
    ("water", "ocean"): "ocean",
    ("water", "ground-"): "groundwater",
    ("water", "ground-, long-term"): "groundwater, long-term",
    ("water", "fossil well"): "fossilwater",
    ("soil", None): "unspecified",
    ("soil", "agricultural"): "agricultural",
    ("soil", "industrial"): "industrial",
    ("soil", "forestry"): "forestry",
    ("natural resource", None): "unspecified",
    ("natural resource", "land"): "land",
    ("natural resource", "in ground"): "in ground",
    ("natural resource", "in water"): "in water",
    ("natural resource", "in air"): "in air",
    ("natural resource", "biotic"): "biotic",
}

#: Eaternity unit -> the BAFU units it stands for, with the BAFU -> Eaternity factor.
UNIT_TWINS: dict[str, tuple[tuple[str, float], ...]] = {
    "kBq": (("kBq", 1.0), ("Bq", 0.001)),
}

#: Only FromNature / ToNature exchanges are elementary flows.
_BIOSPHERE_GROUP = 4


@dataclass(frozen=True)
class BafuFlow:
    """One bafu-2026-v1 elementary flow at unit level."""

    name: str
    category: str
    subcategory: str
    unit: str

    @property
    def code(self) -> str:
        """The id sentier-vocab mints for this flow (joins to the published IRI)."""
        return flow_id(self.name, self.category, self.subcategory, self.unit)

    @property
    def context(self) -> list[str]:
        return [c for c in (self.category, self.subcategory) if c]


@dataclass(frozen=True)
class Resolution:
    """A BAFU flow an Eaternity flow stands for, with the unit factor to reach it."""

    flow: BafuFlow
    conversion_factor: float


def _norm(text: str) -> str:
    return text.strip().lower()


class BafuFlowIndex:
    """Lookup of the bafu-2026-v1 flow universe by ``(name, category, subcategory, unit)``."""

    def __init__(self, flows: Iterable[BafuFlow]) -> None:
        self._flows: dict[tuple[str, str, str, str], BafuFlow] = {}
        for flow in flows:
            key = (_norm(flow.name), flow.category, flow.subcategory, flow.unit)
            self._flows.setdefault(key, flow)

    @classmethod
    def from_flows(cls, flows: Iterable[BafuFlow]) -> BafuFlowIndex:
        return cls(flows)

    @classmethod
    def from_ecospold(cls, records: Iterable[Record]) -> BafuFlowIndex:
        """Build from parsed ecoSpold datasets (``ecospold.parse_ecospold_zip`` output)."""

        def flows() -> Iterator[BafuFlow]:
            for record in records:
                for exchange in record["exchanges"]:
                    if exchange.get("group_code") != _BIOSPHERE_GROUP:
                        continue
                    yield BafuFlow(
                        exchange["name"],
                        exchange.get("category") or "",
                        exchange.get("subcategory") or "",
                        exchange.get("unit") or "",
                    )

        return cls(flows())

    def lookup(self, name: str, category: str, subcategory: str, unit: str) -> BafuFlow | None:
        return self._flows.get((_norm(name), category, subcategory, unit))

    def __iter__(self) -> Iterator[BafuFlow]:
        return iter(self._flows.values())

    def __len__(self) -> int:
        return len(self._flows)


def bafu_subcategory(b3_context: list[str]) -> str | None:
    """BAFU ``subCategory`` a biosphere3 context places a flow in, or ``None`` if unknown."""
    if not b3_context:
        return None
    root = b3_context[0]
    sub = b3_context[1] if len(b3_context) > 1 else None
    return B3_SUB_TO_BAFU.get((root, sub))


def resolve(
    index: BafuFlowIndex, name: str, root: str, unit: str, b3_context: list[str]
) -> list[Resolution]:
    """BAFU flows the Eaternity flow ``(name, root, unit)`` stands for, placed by the
    biosphere3 sub-compartment. Empty when any part does not resolve exactly."""
    category = ROOT_TO_CATEGORY.get(root)
    subcategory = bafu_subcategory(b3_context)
    if category is None or subcategory is None:
        return []
    resolutions: list[Resolution] = []
    for bafu_unit, factor in UNIT_TWINS.get(unit, ((unit, 1.0),)):
        flow = index.lookup(name, category, subcategory, bafu_unit)
        if flow is not None:
            resolutions.append(Resolution(flow, factor))
    return resolutions
