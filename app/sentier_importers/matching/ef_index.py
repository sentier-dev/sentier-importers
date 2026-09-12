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
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

import pyarrow.parquet as pq
from sentier_importers.matching.compartments import bucket_of_ef_context, leaf_of

EF_SOURCE = "https://vocab.sentier.dev/sources/ef-3.1"
_CF_COLUMNS = ["method_id", "flow", "flow_name", "factor_value", "flow_context", "location"]
_VOCAB_COLUMNS = ["iri", "pref_label", "alt_labels", "cas_number", "source"]
_SIGNIFICANT_DIGITS = 12


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
    synonyms: tuple[str, ...] = field(default=())
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
    def __init__(self, flows: Iterable[EfFlow], vectors: dict[str, dict[str, float]]) -> None:
        self._flows: dict[str, EfFlow] = {}
        self._vectors: dict[str, dict[str, float]] = dict(vectors)
        by_name: dict[tuple[str, str | None], list[EfFlow]] = {}
        by_synonym: dict[tuple[str, str | None], list[EfFlow]] = {}
        by_cas: dict[tuple[str, str | None], list[EfFlow]] = {}
        for flow in flows:
            self._flows[flow.code] = flow
            bucket = flow.bucket
            by_name.setdefault((flow.name.lower(), bucket), []).append(flow)
            for synonym in flow.synonyms:
                by_synonym.setdefault((synonym.lower(), bucket), []).append(flow)
            if flow.cas is not None:
                by_cas.setdefault((flow.cas, bucket), []).append(flow)

        def _sorted(index: dict[tuple[str, str | None], list[EfFlow]]) -> dict:
            return {key: sorted(items, key=lambda f: f.code) for key, items in index.items()}

        self._by_name = _sorted(by_name)
        self._by_synonym = _sorted(by_synonym)
        self._by_cas = _sorted(by_cas)

    @classmethod
    def from_tables(cls, cf_rows: Iterable[dict], vocab_rows: Iterable[dict]) -> "EfFlowIndex":
        labels = {_code(r["iri"]): r for r in vocab_rows if (r.get("source") or "") == EF_SOURCE}
        contexts: dict[str, tuple[str, str]] = {}
        vectors: dict[str, dict[str, float]] = {}
        for r in cf_rows:
            code = _code(r["flow"])
            context = str(r.get("flow_context") or "")
            if not context:
                continue  # a factor with no context cannot be placed
            contexts.setdefault(code, (str(r.get("flow_name") or ""), context))
            if r.get("location"):
                continue  # location-specific factor: not identity
            vectors.setdefault(code, {})[r["method_id"]] = float(
                "%.*g" % (_SIGNIFICANT_DIGITS, float(r["factor_value"]))
            )
        flows = [
            EfFlow(
                code=code,
                name=(labels.get(code) or {}).get("pref_label") or cf_name,
                context=tuple(p.strip() for p in context.split("/") if p.strip()),
                synonyms=tuple(str(s) for s in ((labels.get(code) or {}).get("alt_labels") or [])),
                cas=normalise_cas((labels.get(code) or {}).get("cas_number")),
            )
            for code, (cf_name, context) in contexts.items()
        ]
        return cls(flows, vectors)

    @classmethod
    def from_files(cls, cf_parquet: Path, vocab_dir: Path) -> "EfFlowIndex":
        cf_rows = pq.read_table(cf_parquet, columns=_CF_COLUMNS).to_pylist()
        vocab_rows = []
        for path in sorted(Path(vocab_dir).glob("*.parquet")):
            vocab_rows += pq.read_table(path, columns=_VOCAB_COLUMNS).to_pylist()
        return cls.from_tables(cf_rows, vocab_rows)

    def get(self, code: str) -> EfFlow | None:
        return self._flows.get(code)

    def vector(self, code: str) -> dict[str, float]:
        return dict(self._vectors.get(code, {}))

    def by_name(self, name: str, bucket: str | None) -> list[EfFlow]:
        return list(self._by_name.get((name.lower(), bucket), []))

    def by_synonym(self, name: str, bucket: str | None) -> list[EfFlow]:
        return list(self._by_synonym.get((name.lower(), bucket), []))

    def by_cas(self, cas: str | None, bucket: str | None) -> list[EfFlow]:
        normalised = normalise_cas(cas)
        if normalised is None:
            return []
        return list(self._by_cas.get((normalised, bucket), []))

    def __len__(self) -> int:
        return len(self._flows)
