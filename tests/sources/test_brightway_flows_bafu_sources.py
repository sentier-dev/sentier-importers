import orjson
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
    REDIR,
    SCALED,
    THORIUM,
    TOXIC,
    UNIVERSE,
    VAPOUR,
    ZERO,
    bw_document,
    config,
    entry,
    stage,
    vapour_entry,
)

CURATED = [entry(AMMONIA, "ef-ammonia", "ammonia"), entry(BENZENE, "ef-benzene", "benzene")]


def run(cls, tmp_path, curated=CURATED, doc=None, universe=UNIVERSE, **config_kw):
    root = stage(tmp_path, curated=curated, doc=doc, universe=universe)
    source = cls(config(root, **config_kw))
    source.expected_sha256 = None
    ctx = RunContext(cache_dir=tmp_path / "cache", output_dir=tmp_path / "out")
    return source.transform(source.parse(source.fetch(ctx)))


def test_package_holds_only_unmapped_flows_with_an_accepted_link(tmp_path):
    rows = run(BafuBrightwayFlowsPackageSource, tmp_path)
    assert [r["source"]["name"] for r in rows] == ["Borax", "Thorium-232"]


def test_package_is_sorted_and_stable(tmp_path):
    shuffled = [ZERO, THORIUM, *reversed(UNIVERSE[:6])]  # Zero, Thorium, ..., Borax, ...
    first = run(BafuBrightwayFlowsPackageSource, tmp_path / "a", universe=shuffled)
    assert [r["source"]["name"] for r in first] == ["Borax", "Thorium-232", "Zero"]
    again = run(BafuBrightwayFlowsPackageSource, tmp_path / "b", universe=shuffled)
    as_bytes = [orjson.dumps(rows, option=orjson.OPT_INDENT_2) for rows in (first, again)]
    assert as_bytes[0] == as_bytes[1]


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


def test_a_retarget_between_two_factorless_flows_is_applied_not_reviewed(tmp_path):
    curated = [entry(BORAX, "ef-long", "Longterm")]
    rows = run(BafuBrightwayFlowsReviewSource, tmp_path, curated=curated, verb="review")
    assert "Borax" not in {r["source"]["name"] for r in rows}
    aligned = run(
        BafuBrightwayFlowsAlignSource, tmp_path, curated=curated, emit="biosphere-1-curated"
    )
    assert aligned[0]["target"]["code"] == "ef-borax"
    assert "neither flow carries a factor" in aligned[0]["comment"]


def review(tmp_path, **kw):
    rows = run(BafuBrightwayFlowsReviewSource, tmp_path, verb="review", **kw)
    return {r["source"]["name"]: r for r in rows}


def test_review_lists_a_retarget_held_back_by_the_conversion(tmp_path):
    curated = [entry(SCALED, "ef-ammonia", "ammonia")]
    scaled = review(tmp_path, curated=curated, universe=[*UNIVERSE, SCALED])["Scaled"]
    assert scaled["kind"] == "retarget_withheld" and scaled["reason"] == "unit_differs"
    assert scaled["methods"] == [] and scaled["ours"]["code"] == "ef-ammonia"


def test_review_lists_an_entry_of_ours_without_target_code(tmp_path):
    curated = [{"source": AMMONIA, "target": {"name": "ammonia", "unit": "kilogram"}}]
    ammonia = review(tmp_path, curated=curated)["Ammonia"]
    assert ammonia["kind"] == "retarget_withheld" and ammonia["reason"] == "ours_without_code"
    assert ammonia["brightway_flows"]["code"] == "ef-ammonia-twin"


def test_review_says_which_side_scores_nothing(tmp_path):
    curated = [entry(BORAX, "ef-toxic", "toxic"), entry(TOXIC, "ef-borax", "Borax")]
    by_name = review(tmp_path, curated=curated)
    assert by_name["Borax"]["kind"] == "score_changing_retarget"
    assert by_name["Borax"]["factors"] == "theirs_zero"
    assert by_name["Toxic"]["factors"] == "ours_zero"
    assert by_name["Toxic"]["methods"] == ["ef-3.1:human-toxicity-cancer"]


def test_review_lists_a_merged_ef_flow_whose_factors_differ(tmp_path):
    doc = bw_document(merged=[("ef-benzene", "ef-benzene-other")])
    benzene = review(tmp_path, doc=doc)["Benzene"]
    assert benzene["kind"] == "score_changing_retarget" and benzene["factors"] == "differ"


def test_review_skips_an_unusable_link_that_names_our_own_target(tmp_path):
    by_name = review(tmp_path, curated=[entry(BROAD, "ef-borax", "Borax")])
    assert "Broad" not in by_name  # a broadMatch onto the flow we already use: they agree


def test_review_names_the_resolved_target_of_a_redirected_link(tmp_path):
    redir = review(tmp_path, universe=[*UNIVERSE, REDIR])["Redir"]
    assert redir["reason"] == "broad_match"
    assert redir["brightway_flows"]["code"] == "ef-borax"
    mapped = review(
        tmp_path / "m", curated=[entry(REDIR, "ef-borax", "Borax")], universe=[*UNIVERSE, REDIR]
    )
    assert "Redir" not in mapped  # the resolved target is ours


def test_factorless_flows_in_another_unit_are_aligned_not_reviewed(tmp_path):
    kw = {"curated": [vapour_entry(), CURATED[1]], "universe": [*UNIVERSE, VAPOUR]}
    aligned = run(BafuBrightwayFlowsAlignSource, tmp_path / "a", emit="biosphere-1-curated", **kw)
    assert len(aligned) == 2 and aligned[1] == CURATED[1]
    vapour = aligned[0]
    assert vapour["target"]["code"] == "ef-vapour-kg" and vapour["target"]["unit"] == "kilogram"
    assert "conversion_factor" not in vapour
    assert "aligned with brightway-flows" in vapour["comment"]
    assert "rescaled" not in vapour["comment"]
    by_name = review(tmp_path / "r", **kw)
    assert "Vapour" not in by_name
    assert not [r for r in by_name.values() if r.get("reason") == "unit_differs"]
