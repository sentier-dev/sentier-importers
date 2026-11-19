from collections import Counter

from sentier_importers.sources.agribalyse.simapro_flows import FlowRecord, read_flows
from sentier_importers.sources.brightway_flows.correspondences import Link, load
from sentier_importers.sources.brightway_flows.identifiers import (
    AGRIBALYSE_LIST,
    agribalyse_id,
    agribalyse_link,
    bafu_candidates,
    bafu_link,
)

from tests.sources.agribalyse_fixture import real_brightway_flows, real_csv

RADON = "002fa2b9-8a73-579f-87e7-811c27caecd3"
WATER_AE = "25bd4c01-a25a-5416-9e27-399efb3218e5"
#: Two identifiers of the agribalyse-3.2 correspondence of Brightway Flows 1.1.0.
AMMONIA_AIR = FlowRecord("Ammonia", "Emissions to air", "", "kg")
AMMONIA_AIR_ID = "5acc0457-7113-5dd0-9ecb-370f5b58be55"
AMMONIA_URBAN = FlowRecord("Ammonia", "Emissions to air", "high. pop.", "kg")
AMMONIA_URBAN_ID = "2b034729-aaad-5480-abc8-a59a1f2bb092"


def _link(source_id):
    return Link(source_id, "x", "a / b", "KiloGM", "closeMatch", "ef-a")


def test_plain_flow_uses_the_four_fields():
    assert bafu_candidates("Radon-222", "emissions to air", "high. pop.", "Bq")[0] == RADON


def test_regional_flow_moves_the_place_to_a_fifth_field():
    assert WATER_AE in bafu_candidates("Water, AE", "emissions to water", "river", "m3")


def test_link_is_found_from_one_of_our_source_records():
    by_id = {RADON: _link(RADON), WATER_AE: _link(WATER_AE)}
    radon = {"name": "Radon-222", "unit": "Bq", "context": ["emissions to air", "high. pop."]}
    water = {"name": "Water, AE", "unit": "m3", "context": ["emissions to water", "river"]}
    assert bafu_link(radon, by_id).source_id == RADON
    assert bafu_link(water, by_id).source_id == WATER_AE


def test_a_flow_they_do_not_list_has_no_link():
    assert bafu_link({"name": "Nothing", "unit": "kg", "context": ["a", "b"]}, {}) is None


def test_a_source_without_context_or_unit_does_not_crash():
    assert bafu_link({"name": "Nothing"}, {}) is None


def test_agribalyse_identifier_is_made_of_the_four_raw_fields():
    assert agribalyse_id("Ammonia", "Emissions to air", "", "kg") == AMMONIA_AIR_ID
    assert agribalyse_id("Ammonia", "Emissions to air", "high. pop.", "kg") == AMMONIA_URBAN_ID
    # the unit is part of the identifier, as the export spells it
    assert agribalyse_id("Ammonia", "Emissions to air", "", "g") != AMMONIA_AIR_ID


def test_agribalyse_link_is_found_from_a_flow_record():
    by_id = {AMMONIA_AIR_ID: _link(AMMONIA_AIR_ID), AMMONIA_URBAN_ID: _link(AMMONIA_URBAN_ID)}
    assert agribalyse_link(AMMONIA_AIR, by_id).source_id == AMMONIA_AIR_ID
    assert agribalyse_link(AMMONIA_URBAN, by_id).source_id == AMMONIA_URBAN_ID


def test_an_agribalyse_flow_they_do_not_list_has_no_link():
    by_id = {AMMONIA_AIR_ID: _link(AMMONIA_AIR_ID)}
    assert agribalyse_link(FlowRecord("Nothing", "Emissions to air", "", "kg"), by_id) is None


def test_every_real_agribalyse_flow_is_linked_or_excluded(pytestconfig):
    flows, _ = read_flows(real_csv())
    bw = load(real_brightway_flows(pytestconfig))
    by_id = {item.source_id: item for item in bw.links[AGRIBALYSE_LIST]}
    excluded = bw.excluded[AGRIBALYSE_LIST]
    assert by_id[AMMONIA_AIR_ID].label == "Ammonia"
    assert by_id[AMMONIA_URBAN_ID].context == "Emissions to air / high. pop."

    def status(flow):
        if agribalyse_link(flow, by_id) is not None:
            return "link"
        identifier = agribalyse_id(flow.name, flow.compartment, flow.subcompartment, flow.unit)
        return "excluded" if identifier in excluded else "unknown"

    assert Counter(status(flow) for flow in flows) == {"link": 5402, "excluded": 43}
    assert {f.compartment for f in flows if status(f) == "excluded"} == {"Final waste flows"}
