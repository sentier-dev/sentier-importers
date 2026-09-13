"""biosphere3 code -> EF 3.1 flow, by characterization-factor vector identity.

The EF v3.1 methods were matched onto both flow universes at Brightway import time,
so a biosphere3 code and an EF code carrying the same factor in every method are the
same characterization factor. Comparing CF vectors recovers that pairing without
publishing anything ecoinvent-shaped: the output names only the EF flow. This is the
de-bridging argument of ``docs/specs/2026-08-06-bafu-ef-debridged-mappings.md``,
reused here for the biosphere3 side of the ecoinvent-biosphere3 -> eaternity-bafu-ext
bridge.

Tiers, first hit wins, each gated on compartment:

- **T2 exact**: an EF flow with the identical vector. Several candidates are
  harmless (identical factors); prefer one whose name agrees with the BAFU flow,
  then one in the matching EF sub-compartment, then the smallest code.
- **T3 superset**: an EF flow agreeing on every method the biosphere3 code has and
  characterised in more. Only name-matched candidates count, and they must agree
  on the added methods; otherwise the pick would silently choose factors the flow
  does not receive today.

Compartment gate: identical factors are not identical flows. A metal emitted to air
and the same metal extracted from ground can share a single ADP factor; the gate
keeps an emission from resolving to a resource.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote

import pyarrow.parquet as pq
from sentier_importers.core.types import Record
from sentier_importers.sources.eaternity.bridge import BafuFlow

_ECO_DB = "ecoinvent-3.9.1-biosphere"
_EF_DB = "ef"
_SIGNIFICANT_DIGITS = 12

#: BAFU ecoSpold category -> compartment bucket shared with the EF category path.
BAFU_BUCKET: dict[str, str] = {
    "emissions to air": "air",
    "emissions to water": "water",
    "emissions to soil": "soil",
    "resources": "resource",
}

#: BAFU ``subCategory`` -> whole EF leaf tail(s) it accepts, once the connective is
#: removed (see ``_LEAF_CONNECTIVES``): a plain ``str`` for a single tail, or a ``tuple``
#: of tails when more than one EF leaf counts as a match (e.g. both land-use leaves).
BAFU_SUB_TO_EF_LEAF: dict[str, str | tuple[str, ...]] = {
    "unspecified": "unspecified",
    "high. pop.": "urban air close to ground",
    "low. pop.": "non-urban air or from high stacks",
    "low. pop., long-term": "low population density, long-term",
    "stratosphere + troposphere": "lower stratosphere and upper troposphere",
    "indoor": "indoor",
    "river": "fresh water",
    "lake": "fresh water",
    "river, long-term": "fresh water, long-term",
    "ocean": "sea water",
    "groundwater": "ground water",
    "groundwater, long-term": "ground water, long-term",
    "fossilwater": "ground water",
    "agricultural": "agricultural soil",
    "industrial": "non-agricultural soil",
    "forestry": "non-agricultural soil",
    "in ground": "ground",
    "in water": "water",
    "in air": "air",
    "land": ("land occupation", "land transformation"),
    "biotic": "biotic",
}

#: Connective removed from the middle of an EF leaf before comparing it to a
#: ``BAFU_SUB_TO_EF_LEAF`` tail. Deliberately not anchored to the start of the string:
#: e.g. "non-renewable element resources from ground" -> "non-renewable element ground"
#: strips a connective out of the middle of the leaf, not off its front.
_LEAF_CONNECTIVES = ("emissions to ", "resources from ")

Vector = dict[str, float]


@dataclass(frozen=True)
class Twin:
    code: str
    tier: str
    candidates: int
    name_matched: bool
    sub_matched: bool


@dataclass(frozen=True)
class Withheld:
    reason: str
    detail: str


def _normalise(amount: float) -> float:
    return float(f"%.{_SIGNIFICANT_DIGITS}g" % amount)


def _fingerprint(vector: Vector) -> tuple:
    return tuple(sorted(vector.items()))


class CfVectors:
    """``(database, code) -> {method: amount}`` over the EF v3.1 method tables."""

    def __init__(self, vectors: dict[tuple[str, str], Vector]) -> None:
        self._vectors = vectors
        self._ef_by_fingerprint: dict[tuple, list[str]] = {}
        for (database, code), vector in vectors.items():
            if database == _EF_DB:
                self._ef_by_fingerprint.setdefault(_fingerprint(vector), []).append(code)

    @classmethod
    def from_tables(cls, tables: dict[str, Iterable[Record]]) -> CfVectors:
        """``{method: [{database, code, amount}, ...]}`` -> index."""
        vectors: dict[tuple[str, str], Vector] = {}
        for method, rows in tables.items():
            for row in rows:
                key = (row["database"], row["code"])
                vectors.setdefault(key, {})[method] = _normalise(float(row["amount"]))
        return cls(vectors)

    @classmethod
    def from_directory(cls, root: Path) -> CfVectors:
        """One ``<method-slug>/cfs.parquet`` per method (dds-carbonminds-data layout)."""
        tables = {
            unquote(path.parent.name): pq.read_table(path).to_pylist()
            for path in sorted(root.glob("*/cfs.parquet"))
        }
        if not tables:
            raise FileNotFoundError(f"no */cfs.parquet under {root}")
        return cls.from_tables(tables)

    def vector(self, database: str, code: str) -> Vector | None:
        return self._vectors.get((database, code))

    def biosphere3(self, code: str) -> Vector | None:
        return self.vector(_ECO_DB, code)

    def exact(self, vector: Vector | None) -> list[str]:
        if not vector:
            return []
        return sorted(self._ef_by_fingerprint.get(_fingerprint(vector), []))

    def superset(self, vector: Vector | None) -> list[str]:
        """EF codes agreeing on every method in ``vector`` and characterised in more."""
        if not vector:
            return []
        return sorted(
            code
            for (database, code), candidate in self._vectors.items()
            if database == _EF_DB
            and len(candidate) > len(vector)
            and all(candidate.get(method) == amount for method, amount in vector.items())
        )

    def extra_methods_agree(self, candidates: list[str], vector: Vector) -> bool:
        """True when every candidate adds the same methods with the same amounts."""
        added = {
            code: tuple(
                sorted(
                    (method, amount)
                    for method, amount in self._vectors[(_EF_DB, code)].items()
                    if method not in vector
                )
            )
            for code in candidates
        }
        return len(set(added.values())) == 1


class EfLabels:
    """EF flow code -> name and category path, from the sentier-methods CF table."""

    def __init__(self, labels: dict[str, tuple[str, list[str]]]) -> None:
        self._labels = labels

    @classmethod
    def from_rows(cls, rows: Iterable[Record]) -> EfLabels:
        """Rows with ``flow`` (IRI or bare uuid), ``flow_name``, ``flow_context``."""
        labels: dict[str, tuple[str, list[str]]] = {}
        for row in rows:
            code = str(row["flow"]).rsplit("/", 1)[-1]
            if code in labels:
                continue
            context = [part.strip() for part in str(row.get("flow_context") or "").split("/")]
            labels[code] = (str(row.get("flow_name") or "").strip(), [c for c in context if c])
        return cls(labels)

    def name(self, code: str) -> str:
        return self._labels.get(code, ("", []))[0]

    def context(self, code: str) -> list[str]:
        return self._labels.get(code, ("", []))[1]

    def bucket(self, code: str) -> str | None:
        path = " / ".join(self.context(code)).lower()
        if "resource" in path:
            return "resource"
        for bucket in ("air", "water", "soil"):
            if bucket in path:
                return bucket
        return None

    def name_matches(self, code: str, bafu_name: str) -> bool:
        """``barium (ii)`` reads as ``Barium``; ``copper`` as ``Copper``."""
        wanted = bafu_name.strip().lower()
        ef_name = self.name(code).lower()
        return ef_name == wanted or ef_name.split(" (")[0] == wanted

    def sub_matches(self, code: str, bafu_subcategory: str) -> bool:
        """True when the EF leaf's tail equals a mapped token: the token must equal the
        leaf tail or be preceded by a space, so a hyphen is not a boundary (this is what
        rejects ``non-agricultural``), and the long-term qualifier is part of the tail
        so it is self-enforcing."""
        tokens = BAFU_SUB_TO_EF_LEAF.get(bafu_subcategory)
        context = self.context(code)
        if tokens is None or not context:
            return False
        if isinstance(tokens, str):
            tokens = (tokens,)
        leaf = context[-1].lower()
        for connective in _LEAF_CONNECTIVES:
            if connective in leaf:
                leaf = leaf.replace(connective, "", 1)
                break
        return any(leaf == token or leaf.endswith(" " + token) for token in tokens)


def _pick(codes: list[str], labels: EfLabels, bafu: BafuFlow) -> tuple[str, bool, bool]:
    named = [c for c in codes if labels.name_matches(c, bafu.name)]
    pool = named or codes
    placed = [c for c in pool if labels.sub_matches(c, bafu.subcategory)]
    return sorted(placed or pool)[0], bool(named), bool(placed)


def find_twin(
    vectors: CfVectors, labels: EfLabels, b3_code: str, bafu: BafuFlow
) -> Twin | Withheld:
    """The EF flow carrying the factors the biosphere3 code carries, for ``bafu``."""
    vector = vectors.biosphere3(b3_code)
    if not vector:
        return Withheld("uncharacterised", "biosphere3 partner carries no EF 3.1 factor")
    bucket = BAFU_BUCKET.get(bafu.category)

    exact = vectors.exact(vector)
    gated = [c for c in exact if labels.bucket(c) == bucket]
    if gated:
        code, named, placed = _pick(gated, labels, bafu)
        return Twin(code, "T2", len(gated), named, placed)
    if exact:
        return Withheld(
            "compartment_mismatch",
            f"{len(exact)} CF-identical EF flow(s), none in the {bucket} compartment",
        )

    superset = [
        c
        for c in vectors.superset(vector)
        if labels.bucket(c) == bucket and labels.name_matches(c, bafu.name)
    ]
    if superset and vectors.extra_methods_agree(superset, vector):
        code, named, placed = _pick(superset, labels, bafu)
        return Twin(code, "T3", len(superset), named, placed)
    if superset:
        return Withheld(
            "superset_candidates_disagree",
            f"{len(superset)} name-matched EF flows agree on the current factors but "
            "disagree on the added methods",
        )
    return Withheld(
        "no_cf_compatible_ef_flow",
        f"no EF flow in the {bucket} compartment carries these factors",
    )
