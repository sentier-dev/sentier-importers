"""Brightway-context crosswalk for EF 3.1 flows that carry no characterization factor.

Every EF 3.1 vocab row (sentier-vocab ``elementary-flows/*.parquet``, ``source ==
ef-3.1``) carries an ``additional_notations`` list with exactly one
``bw-context:<code>`` entry -- the Brightway/ecoinvent context code the flow was
imported under. For the ~89,070 flows that carry a factor, the CF table
(``characterization-factors.parquet``) already gives the flow's EF context path
directly, so this crosswalk is never consulted for them. For the ~3,971 flows that
carry no factor, the CF table has nothing to say and the ``bw-context`` code is the
only link back to an EF context.

``BW_CONTEXT_PATH`` below was derived by joining every *characterised* flow's code to
its CF-table context through its ``bw-context`` code and taking, for each code, the EF
context path the overwhelming majority of its rows carry. For 19 of the 24 codes this
join is unambiguous or nearly so; ``AMBIGUOUS_CODES`` flags the five where a real,
non-trivial minority of rows (up to ~46%) sit on a different leaf -- an uncharacterised
row placed via one of those codes gets the majority leaf, marked ``context_uncertain``
so a caller can choose to distrust it. Four codes (``envi-grou-indu``, ``reso-wate``,
``reso-air``, ``reso-biot``) occur only on uncharacterised rows, so no join is possible
for them at all; their EF leaf is inferred from the code's own name and is unambiguous
by inspection. ``envi-biot`` (biotic emission) has no EF leaf at all -- EF 3.1 has no
"emissions to biosphere" context -- so it maps to ``None`` and any flow carrying it is
skipped, never placed.

This table is a snapshot of the sentier-vocab / sentier-methods data as it stood when
derived; ``tests/matching/test_bw_context.py`` re-derives the majority leaf from the
real inputs (when available) and pins it against this table, to catch drift.
"""

from __future__ import annotations

from collections.abc import Iterable

#: ``bw-context`` code -> the EF 3.1 context path it maps to (full ``" / "``-joined
#: path, matching ``EfFlow.context_path``), or ``None`` when there is no EF leaf at all
#: (``envi-biot``, a biotic emission).
BW_CONTEXT_PATH: dict[str, str | None] = {
    "envi-air-unkn": "Emissions / Emissions to air / Emissions to air, unspecified",
    "envi-air-grle-ur10pesq": (
        "Emissions / Emissions to air / Emissions to urban air close to ground"
    ),
    "envi-air-hist15me": (
        "Emissions / Emissions to air / Emissions to non-urban air or from high stacks"
    ),
    "envi-air-grle-ru10pesq": (
        "Emissions / Emissions to air / Emissions to air, unspecified (long-term)"
    ),
    "envi-air-aicrhe": (
        "Emissions / Emissions to air / Emissions to lower stratosphere and upper troposphere"
    ),
    "envi-air-indr-unkn": "Emissions / Emissions to air / Emissions to air, indoor",
    "envi-air-lost25me-ur10pesq": (
        "Emissions / Emissions to air / Emissions to urban air low stack"
    ),
    "envi-air-lost25me-ru10pesq": (
        "Emissions / Emissions to air / Emissions to non-urban air low stack"
    ),
    "envi-air-mest15me-ur10pesq": (
        "Emissions / Emissions to air / Emissions to urban air high stack"
    ),
    "envi-air-mest15me-ru10pesq": (
        "Emissions / Emissions to air / Emissions to non-urban air high stack"
    ),
    "envi-air-grle-unkn": (
        "Resources / Resources from air / Renewable material resources from air"
    ),
    "envi-grou-agri": "Emissions / Emissions to soil / Emissions to agricultural soil",
    "envi-grou-unkn": "Emissions / Emissions to soil / Emissions to soil, unspecified",
    "envi-grou-indu": "Emissions / Emissions to soil / Emissions to non-agricultural soil",
    "envi-wate-suwa": "Emissions / Emissions to water / Emissions to fresh water",
    "envi-wate-ocea": "Emissions / Emissions to water / Emissions to sea water",
    "envi-wate-unkn": "Emissions / Emissions to water / Emissions to water, unspecified",
    "laus-occu": "Land use / Land occupation",
    "laus-tran": "Land use / Land transformation",
    "reso-grou": (
        "Resources / Resources from ground / Non-renewable element resources from ground"
    ),
    "reso-wate": "Resources / Resources from water / Renewable material resources from water",
    "reso-air": "Resources / Resources from air / Renewable material resources from air",
    "reso-biot": (
        "Resources / Resources from biosphere / Renewable material resources from biosphere"
    ),
    "envi-biot": None,
}

#: The five ``bw-context`` codes whose majority-leaf join is not clean, mapped to a
#: plain description of the alternative leaf a real minority of their rows carry.
#: ``BW_CONTEXT_PATH`` still gives these codes the majority leaf; this dict is what
#: makes ``context_for`` mark such a placement ``uncertain``.
AMBIGUOUS_CODES: dict[str, str] = {
    "envi-air-hist15me": "very high stack (urban or non-urban)",
    "envi-air-grle-ru10pesq": "non-urban air close to ground",
    "envi-grou-unkn": "non-agricultural soil",
    "envi-wate-unkn": "water, unspecified (long-term)",
    "reso-grou": "non-renewable energy resources from ground",
}


def _bw_code(notations: Iterable[str]) -> str | None:
    for notation in notations:
        if notation.startswith("bw-context:"):
            return notation.split(":", 1)[1]
    return None


def context_for(notations: Iterable[str]) -> tuple[str | None, bool]:
    """The EF context path a row's ``additional_notations`` cross-walks to, and
    whether that placement is uncertain.

    Reads the first ``bw-context:`` entry found in ``notations``. Returns
    ``(None, False)`` when there is none, when the code is not in
    ``BW_CONTEXT_PATH`` (unknown code), or when it maps to ``None`` (``envi-biot``,
    a biotic emission with no EF leaf). Otherwise returns ``(path, uncertain)``,
    where ``uncertain`` is ``True`` exactly when the code is one of
    ``AMBIGUOUS_CODES``.
    """
    code = _bw_code(notations)
    if code is None:
        return None, False
    path = BW_CONTEXT_PATH.get(code)
    if path is None:
        return None, False
    return path, code in AMBIGUOUS_CODES
