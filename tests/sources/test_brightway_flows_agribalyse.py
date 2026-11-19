import importlib.util
import os
import shutil
from collections import Counter
from dataclasses import replace
from pathlib import Path

import orjson
import pytest
from sentier_importers.core.context import RunContext
from sentier_importers.core.errors import ValidationError
from sentier_importers.core.source import SourceConfig
from sentier_importers.sources.agribalyse.simapro_flows import FlowRecord
from sentier_importers.sources.brightway_flows import agribalyse, links
from sentier_importers.sources.brightway_flows.agribalyse_package import (
    AgribalyseBrightwayFlowsPackage,
)
from sentier_importers.sources.brightway_flows.agribalyse_review import (
    AgribalyseBrightwayFlowsReview,
)
from sentier_importers.sources.brightway_flows.identifiers import agribalyse_id

from tests.matching.ef_fixtures import (
    AIR_RURAL,
    AIR_UNSPEC,
    RES_GROUND,
    WATER_FRESH,
    cf_row,
    vocab_row,
    write_ef_inputs,
)
from tests.sources.agribalyse_fixture import MINI_CSV, real_brightway_flows, real_csv
from tests.sources.brightway_flows_fixture import document, excluded_concept, flow, gz, link

AIR, WATER, SOIL = "Emissions to air", "Emissions to water", "Emissions to soil"
RESOURCES, WASTE = "Resources", "Final waste flows"
TOX = "ef-3.1:human-toxicity-cancer"
FOSSIL = "ef-3.1:resource-use-fossils"

#: The eleven flows of the fixture export, each with the case it stands for.
AMMONIA = FlowRecord("Ammonia", AIR, "", "kg")  # scoring target, no sub-compartment
AMMONIA_RURAL = FlowRecord("Ammonia", AIR, "low. pop.", "kg")  # scoring target
CADMIUM = FlowRecord("Cadmium", SOIL, "", "kg")  # onto a flow EF lacks
CADMIUM_AGRI = FlowRecord("Cadmium", SOIL, "agricultural", "kg")  # broad match
COAL = FlowRecord("Coal, hard", RESOURCES, "in ground", "kg")  # kg onto MJ, their multiplier
NITRATE = FlowRecord("Nitrate", WATER, "groundwater", "kg")  # factorless target
OCCUPATION = FlowRecord("Occupation, arable", RESOURCES, "land", "m2a")  # unit change
RADON = FlowRecord("Radon-222", AIR, "low. pop., long-term", "kBq")  # onto an immediate leaf
SLAGS = FlowRecord("Slags", WASTE, "", "kg")  # excluded by Brightway Flows
VAPOUR = FlowRecord("Water", AIR, "", "kg")  # all factors zero
WATER_IN = FlowRecord("Water", RESOURCES, "in water", "m3")  # target not in sentier-vocab

CF = [
    cf_row("ef-ammonia", "ammonia", AIR_UNSPEC, TOX, 1.0),
    cf_row("ef-ammonia-rural", "ammonia", AIR_RURAL, TOX, 2.0),
    cf_row("ef-cadmium", "cadmium", AIR_UNSPEC, TOX, 3.0),
    cf_row("ef-coal", "hard coal", RES_GROUND, FOSSIL, 1.0),
    cf_row("ef-radon", "radon-222", AIR_UNSPEC, TOX, 1.0),
    cf_row("ef-vapour", "water vapour", AIR_UNSPEC, TOX, 0.0),
]
VOCAB = [
    vocab_row("ef-ammonia", "Ammonia"),
    vocab_row("ef-ammonia-rural", "Ammonia"),
    vocab_row("ef-cadmium", "Cadmium"),
    vocab_row("ef-coal", "Hard coal"),
    vocab_row("ef-nitrate", "Nitrate", ef_context=WATER_FRESH),
    vocab_row("ef-occupation", "Arable", bw="envi-wate-unkn"),
    vocab_row("ef-radon", "Radon-222"),
    vocab_row("ef-vapour", "Water vapour"),
]
BW_UNIT = {"ef-coal": "MJ"}


def their_id(record):
    return agribalyse_id(record.name, record.compartment, record.subcompartment, record.unit)


def bw_document():
    def to(record, target, **kw):
        return link("agribalyse/3.2", their_id(record), target, **kw)

    return document(
        flows=[
            flow(code, row["pref_label"], unit=BW_UNIT.get(code, "kg"))
            for row in VOCAB
            for code in [row["iri"].rsplit("/", 1)[-1]]
        ]
        + [
            flow("add-cadmium", "Cadmium", source="agribalyse algorithm addition"),
            flow("ef-not-in-vocab", "Water"),
        ],
        links_by_list={
            "agribalyse-3.2": [
                to(AMMONIA, "ef-ammonia"),
                to(AMMONIA_RURAL, "ef-ammonia-rural", match="exactMatch"),
                to(CADMIUM, "add-cadmium", match="exactMatch"),
                to(CADMIUM_AGRI, "ef-cadmium", match="broadMatch"),
                to(COAL, "ef-coal", multiplier=27.9, basis="curated"),
                to(NITRATE, "ef-nitrate"),
                to(OCCUPATION, "ef-occupation", unit="M2-YR"),
                to(RADON, "ef-radon", unit="KiloBQ"),
                to(VAPOUR, "ef-vapour"),
                to(WATER_IN, "ef-not-in-vocab", unit="M3"),
            ]
        },
        excluded_by_list={
            "agribalyse-3.2": [excluded_concept("agribalyse/3.2", their_id(SLAGS), "Slags")]
        },
    )


def stage(tmp_path):
    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / "bw.json.gz").write_bytes(gz(bw_document()))
    shutil.copy(MINI_CSV, tmp_path / "flows.csv")
    write_ef_inputs(tmp_path, CF, VOCAB)
    return tmp_path


def config(root, inputs=None):
    return SourceConfig(
        name="t",
        module="sentier_importers.sources.brightway_flows.agribalyse_package",
        target="sentier_mappings",
        category="agribalyse-3.2__ef-3.1",
        fetch_url=f"file://{root}/bw.json.gz",
        fetch_format="json",
        output_format="json",
        emit_filename="biosphere-1-brightway-flows",
        package_name="p",
        package_version="0.1.0",
        package_verb="replace",
        inputs=inputs
        or {
            "flows": f"file://{root}/flows.csv",
            "ef_cfs": f"file://{root}/characterization-factors.parquet",
            "ef_vocab": f"file://{root}/terms",
        },
    )


def run(cls, tmp_path, **config_kw):
    source = cls(config(stage(tmp_path), **config_kw))
    source.expected_sha256 = None
    ctx = RunContext(cache_dir=tmp_path / "cache", output_dir=tmp_path / "out")
    return source, source.parse(source.fetch(ctx))


@pytest.fixture
def state(tmp_path):
    return run(AgribalyseBrightwayFlowsPackage, tmp_path)[1][0]["state"]


def test_state_holds_the_flows_of_the_export_and_their_links(state):
    assert len(state.flows) == 11 and state.dropped == 1
    assert len(state.by_id) == 10 and state.excluded == {their_id(SLAGS)}


def test_the_source_context_has_one_level_without_sub_compartment():
    assert agribalyse.source_record(AMMONIA) == {
        "name": "Ammonia",
        "unit": "kg",
        "context": ["Emissions to air"],
    }
    assert agribalyse.source_record(RADON)["context"] == [AIR, "low. pop., long-term"]
    assert agribalyse.source_record(RADON)["unit"] == "kBq"  # as the export spells it


def test_a_scoring_target_is_accepted_without_sub_compartment(state):
    made, reason = agribalyse.package_entry(AMMONIA, state)
    assert reason is None
    assert made == {
        "source": {"name": "Ammonia", "unit": "kg", "context": ["Emissions to air"]},
        "target": {
            "name": "Ammonia",
            "code": "ef-ammonia",
            "unit": "kilogram",
            "context": ["Emissions", "Emissions to air", "Emissions to air, unspecified"],
        },
        "comment": "brightway-flows 1.1.0, skos:closeMatch",
    }


def test_a_scoring_target_is_accepted_with_a_sub_compartment(state):
    made, reason = agribalyse.package_entry(AMMONIA_RURAL, state)
    assert reason is None
    assert made["source"]["context"] == ["Emissions to air", "low. pop."]
    assert made["target"]["code"] == "ef-ammonia-rural"
    assert made["comment"] == "brightway-flows 1.1.0, skos:exactMatch"


def test_a_factorless_target_says_so(state):
    made, _ = agribalyse.package_entry(NITRATE, state)
    assert made["target"]["code"] == "ef-nitrate" and made["target"]["unit"] == "kilogram"
    assert made["comment"] == (
        "brightway-flows 1.1.0, skos:closeMatch; "
        "uncharacterised in EF 3.1: no factor in any method"
    )
    zero, _ = agribalyse.package_entry(VAPOUR, state)
    assert zero["comment"].endswith("all factors are zero in EF 3.1")


def test_their_multiplier_becomes_the_conversion_factor(state):
    made, reason = agribalyse.package_entry(COAL, state)
    assert reason is None
    assert made["source"]["unit"] == "kg" and made["target"]["unit"] == "megajoule"
    assert made["conversion_factor"] == 27.9
    assert made["comment"].endswith("amount rescaled by 27.9 (curated)")


@pytest.mark.parametrize(
    "record,reason",
    [
        (CADMIUM, "no_ef_target"),
        (CADMIUM_AGRI, "broad_match"),
        (RADON, "long_term_onto_immediate"),
        (OCCUPATION, "unit_mismatch"),
        (WATER_IN, "target_not_in_vocab"),
        (SLAGS, "excluded_by_brightway_flows"),
        (FlowRecord("Nothing", AIR, "", "kg"), "no_link"),
    ],
)
def test_a_link_that_fails_a_rule_is_withheld_with_its_reason(state, record, reason):
    assert agribalyse.package_entry(record, state) == (None, reason)


def test_a_target_whose_context_is_uncertain_ships_without_it(state):
    # the same flow written in kg: the unit rule passes, the index could not place it
    by_id = {**state.by_id, their_id(NITRATE): state.by_id[their_id(OCCUPATION)]}
    made, _ = agribalyse.package_entry(NITRATE, replace(state, by_id=by_id))
    assert made["target"] == {"name": "Arable", "code": "ef-occupation", "unit": "kilogram"}
    assert links.UNCERTAIN_CONTEXT in made["comment"]


def test_a_volume_time_flow_keeps_its_dimension(state):
    # m3y onto a factorless EF flow in m3.a: both spellings are known to the validator
    reservoir = FlowRecord("Volume occupied, reservoir", RESOURCES, "in water", "m3y")
    flows = {**state.bw.flows, "ef-nitrate": replace(state.bw.flows["ef-nitrate"], unit="m3.a")}
    by_id = {**state.by_id, their_id(reservoir): state.by_id[their_id(NITRATE)]}
    volume = replace(state, bw=replace(state.bw, flows=flows), by_id=by_id)
    made, reason = agribalyse.package_entry(reservoir, volume)
    assert reason is None
    assert (made["source"]["unit"], made["target"]["unit"]) == ("m3y", "m3*a")
    assert "conversion_factor" not in made
    # a mass-time flow has no spelling the validator knows: withheld
    flows["ef-nitrate"] = replace(flows["ef-nitrate"], unit="kg.a")
    delayed = FlowRecord("Correction flow", AIR, "", "kgy")
    by_id[their_id(delayed)] = state.by_id[their_id(NITRATE)]
    assert agribalyse.package_entry(delayed, volume) == (None, "unit_unknown")


def test_package_holds_one_sorted_entry_per_accepted_flow(tmp_path):
    source, records = run(AgribalyseBrightwayFlowsPackage, tmp_path)
    rows = source.transform(records)
    assert [(r["source"]["name"], r["source"]["context"]) for r in rows] == [
        ("Ammonia", [AIR]),
        ("Ammonia", [AIR, "low. pop."]),
        ("Coal, hard", [RESOURCES, "in ground"]),
        ("Nitrate", [WATER, "groundwater"]),
        ("Water", [AIR]),
    ]
    assert all(r["target"]["code"].startswith("ef-") for r in rows)


def test_review_holds_one_row_per_flow_without_an_entry(tmp_path):
    source, records = run(AgribalyseBrightwayFlowsReview, tmp_path)
    rows = source.transform(records)
    assert [(r["source"]["name"], r["reason"]) for r in rows] == [
        ("Cadmium", "no_ef_target"),
        ("Cadmium", "broad_match"),
        ("Occupation, arable", "unit_mismatch"),
        ("Radon-222", "long_term_onto_immediate"),
        ("Slags", "excluded_by_brightway_flows"),
        ("Water", "target_not_in_vocab"),
    ]
    assert all(r["kind"] == "withheld" for r in rows)
    by_reason = {r["reason"]: r for r in rows}
    broad = by_reason["broad_match"]
    assert broad["source"] == {"name": "Cadmium", "unit": "kg", "context": [SOIL, "agricultural"]}
    assert broad["brightway_flows"] == {
        "code": "ef-cadmium",
        "match": "broadMatch",
        "name": "Cadmium",
        "context": "envi-air-unkn",
        "in_ef": True,
    }
    assert broad["methods"] == [TOX]  # the methods their target scores in
    assert by_reason["no_ef_target"]["brightway_flows"]["in_ef"] is False
    assert "methods" not in by_reason["no_ef_target"]
    assert by_reason["excluded_by_brightway_flows"]["brightway_flows"] is None


def test_package_and_review_share_the_flows_between_them(tmp_path):
    package, records = run(AgribalyseBrightwayFlowsPackage, tmp_path / "p")
    review, _ = run(AgribalyseBrightwayFlowsReview, tmp_path / "r")
    keys = [
        (r["source"]["name"], tuple(r["source"]["context"]))
        for r in [*package.transform(records), *review.transform(records)]
    ]
    assert len(keys) == len(set(keys)) == 11


def test_an_excluded_flow_never_becomes_an_entry_even_with_a_link(state):
    slags_link = replace(state.by_id[their_id(AMMONIA)], source_id=their_id(SLAGS))
    linked = replace(state, by_id={**state.by_id, their_id(SLAGS): slags_link})
    assert agribalyse.package_entry(SLAGS, linked) == (None, "excluded_by_brightway_flows")


def test_two_flows_with_one_name_and_context_are_refused(state):
    twin = replace(AMMONIA, unit="kilogram")
    by_id = {**state.by_id, their_id(twin): state.by_id[their_id(AMMONIA)]}
    doubled = replace(state, flows=(*state.flows, twin), by_id=by_id)
    with pytest.raises(ValidationError, match="Ammonia"):
        agribalyse.package_rows(doubled)


def test_output_is_byte_stable_over_two_runs(tmp_path):
    def emitted(cls, where):
        source, records = run(cls, where)
        return orjson.dumps(source.transform(records), option=orjson.OPT_INDENT_2)

    for cls in (AgribalyseBrightwayFlowsPackage, AgribalyseBrightwayFlowsReview):
        assert emitted(cls, tmp_path / "a" / cls.__name__) == emitted(
            cls, tmp_path / "b" / cls.__name__
        )


def test_a_registry_block_without_the_flows_input_is_refused(tmp_path):
    root = stage(tmp_path)
    inputs = {
        "ef_cfs": f"file://{root}/characterization-factors.parquet",
        "ef_vocab": f"file://{root}/terms",
    }
    source = AgribalyseBrightwayFlowsPackage(config(root, inputs=inputs))
    source.expected_sha256 = None
    ctx = RunContext(cache_dir=tmp_path / "cache", output_dir=tmp_path / "out")
    with pytest.raises(RuntimeError, match="flows"):
        source.parse(source.fetch(ctx))


def test_the_pinned_release_is_checked_by_default(tmp_path):
    assert AgribalyseBrightwayFlowsPackage.expected_sha256 is not None


@pytest.mark.parametrize(
    "unit", ["kg", "m3", "kBq", "m2", "m2a", "MJ", "m3y", "kilogram", "cubic meter", "m2*a"]
)
def test_every_unit_the_rule_knows_is_known_to_the_validator_alike(unit):
    # sentier-mappings/scripts/validate.py::_UNITS, copied 2026-11-19: an entry passes
    # its check only if both tables give a unit the same dimension and scale
    validator = {
        "kg": ("mass", 1.0),
        "kilogram": ("mass", 1.0),
        "kbq": ("activity", 1e3),
        "m3": ("volume", 1.0),
        "cubic meter": ("volume", 1.0),
        "m2": ("area", 1.0),
        "m2a": ("area-time", 1.0),
        "m2*a": ("area-time", 1.0),
        "m3y": ("volume-time", 1.0),
        "mj": ("energy", 1.0),
    }
    assert links._DIMENSION[unit.lower()] == validator[unit.lower()]


def test_a_unit_the_validator_does_not_know_is_withheld():
    assert links.conversion("kgy", "kilogram", None) == (None, "unit_unknown")
    assert links.conversion("m3y", "cubic meter", None) == (None, "unit_mismatch")
    assert links.conversion("m3y", "m3*a", None) == (None, None)


# --- real data: SENTIER_DATA_ROOT names the directory of the sibling checkouts ---


@pytest.fixture(scope="module")
def real_state(pytestconfig, tmp_path_factory):
    flows, bw = real_csv(), real_brightway_flows(pytestconfig)
    root = Path(os.environ["SENTIER_DATA_ROOT"])
    cfs = root / "sentier-methods/data/01-ef-3.1/characterization-factors.parquet"
    vocab = root / "sentier-vocab/terms"
    if not (cfs.exists() and vocab.exists()):
        pytest.skip("real EF inputs not available")
    work = tmp_path_factory.mktemp("agribalyse-real")
    (work / "bw.json.gz").write_bytes(bw)
    (work / "flows.csv.xz").write_bytes(flows)
    inputs = {
        "flows": f"file://{work}/flows.csv.xz",
        "ef_cfs": f"file://{cfs}",
        "ef_vocab": f"file://{vocab}",
    }
    source = AgribalyseBrightwayFlowsPackage(config(work, inputs=inputs))
    ctx = RunContext(cache_dir=work / "cache", output_dir=work / "out")
    return source.parse(source.fetch(ctx))[0]["state"]


def test_real_package_and_review_account_for_every_flow(real_state):
    package = agribalyse.package_rows(real_state)
    review = agribalyse.review_rows(real_state)
    assert len(real_state.flows) == 5445 and real_state.dropped == 0
    assert len(package) + len(review) == 5445
    reasons = Counter(row["reason"] for row in review)
    assert reasons["excluded_by_brightway_flows"] == 43 and reasons["no_link"] == 0
    assert reasons["no_ef_target"] == 1627


def test_real_entries_pass_the_unit_check_of_sentier_mappings(real_state):
    script = Path(os.environ["SENTIER_DATA_ROOT"]) / "sentier-mappings/scripts/validate.py"
    if not script.exists():
        pytest.skip("sentier-mappings checkout not available")
    spec = importlib.util.spec_from_file_location("sentier_mappings_validate", script)
    validate = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(validate)
    failures = [
        (entry["source"], error)
        for entry in agribalyse.package_rows(real_state)
        if (error := validate.check_entry_units(entry)) is not None
    ]
    assert failures == []
    # and no entry escapes the check through a unit the validator does not know
    for entry in agribalyse.package_rows(real_state):
        for side in ("source", "target"):
            assert entry[side]["unit"].lower() in validate._UNITS
