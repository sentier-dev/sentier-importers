"""The ef31-elementary-flows-bw11 part adds the EF flows only Brightway Flows 1.1 lists."""

import gzip
import json
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from sentier_importers.core import registry as registry_mod
from sentier_importers.core import vocab_contract, vocab_layers
from sentier_importers.core.context import RunContext
from sentier_importers.core.errors import ParseError, ValidationError
from sentier_importers.core.source import SourceConfig
from sentier_importers.matching.bw_context import ef_context_for
from sentier_importers.sources.agribalyse.provenance import PROVENANCE_IRI
from sentier_importers.sources.brightway_flows.correspondences import PINNED_SHA256
from sentier_importers.sources.brightway_flows.vocab_flows import (
    HARMONISED,
    JRC,
    BrightwayFlowsVocabSource,
)

from tests.sources.brightway_flows_fixture import document, flow, gz, link

FIXTURES = Path(__file__).parents[1] / "core" / "fixtures" / "vocab"
MODULE = "sentier_importers.sources.brightway_flows.vocab_flows"
NAME = "ef31-elementary-flows-bw11"
FLOWS = "https://vocab.sentier.dev/flows/"
LT_WATER = "Emissions/Emissions to water/Emissions to water, unspecified (long-term)"
SOIL = "Emissions/Emissions to soil/Emissions to soil, unspecified"
RESOURCE = "Resources/Resources from ground/Non-renewable energy resources from ground"
LAND = "Land use/Land occupation"


def own(identifier, label, context):
    """A flow's own entry in the EF correspondence: the JRC name and context path."""
    return link("ef/3.1", identifier, identifier, match="exactMatch", label=label, context=context)


FLOW_LIST = [
    flow("in-harmonised", "Ammonia"),
    flow("in-jrc", "Benzene"),
    flow("not-ef", "Hexane", source="bafu algorithm addition"),
    flow("no-label", "  "),
    flow(
        "new-2",
        "1-Chloro-1,1,2,2,2-pentafluoroethane",
        context_code="envi-wate-lote",
        alt_labels=["R 115", "<i>r 115</i>", "1-chloro-1,1,2,2,2-pentafluoroethane", "Freon 115"],
        cas_numbers=["76-15-3", "0-0-0"],
        ec_numbers=["200-938-2", "", "999-999-9"],
        formula=["C2ClF5", "x"],
    ),
    flow("new-1", " Fenoxycarb ", context_code="envi-grou-unkn"),
    flow("new-3", "Energy, tidal", unit="MJ"),
    flow("new-4", "Arable", unit="m2.a", alt_labels=["arable land"]),
]
EF_LINKS = [
    own("in-harmonised", "ammonia", "Emissions/Emissions to air/Emissions to air, unspecified"),
    own("new-2", "CFC-115", LT_WATER),
    own("new-1", "fenoxycarb", SOIL),
    own("new-3", "Energy, tidal", RESOURCE),
    own("new-4", "Arable Land", " Land use / Land occupation "),
    # an EF flow merged into another one: not the target's own entry
    link("ef/3.1", "merged-away", "new-1", match="exactMatch", label="other", context=LAND),
]
HARMONISED_FLOWS = [
    {"identifier": "in-harmonised", "source": "EF 3.1", "prefLabel": "Ammonia"},
    # an ecoinvent addition is not emitted by the harmonised part, so it does not exclude
    {"identifier": "new-1", "source": "ecoinvent algorithm addition", "prefLabel": "x"},
]


def _write(
    tmp_path, flows=FLOW_LIST, ef_links=EF_LINKS, harmonised=HARMONISED_FLOWS, jrc=("in-jrc",)
):
    tmp_path.mkdir(parents=True, exist_ok=True)
    bw = tmp_path / "bw.json.gz"
    bw.write_bytes(gz(document(flows, {"ef-3.1": ef_links})))
    harmonised_path = tmp_path / "harmonised.json.gz"
    harmonised_path.write_bytes(gzip.compress(json.dumps({"flows": harmonised}).encode()))
    jrc_path = tmp_path / "cf.parquet"
    pq.write_table(pa.table({"FLOW_uuid": pa.array(list(jrc), pa.string())}), jrc_path)
    return bw, {HARMONISED: f"file://{harmonised_path}", JRC: f"file://{jrc_path}"}


def _config(bw, inputs) -> SourceConfig:
    return SourceConfig(
        name=NAME,
        module=MODULE,
        target="sentier_vocab",
        category="ef-3.1",
        term_type="elementary-flow",
        fetch_url=f"file://{bw}",
        fetch_format="json",
        output_format="parquet",
        inputs=inputs,
    )


def _source(config) -> BrightwayFlowsVocabSource:
    source = BrightwayFlowsVocabSource(config)
    source.expected_sha256 = None
    return source


def _run(tmp_path, source):
    ctx = RunContext(cache_dir=tmp_path / "cache", output_dir=tmp_path / "out")
    return source.transform(source.parse(source.fetch(ctx)))


def _rows(tmp_path, **kwargs):
    return _run(tmp_path, _source(_config(*_write(tmp_path, **kwargs))))


def test_only_ef_flows_neither_existing_part_emits_are_rows_sorted_by_iri(tmp_path):
    # skipped: in the earlier list, in the JRC table, not an EF flow, without a label
    assert [r["iri"] for r in _rows(tmp_path)] == [
        FLOWS + "new-1",
        FLOWS + "new-2",
        FLOWS + "new-3",
        FLOWS + "new-4",
    ]


def test_a_row_maps_label_alt_labels_cas_formula_compartment_and_notations(tmp_path):
    by_code = {r["iri"].rsplit("/", 1)[1]: r for r in _rows(tmp_path)}
    assert by_code["new-2"] == {
        "iri": FLOWS + "new-2",
        "pref_label": "1-Chloro-1,1,2,2,2-pentafluoroethane",
        # HTML stripped, de-duplicated without case, never the label; the JRC name last
        "alt_labels": ["R 115", "Freon 115", "CFC-115"],
        "cas_number": "76-15-3",
        "formula": "C2ClF5",
        "compartment": "water",
        "additional_notations": [
            "ec:200-938-2",
            "ec:999-999-9",
            "ef-context:Emissions / Emissions to water / "
            "Emissions to water, unspecified (long-term)",
        ],
        "source": PROVENANCE_IRI,
        "status": "draft",
    }


def test_the_jrc_name_is_an_alt_label_only_when_it_differs_from_the_label(tmp_path):
    by_code = {r["iri"].rsplit("/", 1)[1]: r for r in _rows(tmp_path)}
    # "fenoxycarb" is the label in another casing: not an alt label; no CAS, no formula
    assert by_code["new-1"] == {
        "iri": FLOWS + "new-1",
        "pref_label": "Fenoxycarb",
        "compartment": "soil",
        "additional_notations": [
            "ef-context:Emissions / Emissions to soil / Emissions to soil, unspecified"
        ],
        "source": PROVENANCE_IRI,
        "status": "draft",
    }
    assert "alt_labels" not in by_code["new-3"]
    # "Arable Land" is already an alt label in another casing: not added twice
    assert by_code["new-4"]["alt_labels"] == ["arable land"]


def test_every_row_has_one_ef_context_the_index_can_read_and_no_bw_context(tmp_path):
    rows = _rows(tmp_path)
    by_code = {r["iri"].rsplit("/", 1)[1]: r for r in rows}
    for row in rows:
        notations = row["additional_notations"]
        assert sum(n.startswith("ef-context:") for n in notations) == 1
        assert notations[-1].startswith("ef-context:")
        assert not any(n.startswith("bw-context:") for n in notations)
    assert by_code["new-3"]["compartment"] == "natural resource"
    assert by_code["new-4"]["compartment"] == "land use"
    assert ef_context_for(by_code["new-4"]["additional_notations"]) == "Land use / Land occupation"


def test_a_flow_without_its_own_ef_correspondence_entry_is_refused(tmp_path):
    links = [item for item in EF_LINKS if "/new-1" not in item["xkos:sourceConcept"]["@id"]]
    with pytest.raises(ValidationError, match="new-1"):
        _rows(tmp_path, ef_links=links)


def test_a_flow_with_an_empty_context_path_is_refused(tmp_path):
    links = [own("new-1", "fenoxycarb", " / "), *EF_LINKS[:2], *EF_LINKS[3:5]]
    with pytest.raises(ValidationError, match="new-1"):
        _rows(tmp_path, ef_links=links)


def test_a_flow_with_two_context_paths_is_refused(tmp_path):
    with pytest.raises(ValidationError, match="new-1"):
        _rows(tmp_path, ef_links=[*EF_LINKS, own("new-1", "fenoxycarb", LAND)])


@pytest.mark.parametrize("missing", [HARMONISED, JRC])
def test_a_missing_input_is_a_validation_error(tmp_path, missing):
    bw, inputs = _write(tmp_path)
    source = _source(_config(bw, {k: v for k, v in inputs.items() if k != missing}))
    with pytest.raises(ValidationError, match=missing):
        _run(tmp_path, source)


def test_a_harmonised_list_that_excludes_nothing_is_a_parse_error(tmp_path):
    with pytest.raises(ParseError, match="harmonised"):
        _rows(tmp_path, harmonised=[])


def test_a_jrc_table_that_excludes_nothing_is_a_parse_error(tmp_path):
    with pytest.raises(ParseError, match="jrc"):
        _rows(tmp_path, jrc=())


def test_a_jrc_input_that_is_not_the_cf_table_is_a_parse_error(tmp_path):
    bw, inputs = _write(tmp_path)
    garbage = tmp_path / "garbage.parquet"
    garbage.write_bytes(b"not parquet")
    source = _source(_config(bw, {**inputs, JRC: f"file://{garbage}"}))
    with pytest.raises(ParseError, match="jrc"):
        _run(tmp_path, source)


def test_the_pinned_release_is_checked_by_default(tmp_path):
    assert BrightwayFlowsVocabSource.expected_sha256 == PINNED_SHA256
    source = BrightwayFlowsVocabSource(_config(*_write(tmp_path)))
    with pytest.raises(ParseError, match="sha256"):
        _run(tmp_path, source)


def test_rows_pass_the_vocab_contract(tmp_path):
    contract = vocab_contract.load_contract(FIXTURES / "term.yaml", FIXTURES / "relation.yaml")
    terms, relations = vocab_layers.to_layers(
        _rows(tmp_path), term_type="elementary-flow", release="ef-3.1", contract=contract
    )
    assert vocab_contract.check(terms, relations, contract, release="ef-3.1", complete=False) == []


def test_registry_block_is_a_part_of_the_ef31_release():
    (config,) = [c for c in registry_mod.load_registry() if c.module == MODULE]
    assert config.name == NAME
    assert (config.target, config.category, config.term_type) == (
        "sentier_vocab",
        "ef-3.1",
        "elementary-flow",
    )
    assert (config.access, config.enabled, config.emit_filename) == ("private", False, None)
    assert config.fetch_url.endswith(
        "zenodo.org/api/records/23554066/files/harmonised-flows-simple.json.gz/content"
    )
    assert set(config.inputs) == {HARMONISED, JRC}
    assert config.inputs[HARMONISED].endswith("harmonised-flows-simple.json.gz")
    assert config.inputs[JRC].endswith("lciamethods_CF.parquet")
    assert (config.dedup_on_existing, config.dedup_check_existing) == ("skip", True)


def test_registry_loads_the_one_source_of_the_module():
    (config,) = [c for c in registry_mod.load_registry() if c.module == MODULE]
    source = registry_mod.load_source(config)
    assert isinstance(source, BrightwayFlowsVocabSource)
    assert source.config.name == NAME
