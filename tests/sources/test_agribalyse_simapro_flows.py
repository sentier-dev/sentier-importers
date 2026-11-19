import lzma
from collections import Counter

import pytest
from sentier_importers.core.errors import ParseError
from sentier_importers.sources.agribalyse.simapro_flows import FlowRecord, read_flows

from tests.sources.agribalyse_fixture import MINI_CSV, real_csv

MINI = MINI_CSV.read_bytes()
AIR, WATER, SOIL = "Emissions to air", "Emissions to water", "Emissions to soil"
RESOURCES, WASTE = "Resources", "Final waste flows"


def test_flows_are_unique_per_compartment_name_and_subcompartment():
    flows, _ = read_flows(MINI)
    assert flows == [
        FlowRecord("Ammonia", AIR, "", "kg"),  # exchanged by both processes
        FlowRecord("Ammonia", AIR, "low. pop.", "kg"),
        FlowRecord("Cadmium", SOIL, "", "kg"),
        FlowRecord("Cadmium", SOIL, "agricultural", "kg"),
        FlowRecord("Coal, hard", RESOURCES, "in ground", "kg"),
        FlowRecord("Nitrate", WATER, "groundwater", "kg"),
        FlowRecord("Occupation, arable", RESOURCES, "land", "m2a"),
        FlowRecord("Radon-222", AIR, "low. pop., long-term", "kBq"),
        FlowRecord("Slags", WASTE, "", "kg"),
        FlowRecord("Water", AIR, "", "kg"),
        FlowRecord("Water", RESOURCES, "in water", "m3"),
    ]


def test_the_unit_is_the_one_of_the_substance_block_of_the_compartment():
    by_key = {(f.name, f.compartment, f.subcompartment): f.unit for f in read_flows(MINI)[0]}
    # the exchange row is written in g, the substance row in kg
    assert by_key[("Ammonia", AIR, "")] == "kg"
    # one name, two substance blocks, two units
    assert by_key[("Water", AIR, "")] == "kg"
    assert by_key[("Water", RESOURCES, "in water")] == "m3"


def test_a_name_without_a_substance_row_is_dropped_and_counted():
    flows, dropped = read_flows(MINI)
    assert dropped == 1
    assert "Not a substance" not in {f.name for f in flows}


def test_only_the_elementary_sections_of_a_process_are_read():
    names = {f.name for f in read_flows(MINI)[0]}
    assert not {n for n in names if "{FR}" in n}  # products, inputs, waste treatment
    assert "Unused resource" not in names  # a substance no process exchanges


def test_a_comment_is_never_read_as_a_row():
    # the NEL byte (0x85) of a comment is no line break, and its separators no fields
    names = {f.name for f in read_flows(MINI)[0]}
    assert not {n for n in names if "ller" in n or "paragraph" in n}


def test_xz_and_plain_inputs_give_the_same_records():
    assert read_flows(lzma.compress(MINI)) == read_flows(MINI)


def test_unix_line_ends_give_the_same_records():
    assert read_flows(MINI.replace(b"\r\n", b"\n")) == read_flows(MINI)


def test_records_are_sorted_whatever_the_order_of_the_file():
    first, second = MINI.split(b"\r\nProcess\r\n")[1:3]
    swapped = MINI.replace(first, b"@@").replace(second, first).replace(b"@@", second)
    assert swapped != MINI
    assert read_flows(swapped) == read_flows(MINI)


def test_a_substance_written_with_two_units_is_refused():
    twice = MINI.replace(b"Slags;kg;;", b"Slags;kg;;\r\nSlags;ton;;")
    with pytest.raises(ParseError, match="Slags"):
        read_flows(twice)


def test_a_quoted_flow_name_is_refused():
    quoted = MINI.replace(b"Nitrate;groundwater", b'"Nitrate; total";groundwater')
    with pytest.raises(ParseError, match="quoted"):
        read_flows(quoted)


def test_a_file_without_elementary_flows_is_refused():
    with pytest.raises(ParseError, match="no elementary flow"):
        read_flows(b"{SimaPro 9.6.0.1}\r\n\r\nUnits\r\nkg;Mass;1;kg\r\n\r\nEnd\r\n")
    with pytest.raises(ParseError):
        read_flows(b"\xfd7zXZ\x00 not really xz")


def test_real_export_has_5445_flows():
    flows, dropped = read_flows(real_csv())
    # every exchanged name has its substance row: nothing is dropped
    assert (len(flows), dropped) == (5445, 0)
    assert Counter(f.compartment for f in flows) == {
        WATER: 1716,
        AIR: 1375,
        RESOURCES: 1217,
        SOIL: 1094,
        WASTE: 43,
    }
    assert Counter(f.unit for f in flows) == {
        "kg": 3847,
        "m3": 1035,
        "kBq": 324,
        "m2": 134,
        "m2a": 65,
        "MJ": 38,
        "m3y": 1,
        "kgy": 1,
    }
    assert sum(1 for f in flows if not f.subcompartment) == 1619
    assert len({(f.name, f.compartment, f.subcompartment) for f in flows}) == 5445
