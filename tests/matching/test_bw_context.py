import collections
import glob
import os
from pathlib import Path

import pyarrow.parquet as pq
import pytest
from sentier_importers.matching.bw_context import (
    AMBIGUOUS_CODES,
    BW_CONTEXT_PATH,
    NEVER_CHARACTERISED_CODES,
    context_for,
)
from sentier_importers.matching.compartments import (
    BAFU_BUCKET,
    KNOWN_SUBCATEGORIES,
    bucket_of_ef_context,
    leaf_matches,
)

_DEFAULT_CF = "/home/laurenz/dds/sentier-methods/data/01-ef-3.1/characterization-factors.parquet"
_DEFAULT_VOCAB_DIR = "/home/laurenz/dds/sentier-vocab/data/elementary-flows"
_REAL_CF = Path(os.environ.get("SENTIER_METHODS_CF", _DEFAULT_CF))
_REAL_VOCAB_DIR = Path(os.environ.get("SENTIER_VOCAB_FLOWS", _DEFAULT_VOCAB_DIR))
_EF_SOURCE = "https://vocab.sentier.dev/sources/ef-3.1"

#: BAFU has no subCategory at all for a "low stack" / "high stack" distinction (only
#: "high. pop." / "low. pop." close-to-ground vs. high-stack), so these four codes'
#: leafs can never be reached from a BAFU flow through ``leaf_matches`` no matter what
#: ``(category, subcategory)`` pair is tried -- excluded from the placeability test.
_NO_BAFU_SUBCATEGORY = frozenset(
    {
        "envi-air-lost25me-ur10pesq",
        "envi-air-lost25me-ru10pesq",
        "envi-air-mest15me-ur10pesq",
        "envi-air-mest15me-ru10pesq",
    }
)
#: ``envi-air-grle-unkn`` crosswalks (on 2 characterised rows) to a *resource* leaf
#: despite its "air" name -- a vocab quirk documented in ``bw_context``'s module
#: docstring -- excluded from the placeability test for that reason, independent of
#: the stack-height exclusion above.
_VOCAB_QUIRK = frozenset({"envi-air-grle-unkn"})


def test_all_24_keys_present():
    assert len(BW_CONTEXT_PATH) == 24


def test_ambiguous_codes_are_flagged_uncertain():
    assert len(AMBIGUOUS_CODES) == 5
    for code in AMBIGUOUS_CODES:
        path, uncertain = context_for([f"bw-context:{code}"])
        assert path == BW_CONTEXT_PATH[code]
        assert uncertain is True


def test_non_ambiguous_known_codes_are_not_uncertain():
    for code, path in BW_CONTEXT_PATH.items():
        if path is None or code in AMBIGUOUS_CODES:
            continue
        assert context_for([f"bw-context:{code}"]) == (path, False)


def test_envi_biot_has_no_ef_leaf():
    assert BW_CONTEXT_PATH["envi-biot"] is None
    assert context_for(["bw-context:envi-biot"]) == (None, False)


def test_unknown_code_returns_none():
    assert context_for(["bw-context:not-a-real-code"]) == (None, False)


def test_row_without_bw_notation_returns_none():
    assert context_for([]) == (None, False)
    assert context_for(["ec:807-840-4"]) == (None, False)


def test_first_bw_context_notation_wins():
    # a row should never carry two, but context_for must still be deterministic
    path, uncertain = context_for(["bw-context:laus-occu", "bw-context:laus-tran"])
    assert path == BW_CONTEXT_PATH["laus-occu"]
    assert uncertain is False


def test_non_string_notations_are_ignored():
    # real rows never carry a non-string additional_notations entry, but context_for
    # must not blow up on one built by hand (e.g. a stray None from a lossy round trip)
    assert context_for([None, "bw-context:laus-occu"]) == (BW_CONTEXT_PATH["laus-occu"], False)
    assert context_for([None]) == (None, False)


def test_every_placeable_path_has_a_bucket_and_is_exact_placeable():
    """Every non-``None`` ``BW_CONTEXT_PATH`` leaf, except the ones BAFU's own
    vocabulary has no route to at all, must resolve to a real bucket and be reachable
    (``Placement.EXACT`` via ``leaf_matches``) from at least one
    ``(category, subcategory)`` pair drawn from ``compartments.KNOWN_SUBCATEGORIES`` --
    otherwise an uncharacterised flow placed there could never be matched by anything
    BAFU-sourced, silently.

    Skipped, each for its own documented reason (see the module-level frozensets
    above): the four stack-height codes (``envi-air-lost25me-ur10pesq``,
    ``envi-air-lost25me-ru10pesq``, ``envi-air-mest15me-ur10pesq``,
    ``envi-air-mest15me-ru10pesq`` -- BAFU has no stack-height subCategory at all) and
    ``envi-air-grle-unkn`` (a vocab quirk: an "air" code crosswalking to a resource
    leaf, on just 2 characterised rows).
    """
    skipped = _NO_BAFU_SUBCATEGORY | _VOCAB_QUIRK
    checked = 0
    for code, path in BW_CONTEXT_PATH.items():
        if path is None or code in skipped:
            continue
        checked += 1
        bucket = bucket_of_ef_context(path)
        assert bucket is not None, code
        placeable = any(
            leaf_matches(category, subcategory, path)
            for category in BAFU_BUCKET
            for subcategory in KNOWN_SUBCATEGORIES
        )
        assert placeable, code
    assert checked == len(BW_CONTEXT_PATH) - 1 - len(skipped)  # -1 for envi-biot (None)


@pytest.mark.skipif(
    not (_REAL_CF.exists() and _REAL_VOCAB_DIR.exists()),
    reason="real EF inputs not available",
)
def test_pins_majority_leaf_against_real_data():
    """Re-derive the majority (and, for ambiguous codes, runner-up) EF context path
    per bw-context code from the real CF table and vocab shards, and assert both match
    this module's checked-in tables exactly (full path strings, not just the leaf) --
    this guards the crosswalk against drift in the underlying data. Also asserts the
    set of codes this derivation actually sees equals the set of codes expected to
    have characterised rows at all (``BW_CONTEXT_PATH`` minus the ``None`` entry and
    minus ``NEVER_CHARACTERISED_CODES``), so a code silently vanishing from -- or
    newly appearing in -- the characterised data is caught too.
    """
    cf_rows = pq.read_table(_REAL_CF, columns=["flow", "flow_context"]).to_pylist()
    code_to_ctx: dict[str, str] = {}
    for row in cf_rows:
        if row["flow_context"]:
            code = row["flow"].rsplit("/", 1)[-1]
            code_to_ctx.setdefault(code, row["flow_context"])

    vocab_rows = []
    for path in sorted(glob.glob(str(_REAL_VOCAB_DIR / "*.parquet"))):
        vocab_rows += pq.read_table(
            path, columns=["iri", "source", "additional_notations"]
        ).to_pylist()
    ef_rows = [r for r in vocab_rows if (r.get("source") or "") == _EF_SOURCE]

    leafs_by_bw_code: dict[str, collections.Counter] = collections.defaultdict(collections.Counter)
    for row in ef_rows:
        code = row["iri"].rsplit("/", 1)[-1]
        context = code_to_ctx.get(code)
        if context is None:
            continue  # uncharacterised: no CF-table context to join against
        bw_code = None
        for notation in row.get("additional_notations") or []:
            if notation.startswith("bw-context:"):
                bw_code = notation.split(":", 1)[1]
                break
        if bw_code is not None:
            leafs_by_bw_code[bw_code][context] += 1

    assert leafs_by_bw_code, "expected at least one characterised row with a bw-context notation"

    expected_codes = {c for c, p in BW_CONTEXT_PATH.items() if p is not None}
    expected_codes -= NEVER_CHARACTERISED_CODES
    assert set(leafs_by_bw_code) == expected_codes

    for bw_code, counter in leafs_by_bw_code.items():
        ranked = counter.most_common()
        majority_context, _ = ranked[0]
        expected_path = BW_CONTEXT_PATH[bw_code]
        assert expected_path is not None, bw_code
        assert majority_context == expected_path, bw_code
        if bw_code in AMBIGUOUS_CODES:
            assert len(ranked) >= 2, bw_code
            runner_up_context, _ = ranked[1]
            assert runner_up_context == AMBIGUOUS_CODES[bw_code], bw_code
