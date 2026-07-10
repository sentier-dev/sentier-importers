import gzip
import json
from pathlib import Path

import pyarrow.parquet as pq
import yaml
from sentier_importers.core import fetch as fetch_mod
from sentier_importers.core import pipeline
from sentier_importers.core.context import RunContext
from sentier_importers.core.source import SourceConfig
from sentier_importers.core.types import RawData
from sentier_importers.sources.agribalyse.flows import (
    AgribalyseFlowsSource,
    cas_number,
    compartment_for_context,
    exact_matches,
    extra_notations,
    formula,
    strip_html,
)

_C = "https://vocab.brightway.one/flow-contexts/"


# --- Task 2: context -> compartment -----------------------------------------
def test_compartment_for_context_covers_all_domains():
    assert compartment_for_context(_C + "envi-air-indr-unkn") == "air"
    assert compartment_for_context(_C + "envi-wate-suwa") == "water"
    assert compartment_for_context(_C + "envi-grou-agri") == "soil"
    assert compartment_for_context(_C + "envi-biot") == "biota"
    assert compartment_for_context(_C + "reso-grou") == "natural resource"
    assert compartment_for_context(_C + "reso-wate") == "natural resource"
    assert compartment_for_context(_C + "laus-occu") == "land use"
    assert compartment_for_context(_C + "laus-tran") == "land use"
    assert compartment_for_context(None) is None
    assert compartment_for_context("https://example.com/weird") is None


# --- Task 3: field helpers ---------------------------------------------------
def test_strip_html_removes_tags_and_collapses_space():
    assert strip_html('<span class="text-smallcaps">D</span>-Glucitol') == "D-Glucitol"
    assert strip_html("Furo[3,2-<em>b</em>]furan") == "Furo[3,2-b]furan"
    assert strip_html("plain") == "plain"


def test_cas_and_formula_pick_first_and_property():
    flow = {
        "cas_numbers": ["64896-70-4", "999"],
        "properties": {"https://w3id.org/chemrof/molecular_formula": "C22H38O6"},
    }
    assert cas_number(flow) == "64896-70-4"
    assert formula(flow) == "C22H38O6"
    assert cas_number({}) is None
    assert formula({"properties": {}}) is None
    # molecular_formula is sometimes a list of candidate formulas -> take the first.
    list_flow = {
        "properties": {"https://w3id.org/chemrof/molecular_formula": ["C11H9NO3", "C13H23BN2O3"]}
    }
    assert formula(list_flow) == "C11H9NO3"
    assert formula({"properties": {"https://w3id.org/chemrof/molecular_formula": []}}) is None


def test_exact_matches_reads_exactmatch_associations_only():
    flow = {
        "concept_associations": [
            {
                "xkos:sourceConcept": {"@id": "https://vocab.brightway.one/ef/3.1/flow/abc"},
                "xkos:targetConcept": {"@id": "https://vocab.brightway.dev/elementary-flows/abc"},
                "xkos:mapType": {"@id": "http://www.w3.org/2004/02/skos/core#exactMatch"},
            },
            {
                "xkos:sourceConcept": {"@id": "https://x/close"},
                "xkos:targetConcept": {"@id": "https://x/close2"},
                "xkos:mapType": {"@id": "http://www.w3.org/2004/02/skos/core#closeMatch"},
            },
        ]
    }
    assert exact_matches(flow) == [
        "https://vocab.brightway.one/ef/3.1/flow/abc",
        "https://vocab.brightway.dev/elementary-flows/abc",
    ]
    assert exact_matches({}) == []


def test_extra_notations_prefixes_ec_and_context():
    flow = {
        "ec_numbers": ["807-840-4"],
        "context_iri": _C + "envi-air-indr-unkn",
    }
    assert extra_notations(flow) == ["ec:807-840-4", "bw-context:envi-air-indr-unkn"]
    assert extra_notations({}) == []


# --- Task 4: parse + transform ----------------------------------------------
_FLOWS = [
    {  # plain EF flow, CAS + formula + exactMatch crosswalk
        "identifier": "abc",
        "source": "EF 3.1",
        "prefLabel": "Carbon dioxide",
        "altLabel": ["CO2"],
        "cas_numbers": ["124-38-9"],
        "ec_numbers": ["204-696-9"],
        "context_iri": _C + "envi-air-unkn",
        "properties": {"https://w3id.org/chemrof/molecular_formula": "CO2"},
        "definition": [],
        "concept_associations": [
            {
                "xkos:sourceConcept": {"@id": "https://vocab.brightway.one/ef/3.1/flow/abc"},
                "xkos:targetConcept": {"@id": "https://vocab.brightway.dev/elementary-flows/abc"},
                "xkos:mapType": {"@id": "http://www.w3.org/2004/02/skos/core#exactMatch"},
            }
        ],
    },
    {  # HTML altLabel, no CAS, resource compartment
        "identifier": "def",
        "source": "EF 3.1",
        "prefLabel": "Iron",
        "altLabel": ['<span class="text-smallcaps">Fe</span>'],
        "context_iri": _C + "reso-grou",
        "properties": {},
    },
    {  # excluded — ecoinvent algorithm addition
        "identifier": "zzz",
        "source": "ecoinvent algorithm addition",
        "prefLabel": "Should be dropped",
        "context_iri": _C + "envi-air-unkn",
    },
]


def _flows_config():
    return SourceConfig(
        name="agribalyse-elementary-flows",
        module="sentier_importers.sources.agribalyse.flows",
        target="sentier_vocab",
        category="elementary-flows",
        fetch_url="unused://",
        fetch_format="json-gz",
        output_format="parquet",
        collection_class="ElementaryFlowCollection",
        collection_items_key="flows",
        collection_scheme="https://vocab.sentier.dev/flows/",
        schema_file="elementary-flow",
        validate_against="ElementaryFlow",
        dedup_check_existing=False,  # Layer B needs network; off for unit tests
    )


def test_transform_maps_ef_flows_and_drops_ecoinvent_additions():
    rows = AgribalyseFlowsSource(_flows_config()).transform(_FLOWS)
    assert [r["iri"] for r in rows] == [
        "https://vocab.sentier.dev/flows/abc",
        "https://vocab.sentier.dev/flows/def",
    ]  # 'zzz' excluded

    co2 = rows[0]
    assert co2["pref_label"] == "Carbon dioxide"
    assert co2["alt_labels"] == ["CO2"]
    assert co2["cas_number"] == "124-38-9"
    assert co2["formula"] == "CO2"
    assert co2["compartment"] == "air"
    assert co2["additional_notations"] == ["ec:204-696-9", "bw-context:envi-air-unkn"]
    assert co2["exact_match"] == [
        "https://vocab.brightway.one/ef/3.1/flow/abc",
        "https://vocab.brightway.dev/elementary-flows/abc",
    ]
    assert co2["source"] == "https://vocab.sentier.dev/sources/ef-3.1"
    assert co2["status"] == "draft"
    assert "definition" not in co2  # empty list -> omitted

    iron = rows[1]
    assert iron["alt_labels"] == ["Fe"]  # HTML stripped
    assert iron["compartment"] == "natural resource"
    assert "cas_number" not in iron
    assert "formula" not in iron


def test_parse_gunzips_and_reads_flows(tmp_path):
    payload = {"schema_version": 1, "flows": _FLOWS}
    raw_path = tmp_path / "f.json.gz"
    raw_path.write_bytes(gzip.compress(json.dumps(payload).encode()))

    raw = RawData(content=raw_path.read_bytes(), source_url=f"file://{raw_path}")
    records = AgribalyseFlowsSource(_flows_config()).parse(raw)
    assert [r["identifier"] for r in records] == ["abc", "def", "zzz"]


# --- Task 5: offline full pipeline ------------------------------------------
_FIXTURES = Path(__file__).parent / "fixtures"


def test_run_source_emits_valid_flow_collection(tmp_path, monkeypatch):
    flow_schema = yaml.safe_load((_FIXTURES / "elementary_flow_collection.yaml").read_text())
    monkeypatch.setattr(pipeline.schema_provider, "resolve_schema", lambda *a, **k: flow_schema)

    gz = _FIXTURES / "harmonised_flows_sample.json.gz"
    src = AgribalyseFlowsSource(_flows_config())
    # Fetch from the bundled fixture so the test is fully offline.
    monkeypatch.setattr(src, "fetch", lambda ctx: fetch_mod.fetch(f"file://{gz}", ctx))

    # Matches tests/sources/test_foodex2.py::_ctx — dry_run defaults True (stage, no PR).
    ctx = RunContext(cache_dir=tmp_path / "c", output_dir=tmp_path / "o")
    out = pipeline.run_source(src, ctx)

    table = pq.read_table(out)
    assert set(table.column("iri").to_pylist()) == {
        "https://vocab.sentier.dev/flows/abc",
        "https://vocab.sentier.dev/flows/def",
    }
    assert table.schema.metadata[b"scheme"] == b"https://vocab.sentier.dev/flows/"
