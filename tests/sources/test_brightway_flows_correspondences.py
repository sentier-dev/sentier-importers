import hashlib

import pytest
from sentier_importers.core.errors import ParseError
from sentier_importers.sources.brightway_flows.correspondences import load

from tests.sources.brightway_flows_fixture import document, flow, gz, link

DOC = document(
    flows=[
        flow("ef-a", "Ammonia"),
        flow("ef-b", "Benzene"),
        flow("add-1", "Hexane", context_code="envi-wate-rive", source="bafu algorithm addition"),
    ],
    links_by_list={
        "ef-3.1": [
            link("ef/3.1", "ef-a", "ef-a", match="exactMatch"),
            link("ef/3.1", "ef-old", "ef-b", match="broadMatch"),
        ],
        "bafu-2026-v1": [
            link(
                "bafu/2026-v1",
                "s1",
                "ef-a",
                label="Ammonia",
                context="emissions to air / unspecified",
            ),
            link("bafu/2026-v1", "s2", "add-1", match="exactMatch"),
            link("bafu/2026-v1", "s3", "ef-b", unit="BQ", multiplier=0.001, basis="unit-table"),
        ],
    },
    redirects=[("gone", "ef-a")],
)


def test_flows_know_whether_they_are_ef_flows():
    bw = load(gz(DOC), expected_sha256=None)
    assert bw.flows["ef-a"].is_ef and not bw.flows["add-1"].is_ef
    assert bw.flows["add-1"].context_code == "envi-wate-rive"


def test_links_carry_match_type_unit_and_multiplier():
    bw = load(gz(DOC), expected_sha256=None)
    s1, s2, s3 = bw.links["bafu-2026-v1"]
    assert (s1.source_id, s1.target_id, s1.match, s1.unit) == (
        "s1",
        "ef-a",
        "closeMatch",
        "KiloGM",
    )
    assert s1.label == "Ammonia" and s1.context == "emissions to air / unspecified"
    assert s2.match == "exactMatch" and s2.multiplier is None
    assert (s3.multiplier, s3.basis) == (0.001, "unit-table")


def test_resolve_follows_ef_merges_and_redirects():
    bw = load(gz(DOC), expected_sha256=None)
    assert bw.resolve("ef-a") == "ef-a"
    assert bw.resolve("ef-old") == "ef-b"  # an EF flow merged into another one
    assert bw.resolve("gone") == "ef-a"  # a redirected identifier
    assert bw.resolve("unknown") == "unknown"


def test_a_file_that_is_not_the_pinned_release_is_refused():
    content = gz(DOC)
    assert load(content, expected_sha256=hashlib.sha256(content).hexdigest()).flows
    with pytest.raises(ParseError, match="sha256"):
        load(content, expected_sha256="0" * 64)


def test_garbage_is_a_parse_error():
    with pytest.raises(ParseError):
        load(b"not gzip", expected_sha256=None)
