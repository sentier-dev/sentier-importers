"""The ef31-flow-units part states the unit of every EF 3.1 flow term of the release."""

import gzip
import json
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from loguru import logger
from sentier_importers.core import pipeline
from sentier_importers.core import registry as registry_mod
from sentier_importers.core import vocab_contract, vocab_layers
from sentier_importers.core.context import RunContext
from sentier_importers.core.errors import ParseError, ValidationError
from sentier_importers.core.source import SourceConfig
from sentier_importers.sources.agribalyse.ef_context import BRIGHTWAY_FLOWS
from sentier_importers.sources.agribalyse.flows import AgribalyseFlowsSource
from sentier_importers.sources.agribalyse.flows_jrc import JrcFlowsSource
from sentier_importers.sources.brightway_flows.correspondences import PINNED_SHA256
from sentier_importers.sources.brightway_flows.vocab_flow_units import Ef31FlowUnitsSource
from sentier_importers.sources.brightway_flows.vocab_flows import (
    HARMONISED,
    JRC,
    BrightwayFlowsVocabSource,
)

from tests.sources.brightway_flows_fixture import document, flow, gz, link

FIXTURES = Path(__file__).parents[1] / "core" / "fixtures" / "vocab"
MODULE = "sentier_importers.sources.brightway_flows.vocab_flow_units"
NAME = "ef31-flow-units"
FLOWS = "https://vocab.sentier.dev/flows/"
QUDT = "http://qudt.org/vocab/unit/"
SENTIER = "https://vocab.sentier.dev/units/unit/"
AIR = "Emissions/Emissions to air/Emissions to air, unspecified"
JRC_COLUMNS = ["FLOW_uuid", "FLOW_name", "FLOW_class0", "FLOW_class1", "FLOW_class2"]


def own(identifier, unit="KiloGM"):
    """A flow's own entry in the EF correspondence."""
    return link("ef/3.1", identifier, identifier, match="exactMatch", context=AIR, unit=unit)


FLOW_LIST = [
    flow("harmonised-kg", "Ammonia"),
    flow("jrc-kbq", "Uranium Alpha", unit="kBq"),
    flow("bw11-m2a", "Arable", unit="m2.a"),
    flow("merge-target", "Water"),
    flow("redirect-target", "Benzene"),
    flow("not-ef", "Hexane", source="bafu algorithm addition"),
]
EF_LINKS = [
    own("harmonised-kg"),
    own("jrc-kbq", "KiloBQ"),
    own("bw11-m2a", "M2-YR"),
    own("merge-target"),
    own("redirect-target", "MegaJ"),
    # an EF flow merged into another one states its own unit, here not the target's
    link("ef/3.1", "jrc-merged-m3", "merge-target", match="exactMatch", context=AIR, unit="M3"),
]
REDIRECTS = [("harmonised-redirected", "redirect-target"), ("jrc-lost", "nowhere")]
HARMONISED_FLOWS = [
    {"identifier": "harmonised-kg", "source": "EF 3.1", "prefLabel": "Ammonia"},
    {"identifier": "harmonised-redirected", "source": "EF 3.1", "prefLabel": "Benzol"},
    {"identifier": "merge-target", "source": "EF 3.1", "prefLabel": "Water"},
    {"identifier": "redirect-target", "source": "EF 3.1", "prefLabel": "Benzene"},
    # not emitted by the harmonised part, so not a flow of the release
    {"identifier": "ecoinvent-1", "source": "ecoinvent algorithm addition", "prefLabel": "x"},
]
JRC_FLOWS = ("harmonised-kg", "jrc-kbq", "jrc-merged-m3", "jrc-lost", "jrc-unknown")
EXPECTED = {
    "harmonised-kg": QUDT + "KiloGM",
    "harmonised-redirected": QUDT + "MegaJ",  # the unit of the flow it is redirected to
    "merge-target": QUDT + "KiloGM",
    "redirect-target": QUDT + "MegaJ",
    "jrc-kbq": QUDT + "KiloBQ",
    "jrc-merged-m3": QUDT + "M3",
    "bw11-m2a": SENTIER + "M2-YR",
}
#: Flows of the CF table with an entry in the EF correspondence: the two must agree.
PLACED = ("harmonised-kg", "jrc-kbq", "jrc-merged-m3")
JRC_ONLY = ("Emissions", "Emissions to air", "x")
#: In no EF correspondence entry and redirected to no flow with one: no edge.
WITHOUT_UNIT = ("jrc-lost", "jrc-unknown")


def _write(tmp_path, ef_links=EF_LINKS, harmonised=HARMONISED_FLOWS, jrc=JRC_FLOWS):
    tmp_path.mkdir(parents=True, exist_ok=True)
    bw = tmp_path / "bw.json.gz"
    bw.write_bytes(gz(document(FLOW_LIST, {"ef-3.1": ef_links}, redirects=REDIRECTS)))
    harmonised_path = tmp_path / "harmonised.json.gz"
    harmonised_path.write_bytes(gzip.compress(json.dumps({"flows": harmonised}).encode()))
    jrc_path = tmp_path / "cf.parquet"
    rows = [
        (uuid, f"name of {uuid}", *(AIR.split("/") if uuid in PLACED else JRC_ONLY))
        for uuid in jrc
    ]
    pq.write_table(
        pa.table(
            {c: pa.array([r[i] for r in rows], pa.string()) for i, c in enumerate(JRC_COLUMNS)}
        ),
        jrc_path,
    )
    return bw, {HARMONISED: f"file://{harmonised_path}", JRC: f"file://{jrc_path}"}


def _config(fetch, inputs, name=NAME, module=MODULE, term_type="elementary-flow") -> SourceConfig:
    return SourceConfig(
        name=name,
        module=module,
        target="sentier_vocab",
        category="ef-3.1",
        term_type=term_type,
        fetch_url=str(fetch) if "://" in str(fetch) else f"file://{fetch}",
        fetch_format="json",
        output_format="parquet",
        inputs=inputs,
        dedup_check_existing=False,
    )


def _source(config) -> Ef31FlowUnitsSource:
    source = Ef31FlowUnitsSource(config)
    source.expected_sha256 = None
    return source


def _ctx(tmp_path, **kw):
    return RunContext(cache_dir=tmp_path / "cache", output_dir=tmp_path / "out", **kw)


def _run(tmp_path, source):
    return source.transform(source.parse(source.fetch(_ctx(tmp_path))))


def _rows(tmp_path, **kwargs):
    return _run(tmp_path, _source(_config(*_write(tmp_path, **kwargs))))


@pytest.fixture
def info():
    """Capture loguru messages from INFO up (loguru bypasses pytest's caplog)."""
    messages: list[str] = []
    sink_id = logger.add(lambda m: messages.append(str(m)), level="INFO")
    yield messages
    logger.remove(sink_id)


def test_the_part_emits_relations_only():
    assert Ef31FlowUnitsSource.relations_only is True


def test_one_row_per_flow_with_a_unit_holding_the_iri_and_the_unit_sorted_by_iri(tmp_path):
    rows = _rows(tmp_path)
    assert rows == [{"iri": FLOWS + code, "unit": EXPECTED[code]} for code in sorted(EXPECTED)]


def test_a_flow_has_the_unit_of_its_own_entry_even_when_merged_into_another_flow(tmp_path):
    by_code = {r["iri"].rsplit("/", 1)[1]: r["unit"] for r in _rows(tmp_path)}
    assert by_code["jrc-merged-m3"] == QUDT + "M3"
    assert by_code["merge-target"] == QUDT + "KiloGM"


def test_a_redirected_flow_has_the_unit_of_the_flow_it_is_redirected_to(tmp_path):
    by_code = {r["iri"].rsplit("/", 1)[1]: r["unit"] for r in _rows(tmp_path)}
    assert by_code["harmonised-redirected"] == by_code["redirect-target"] == QUDT + "MegaJ"


def test_a_flow_without_a_stated_unit_gets_no_edge_and_is_reported(tmp_path, info):
    codes = {r["iri"].rsplit("/", 1)[1] for r in _rows(tmp_path)}
    assert not codes & set(WITHOUT_UNIT)
    (line,) = [m for m in info if NAME in m]
    assert "7 unit edges" in line
    assert "6 from the flow's own entry" in line
    assert "1 from the flow it is redirected to" in line
    assert "2 flows without a unit" in line
    assert all(code in line for code in WITHOUT_UNIT)


def test_flows_no_part_of_the_release_emits_get_no_edge(tmp_path):
    codes = {r["iri"].rsplit("/", 1)[1] for r in _rows(tmp_path)}
    assert not codes & {"not-ef", "ecoinvent-1", "nowhere"}


def test_two_units_for_one_flow_are_refused(tmp_path):
    with pytest.raises(ValidationError, match="harmonised-kg.*KiloGM.*M3"):
        _rows(tmp_path, ef_links=[*EF_LINKS, own("harmonised-kg", "M3")])


def test_a_unit_without_an_iri_is_refused(tmp_path):
    with pytest.raises(ValidationError, match="'KiloM'"):
        _rows(tmp_path, ef_links=[own("harmonised-kg", "KiloM"), *EF_LINKS[1:]])


@pytest.mark.parametrize("missing", [HARMONISED, JRC])
def test_a_missing_input_is_a_validation_error(tmp_path, missing):
    bw, inputs = _write(tmp_path)
    source = _source(_config(bw, {k: v for k, v in inputs.items() if k != missing}))
    with pytest.raises(ValidationError, match=missing):
        _run(tmp_path, source)


def test_the_pinned_release_is_checked_by_default(tmp_path):
    assert Ef31FlowUnitsSource.expected_sha256 == PINNED_SHA256
    source = Ef31FlowUnitsSource(_config(*_write(tmp_path)))
    with pytest.raises(ParseError, match="sha256"):
        _run(tmp_path, source)


def test_the_part_alone_passes_the_vocab_contract_as_relations(tmp_path):
    contract = vocab_contract.load_contract(FIXTURES / "term.yaml", FIXTURES / "relation.yaml")
    terms, relations = vocab_layers.to_layers(
        _rows(tmp_path),
        term_type="elementary-flow",
        release="ef-3.1",
        contract=contract,
        relations_only=True,
    )
    assert terms == []
    assert {r["predicate"] for r in relations} == {"qudt:hasUnit"}
    assert {r["source"] for r in relations} == {"https://vocab.sentier.dev/sources/ef-3.1"}
    assert (
        vocab_contract.check(
            terms, relations, contract, release="ef-3.1", complete=False, subjects_elsewhere=True
        )
        == []
    )


def test_in_the_release_every_flow_term_of_the_three_parts_has_its_edge(tmp_path):
    """The part re-derives the flows of the release from the inputs of the three flow
    parts; run with them, no edge is on a term nobody emits (the release would refuse
    it) and no flow with a stated unit is left out."""
    bw, inputs = _write(tmp_path)
    harmonised, jrc = inputs[HARMONISED], inputs[JRC]
    bw11 = BrightwayFlowsVocabSource(
        _config(bw, inputs, "bw11", "sentier_importers.sources.brightway_flows.vocab_flows")
    )
    bw11.expected_sha256 = None
    context = {BRIGHTWAY_FLOWS: f"file://{bw}"}
    first = AgribalyseFlowsSource(_config(harmonised, {**context, JRC: jrc}, "harmonised", "m"))
    second = JrcFlowsSource(_config(jrc, {**context, HARMONISED: harmonised}, "jrc", "m"))
    first.expected_sha256 = second.expected_sha256 = None
    parts = [
        first,
        second,
        bw11,
        _source(_config(bw, inputs)),
        registry_mod.load_source(registry_mod.get_config("agribalyse-ef31-source")),
    ]
    files = pipeline.run_release("ef-3.1", parts, _ctx(tmp_path, schema_dir=FIXTURES))
    terms = pq.read_table(files[0]).to_pylist()
    flows = {t["iri"].rsplit("/", 1)[1] for t in terms if t["type"] == "elementary-flow"}
    edges = pq.read_table(files[1]).to_pylist()
    units = {e["subject"].rsplit("/", 1)[1]: e["object"] for e in edges}
    assert len(units) == len(edges)
    assert {e["predicate"] for e in edges} == {"qudt:hasUnit"}
    assert units == EXPECTED
    assert flows - set(units) == set(WITHOUT_UNIT)
    # every flow the EF correspondence or the CF table places has its context leaf
    leaves = {
        t["iri"].rsplit("/", 1)[1]: json.loads(t["attributes"] or "{}").get("sub_compartment")
        for t in terms
        if t["type"] == "elementary-flow"
    }
    assert leaves["harmonised-redirected"] is None  # in neither source
    assert leaves["jrc-lost"] == "x"  # the CF table alone
    assert {v for k, v in leaves.items() if k in EXPECTED and k != "harmonised-redirected"} == {
        "Emissions to air, unspecified"
    }


def test_registry_block_is_a_private_opt_in_part_of_the_ef31_release():
    (config,) = [c for c in registry_mod.load_registry() if c.module == MODULE]
    assert config.name == NAME
    assert (config.target, config.category, config.term_type) == (
        "sentier_vocab",
        "ef-3.1",
        "elementary-flow",
    )
    assert (config.access, config.enabled, config.emit_filename) == ("private", False, None)
    bw11 = registry_mod.get_config("ef31-elementary-flows-bw11")
    assert config.fetch_url == bw11.fetch_url
    assert config.inputs == bw11.inputs
    # the subjects are terms of this release: nothing to dedup against other releases
    assert config.dedup_check_existing is False
    source = registry_mod.load_source(config)
    assert isinstance(source, Ef31FlowUnitsSource)
