"""The ef31-elementary-flows-jrc part adds the JRC flows the harmonised list lacks."""

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
from sentier_importers.sources.agribalyse.flows import AgribalyseFlowsSource
from sentier_importers.sources.agribalyse.flows_jrc import (
    HARMONISED,
    JrcFlowsSource,
    compartment_for_path,
    context_path,
)
from sentier_importers.sources.agribalyse.provenance import PROVENANCE_IRI

FIXTURES = Path(__file__).parents[1] / "core" / "fixtures" / "vocab"
MODULE = "sentier_importers.sources.agribalyse.flows_jrc"
FLOWS = "https://vocab.sentier.dev/flows/"
AIR = ("Emissions", "Emissions to air", "Emissions to air, unspecified")
LONG_TERM_WATER = (
    "Emissions",
    "Emissions to water",
    "Emissions to water, unspecified (long-term)",
)
SOIL = ("Emissions", "Emissions to soil", "Emissions to non-agricultural soil")
RESOURCE_AIR = ("Resources", "Resources from air", "Renewable material resources from air")
LAND = ("Land use", "Land occupation", None)

ROWS = [
    # in the harmonised list: never emitted here, whatever the table says
    ("in-harmonised", "ammonia", *AIR),
    # one flow on three CF rows, two spellings of its name: one row, the cased spelling
    ("jrc-1", "aldehydes, unspecified", *LONG_TERM_WATER),
    ("jrc-1", "Aldehydes, unspecified", *LONG_TERM_WATER),
    ("jrc-1", "aldehydes, unspecified", *LONG_TERM_WATER),
    ("jrc-2", "fenoxycarb", *SOIL),
    ("jrc-3", "carbon dioxide", *RESOURCE_AIR),
    ("jrc-4", "arable", *LAND),
]


def _table(rows):
    columns = ["FLOW_uuid", "FLOW_name", "FLOW_class0", "FLOW_class1", "FLOW_class2"]
    return pa.table(
        {c: pa.array([r[i] for r in rows], pa.string()) for i, c in enumerate(columns)}
    )


def _write_inputs(tmp_path, rows=ROWS):
    cf = tmp_path / "cf.parquet"
    pq.write_table(_table(rows), cf)
    harmonised = tmp_path / "harmonised.json.gz"
    flows = [
        {"identifier": "in-harmonised", "source": "EF 3.1", "prefLabel": "Ammonia"},
        # an ecoinvent addition is not emitted by the harmonised part, so it does not exclude
        {"identifier": "jrc-2", "source": "ecoinvent algorithm addition", "prefLabel": "x"},
    ]
    harmonised.write_bytes(gzip.compress(json.dumps({"flows": flows}).encode()))
    return cf, harmonised


def _config(cf, harmonised) -> SourceConfig:
    return SourceConfig(
        name="ef31-elementary-flows-jrc",
        module=MODULE,
        target="sentier_vocab",
        category="ef-3.1",
        term_type="elementary-flow",
        fetch_url=f"file://{cf}",
        fetch_format="parquet",
        output_format="parquet",
        inputs={HARMONISED: f"file://{harmonised}"},
    )


def _rows(tmp_path, rows=ROWS):
    src = JrcFlowsSource(_config(*_write_inputs(tmp_path, rows)))
    ctx = RunContext(cache_dir=tmp_path / "cache", output_dir=tmp_path / "out")
    return src.transform(src.parse(src.fetch(ctx)))


def test_only_flows_outside_the_harmonised_list_are_emitted(tmp_path):
    rows = _rows(tmp_path)
    assert [r["iri"] for r in rows] == [
        FLOWS + "jrc-1",
        FLOWS + "jrc-2",
        FLOWS + "jrc-3",
        FLOWS + "jrc-4",
    ]


def test_one_row_per_flow_with_the_cased_spelling(tmp_path):
    (aldehydes,) = [r for r in _rows(tmp_path) if r["iri"].endswith("jrc-1")]
    assert aldehydes == {
        "iri": FLOWS + "jrc-1",
        "pref_label": "Aldehydes, unspecified",
        "compartment": "water",
        "source": PROVENANCE_IRI,
        "status": "draft",
    }


def test_context_gives_the_compartment_and_no_bw_context_notation(tmp_path):
    rows = _rows(tmp_path)
    by_code = {r["iri"].rsplit("/", 1)[1]: r for r in rows}
    assert by_code["jrc-2"]["compartment"] == "soil"
    assert by_code["jrc-3"]["compartment"] == "natural resource"
    assert by_code["jrc-4"]["compartment"] == "land use"
    # the bw-context code belongs to the harmonised list; computing one here would
    # feed matching.bw_context its own crosswalk back
    assert all("additional_notations" not in r for r in rows)


def test_context_helpers():
    assert (
        context_path(
            {"FLOW_class0": "Land use", "FLOW_class1": "Land occupation", "FLOW_class2": None}
        )
        == "Land use / Land occupation"
    )
    assert (
        compartment_for_path("Emissions / Emissions to water / Emissions to fresh water")
        == "water"
    )
    assert compartment_for_path("Resources / Resources from ground / x") == "natural resource"
    assert compartment_for_path("Land use / Land transformation") == "land use"
    assert compartment_for_path("Something else") is None


def test_a_flow_with_two_contexts_is_an_error(tmp_path):
    rows = [("jrc-9", "x", *AIR), ("jrc-9", "x", *SOIL)]
    with pytest.raises(ValidationError, match="jrc-9"):
        _rows(tmp_path, rows)


def test_a_row_without_uuid_name_or_context_is_an_error(tmp_path):
    for bad in [(None, "x", *AIR), ("jrc-9", "", *AIR), ("jrc-9", "x", None, None, None)]:
        with pytest.raises(ValidationError):
            _rows(tmp_path, [bad])


def test_rows_pass_the_vocab_contract(tmp_path):
    contract = vocab_contract.load_contract(FIXTURES / "term.yaml", FIXTURES / "relation.yaml")
    terms, relations = vocab_layers.to_layers(
        _rows(tmp_path), term_type="elementary-flow", release="ef-3.1", contract=contract
    )
    assert vocab_contract.check(terms, relations, contract, release="ef-3.1", complete=False) == []


def test_registry_block_is_a_part_of_the_ef31_release():
    (config,) = [c for c in registry_mod.load_registry() if c.module == MODULE]
    assert (config.target, config.category, config.term_type) == (
        "sentier_vocab",
        "ef-3.1",
        "elementary-flow",
    )
    assert set(config.inputs) == {HARMONISED}
    assert config.inputs[HARMONISED].endswith("harmonised-flows-simple.json.gz")
    assert config.fetch_url.endswith("lciamethods_CF.parquet")


def _harmonised_gz(tmp_path, flows):
    path = tmp_path / "h.json.gz"
    path.write_bytes(gzip.compress(json.dumps({"flows": flows}).encode()))
    return path


def test_the_two_parts_are_disjoint_and_cover_every_jrc_flow(tmp_path):
    """The exclusion is the harmonised part's own emission rule, not a copy of it."""
    harmonised = [
        {"identifier": "a", "source": "EF 3.1", "prefLabel": "A"},
        {"identifier": "b", "source": "EF 3.1", "prefLabel": "  "},  # not emitted: no label
        {"identifier": "c", "source": "ecoinvent algorithm addition", "prefLabel": "C"},
    ]
    cf = tmp_path / "cf.parquet"
    pq.write_table(_table([(u, u, *AIR) for u in "abcd"]), cf)
    src = JrcFlowsSource(_config(cf, _harmonised_gz(tmp_path, harmonised)))
    ctx = RunContext(cache_dir=tmp_path / "cache", output_dir=tmp_path / "out")
    jrc = {r["iri"] for r in src.transform(src.parse(src.fetch(ctx)))}
    first = {r["iri"] for r in AgribalyseFlowsSource(_config(cf, "unused")).transform(harmonised)}
    assert first == {FLOWS + "a"}
    assert jrc == {FLOWS + "b", FLOWS + "c", FLOWS + "d"}
    assert not first & jrc


@pytest.mark.parametrize("flows", [{}, ["not a record"], []])
def test_a_harmonised_list_that_excludes_nothing_is_a_parse_error(tmp_path, flows):
    cf, _ = _write_inputs(tmp_path)
    src = JrcFlowsSource(_config(cf, _harmonised_gz(tmp_path, flows)))
    ctx = RunContext(cache_dir=tmp_path / "cache", output_dir=tmp_path / "out")
    with pytest.raises(ParseError, match="harmonised"):
        src.transform(src.parse(src.fetch(ctx)))
