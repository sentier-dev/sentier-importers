from sentier_importers.sources.brightway_flows.correspondences import Link
from sentier_importers.sources.brightway_flows.identifiers import bafu_candidates, bafu_link

RADON = "002fa2b9-8a73-579f-87e7-811c27caecd3"
WATER_AE = "25bd4c01-a25a-5416-9e27-399efb3218e5"


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
