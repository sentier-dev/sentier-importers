"""Factor vectors of EF 3.1 flows, per method and location, and their comparison.

``EfFlowIndex.vector`` is keyed by method only; a retarget must not change a
regionalised factor either, so the comparison here keeps the location.
"""

from __future__ import annotations

import io
from collections.abc import Mapping

import pyarrow.parquet as pq

Vector = Mapping[tuple[str, str], float]


def factor_vectors(cf_parquet: bytes) -> dict[str, dict[tuple[str, str], float]]:
    """``{flow code: {(method_id, location or ""): factor}}`` from sentier-methods' CF table."""
    table = pq.read_table(
        io.BytesIO(cf_parquet), columns=["method_id", "flow", "factor_value", "location"]
    )
    vectors: dict[str, dict[tuple[str, str], float]] = {}
    for row in table.to_pylist():
        code = row["flow"].rsplit("/", 1)[-1]
        vectors.setdefault(code, {})[(row["method_id"], row["location"] or "")] = row[
            "factor_value"
        ]
    return vectors


def compare(ours: Vector, theirs: Vector) -> tuple[str, tuple[str, ...]]:
    """``(kind, methods)``. ``kind`` is ``identical`` (same non-zero factors, so no score
    can change), ``theirs_zero`` / ``ours_zero`` (one side scores nothing at all) or
    ``differ``; ``methods`` are the method ids whose factor differs, sorted."""
    ours_scoring = {key: value for key, value in ours.items() if value}
    theirs_scoring = {key: value for key, value in theirs.items() if value}
    if ours_scoring == theirs_scoring:
        return "identical", ()
    methods = tuple(
        sorted(
            {
                method
                for (method, location) in set(ours_scoring) | set(theirs_scoring)
                if ours_scoring.get((method, location)) != theirs_scoring.get((method, location))
            }
        )
    )
    if not theirs_scoring:
        return "theirs_zero", methods
    if not ours_scoring:
        return "ours_zero", methods
    return "differ", methods
