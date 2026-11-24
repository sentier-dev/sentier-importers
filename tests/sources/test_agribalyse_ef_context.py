"""The exact EF context of an EF 3.1 flow, and its leaf (the ``sub_compartment``)."""

import pytest
from sentier_importers.core.errors import ParseError, ValidationError
from sentier_importers.sources.agribalyse import ef_context
from sentier_importers.sources.agribalyse.ef_context import EfContexts
from sentier_importers.sources.brightway_flows.correspondences import load

from tests.sources.ef_context_fixture import bw_bytes, entry, jrc_bytes

LT_WATER = "Emissions/Emissions to water/Emissions to water, unspecified (long-term)"
LT_WATER_PATH = "Emissions / Emissions to water / Emissions to water, unspecified (long-term)"
SOIL = ("Emissions", "Emissions to soil", "Emissions to agricultural soil")


def _bw(*links):
    return load(bw_bytes(links), None)


def test_the_leaf_is_the_last_level_of_a_path():
    assert ef_context.leaf(LT_WATER_PATH) == "Emissions to water, unspecified (long-term)"
    assert ef_context.leaf("Land use / Land occupation") == "Land occupation"


def test_path_spellings_of_both_sources_are_one_spelling():
    assert ef_context.correspondence_path(" Land use / Land occupation ") == (
        "Land use / Land occupation"
    )
    record = {"FLOW_class0": "Land use", "FLOW_class1": "Land occupation", "FLOW_class2": None}
    assert ef_context.jrc_path(record) == "Land use / Land occupation"


def test_correspondence_paths_are_each_flows_own_entry_also_when_merged():
    bw = _bw(entry("a", LT_WATER), entry("merged", "Land use/Land occupation", target="a"))
    assert ef_context.correspondence_paths(bw) == {
        "a": LT_WATER_PATH,
        "merged": "Land use / Land occupation",
    }


def test_two_paths_for_one_flow_in_one_source_are_refused():
    with pytest.raises(ValidationError, match="'a'.*EF correspondence"):
        ef_context.correspondence_paths(_bw(entry("a", LT_WATER), entry("a", "Land use/x")))
    with pytest.raises(ValidationError, match="'a'.*JRC"):
        ef_context.jrc_paths(jrc_bytes([("a", "x", *SOIL), ("a", "x", "Land use", "y", None)]))


def test_jrc_paths_read_the_cf_table_once_per_flow():
    rows = [("a", "x", *SOIL), ("a", "X", *SOIL), ("b", "y", "Land use", "Land occupation", None)]
    assert ef_context.jrc_paths(jrc_bytes(rows)) == {
        "a": "Emissions / Emissions to soil / Emissions to agricultural soil",
        "b": "Land use / Land occupation",
    }


def test_a_jrc_input_that_is_not_the_cf_table_is_a_parse_error():
    with pytest.raises(ParseError, match="JRC CF table"):
        ef_context.jrc_paths(b"not parquet")


def test_the_correspondence_comes_first_then_the_jrc_table_then_nothing():
    contexts = EfContexts(
        correspondence={"a": LT_WATER_PATH, "both": "Land use / Land occupation"},
        jrc={"both": "Land use / Land occupation", "j": "Land use / Land transformation"},
    )
    assert contexts.path("a") == LT_WATER_PATH
    assert contexts.leaf("a") == "Emissions to water, unspecified (long-term)"
    assert contexts.leaf("both") == "Land occupation"
    assert contexts.leaf("j") == "Land transformation"
    assert contexts.path("nobody") is None and contexts.leaf("nobody") is None


def test_sources_that_disagree_on_a_flow_are_refused_with_every_such_flow():
    with pytest.raises(ValidationError) as error:
        EfContexts(
            correspondence={"a": "Land use / x", "b": "Land use / y", "c": "Land use / z"},
            jrc={"a": "Land use / other", "b": "Land use / y", "c": "Land use / other"},
        )
    message = str(error.value)
    assert "2 EF flow(s)" in message and "'a'" in message and "'c'" in message
    assert "'b'" not in message


def test_a_flow_neither_source_places_takes_the_path_of_the_flow_it_is_redirected_to():
    redirects = {"old": "a", "old-jrc": "j", "lost": "nowhere"}
    contexts = EfContexts(
        correspondence={"a": LT_WATER_PATH, "own": "Land use / Land occupation"},
        jrc={"j": "Land use / Land transformation"},
        resolve=lambda identifier: redirects.get(identifier, identifier),
    )
    assert contexts.leaf("old") == "Emissions to water, unspecified (long-term)"
    assert contexts.leaf("old-jrc") == "Land transformation"
    # its own path first, whatever it is redirected to
    assert contexts.leaf("own") == "Land occupation"
    # a redirect that leads to no placed flow, and no redirect at all: no path
    assert contexts.path("lost") is None and contexts.path("nobody") is None


def test_without_a_resolver_no_redirect_is_followed():
    assert EfContexts(correspondence={"a": LT_WATER_PATH}, jrc={}).path("old") is None
