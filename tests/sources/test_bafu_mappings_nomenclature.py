import json
from dataclasses import replace

import pytest
from sentier_importers.core.context import RunContext
from sentier_importers.matching.ef_index import EfFlowIndex
from sentier_importers.matching.pipeline import Match, default_pipeline
from sentier_importers.sources.bafu.mappings_biosphere_matched import ParsedInputs
from sentier_importers.sources.bafu.mappings_biosphere_nomenclature import BafuEfNomenclatureSource
from sentier_importers.sources.eaternity.bridge import BafuFlow, BafuFlowIndex

from tests.matching.ef_fixtures import AIR_UNSPEC, cf_row, vocab_row
from tests.sources.test_bafu_mappings_matched import (
    CO2,
    GAS,
    LAND,
    MINE_GAS,
    RADON,
    VOCAB,
    WATER,
    _config,
    _run,
    _stage,
)

#: What ``BafuEfMatchedSource`` (rank 7) actually maps over the plain fixture (no
#: rank3/rank6 exclusions, default CF/VOCAB) -- see
#: ``test_bafu_mappings_matched.
#: test_emits_entries_with_flow_ids_ef_units_and_no_comment_for_clean_matches``.
#: The nomenclature source (rank 8) must receive this same set as its own ``rank7``
#: input in every test below, exactly as the real registry pipeline would feed it the
#: just-written rank-7 payload -- otherwise these always-matching, characterised BAFU
#: flows would reach rank 8's inclusive pipeline unexcluded and trip its
#: never-a-characterised-match guard.
_BASE_RANK7 = (CO2, WATER, RADON, LAND)


def _config_nomenclature(root, rank7=_BASE_RANK7):
    """Like ``_config``, but for the nomenclature source, with a ``rank7`` payload input."""
    cfg = _config(
        root,
        name="bafu-ef-biosphere-nomenclature",
        module="sentier_importers.sources.bafu.mappings_biosphere_nomenclature",
        verb="replace",
        emit="biosphere",
    )
    payload = {
        "name": "x",
        "version": "0",
        "replace": [{"source": {"code": c}, "target": {"code": "t"}} for c in rank7],
    }
    (root / "rank7.json").write_text(json.dumps(payload))
    return replace(cfg, inputs={**cfg.inputs, "rank7": f"file://{root}/rank7.json"})


def test_emits_the_uncharacterised_match_with_the_fixed_comment_and_uncertain_suffix(tmp_path):
    # "Mine gas" carries no CF-table factor at all but a synonym that exactly names the
    # otherwise-unmapped mine off-gas BAFU flow (Nm3), placed via the reso-grou bw-context
    # crosswalk -- one of bw_context.AMBIGUOUS_CODES, so context_uncertain is True here.
    vocab = VOCAB + [
        vocab_row(
            "mine-gas-unchar",
            "Mine gas",
            alt=["Gas, mine, off-gas, process, coal mining/m3"],
            bw="reso-grou",
        )
    ]
    root = _stage(tmp_path, vocab=vocab)
    source = BafuEfNomenclatureSource(_config_nomenclature(root))
    rows = _run(source, tmp_path)
    (row,) = [r for r in rows if r["source"]["code"] == MINE_GAS]
    assert row["target"]["code"] == "mine-gas-unchar"
    assert row["target"]["unit"] == "cubic meter"  # Nm3 -> cubic meter, no factor
    assert "conversion_factor" not in row
    assert row["comment"].startswith("uncharacterised in EF 3.1: no factor in any method")
    assert (
        "EF context inferred from the Brightway context code, branch/sub-compartment uncertain"
        in row["comment"]
    )


def test_uncertain_suffix_is_absent_when_the_bw_context_code_is_unambiguous(tmp_path):
    # "envi-air-unkn" is NOT in bw_context.AMBIGUOUS_CODES: context_uncertain is False
    # for it, so the comment must carry no uncertain suffix. A synthetic flow (same
    # injection pattern as the Krypton-85m case below), since none of the shared fixture
    # flows lands cleanly on an unambiguous bw-context code without other side effects.
    vocab = VOCAB + [vocab_row("widget-unchar", "Widget", bw="envi-air-unkn")]
    root = _stage(tmp_path, vocab=vocab)
    source = BafuEfNomenclatureSource(_config_nomenclature(root))
    ctx = RunContext(cache_dir=tmp_path / "cache", output_dir=tmp_path / "out")
    records = source.parse(source.fetch(ctx))
    inputs = records[0]["inputs"]
    widget = BafuFlow("Widget", "emissions to air", "unspecified", "kg")
    augmented = replace(inputs, bafu=BafuFlowIndex.from_flows(list(inputs.bafu) + [widget]))
    rows = source.transform([{"inputs": augmented}])
    (row,) = [r for r in rows if r["source"]["name"] == "Widget"]
    assert row["comment"] == (
        "uncharacterised in EF 3.1: no factor in any method; target unit is the "
        "source unit's EF spelling (EF states no reference unit for this flow)"
    )


def test_becquerel_flow_keeps_its_own_unit_with_no_rescale(tmp_path):
    # An uncharacterised target has no reference unit at all, so rank 8 must never
    # rescale an amount: a BAFU Bq-denominated flow stays "Bq" (NOT respelled to kBq,
    # unlike a characterised ionising-radiation match, which does rescale -- see the
    # sibling matched-source test_becquerel_sources_land_on_kilobecquerel_with_a_
    # conversion), and carries no conversion_factor at all.
    vocab = VOCAB + [vocab_row("kr85m-unchar", "Krypton-85m", bw="envi-air-hist15me")]
    root = _stage(tmp_path, vocab=vocab)
    source = BafuEfNomenclatureSource(_config_nomenclature(root))
    ctx = RunContext(cache_dir=tmp_path / "cache", output_dir=tmp_path / "out")
    records = source.parse(source.fetch(ctx))
    inputs = records[0]["inputs"]
    krypton = BafuFlow("Krypton-85m", "emissions to air", "low. pop.", "Bq")
    augmented = replace(inputs, bafu=BafuFlowIndex.from_flows(list(inputs.bafu) + [krypton]))
    rows = source.transform([{"inputs": augmented}])
    (row,) = [r for r in rows if r["source"]["name"] == "Krypton-85m"]
    assert row["target"]["code"] == "kr85m-unchar"
    assert row["target"]["unit"] == "Bq"
    assert "conversion_factor" not in row
    assert row["comment"].startswith("uncharacterised in EF 3.1: no factor in any method")


def test_rank8_resource_branch_fallback_caveat_is_honest_about_uncharacterised(tmp_path):
    # "Iridium" is filed by BAFU under the uninformative "unspecified" sub-compartment;
    # it resolves through the resource-branch fallback onto the one (uncharacterised)
    # EF leaf reso-grou reaches -- the pipeline's own caveat wording ("EF has ... only
    # as ...") would assert a fact about EF's characterised branches that does not hold
    # for this target, so entry_for must replace it. (Not an energy-carrier name, so
    # rule 3 of _decide never withholds it -- that is covered separately.)
    vocab = VOCAB + [vocab_row("iridium-unchar", "Iridium", bw="reso-grou")]
    root = _stage(tmp_path, vocab=vocab)
    source = BafuEfNomenclatureSource(_config_nomenclature(root))
    ctx = RunContext(cache_dir=tmp_path / "cache", output_dir=tmp_path / "out")
    records = source.parse(source.fetch(ctx))
    inputs = records[0]["inputs"]
    flow = BafuFlow("Iridium", "resources", "unspecified", "kg")
    augmented = replace(inputs, bafu=BafuFlowIndex.from_flows(list(inputs.bafu) + [flow]))
    rows = source.transform([{"inputs": augmented}])
    (row,) = [r for r in rows if r["source"]["name"] == "Iridium"]
    assert row["target"]["code"] == "iridium-unchar"
    assert "EF has" not in row["comment"] and "only as" not in row["comment"]
    assert (
        "BAFU files this resource under unspecified; placed on the inferred EF "
        "resource branch non-renewable element resources from ground (uncharacterised, "
        "context from the Brightway code)" in row["comment"]
    )


def test_energy_carrier_resource_names_are_withheld_entirely_not_emitted(tmp_path):
    # decision 2026-09-13 (energy-carrier withholding): the bw-context crosswalk has no
    # code that reaches an EF energy-resource branch at all, so a Match onto one of
    # these uncharacterised targets is not just uncertain, it is unresolvable -- _decide
    # withholds it outright (context_unresolved) rather than emit it with any caveat.
    vocab = VOCAB + [
        vocab_row("energy-geo-unchar", "Energy, geothermal, converted", bw="reso-grou")
    ]
    root = _stage(tmp_path, vocab=vocab)
    source = BafuEfNomenclatureSource(_config_nomenclature(root))
    ctx = RunContext(cache_dir=tmp_path / "cache", output_dir=tmp_path / "out")
    records = source.parse(source.fetch(ctx))
    inputs = records[0]["inputs"]
    flow = BafuFlow("Energy, geothermal, converted", "resources", "land", "MJ")
    augmented = replace(inputs, bafu=BafuFlowIndex.from_flows(list(inputs.bafu) + [flow]))
    rows = source.transform([{"inputs": augmented}])
    assert not any(r["source"]["name"] == "Energy, geothermal, converted" for r in rows)


def test_rank8_location_and_regional_aggregate_caveat_are_reported(tmp_path):
    # Same "Water, KR" / "Water, Europe" region-strip scenario the matched source's own
    # test_location_and_caveats_are_carried and the coverage sidecar's
    # test_rank7_match_location_and_caveats_are_reported_when_present pin for rank 7,
    # but onto an uncharacterised "Water" vocab row: a real match.caveats entry (the
    # regional-aggregate one, untouched -- only a resource_branch_fallback caveat is
    # ever rewritten) must still land in the comment, after the fixed prefix, and
    # target["location"] must still be set for the flow the pipeline resolves an ISO
    # location for.
    # envi-wate-suwa -> "fresh water" is NOT ambiguous (unlike envi-wate-unkn), so the
    # comment carries no uncertain suffix either -- only the fixed prefix and the
    # region caveat/location, keeping this test's assertions unambiguous about which
    # part of entry_for's output each one is pinning.
    vocab = VOCAB + [vocab_row("water-em-unchar", "Water", cas="7732-18-5", bw="envi-wate-suwa")]
    root = _stage(tmp_path, vocab=vocab)
    source = BafuEfNomenclatureSource(_config_nomenclature(root))
    ctx = RunContext(cache_dir=tmp_path / "cache", output_dir=tmp_path / "out")
    records = source.parse(source.fetch(ctx))
    inputs = records[0]["inputs"]
    extra = [
        BafuFlow("Water, KR", "emissions to water", "river", "m3"),
        BafuFlow("Water, Europe", "emissions to water", "river", "m3"),
    ]
    augmented = replace(inputs, bafu=BafuFlowIndex.from_flows(list(inputs.bafu) + extra))
    rows = source.transform([{"inputs": augmented}])
    by_name = {r["source"]["name"]: r for r in rows}

    kr = by_name["Water, KR"]
    assert kr["target"]["location"] == "KR"
    assert kr["comment"] == (
        "uncharacterised in EF 3.1: no factor in any method; target unit is the "
        "source unit's EF spelling (EF states no reference unit for this flow)"
    )

    europe = by_name["Water, Europe"]
    assert "location" not in europe["target"]
    assert (
        "regional aggregate Europe in the source name; EF applies the global default "
        "factor" in europe["comment"]
    )
    assert europe["comment"].startswith("uncharacterised in EF 3.1: no factor in any method")


def test_flows_already_in_rank_3_6_or_7_are_excluded(tmp_path):
    vocab = VOCAB + [
        vocab_row(
            "mine-gas-unchar",
            "Mine gas",
            alt=["Gas, mine, off-gas, process, coal mining/m3"],
            bw="reso-grou",
        )
    ]
    root = _stage(tmp_path, vocab=vocab)
    source = BafuEfNomenclatureSource(_config_nomenclature(root, rank7=(*_BASE_RANK7, MINE_GAS)))
    rows = _run(source, tmp_path)
    assert MINE_GAS not in {r["source"]["code"] for r in rows}


def test_no_uncharacterised_match_means_no_rows_for_untouched_fixture_flows(tmp_path):
    # sanity: with no uncharacterised vocab row added at all, nothing in the default
    # fixture is emitted -- GAS (natural gas, m3) stays unmapped by both rank 7 and rank 8.
    root = _stage(tmp_path)
    source = BafuEfNomenclatureSource(_config_nomenclature(root))
    rows = _run(source, tmp_path)
    assert GAS not in {r["source"]["code"] for r in rows}


def test_characterised_match_reaching_transform_raises_runtime_error(tmp_path):
    # Hand-built inputs whose (inclusive) index carries a normally-characterised flow
    # reachable by exact name -- transform()'s own guard must never let a characterised
    # match slip through as a rank-8 nomenclature entry.
    cf = [cf_row("target", "widget", AIR_UNSPEC, method="ef-3.1:human-toxicity-cancer", value=1.0)]
    vocab = [vocab_row("target", "Widget")]
    index = EfFlowIndex.from_tables(cf, vocab, include_uncharacterised=True)
    pipeline = default_pipeline(index, {})
    flow = BafuFlow("Widget", "emissions to air", "unspecified", "kg")
    inputs = ParsedInputs(
        bafu=BafuFlowIndex.from_flows([flow]),
        cas={},
        cas_conflicts={},
        rank3_codes=frozenset(),
        rank6_codes=frozenset(),
        index=index,
        pipeline=pipeline,
    )
    source = BafuEfNomenclatureSource(_config(tmp_path))
    with pytest.raises(RuntimeError, match="characterised match reached the nomenclature source"):
        source.transform([{"inputs": inputs}])


def test_no_intermediate_database_identifier_in_output(tmp_path):
    # an ore-composite name can resolve onto an uncharacterised target too (the ore
    # caveat is carried through entry_for's uncharacterised branch like any other
    # match.caveats entry) -- exercise that path and check the reworded caveat text
    # never names the intermediate database.
    vocab = VOCAB + [vocab_row("zinc-unchar", "Zinc", bw="reso-grou")]
    root = _stage(tmp_path, vocab=vocab)
    source = BafuEfNomenclatureSource(_config_nomenclature(root))
    ctx = RunContext(cache_dir=tmp_path / "cache", output_dir=tmp_path / "out")
    records = source.parse(source.fetch(ctx))
    inputs = records[0]["inputs"]
    ore_flow = BafuFlow(
        "Zinc, Zn 0.63%, Au 9.7E-4%, Ag 9.7E-4%, Cu 0.38%, Pb 0.014%, in ore",
        "resources",
        "in ground",
        "kg",
    )
    augmented = replace(inputs, bafu=BafuFlowIndex.from_flows(list(inputs.bafu) + [ore_flow]))
    rows = source.transform([{"inputs": augmented}])

    # sanity: the ore-composite match actually landed in this entry's comment
    (zinc,) = [r for r in rows if r["source"]["name"].startswith("Zinc,")]
    assert "ore composite" in zinc["comment"]

    blob = json.dumps(rows).lower()
    assert "ecoinvent" not in blob and "biosphere3" not in blob


def test_resource_correction_flow_carries_its_own_caveat_onto_an_uncharacterised_target(
    tmp_path,
):
    # entry_for is exercised directly (a hand-built Match, like the sibling matched-
    # source test): no matcher tier strips a ", resource correction" suffix by name, so
    # this flow could not resolve through the real pipeline on its own -- the caveat
    # itself, though, must still be added regardless of which tier/placement matched it.
    vocab = VOCAB + [vocab_row("iron-unchar", "Iron", bw="reso-grou")]
    root = _stage(tmp_path, vocab=vocab)
    source = BafuEfNomenclatureSource(_config_nomenclature(root))
    ctx = RunContext(cache_dir=tmp_path / "cache", output_dir=tmp_path / "out")
    records = source.parse(source.fetch(ctx))
    index = records[0]["inputs"].index
    flow = BafuFlow("Iron, resource correction", "resources", "unspecified", "kg")
    match = Match(
        code="iron-unchar",
        tier="name",
        placement="resource_branch_fallback",
        location=None,
        candidates=1,
        caveats=("BAFU files this resource under unspecified; EF has iron only as x",),
    )
    entry = source.entry_for(flow, match, index)
    assert (
        "source is a resource-correction flow, mapped to the extraction of the same "
        "element" in entry["comment"]
    )
