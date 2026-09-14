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
    CF,
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

#: What ``BafuEfMatchedSource`` (biosphere-3-matched) actually maps over the plain
#: fixture (no curated/inferred exclusions, default CF/VOCAB) -- see
#: ``test_bafu_mappings_matched.
#: test_emits_entries_with_flow_ids_ef_units_and_no_comment_for_clean_matches``.
#: The nomenclature source (biosphere-4-nomenclature) must receive this same set as
#: its own ``matched`` input in every test below, exactly as the real registry
#: pipeline would feed it the just-written biosphere-3-matched payload -- otherwise
#: these always-matching, characterised BAFU flows would reach the nomenclature
#: package's inclusive pipeline unexcluded and trip its never-a-characterised-match
#: guard.
_BASE_MATCHED = (CO2, WATER, RADON, LAND)


def _config_nomenclature(root, matched=_BASE_MATCHED):
    """Like ``_config``, but for the nomenclature source, with a ``matched`` payload input."""
    cfg = _config(
        root,
        name="bafu-ef-biosphere-nomenclature",
        module="sentier_importers.sources.bafu.mappings_biosphere_nomenclature",
        verb="replace",
        emit="biosphere-4-nomenclature",
    )
    payload = {
        "name": "x",
        "version": "0",
        "replace": [{"source": {"code": c}, "target": {"code": "t"}} for c in matched],
    }
    (root / "matched.json").write_text(json.dumps(payload))
    return replace(cfg, inputs={**cfg.inputs, "matched": f"file://{root}/matched.json"})


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
        "EF convention for this dimension (EF states no reference unit for this flow)"
    )


def test_becquerel_flow_is_rescaled_onto_kbq_like_its_characterised_twin(tmp_path):
    # Round 5, decision 2026-09-14: before this decision, an uncharacterised target
    # kept the SOURCE unit's own spelling and never rescaled -- so a BAFU
    # Bq-denominated flow stayed "Bq" while a kBq-denominated twin of the very same EF
    # flow stayed "kBq", with no conversion_factor on either, silently off by 1000x
    # once a downstream consumer merged the two amounts onto one EF node. Now every
    # activity-dimension source is canonicalised onto EF's kBq convention, the same
    # target unit a characterised ionising-radiation match uses (see the sibling
    # matched-source test_becquerel_sources_land_on_kilobecquerel_with_a_conversion),
    # with the same 0.001 factor made explicit.
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
    assert row["target"]["unit"] == "kBq"
    assert row["conversion_factor"] == 0.001
    assert row["comment"].startswith("uncharacterised in EF 3.1: no factor in any method")
    assert "amount rescaled by 0.001" in row["comment"]


def test_kbq_flow_keeps_its_own_unit_with_no_rescale(tmp_path):
    # the other half of the same twin: a BAFU flow already reported in kBq needs no
    # factor at all -- it is already at EF's activity convention.
    vocab = VOCAB + [vocab_row("kr85m-unchar-2", "Krypton-85m", bw="envi-air-hist15me")]
    root = _stage(tmp_path, vocab=vocab)
    source = BafuEfNomenclatureSource(_config_nomenclature(root))
    ctx = RunContext(cache_dir=tmp_path / "cache", output_dir=tmp_path / "out")
    records = source.parse(source.fetch(ctx))
    inputs = records[0]["inputs"]
    krypton = BafuFlow("Krypton-85m", "emissions to air", "low. pop.", "kBq")
    augmented = replace(inputs, bafu=BafuFlowIndex.from_flows(list(inputs.bafu) + [krypton]))
    rows = source.transform([{"inputs": augmented}])
    (row,) = [r for r in rows if r["source"]["name"] == "Krypton-85m"]
    assert row["target"]["unit"] == "kBq"
    assert "conversion_factor" not in row
    assert "amount rescaled" not in row["comment"]


def test_nomenclature_resource_branch_fallback_caveat_is_honest_about_uncharacterised(tmp_path):
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


def test_energy_carrier_resource_names_are_emitted_without_a_target_context(tmp_path):
    # decision (f)(2), 2026-09-13: the bw-context crosswalk has no code that reaches
    # an EF energy-resource branch at all, so the branch a Match lands on is known
    # wrong, not just unverified -- but the Match itself is now emitted (not withheld):
    # entry_for omits target["context"] entirely and says so in the comment, instead
    # of asserting (even uncertainly) a branch that cannot be right.
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
    (row,) = [r for r in rows if r["source"]["name"] == "Energy, geothermal, converted"]
    assert row["target"]["code"] == "energy-geo-unchar"
    assert "context" not in row["target"]
    assert "EF context not recoverable from the source context code" in row["comment"]
    assert "branch/sub-compartment uncertain" not in row["comment"]
    # "land" is itself an uninformative resource sub-compartment (decision (b)): this
    # flow also resolves through the resource_branch_fallback placement, whose own
    # "placed on the inferred EF resource branch ..." wording must NOT appear here --
    # naming a specific branch would contradict "not recoverable" just above.
    assert "placed on the inferred EF resource branch" not in row["comment"]


def test_energy_shaped_emission_flow_keeps_its_context(tmp_path):
    # "uncertain_energy" is gated on ef_flow.bucket == "resource" (matching
    # ef_index.UNCERTAIN_RESOURCE_NAME's own scope: the bw-context crosswalk cannot
    # reach an EF energy-*resource* branch at all, but says nothing about emissions).
    # An uncharacterised EMISSION-bucket flow that merely happens to share an
    # energy-shaped name must keep its target context and the ordinary
    # branch/sub-compartment-uncertain wording, not the energy-carrier omission.
    vocab = VOCAB + [vocab_row("energy-emission-unchar", "Energy, waste heat", bw="envi-air-unkn")]
    root = _stage(tmp_path, vocab=vocab)
    source = BafuEfNomenclatureSource(_config_nomenclature(root))
    ctx = RunContext(cache_dir=tmp_path / "cache", output_dir=tmp_path / "out")
    records = source.parse(source.fetch(ctx))
    inputs = records[0]["inputs"]
    flow = BafuFlow("Energy, waste heat", "emissions to air", "unspecified", "MJ")
    augmented = replace(inputs, bafu=BafuFlowIndex.from_flows(list(inputs.bafu) + [flow]))
    rows = source.transform([{"inputs": augmented}])
    (row,) = [r for r in rows if r["source"]["name"] == "Energy, waste heat"]
    assert row["target"]["code"] == "energy-emission-unchar"
    assert row["target"]["context"] == [
        "Emissions",
        "Emissions to air",
        "Emissions to air, unspecified",
    ]
    assert "EF context not recoverable from the source context code" not in row["comment"]


_ENERGY_TWO_LEAF_VOCAB = [
    # neither leaf is "resources from water", so "in water" below is not uninformative
    # (decision (b)) and the resource-branch fallback never intercepts this flow.
    vocab_row("energy-tidal-ground", "Energy, tidal, converted", bw="reso-grou"),
    vocab_row("energy-tidal-air", "Energy, tidal, converted", bw="reso-air"),
]


def test_relaxed_placement_drops_the_leaf_naming_caveat_for_an_energy_carrier(tmp_path):
    # two uncharacterised candidates for an energy-carrier-shaped resource name,
    # spread over two distinct (wrong-anyway) leafs, with a sub-compartment
    # ("in water") that is neither EXACT nor uninformative -- this reaches the
    # relaxed nomenclature-package placement (Placement.NOMENCLATURE), whose own
    # first caveat names the leafs it chose between. For an energy carrier, that
    # leaf-naming caveat must be dropped just like resource_branch_fallback's is --
    # the comment discloses non-recoverability instead, never a specific leaf.
    vocab = VOCAB + _ENERGY_TWO_LEAF_VOCAB
    root = _stage(tmp_path, vocab=vocab)
    source = BafuEfNomenclatureSource(_config_nomenclature(root))
    ctx = RunContext(cache_dir=tmp_path / "cache", output_dir=tmp_path / "out")
    records = source.parse(source.fetch(ctx))
    inputs = records[0]["inputs"]
    flow = BafuFlow("Energy, tidal, converted", "resources", "in water", "MJ")
    augmented = replace(inputs, bafu=BafuFlowIndex.from_flows(list(inputs.bafu) + [flow]))
    rows = source.transform([{"inputs": augmented}])
    (row,) = [r for r in rows if r["source"]["name"] == "Energy, tidal, converted"]
    assert "context" not in row["target"]
    assert "EF context not recoverable from the source context code" in row["comment"]
    assert "EF has this name only in" not in row["comment"]
    assert "placed on" not in row["comment"]


def test_nomenclature_location_and_regional_aggregate_caveat_are_reported(tmp_path):
    # Same "Water, KR" / "Water, Europe" region-strip scenario the matched source's own
    # test_location_and_caveats_are_carried and the coverage sidecar's
    # test_matched_package_location_and_caveats_are_reported_when_present pin for the
    # matched package, but onto an uncharacterised "Water" vocab row: a real
    # match.caveats entry (the regional-aggregate one, untouched -- only a
    # resource_branch_fallback caveat is
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
        "EF convention for this dimension (EF states no reference unit for this flow)"
    )

    europe = by_name["Water, Europe"]
    assert "location" not in europe["target"]
    assert (
        "regional aggregate Europe in the source name; EF applies the global default "
        "factor" in europe["comment"]
    )
    assert europe["comment"].startswith("uncharacterised in EF 3.1: no factor in any method")


def test_flows_already_in_curated_inferred_or_matched_are_excluded(tmp_path):
    vocab = VOCAB + [
        vocab_row(
            "mine-gas-unchar",
            "Mine gas",
            alt=["Gas, mine, off-gas, process, coal mining/m3"],
            bw="reso-grou",
        )
    ]
    root = _stage(tmp_path, vocab=vocab)
    source = BafuEfNomenclatureSource(
        _config_nomenclature(root, matched=(*_BASE_MATCHED, MINE_GAS))
    )
    rows = _run(source, tmp_path)
    assert MINE_GAS not in {r["source"]["code"] for r in rows}


def test_no_uncharacterised_match_means_no_rows_for_untouched_fixture_flows(tmp_path):
    # sanity: with no uncharacterised vocab row added at all, nothing in the default
    # fixture is emitted -- GAS (natural gas, m3) stays unmapped by both the matched and
    # nomenclature packages.
    root = _stage(tmp_path)
    source = BafuEfNomenclatureSource(_config_nomenclature(root))
    rows = _run(source, tmp_path)
    assert GAS not in {r["source"]["code"] for r in rows}


def test_name_only_alignment_emits_with_no_context_and_the_exact_comment(tmp_path):
    # round 5, decision 2026-09-14: "Basalt" is a resource extraction the ordinary
    # pipeline never places at all (EF carries no "Basalt" in the resource bucket),
    # but EF does carry a factorless "Basalt" elsewhere (a soil-emission leaf, via the
    # always-uncharacterised envi-grou-indu bw-context code) -- named alignment only,
    # no context, no factor.
    vocab = VOCAB + [vocab_row("basalt-soil-unchar", "Basalt", bw="envi-grou-indu")]
    root = _stage(tmp_path, vocab=vocab)
    source = BafuEfNomenclatureSource(_config_nomenclature(root))
    ctx = RunContext(cache_dir=tmp_path / "cache", output_dir=tmp_path / "out")
    records = source.parse(source.fetch(ctx))
    inputs = records[0]["inputs"]
    flow = BafuFlow("Basalt", "resources", "in ground", "kg")
    augmented = replace(inputs, bafu=BafuFlowIndex.from_flows(list(inputs.bafu) + [flow]))
    rows = source.transform([{"inputs": augmented}])
    (row,) = [r for r in rows if r["source"]["name"] == "Basalt"]
    assert row["target"] == {
        "code": "basalt-soil-unchar",
        "name": "Basalt",
        "unit": "kilogram",
    }
    assert "conversion_factor" not in row
    assert row["comment"] == (
        "uncharacterised in EF 3.1: no factor in any method; target unit is the EF "
        "convention for this dimension (EF states no reference unit for this flow); "
        "EF has this name only as emissions to non-agricultural soil; the source is "
        "a resource extraction, so the EF context is omitted (name alignment only, "
        "decision 2026-09-14)"
    )


def test_name_only_alignment_prefers_the_namesake_on_its_own_unspecified_leaf(tmp_path):
    # two factorless "Shale" namesakes spread over two buckets -- the one on the air
    # bucket's own unspecified leaf must be chosen, deterministically, and the caveat
    # names both leafs, sorted.
    vocab = VOCAB + [
        vocab_row("shale-soil-unchar", "Shale", bw="envi-grou-indu"),
        vocab_row("shale-air-unchar", "Shale", bw="envi-air-unkn"),
    ]
    root = _stage(tmp_path, vocab=vocab)
    source = BafuEfNomenclatureSource(_config_nomenclature(root))
    ctx = RunContext(cache_dir=tmp_path / "cache", output_dir=tmp_path / "out")
    records = source.parse(source.fetch(ctx))
    inputs = records[0]["inputs"]
    flow = BafuFlow("Shale", "resources", "in ground", "kg")
    augmented = replace(inputs, bafu=BafuFlowIndex.from_flows(list(inputs.bafu) + [flow]))
    rows = source.transform([{"inputs": augmented}])
    (row,) = [r for r in rows if r["source"]["name"] == "Shale"]
    assert row["target"]["code"] == "shale-air-unchar"
    assert "context" not in row["target"]
    assert (
        "EF has this name only as emissions to air, unspecified, emissions to "
        "non-agricultural soil" in row["comment"]
    )


def test_name_only_alignment_is_withheld_when_any_namesake_is_characterised(tmp_path):
    # "Talc" carries a real factor in the air bucket (a different bucket than this
    # source's own "resource" category, so the ordinary pipeline never finds it
    # either) and a second, factorless "Talc" elsewhere -- the flow must stay
    # unmapped by this source rather than align onto the factorless one.
    cf = CF + [cf_row("talc-air", "talc", AIR_UNSPEC, method="ef-3.1:human-toxicity-cancer")]
    vocab = VOCAB + [
        vocab_row("talc-air", "Talc"),
        vocab_row("talc-soil-unchar", "Talc", bw="envi-grou-indu"),
    ]
    root = _stage(tmp_path, cf=cf, vocab=vocab)
    source = BafuEfNomenclatureSource(_config_nomenclature(root))
    ctx = RunContext(cache_dir=tmp_path / "cache", output_dir=tmp_path / "out")
    records = source.parse(source.fetch(ctx))
    inputs = records[0]["inputs"]
    flow = BafuFlow("Talc", "resources", "in ground", "kg")
    augmented = replace(inputs, bafu=BafuFlowIndex.from_flows(list(inputs.bafu) + [flow]))
    rows = source.transform([{"inputs": augmented}])
    assert not [r for r in rows if r["source"]["name"] == "Talc"]


def test_characterised_match_reaching_transform_raises_runtime_error(tmp_path):
    # Hand-built inputs whose (inclusive) index carries a normally-characterised flow
    # reachable by exact name -- transform()'s own guard must never let a characterised
    # match slip through as a nomenclature-package entry.
    cf = [cf_row("target", "widget", AIR_UNSPEC, method="ef-3.1:human-toxicity-cancer", value=1.0)]
    vocab = [vocab_row("target", "Widget")]
    index = EfFlowIndex.from_tables(cf, vocab, include_uncharacterised=True)
    pipeline = default_pipeline(index, {})
    flow = BafuFlow("Widget", "emissions to air", "unspecified", "kg")
    inputs = ParsedInputs(
        bafu=BafuFlowIndex.from_flows([flow]),
        cas={},
        cas_conflicts={},
        curated_codes=frozenset(),
        inferred_codes=frozenset(),
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
