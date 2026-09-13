"""Brightway-context crosswalk for EF 3.1 flows that carry no characterization factor.

Every EF 3.1 vocab row (sentier-vocab ``elementary-flows/*.parquet``, ``source ==
ef-3.1``) carries an ``additional_notations`` list with exactly one
``bw-context:<code>`` entry -- the Brightway/ecoinvent context code the flow was
imported under. For the ~89,070 flows that carry a factor, the CF table
(``characterization-factors.parquet``) already gives the flow's EF context path
directly, so this crosswalk is never consulted for them. For the ~3,971 flows that
carry no factor, the CF table has nothing to say and the ``bw-context`` code is the
only link back to an EF context.

``BW_CONTEXT_PATH`` below was derived 2026-09-13 by joining every *characterised*
flow's code (``characterization-factors.parquet``, sentier-methods) to its CF-table
context through its ``bw-context`` code (``elementary-flows/*.parquet``,
sentier-vocab), and taking, for each code, the EF context path the overwhelming
majority of its rows carry. For 19 of the 24 codes this join is unambiguous or nearly
so (a handful of stray rows, well under 1%, sit elsewhere); ``AMBIGUOUS_CODES`` flags
the five where a real, non-trivial minority sits on a different leaf, giving the
majority (of characterised rows carrying that code) / runner-up counts:

- ``envi-air-hist15me``: 6,921 / 8 (of 6,929) -- the 8 split 5 "non-urban air very
  high stack" / 3 "urban air very high stack"; ``AMBIGUOUS_CODES`` names the larger.
- ``envi-air-grle-ru10pesq``: 6,916 / 10 (of 6,926) -- "non-urban air close to ground".
- ``envi-grou-unkn``: 3,550 / 3,178 (of 6,728) -- "non-agricultural soil". The worst
  case by far (47.2%): "soil, unspecified" is barely the majority leaf at all.
- ``envi-wate-unkn``: 6,650 / 105 (of 6,758) -- "water, unspecified (long-term)"; a
  further 3 rows are "Resources / Resources from water / ..." entirely, a
  bucket-crossing minority smaller than the long-term one and not the runner-up.
- ``reso-grou``: 48 / 6 (of 54) -- "Non-renewable energy resources from ground".

An uncharacterised row placed via one of these five codes gets the majority leaf,
marked ``context_uncertain`` so a caller can choose to distrust it.

Four codes (``envi-grou-indu``, ``reso-wate``, ``reso-air``, ``reso-biot``,
collected in ``NEVER_CHARACTERISED_CODES``) occur only on uncharacterised rows, so no
join is possible for them at all; their EF leaf is inferred from the code's own name
and is unambiguous by inspection. ``envi-air-grle-unkn`` -- despite its ``air`` name,
crosswalking to a *resource* leaf ("Renewable material resources from air") -- rests
on only 2 characterised rows: thin evidence, but the join is unanimous and the leaf is
otherwise inert (see ``compartments.py`` on why BAFU cannot reach it anyway). The four
stack-height codes (``envi-air-lost25me-ur10pesq``, ``envi-air-lost25me-ru10pesq``,
``envi-air-mest15me-ur10pesq``, ``envi-air-mest15me-ru10pesq``) place seven
uncharacterised rows total (2, 2, 2, 1) onto real EF leafs that BAFU's own vocabulary
has no subCategory for at all (see ``compartments.KNOWN_SUBCATEGORIES``): those seven
flows are indexed by ``include_uncharacterised=True`` like any other, but unreachable
from a BAFU-sourced match. ``envi-biot`` (biotic emission) has no EF leaf at all -- EF
3.1's characterised index has no "emissions to biosphere" context -- so it maps to
``None`` and any flow carrying it is skipped, never placed; see ``compartments.py``
for the one place this is not quite the whole story once uncharacterised resource
flows are in the index (``reso-biot``, not ``envi-biot``).

This table is a snapshot of the sentier-vocab / sentier-methods data as it stood when
derived; ``tests/matching/test_bw_context.py`` re-derives the majority (and runner-up)
leaf from the real inputs (when available, resolved via the ``SENTIER_METHODS_CF`` /
``SENTIER_VOCAB_FLOWS`` environment variables) and pins it against this table, to
catch drift.
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

#: The five ``bw-context`` codes whose majority-leaf join is not clean, mapped to the
#: full EF context path of the runner-up leaf (see the module docstring for the exact
#: majority/runner-up counts). ``BW_CONTEXT_PATH`` still gives these codes the
#: majority leaf; this dict is what makes ``context_for`` mark such a placement
#: ``uncertain``. ``envi-wate-unkn``'s runner-up here (105 rows, "(long-term)") is
#: distinct from -- and larger than -- its separate 3-row bucket-crossing minority
#: ("Resources from water"), which is not represented here at all.
AMBIGUOUS_CODES: dict[str, str] = {
    "envi-air-hist15me": (
        "Emissions / Emissions to air / Emissions to non-urban air very high stack"
    ),
    "envi-air-grle-ru10pesq": (
        "Emissions / Emissions to air / Emissions to non-urban air close to ground"
    ),
    "envi-grou-unkn": "Emissions / Emissions to soil / Emissions to non-agricultural soil",
    "envi-wate-unkn": (
        "Emissions / Emissions to water / Emissions to water, unspecified (long-term)"
    ),
    "reso-grou": (
        "Resources / Resources from ground / Non-renewable energy resources from ground"
    ),
}

#: ``bw-context`` codes that occur only on uncharacterised rows -- no flow carrying
#: them ever has a CF-table factor, so no majority-leaf join against the CF table is
#: possible for them at all (their ``BW_CONTEXT_PATH`` entry is inferred from the
#: code's own name instead). ``envi-biot`` is not included here even though it is also
#: never characterised: it maps to ``None`` in ``BW_CONTEXT_PATH``, not a leaf, so it
#: is excluded from any leaf-derivation exercise by that check alone.
NEVER_CHARACTERISED_CODES: frozenset[str] = frozenset(
    {"envi-grou-indu", "reso-wate", "reso-air", "reso-biot"}
)


def _bw_code(notations: Iterable[str]) -> str | None:
    """The code in a row's first ``bw-context:<code>`` notation, or ``None``.

    Ignores any non-string entry in ``notations`` (defensive: the real data has none,
    but a caller building rows by hand should not have to guarantee that).
    """
    for notation in notations:
        if isinstance(notation, str) and notation.startswith("bw-context:"):
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
