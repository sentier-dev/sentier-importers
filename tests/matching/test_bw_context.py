import collections
import glob
from pathlib import Path

import pyarrow.parquet as pq
import pytest
from sentier_importers.matching.bw_context import AMBIGUOUS_CODES, BW_CONTEXT_PATH, context_for
from sentier_importers.matching.compartments import leaf_of

_REAL_CF = Path(
    "/home/laurenz/dds/sentier-methods/data/01-ef-3.1/characterization-factors.parquet"
)
_REAL_VOCAB_DIR = Path("/home/laurenz/dds/sentier-vocab/data/elementary-flows")
_EF_SOURCE = "https://vocab.sentier.dev/sources/ef-3.1"


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


@pytest.mark.skipif(
    not (_REAL_CF.exists() and _REAL_VOCAB_DIR.exists()),
    reason="real EF inputs not available",
)
def test_pins_majority_leaf_against_real_data():
    """Re-derive the majority EF leaf per bw-context code from the real CF table and
    vocab shards, and assert it matches ``leaf_of(BW_CONTEXT_PATH[code])`` for every
    code that occurs on a characterised row -- this guards the checked-in crosswalk
    against drift in the underlying data.
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
    for bw_code, counter in leafs_by_bw_code.items():
        majority_context, _ = counter.most_common(1)[0]
        expected_path = BW_CONTEXT_PATH[bw_code]
        assert expected_path is not None, bw_code
        assert leaf_of(majority_context) == leaf_of(expected_path), bw_code
