"""Tests for the shared BAFU EcoSpold1 reader and its helpers."""

import math
import uuid

from sentier_importers.sources.bafu import ecospold

from tests.sources.bafu_fixture import UUID_ELEC, UUID_GAS, UUID_OBSOLETE, fixture_zip


def test_parse_zip_one_record_per_dataset():
    records = ecospold.parse_ecospold_zip(fixture_zip())
    assert {r["uuid"] for r in records} == {UUID_ELEC, UUID_GAS, UUID_OBSOLETE}
    elec = next(r for r in records if r["uuid"] == UUID_ELEC)
    assert elec["number"] == "101"
    assert elec["name"] == "Electricity, at test plant"
    assert elec["category"] == "electricity"
    assert elec["subcategory"] == "production mix"
    assert elec["unit"] == "kWh"
    assert elec["location"] == "CH"
    assert elec["technology"] == "Combined cycle test turbine"
    assert elec["obsolete"] is False
    assert len(elec["exchanges"]) == 4


def test_parse_zip_exchange_fields():
    records = ecospold.parse_ecospold_zip(fixture_zip())
    elec = next(r for r in records if r["uuid"] == UUID_ELEC)
    by_number = {ex["number"]: ex for ex in elec["exchanges"]}
    prod, tech, out_bio, in_bio = (
        by_number["101"],
        by_number["102"],
        by_number["201"],
        by_number["202"],
    )
    assert (prod["group"], prod["group_code"]) == ("output", 0)
    assert (tech["group"], tech["group_code"]) == ("input", 5)
    assert (out_bio["group"], out_bio["group_code"]) == ("output", 4)
    assert (in_bio["group"], in_bio["group_code"]) == ("input", 4)
    assert tech["amount"] == 2.0
    assert tech["uncertainty_type"] == "1"
    assert tech["sd95"] == 1.21
    assert out_bio["cas"] == "000124-38-9"
    assert in_bio["cas"] is None


def test_obsolete_markers():
    records = ecospold.parse_ecospold_zip(fixture_zip())
    flags = {r["uuid"]: r["obsolete"] for r in records}
    assert flags == {UUID_ELEC: False, UUID_GAS: False, UUID_OBSOLETE: True}


def test_parse_zip_is_memoized():
    raw = fixture_zip()
    assert ecospold.parse_ecospold_zip(raw) is ecospold.parse_ecospold_zip(raw)


def test_sector_map_routes_all_spec_categories():
    # every category maps to exactly one sector folder; spec table has 59 categories
    cats = [c for cs in ecospold.SECTORS.values() for c in cs]
    assert len(cats) == len(set(cats)) == 59
    assert ecospold.sector_for("electricity") == "02-electricity"
    assert ecospold.sector_for("natural gas") == "05-energy"
    assert ecospold.sector_for("material, obsolete") == "99-obsolete"
    assert ecospold.sector_for("energy supply, kbob recommendation") == "05-energy"


def test_flow_id_deterministic_uuid5():
    a = ecospold.flow_id("Carbon dioxide, fossil", "emissions to air", "unspecified")
    b = ecospold.flow_id("Carbon dioxide, fossil", "emissions to air", "unspecified")
    assert a == b
    expected = str(
        uuid.uuid5(ecospold.BAFU_FLOW_NS, "Carbon dioxide, fossil|emissions to air|unspecified")
    )
    assert a == expected
    assert ecospold.flow_id("Water, river", "resources", None) != a


def test_bw_uncertainty_mapping():
    # EcoSpold 1 (lognormal) -> Brightway 2: loc = ln(|amount|), scale = ln(sqrt(SD95))
    utype, loc, scale = ecospold.bw_uncertainty(2.0, "1", 1.21)
    assert utype == 2
    assert math.isclose(loc, math.log(2.0))
    assert math.isclose(scale, math.log(math.sqrt(1.21)))
    # EcoSpold 2 (normal) -> Brightway 3: loc = amount, scale = SD95 / 2
    assert ecospold.bw_uncertainty(0.5, "2", 0.1) == (3, 0.5, 0.05)
    # type 0 / missing SD95 / zero amount -> undefined
    assert ecospold.bw_uncertainty(1.0, "0", None) == (None, None, None)
    assert ecospold.bw_uncertainty(1.0, "1", None) == (None, None, None)
    assert ecospold.bw_uncertainty(0.0, "1", 1.21) == (None, None, None)
