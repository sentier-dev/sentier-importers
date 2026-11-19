import io

import pyarrow as pa
import pyarrow.parquet as pq
from sentier_importers.sources.brightway_flows.factors import compare, factor_vectors

from tests.matching.ef_fixtures import AIR_UNSPEC, CF_SCHEMA, cf_row

TOX = "ef-3.1:human-toxicity-cancer"
ECO = "ef-3.1:ecotoxicity-freshwater"


def _bytes(rows):
    sink = io.BytesIO()
    pq.write_table(pa.Table.from_pylist(rows, schema=CF_SCHEMA), sink)
    return sink.getvalue()


def test_vectors_are_keyed_by_method_and_location():
    vectors = factor_vectors(
        _bytes(
            [cf_row("a", "A", AIR_UNSPEC, TOX, 2.0), cf_row("a", "A", AIR_UNSPEC, TOX, 3.0, "DE")]
        )
    )
    assert vectors == {"a": {(TOX, ""): 2.0, (TOX, "DE"): 3.0}}


def test_same_non_zero_factors_are_identical_even_if_one_lists_a_zero():
    assert compare({(TOX, ""): 2.0, (ECO, ""): 0.0}, {(TOX, ""): 2.0}) == ("identical", ())


def test_two_flows_without_any_factor_are_identical():
    assert compare({}, {}) == ("identical", ())


def test_different_factors_name_the_methods():
    assert compare({(TOX, ""): 2.0}, {(TOX, ""): 5.0, (ECO, ""): 1.0}) == ("differ", (ECO, TOX))


def test_a_flow_that_loses_its_score_is_named_so():
    assert compare({(TOX, ""): 2.0}, {}) == ("theirs_zero", (TOX,))
    assert compare({}, {(TOX, ""): 2.0}) == ("ours_zero", (TOX,))
