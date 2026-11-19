import gzip
import json
from pathlib import Path

import pyarrow.parquet as pq
import pytest
from loguru import logger
from sentier_importers.core import fetch as fetch_mod
from sentier_importers.core import pipeline
from sentier_importers.core.context import RunContext
from sentier_importers.core.errors import ValidationError
from sentier_importers.core.source import SourceConfig
from sentier_importers.core.types import RawData
from sentier_importers.sources.agribalyse.ef_context import BRIGHTWAY_FLOWS, JRC
from sentier_importers.sources.agribalyse.flows import (
    AgribalyseFlowsSource,
    cas_number,
    compartment_for_context,
    exact_matches,
    extra_notations,
    formula,
    strip_html,
)
from sentier_importers.sources.brightway_flows.correspondences import PINNED_SHA256

from tests.sources.ef_context_fixture import bw_bytes, entry, jrc_bytes, raw

_C = "https://vocab.brightway.one/flow-contexts/"
_AIR = "Emissions/Emissions to air/Emissions to air, unspecified"
_GROUND = ("Resources", "Resources from ground", "Non-renewable element resources from ground")


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


@pytest.fixture
def info():
    """Capture loguru messages from INFO up (loguru bypasses pytest's caplog)."""
    messages: list[str] = []
    sink_id = logger.add(lambda m: messages.append(str(m)), level="INFO")
    yield messages
    logger.remove(sink_id)


def _flows_config():
    return SourceConfig(
        name="ef31-elementary-flows",
        module="sentier_importers.sources.agribalyse.flows",
        target="sentier_vocab",
        category="ef-3.1",
        term_type="elementary-flow",
        fetch_url="unused://",
        fetch_format="json-gz",
        output_format="parquet",
        dedup_check_existing=False,  # Layer B needs network; off for unit tests
    )


def _flows_source(ef_links=(entry("abc", _AIR),), jrc_rows=(("def", "iron", *_GROUND),)):
    """The source with its two context inputs as if fetched: 'abc' has an entry in the
    EF correspondence, 'def' is only in the JRC CF table."""
    source = AgribalyseFlowsSource(_flows_config())
    source.expected_sha256 = None
    source.inputs = {
        BRIGHTWAY_FLOWS: raw(bw_bytes(ef_links)),
        JRC: raw(jrc_bytes(jrc_rows or [("other", "x", *_GROUND)])),
    }
    return source


def test_transform_maps_ef_flows_and_drops_ecoinvent_additions():
    rows = _flows_source().transform(_FLOWS)
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
    # the leaf of the exact EF context, from the flow's entry in the EF correspondence
    assert co2["sub_compartment"] == "Emissions to air, unspecified"
    assert co2["additional_notations"] == ["ec:204-696-9", "bw-context:envi-air-unkn"]
    # Brightway correspondences are a sentier-mappings bridge, not vocab relations
    assert "exact_match" not in co2
    assert co2["source"] == "https://vocab.sentier.dev/sources/ef-3.1"
    assert co2["status"] == "draft"
    assert "definition" not in co2  # empty list -> omitted

    iron = rows[1]
    assert iron["alt_labels"] == ["Fe"]  # HTML stripped
    assert iron["compartment"] == "natural resource"
    # no entry in the EF correspondence: the path of the JRC CF table
    assert iron["sub_compartment"] == "Non-renewable element resources from ground"
    assert "cas_number" not in iron
    assert "formula" not in iron


def test_transform_keeps_a_flow_without_a_resolvable_compartment():
    orphan = [{"identifier": "xyz", "source": "EF 3.1", "prefLabel": "Weird", "context_iri": None}]
    (row,) = _flows_source().transform(orphan)
    assert "compartment" not in row


def test_a_flow_neither_source_places_has_no_sub_compartment_and_is_reported(info):
    rows = _flows_source(jrc_rows=()).transform(_FLOWS)
    assert "sub_compartment" in rows[0]
    # never inferred from the bw-context code of the flow
    assert "sub_compartment" not in rows[1]
    assert rows[1]["additional_notations"] == ["bw-context:reso-grou"]
    (line,) = [m for m in info if "without a sub_compartment" in m]
    assert "1 of 2 flows" in line and "'def'" in line


def test_sources_that_disagree_on_a_context_path_are_refused():
    source = _flows_source(jrc_rows=(("abc", "co2", "Emissions", "Emissions to air", "other"),))
    with pytest.raises(ValidationError, match="'abc'"):
        source.transform(_FLOWS)


@pytest.mark.parametrize("missing", [BRIGHTWAY_FLOWS, JRC])
def test_a_missing_context_input_is_a_validation_error(missing):
    source = _flows_source()
    del source.inputs[missing]
    with pytest.raises(ValidationError, match=missing):
        source.transform(_FLOWS)


def test_the_pinned_brightway_flows_release_is_checked_by_default():
    assert AgribalyseFlowsSource.expected_sha256 == PINNED_SHA256


def test_registry_declares_the_three_ef31_flow_parts_of_the_release():
    from sentier_importers.core.registry import load_registry

    parts = [c for c in load_registry() if c.name.startswith("ef31-elementary-flows")]
    # the harmonised list, the JRC flows it lacks (flows_jrc), and the flows only
    # Brightway Flows 1.1 lists (brightway_flows.vocab_flows); one release, one type
    assert [c.name for c in parts] == [
        "ef31-elementary-flows",
        "ef31-elementary-flows-jrc",
        "ef31-elementary-flows-bw11",
    ]
    for part in parts:
        assert (part.category, part.term_type) == ("ef-3.1", "elementary-flow")
        assert part.emit_filename is None and not part.enabled
    # each part reads the exact EF context paths from the same two files
    harmonised, jrc, bw11 = parts
    assert harmonised.inputs == {BRIGHTWAY_FLOWS: bw11.fetch_url, JRC: jrc.fetch_url}
    assert jrc.inputs[BRIGHTWAY_FLOWS] == bw11.fetch_url
    assert bw11.inputs[JRC] == jrc.fetch_url


def test_parse_gunzips_and_reads_flows(tmp_path):
    payload = {"schema_version": 1, "flows": _FLOWS}
    raw_path = tmp_path / "f.json.gz"
    raw_path.write_bytes(gzip.compress(json.dumps(payload).encode()))

    raw = RawData(content=raw_path.read_bytes(), source_url=f"file://{raw_path}")
    records = AgribalyseFlowsSource(_flows_config()).parse(raw)
    assert [r["identifier"] for r in records] == ["abc", "def", "zzz"]


# --- Task 5: offline full pipeline ------------------------------------------
_FIXTURES = Path(__file__).parent / "fixtures"
#: A copy of sentier-vocab's schemas/ folder (term.yaml, relation.yaml).
_VOCAB_SCHEMAS = Path(__file__).parents[1] / "core" / "fixtures" / "vocab"


def test_run_source_stages_a_valid_flow_part(tmp_path, monkeypatch):
    gz = _FIXTURES / "harmonised_flows_sample.json.gz"
    src = _flows_source()
    # Fetch from the bundled fixture so the test is fully offline.
    monkeypatch.setattr(src, "fetch", lambda ctx: fetch_mod.fetch(f"file://{gz}", ctx))

    # dry_run defaults True (stage, no PR); the contracts come from the vendored copy.
    ctx = RunContext(
        cache_dir=tmp_path / "c", output_dir=tmp_path / "o", schema_dir=_VOCAB_SCHEMAS
    )
    out = pipeline.run_source(src, ctx)

    terms = {t["iri"]: t for t in pq.read_table(out).to_pylist()}
    assert set(terms) == {
        "https://vocab.sentier.dev/flows/abc",
        "https://vocab.sentier.dev/flows/def",
    }
    assert {t["type"] for t in terms.values()} == {"elementary-flow"}
    co2 = json.loads(terms["https://vocab.sentier.dev/flows/abc"]["attributes"])
    assert co2["compartment"] == "air" and co2["cas_number"] == "124-38-9"
    assert co2["sub_compartment"] == "Emissions to air, unspecified"
    # no relation file: the flow list carries no IRI-valued slot any more
    assert not (out.parents[1] / "relations").exists()
