"""EF 3.1 flow index built from public inputs only.

- ``characterization-factors.parquet`` (sentier-methods): which EF flows carry a
  factor, their context path, and their CF vector by ``method_id``. A flow that is
  not in this table has no factor and is not a mapping target.
- ``elementary-flows/*.parquet`` (sentier-vocab, rows with the ef-3.1 source):
  preferred label, synonyms (``alt_labels``) and CAS number, keyed by the flow IRI.

The CF vector keeps the global (location-less) factor per method; location-specific
rows (land use, water use, some regionalised categories) are not part of flow identity.

An EF vocab row that carries no factor at all (``characterised=False``) can still be
placed and indexed, opt-in via ``include_uncharacterised``: its context comes not from
the CF table (it has none) but from its ``additional_notations`` ``bw-context:<code>``
entry, resolved through the ``matching.bw_context`` crosswalk. A code with no EF leaf
(``envi-biot``) or a row with no ``bw-context`` notation at all is skipped, not indexed.
"""

from __future__ import annotations

import io
import re
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path

import pyarrow.parquet as pq
from sentier_importers.core.errors import ParseError
from sentier_importers.matching.bw_context import context_for
from sentier_importers.matching.compartments import bucket_of_ef_context, leaf_of

EF_SOURCE = "https://vocab.sentier.dev/sources/ef-3.1"
_CF_COLUMNS = ["method_id", "flow", "flow_name", "factor_value", "flow_context", "location"]
_VOCAB_COLUMNS = [
    "iri",
    "pref_label",
    "alt_labels",
    "cas_number",
    "source",
    "additional_notations",
]
_SIGNIFICANT_DIGITS = 12

#: EF 3.1 / ILCD reference-unit convention, keyed by the one method that fixes a
#: flow's unit; checked in this order because a flow may carry other methods too
#: (e.g. a fossil resource also has a climate-change factor, still reported in MJ).
_MJ_METHOD = "ef-3.1:resource-use-fossils"
_KBQ_METHOD = "ef-3.1:ionising-radiation-human-health"
_M3_METHOD = "ef-3.1:water-use"
_LAND_METHOD = "ef-3.1:land-use"

#: An uncharacterised vocab row's context comes only from the ``bw-context`` crosswalk
#: (``matching.bw_context.BW_CONTEXT_PATH``), and that crosswalk has no code at all for
#: EF's ``Non-renewable energy resources from ground`` leaf or for any renewable-energy
#: resource branch (its resource codes only ever reach the element/material leaves --
#: ``reso-grou``'s own leaf is literally "...element resources...", see
#: ``bw_context.py``). So a BAFU/EF resource flow that is actually an ENERGY resource by
#: name (an ecoinvent-style "Energy, <form>, converted", a "Primary Energy ..." label,
#: an oil-sand or pit-methane flow) can never land on the right branch through this
#: crosswalk, no matter which code placed it -- it is always on the wrong (element or
#: material) leaf. Such a row is marked ``context_uncertain`` unconditionally, not just
#: when its code happens to be one of ``bw_context.AMBIGUOUS_CODES``; also imported by
#: ``mappings_biosphere_matched.BafuEfMatchedSource.entry_for`` (decision (f)(2),
#: 2026-09-13), which -- rather than withhold a nomenclature-package match onto one of
#: these -- omits ``target["context"]`` entirely from the emitted entry and discloses
#: that the EF context is not recoverable from the source context code in the comment
#: instead.
UNCERTAIN_RESOURCE_NAME = re.compile(r"^(Energy|Primary Energy|Oil Sand|Pit Methane)\b", re.I)


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
    #: ``False`` for a flow indexed only via ``include_uncharacterised`` (no CF
    #: vector at all -- ``vector``/``identity`` are ``{}``/``()`` and
    #: ``reference_unit`` is ``None`` for it).
    characterised: bool = True
    #: ``True`` when this flow's context was resolved through one of
    #: ``bw_context.AMBIGUOUS_CODES`` -- the majority leaf was used, but a real
    #: minority of flows under that code sit elsewhere. Always ``False`` for a
    #: characterised flow.
    context_uncertain: bool = False

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
    """Lookup index over EF 3.1 flows: by default only those that carry at least one
    characterization factor, plus uncharacterised ones too when built with
    ``include_uncharacterised=True`` (see ``from_tables``).

    Every lookup's ``bucket`` argument must be a value produced by
    ``compartments.bucket_of_bafu_category`` or ``compartments.bucket_of_ef_context``
    (``"resource"``, not ``"resources"``); a bucket these functions would not produce
    simply matches nothing (``[]``), it is never an error. ``by_name``, ``by_synonym``
    and ``by_cas`` each return a fresh, code-sorted list; ``vector`` returns a fresh
    dict; ``identity`` returns a tuple sorted by ``method_id``. Callers may hold onto
    or mutate any of these results without affecting the index.
    """

    def __init__(
        self,
        flows: Iterable[EfFlow],
        vectors: dict[str, dict[str, float]],
        *,
        includes_uncharacterised: bool = False,
    ) -> None:
        #: Mirrors the ``include_uncharacterised`` flag this index was built with (see
        #: ``from_tables``) -- not whether any uncharacterised flow actually ended up
        #: indexed, just what the index was asked to include. ``MatchPipeline`` reads
        #: this to phrase a ``no_ef_flow`` detail honestly: "with a factor" only makes
        #: sense to say when the search really was restricted to factor-bearing flows.
        self.includes_uncharacterised = includes_uncharacterised
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
    def from_tables(
        cls,
        cf_rows: Iterable[dict],
        vocab_rows: Iterable[dict],
        *,
        include_uncharacterised: bool = False,
    ) -> "EfFlowIndex":
        """Build an index from CF table rows and vocab shard rows already loaded in memory.

        When ``include_uncharacterised`` is ``True``, every EF vocab row whose code
        carries no CF-table context is also indexed (``EfFlow.characterised=False``),
        placed via ``bw_context.context_for`` on its ``additional_notations``. A row
        whose ``bw-context`` code has no EF leaf, or that carries no ``bw-context``
        notation at all, is skipped. Default ``False`` reproduces the exact previous
        behaviour (characterised flows only).
        """
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
        if include_uncharacterised:
            for code, row in labels.items():
                if code in contexts:
                    continue  # already indexed as a characterised flow
                path, uncertain = context_for(row.get("additional_notations") or [])
                if path is None:
                    continue  # no EF leaf for this code, or no bw-context notation at all
                name = (row.get("pref_label") or "").strip()
                if bucket_of_ef_context(path) == "resource" and UNCERTAIN_RESOURCE_NAME.match(
                    name
                ):
                    # the crosswalk cannot reach an energy resource leaf at all (see
                    # UNCERTAIN_RESOURCE_NAME) -- this placement is wrong regardless
                    # of which code produced it.
                    uncertain = True
                flows.append(
                    EfFlow(
                        code=code,
                        name=name,
                        context=tuple(p.strip() for p in path.split("/") if p.strip()),
                        synonyms=tuple(str(s) for s in (row.get("alt_labels") or [])),
                        cas=normalise_cas(row.get("cas_number")),
                        characterised=False,
                        context_uncertain=uncertain,
                    )
                )
        # a characterised flow only exists once it has a context; drop any vector
        # accumulated for a context-less CF row so it cannot outlive the flow it
        # would have belonged to
        return cls(
            flows,
            {code: v for code, v in vectors.items() if code in contexts},
            includes_uncharacterised=include_uncharacterised,
        )

    @classmethod
    def from_files(
        cls, cf_parquet: Path, vocab_dir: Path, *, include_uncharacterised: bool = False
    ) -> "EfFlowIndex":
        """Build an index by reading the CF parquet file and every vocab shard parquet file."""
        cf_rows = pq.read_table(cf_parquet, columns=_CF_COLUMNS).to_pylist()
        return cls.from_tables(
            cf_rows,
            cls._read_vocab_dir(vocab_dir),
            include_uncharacterised=include_uncharacterised,
        )

    @classmethod
    def from_bytes(
        cls, cf_parquet_bytes: bytes, vocab_dir: Path, *, include_uncharacterised: bool = False
    ) -> "EfFlowIndex":
        """Build an index from the CF parquet file's raw bytes (e.g. already fetched
        through the content-addressed cache, so the cache/offline contract holds) and
        every vocab shard parquet file read from ``vocab_dir`` on disk.
        """
        cf_rows = pq.read_table(io.BytesIO(cf_parquet_bytes), columns=_CF_COLUMNS).to_pylist()
        return cls.from_tables(
            cf_rows,
            cls._read_vocab_dir(vocab_dir),
            include_uncharacterised=include_uncharacterised,
        )

    @staticmethod
    def _read_vocab_dir(vocab_dir: Path) -> list[dict]:
        vocab_rows = []
        for path in sorted(Path(vocab_dir).glob("*.parquet")):
            vocab_rows += pq.read_table(path, columns=_VOCAB_COLUMNS).to_pylist()
        return vocab_rows

    def get(self, code: str) -> EfFlow | None:
        """Return the flow for ``code``, or ``None`` if it is not indexed."""
        return self._flows.get(code)

    def vector(self, code: str) -> dict[str, float]:
        """Return a copy of the CF vector (``method_id`` -> factor) for ``code``, or ``{}``."""
        return dict(self._vectors.get(code, {}))

    def identity(self, code: str) -> tuple[tuple[str, float], ...]:
        """Hashable CF-identity key: the vector's ``(method_id, factor)`` pairs, sorted."""
        return tuple(sorted(self._vectors.get(code, {}).items()))

    def reference_unit(self, code: str) -> str | None:
        """The EF 3.1 reference unit for ``code``, inferred from method membership.

        Returns ``None`` when ``code`` is an uncharacterised flow
        (``EfFlow.characterised=False``): it carries no CF vector at all, so there is
        no method membership to infer a unit from. An unknown ``code`` (not indexed
        at all) is unaffected by this and still defaults to ``"kilogram"`` below, same
        as before.

        The CF table (``characterization-factors.parquet``) carries no flow-unit
        column at all -- only a ``factor_value`` per ``(flow, method)`` -- so a
        flow's physical unit is not data to look up but a convention to apply: EF
        3.1 / ILCD fixes one reference unit per impact-category family, and every
        flow that family characterises is reported in it. A resource-use-fossils
        flow is in megajoule, an ionising-radiation-human-health flow in kBq, a
        water-use flow in cubic meter, a land-use flow in m2 (or m2*a when its EF
        context leaf is ``land occupation``, not ``land transformation``); everything
        else -- the overwhelming majority, all substance emissions and non-energy/
        water/land resources -- is in kilogram.

        The land-use unit is keyed on the EF context *leaf*, not the flow name: EF
        occupation flows are named ``Arable``, ``Pasture/meadow`` and so on, never
        anything starting with "occupation" -- biosphere-1-curated's published payload uses
        ``m2*a`` for exactly the flows whose leaf is ``land occupation``.
        """
        flow = self.get(code)
        if flow is not None and not flow.characterised:
            return None
        methods = set(self.vector(code))
        if _MJ_METHOD in methods:
            return "megajoule"
        if _KBQ_METHOD in methods:
            return "kBq"
        if _M3_METHOD in methods:
            return "cubic meter"
        if _LAND_METHOD in methods:
            return "m2*a" if flow is not None and flow.leaf == "land occupation" else "m2"
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
