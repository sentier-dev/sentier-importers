"""EF 3.1 flow index built from public inputs only.

- ``characterization-factors.parquet`` (sentier-methods): which EF flows carry a
  factor, their context path, and their CF vector by ``method_id``. A flow that is
  not in this table has no factor and is not a mapping target.
- ``elementary-flows/*.parquet`` (sentier-vocab, rows with the ef-3.1 source):
  preferred label, synonyms (``alt_labels``) and CAS number, keyed by the flow IRI.

The CF vector keeps the global (location-less) factor per method; location-specific
rows (land use, water use, some regionalised categories) are not part of flow identity.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path

import pyarrow.parquet as pq
from sentier_importers.core.errors import ParseError
from sentier_importers.matching.compartments import bucket_of_ef_context, leaf_of

EF_SOURCE = "https://vocab.sentier.dev/sources/ef-3.1"
_CF_COLUMNS = ["method_id", "flow", "flow_name", "factor_value", "flow_context", "location"]
_VOCAB_COLUMNS = ["iri", "pref_label", "alt_labels", "cas_number", "source"]
_SIGNIFICANT_DIGITS = 12

#: EF 3.1 / ILCD reference-unit convention, keyed by the one method that fixes a
#: flow's unit; checked in this order because a flow may carry other methods too
#: (e.g. a fossil resource also has a climate-change factor, still reported in MJ).
_MJ_METHOD = "ef-3.1:resource-use-fossils"
_KBQ_METHOD = "ef-3.1:ionising-radiation-human-health"
_M3_METHOD = "ef-3.1:water-use"
_LAND_METHOD = "ef-3.1:land-use"


def normalise_cas(cas: str | None) -> str | None:
    """``007440-50-8`` -> ``7440-50-8``; blank -> None."""
    if cas is None:
        return None
    text = str(cas).strip()
    if not text:
        return None
    return re.sub(r"^0+(?=\d)", "", text)


def _code(iri: str) -> str:
    return str(iri).rsplit("/", 1)[-1]


@dataclass(frozen=True)
class EfFlow:
    code: str
    name: str
    context: tuple[str, ...]
    synonyms: tuple[str, ...] = ()
    cas: str | None = None

    @property
    def context_path(self) -> str:
        return " / ".join(self.context)

    @property
    def leaf(self) -> str:
        return leaf_of(self.context_path)

    @property
    def bucket(self) -> str | None:
        return bucket_of_ef_context(self.context_path)


class EfFlowIndex:
    """Lookup index over the EF 3.1 flows that carry at least one characterization factor.

    Every lookup's ``bucket`` argument must be a value produced by
    ``compartments.bucket_of_bafu_category`` or ``compartments.bucket_of_ef_context``
    (``"resource"``, not ``"resources"``); a bucket these functions would not produce
    simply matches nothing (``[]``), it is never an error. ``by_name``, ``by_synonym``
    and ``by_cas`` each return a fresh, code-sorted list; ``vector`` returns a fresh
    dict; ``identity`` returns a tuple sorted by ``method_id``. Callers may hold onto
    or mutate any of these results without affecting the index.
    """

    def __init__(self, flows: Iterable[EfFlow], vectors: dict[str, dict[str, float]]) -> None:
        self._flows: dict[str, EfFlow] = {}
        self._vectors: dict[str, dict[str, float]] = {code: dict(v) for code, v in vectors.items()}
        by_name: dict[tuple[str, str | None], list[EfFlow]] = {}
        by_synonym: dict[tuple[str, str | None], list[EfFlow]] = {}
        by_cas: dict[tuple[str, str | None], list[EfFlow]] = {}
        for flow in flows:
            self._flows[flow.code] = flow
            bucket = flow.bucket
            by_name.setdefault((flow.name.strip().lower(), bucket), []).append(flow)
            for synonym in flow.synonyms:
                by_synonym.setdefault((synonym.strip().lower(), bucket), []).append(flow)
            if flow.cas is not None:
                by_cas.setdefault((flow.cas, bucket), []).append(flow)

        def _sorted(
            index: dict[tuple[str, str | None], list[EfFlow]],
        ) -> dict[tuple[str, str | None], list[EfFlow]]:
            return {key: sorted(items, key=lambda f: f.code) for key, items in index.items()}

        self._by_name = _sorted(by_name)
        self._by_synonym = _sorted(by_synonym)
        self._by_cas = _sorted(by_cas)
        self._sorted_flows: tuple[EfFlow, ...] = tuple(
            sorted(self._flows.values(), key=lambda f: f.code)
        )

    @classmethod
    def from_tables(cls, cf_rows: Iterable[dict], vocab_rows: Iterable[dict]) -> "EfFlowIndex":
        """Build an index from CF table rows and vocab shard rows already loaded in memory."""
        labels = {_code(r["iri"]): r for r in vocab_rows if (r.get("source") or "") == EF_SOURCE}
        contexts: dict[str, tuple[str, str]] = {}
        vectors: dict[str, dict[str, float]] = {}
        for r in cf_rows:
            try:
                code = _code(r["flow"])
                context = str(r.get("flow_context") or "")
                if context:
                    # the CF table carries one context and one location-less factor
                    # per (flow, method); the first context seen for a flow wins.
                    contexts.setdefault(code, (str(r.get("flow_name") or ""), context))
                if r.get("location"):
                    continue  # location-specific factor: not part of flow identity
                # factors are rounded so CF vectors compare equal across the parquet
                # round trip; this rounding is what CF-identity disambiguation relies on
                vectors.setdefault(code, {})[r["method_id"]] = float(
                    "%.*g" % (_SIGNIFICANT_DIGITS, float(r["factor_value"]))
                )
            except (KeyError, TypeError, ValueError) as exc:
                raise ParseError(f"CF row for flow {r.get('flow')!r}: {exc}") from exc
        flows = [
            EfFlow(
                code=code,
                name=((labels.get(code) or {}).get("pref_label") or cf_name).strip(),
                context=tuple(p.strip() for p in context.split("/") if p.strip()),
                synonyms=tuple(str(s) for s in ((labels.get(code) or {}).get("alt_labels") or [])),
                cas=normalise_cas((labels.get(code) or {}).get("cas_number")),
            )
            for code, (cf_name, context) in contexts.items()
        ]
        # a flow only exists once it has a context; drop any vector accumulated for a
        # context-less CF row so it cannot outlive the flow it would have belonged to
        return cls(flows, {code: v for code, v in vectors.items() if code in contexts})

    @classmethod
    def from_files(cls, cf_parquet: Path, vocab_dir: Path) -> "EfFlowIndex":
        """Build an index by reading the CF parquet file and every vocab shard parquet file."""
        cf_rows = pq.read_table(cf_parquet, columns=_CF_COLUMNS).to_pylist()
        vocab_rows = []
        for path in sorted(Path(vocab_dir).glob("*.parquet")):
            vocab_rows += pq.read_table(path, columns=_VOCAB_COLUMNS).to_pylist()
        return cls.from_tables(cf_rows, vocab_rows)

    def get(self, code: str) -> EfFlow | None:
        """Return the flow for ``code``, or ``None`` if it is not indexed."""
        return self._flows.get(code)

    def vector(self, code: str) -> dict[str, float]:
        """Return a copy of the CF vector (``method_id`` -> factor) for ``code``, or ``{}``."""
        return dict(self._vectors.get(code, {}))

    def identity(self, code: str) -> tuple[tuple[str, float], ...]:
        """Hashable CF-identity key: the vector's ``(method_id, factor)`` pairs, sorted."""
        return tuple(sorted(self._vectors.get(code, {}).items()))

    def reference_unit(self, code: str) -> str:
        """The EF 3.1 reference unit for ``code``, inferred from method membership.

        The CF table (``characterization-factors.parquet``) carries no flow-unit
        column at all -- only a ``factor_value`` per ``(flow, method)`` -- so a
        flow's physical unit is not data to look up but a convention to apply: EF
        3.1 / ILCD fixes one reference unit per impact-category family, and every
        flow that family characterises is reported in it. A resource-use-fossils
        flow is in megajoule, an ionising-radiation-human-health flow in kBq, a
        water-use flow in cubic meter, a land-use flow in m2 (or m2*a when the
        flow is an "occupation", not a "transformation"); everything else -- the
        overwhelming majority, all substance emissions and non-energy/water/land
        resources -- is in kilogram.
        """
        methods = set(self.vector(code))
        if _MJ_METHOD in methods:
            return "megajoule"
        if _KBQ_METHOD in methods:
            return "kBq"
        if _M3_METHOD in methods:
            return "cubic meter"
        if _LAND_METHOD in methods:
            flow = self.get(code)
            name = flow.name.lower() if flow is not None else ""
            return "m2*a" if name.startswith("occupation") else "m2"
        return "kilogram"

    def by_name(self, name: str, bucket: str | None) -> list[EfFlow]:
        """Flows in ``bucket`` whose label matches ``name`` (case- and whitespace-insensitive)."""
        return list(self._by_name.get((name.strip().lower(), bucket), []))

    def by_synonym(self, name: str, bucket: str | None) -> list[EfFlow]:
        """Flows in ``bucket`` whose synonym matches ``name`` (case/whitespace-insensitive)."""
        return list(self._by_synonym.get((name.strip().lower(), bucket), []))

    def by_cas(self, cas: str | None, bucket: str | None) -> list[EfFlow]:
        """Flows in ``bucket`` whose CAS matches ``cas``, normalised; ``[]`` for ``None``."""
        normalised = normalise_cas(cas)
        if normalised is None:
            return []
        return list(self._by_cas.get((normalised, bucket), []))

    def __len__(self) -> int:
        return len(self._flows)

    def __iter__(self) -> Iterator[EfFlow]:
        """Iterate all indexed flows, sorted by code."""
        return iter(self._sorted_flows)
