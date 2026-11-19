import pytest
from sentier_importers.core.context import RunContext
from sentier_importers.core.errors import ValidationError
from sentier_importers.sources.brightway_flows.bafu_sources import (
    BafuBrightwayFlowsAlignSource,
    BafuBrightwayFlowsPackageSource,
    BafuBrightwayFlowsReviewSource,
)

from tests.sources.test_brightway_flows_bafu import (
    AMMONIA,
    BENZENE,
    BORAX,
    BROAD,
    config,
    entry,
    stage,
)

CURATED = [entry(AMMONIA, "ef-ammonia", "ammonia"), entry(BENZENE, "ef-benzene", "benzene")]


def run(cls, tmp_path, curated=CURATED, **config_kw):
    source = cls(config(stage(tmp_path, curated=curated), **config_kw))
    source.expected_sha256 = None
    ctx = RunContext(cache_dir=tmp_path / "cache", output_dir=tmp_path / "out")
    return source.transform(source.parse(source.fetch(ctx)))


def test_package_holds_only_unmapped_flows_with_an_accepted_link(tmp_path):
    rows = run(BafuBrightwayFlowsPackageSource, tmp_path)
    assert [r["source"]["name"] for r in rows] == ["Borax", "Thorium-232"]


def test_package_is_sorted_and_stable(tmp_path):
    first = run(BafuBrightwayFlowsPackageSource, tmp_path)
    assert first == run(BafuBrightwayFlowsPackageSource, tmp_path)


def test_align_emits_the_whole_package_with_only_the_retarget_changed(tmp_path):
    rows = run(BafuBrightwayFlowsAlignSource, tmp_path, emit="biosphere-1-curated")
    assert len(rows) == 2
    assert rows[0]["target"]["code"] == "ef-ammonia-twin"
    assert rows[1] == CURATED[1]


def test_align_of_another_package_leaves_it_as_it_is(tmp_path):
    assert run(BafuBrightwayFlowsAlignSource, tmp_path, emit="biosphere-3-matched") == []


def test_review_lists_what_was_not_applied(tmp_path):
    rows = run(BafuBrightwayFlowsReviewSource, tmp_path, verb="review")
    by_name = {r["source"]["name"]: r for r in rows}
    assert "Borax" not in by_name and "Thorium-232" not in by_name  # shipped in package 5
    assert "Ammonia" not in by_name  # identical factors: aligned, nothing to review
    benzene = by_name["Benzene"]
    assert benzene["kind"] == "score_changing_retarget" and benzene["factors"] == "differ"
    assert benzene["methods"] == ["ef-3.1:human-toxicity-cancer"]
    assert benzene["package"] == "biosphere-1-curated"
    assert benzene["ours"]["code"] == "ef-benzene"
    assert benzene["brightway_flows"]["code"] == "ef-benzene-other"
    assert by_name["Hexane"]["kind"] == "new_link_withheld"
    assert by_name["Hexane"]["reason"] == "no_ef_target"
    assert by_name["Hexane"]["brightway_flows"]["in_ef"] is False
    assert by_name["Toxic"]["reason"] == "characterised_held_for_review"
    assert by_name["Toxic"]["methods"] == ["ef-3.1:human-toxicity-cancer"]


def test_align_refuses_a_package_it_was_not_given(tmp_path):
    with pytest.raises(ValidationError, match="biosphere-5-brightway-flows"):
        run(BafuBrightwayFlowsAlignSource, tmp_path)


def test_review_is_exactly_what_package_and_align_did_not_apply(tmp_path):
    rows = run(BafuBrightwayFlowsReviewSource, tmp_path, verb="review")
    assert [r["source"]["name"] for r in rows] == [
        "Benzene",
        "Broad",
        "Hexane",
        "Longterm",
        "Odd",
        "Toxic",
    ]
    by_name = {r["source"]["name"]: r for r in rows}
    assert by_name["Broad"]["reason"] == "broad_match"
    assert by_name["Longterm"]["reason"] == "long_term_onto_immediate"
    assert by_name["Odd"]["reason"] == "unit_mismatch"
    assert all(r["kind"] == "new_link_withheld" for r in rows if r["source"]["name"] != "Benzene")


def test_review_lists_a_mapped_flow_whose_link_is_not_usable(tmp_path):
    curated = [entry(BROAD, "ef-ammonia", "ammonia")]
    rows = run(BafuBrightwayFlowsReviewSource, tmp_path, curated=curated, verb="review")
    broad = {r["source"]["name"]: r for r in rows}["Broad"]
    assert broad["kind"] == "theirs_not_usable" and broad["reason"] == "broad_match"
    assert broad["package"] == "biosphere-1-curated" and broad["ours"]["code"] == "ef-ammonia"


def test_review_lists_a_retarget_between_two_factorless_flows(tmp_path):
    curated = [entry(BORAX, "ef-long", "Longterm")]
    rows = run(BafuBrightwayFlowsReviewSource, tmp_path, curated=curated, verb="review")
    borax = {r["source"]["name"]: r for r in rows}["Borax"]
    assert borax["kind"] == "retarget_withheld" and borax["reason"] == "both_zero"
    assert borax["ours"]["code"] == "ef-long" and borax["brightway_flows"]["code"] == "ef-borax"
    assert "factors" not in borax and borax["methods"] == []
    aligned = run(
        BafuBrightwayFlowsAlignSource, tmp_path, curated=curated, emit="biosphere-1-curated"
    )
    assert aligned == curated
