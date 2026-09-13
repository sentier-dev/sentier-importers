import json
from dataclasses import replace

import pytest
from sentier_importers.core.context import RunContext
from sentier_importers.matching.ef_index import EfFlowIndex
from sentier_importers.matching.pipeline import default_pipeline
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
        "EF context inferred from the Brightway context code, sub-compartment uncertain"
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
    assert row["comment"] == "uncharacterised in EF 3.1: no factor in any method"


def test_becquerel_flow_lands_on_kilobecquerel_with_the_fixed_conversion(tmp_path):
    # A synthetic Bq-denominated BAFU flow -- injected directly into the parsed BAFU
    # universe, the same pattern the coverage tests use to add flows the fixture zip
    # doesn't carry -- resolving by exact name onto an uncharacterised EF row.
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
