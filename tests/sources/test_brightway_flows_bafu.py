import json
from dataclasses import replace

import pytest
from sentier_importers.core.context import RunContext
from sentier_importers.core.source import SourceConfig
from sentier_importers.matching.ef_index import LabelDefect
from sentier_importers.sources.bafu.ecospold import flow_id
from sentier_importers.sources.brightway_flows import bafu
from sentier_importers.sources.brightway_flows.identifiers import bafu_candidates

from tests.matching.ef_fixtures import AIR_RURAL, AIR_UNSPEC, cf_row, vocab_row, write_ef_inputs
from tests.sources.brightway_flows_fixture import document, flow, gz, link

AIR = ["emissions to air", "unspecified"]
AIR_LT = ["emissions to air", "low. pop., long-term"]
TOX = "ef-3.1:human-toxicity-cancer"
RAD = "ef-3.1:ionising-radiation-human-health"


def src(name, context=AIR, unit="kg"):
    return {
        "name": name,
        "code": flow_id(name, context[0], context[1], unit),
        "unit": unit,
        "context": context,
    }


def their_id(source):
    return bafu_candidates(
        source["name"], source["context"][0], source["context"][1], source["unit"]
    )[0]


AMMONIA, BENZENE, BORAX = src("Ammonia"), src("Benzene"), src("Borax")
HEXANE, BROAD, LONG = src("Hexane"), src("Broad"), src("Longterm", AIR_LT)
THORIUM, TOXIC, ODD = src("Thorium-232", unit="Bq"), src("Toxic"), src("Odd", unit="Bq")
#: Linked by Brightway Flows but outside the coverage universe: decision-rule cases only.
GHOST, UNPLACED, SCALED = src("Ghost"), src("Unplaced"), src("Scaled")
BORAX_CAS = "1303-96-4"

#: code, name, context, method, value; a row with value None is a factorless vocab term
CF = [
    cf_row("ef-ammonia", "ammonia", AIR_UNSPEC, TOX, 1.0),
    cf_row("ef-ammonia-twin", "ammonia", AIR_RURAL, TOX, 1.0),
    cf_row("ef-benzene", "benzene", AIR_UNSPEC, TOX, 2.0),
    cf_row("ef-benzene-other", "benzene", AIR_RURAL, TOX, 9.0),
    cf_row("ef-toxic", "toxic", AIR_UNSPEC, TOX, 4.0),
    cf_row("ef-thorium-k", "thorium-232", AIR_UNSPEC, RAD, 1.0),
]
VOCAB = [
    vocab_row("ef-ammonia", "Ammonia"),
    vocab_row("ef-ammonia-twin", "Ammonia"),
    vocab_row("ef-benzene", "Benzene"),
    vocab_row("ef-benzene-other", "Benzene"),
    vocab_row("ef-toxic", "Toxic"),
    vocab_row("ef-thorium-k", "Thorium-232"),
    vocab_row("ef-borax", "Borax", cas=BORAX_CAS, bw="envi-air-unkn"),
    vocab_row("ef-thorium", "Thorium-232", bw="envi-air-unkn"),
    vocab_row("ef-long", "Longterm", bw="envi-air-unkn"),
    # factorless and without a bw-context notation: listed in the vocab, never indexed
    vocab_row("ef-unplaced", "Unplaced"),
]
#: Unit symbol of the consensus flows that are not in kg (a factorless target takes
#: its unit from the Brightway Flows flow, the index has none for it).
BW_UNIT = {"ef-thorium": "kBq", "ef-thorium-k": "kBq"}


def bw_document():
    def to(source, target, **kw):
        return link("bafu/2026-v1", their_id(source), target, **kw)

    return document(
        flows=[
            flow(code, r["pref_label"], unit=BW_UNIT.get(code, "kg"))
            for r in VOCAB
            for code in [r["iri"].rsplit("/", 1)[-1]]
        ]
        + [
            flow("add-hexane", "Hexane", source="bafu algorithm addition"),
            flow("ef-not-in-vocab", "Ghost"),
        ],
        links_by_list={
            "ef-3.1": [],
            "bafu-2026-v1": [
                to(AMMONIA, "ef-ammonia-twin"),
                to(BENZENE, "ef-benzene-other"),
                to(BORAX, "ef-borax"),
                to(HEXANE, "add-hexane", match="exactMatch"),
                to(BROAD, "ef-borax", match="broadMatch"),
                to(LONG, "ef-long"),
                to(THORIUM, "ef-thorium", unit="BQ", multiplier=0.001, basis="unit-table"),
                to(TOXIC, "ef-toxic"),
                to(ODD, "ef-borax", unit="BQ"),
                to(GHOST, "ef-not-in-vocab"),
                to(UNPLACED, "ef-unplaced"),
                to(SCALED, "ef-ammonia-twin", multiplier=2.0, basis="unit-table"),
            ],
        },
    )


def entry(source, code, name, unit="kilogram"):
    return {
        "source": source,
        "target": {
            "name": name,
            "code": code,
            "unit": unit,
            "context": ["Emissions", "Emissions to air", "x"],
        },
    }


def stage(tmp_path, curated=(), doc=None):
    """Write the Brightway Flows file, the EF inputs, coverage.json and four packages."""
    (tmp_path / "bw.json.gz").write_bytes(gz(doc or bw_document()))
    write_ef_inputs(tmp_path, CF, VOCAB)
    universe = [AMMONIA, BENZENE, BORAX, HEXANE, BROAD, LONG, THORIUM, TOXIC, ODD]
    coverage = {"name": "c", "version": "0", "coverage": [{"source": s} for s in universe]}
    (tmp_path / "coverage.json").write_text(json.dumps(coverage))
    packages = {"curated": list(curated), "inferred": [], "matched": [], "nomenclature": []}
    for name, entries in packages.items():
        (tmp_path / f"{name}.json").write_text(
            json.dumps({"name": name, "version": "0.1.0", "replace": entries})
        )
    return tmp_path


def config(root, module_class="x", emit="biosphere-5-brightway-flows", verb="replace"):
    return SourceConfig(
        name="t",
        module="sentier_importers.sources.brightway_flows.bafu_sources",
        target="sentier_mappings",
        category="bafu-2026-v1__ef-3.1",
        fetch_url=f"file://{root}/bw.json.gz",
        fetch_format="json",
        output_format="json",
        emit_filename=emit,
        package_name="p",
        package_version="0.1.0",
        package_verb=verb,
        inputs={
            "coverage": f"file://{root}/coverage.json",
            "curated": f"file://{root}/curated.json",
            "inferred": f"file://{root}/inferred.json",
            "matched": f"file://{root}/matched.json",
            "nomenclature": f"file://{root}/nomenclature.json",
            "ef_cfs": f"file://{root}/characterization-factors.parquet",
            "ef_vocab": f"file://{root}/terms",
        },
    )


class _Probe(bafu.BafuBrightwayFlowsBase):
    expected_sha256 = None

    def transform(self, records):
        return records


def state_for(tmp_path, curated=()):
    probe = _Probe(config(stage(tmp_path, curated)))
    ctx = RunContext(cache_dir=tmp_path / "cache", output_dir=tmp_path / "out")
    return probe.parse(probe.fetch(ctx))[0]["state"]


def test_state_knows_which_flows_the_four_packages_map(tmp_path):
    state = state_for(tmp_path, curated=[entry(AMMONIA, "ef-ammonia", "ammonia")])
    assert set(state.mapped) == {AMMONIA["code"]}
    assert state.mapped[AMMONIA["code"]][0] == "biosphere-1-curated"
    assert len(state.universe) == 9


@pytest.mark.parametrize(
    "source,reason",
    [
        (HEXANE, "no_ef_target"),
        (BROAD, "broad_match"),
        (LONG, "long_term_onto_immediate"),
        (src("Unknown"), "no_link"),
        (GHOST, "target_not_in_vocab"),
        (UNPLACED, "target_not_indexed"),
        (BORAX, None),
    ],
)
def test_decide_gives_the_reason_a_link_is_not_used(tmp_path, source, reason):
    assert bafu.decide(source, state_for(tmp_path)).reason == reason


def test_a_known_label_defect_for_the_source_name_is_not_used(tmp_path):
    state = state_for(tmp_path)
    defect = LabelDefect(wrong_label="borax", true_name="Something else", note="test")
    defective = replace(state, defects={BORAX_CAS: defect})
    assert bafu.decide(BORAX, defective).reason == "label_defect"
    assert bafu.package_entry(BORAX, defective) == (None, "label_defect")
    other_name = replace(state, defects={BORAX_CAS: replace(defect, wrong_label="sodium")})
    assert bafu.decide(BORAX, other_name).reason is None


def test_a_factorless_link_becomes_an_entry(tmp_path):
    made, reason = bafu.package_entry(BORAX, state_for(tmp_path))
    assert reason is None
    assert made["source"] == BORAX
    assert made["target"]["code"] == "ef-borax" and made["target"]["unit"] == "kilogram"
    assert made["comment"].startswith("brightway-flows 1.1.0, skos:closeMatch")
    assert "no factor in any method" in made["comment"]
    assert "conversion_factor" not in made


def test_their_multiplier_becomes_the_conversion_factor(tmp_path):
    made, reason = bafu.package_entry(THORIUM, state_for(tmp_path))
    assert reason is None
    assert made["conversion_factor"] == 0.001 and made["target"]["unit"] == "kBq"
    assert "amount rescaled by 0.001 (unit-table)" in made["comment"]


def test_a_unit_change_without_multiplier_is_withheld(tmp_path):
    assert bafu.package_entry(ODD, state_for(tmp_path)) == (None, "unit_mismatch")


def test_a_characterised_link_is_held_for_review(tmp_path):
    assert bafu.package_entry(TOXIC, state_for(tmp_path)) == (
        None,
        "characterised_held_for_review",
    )


def test_an_accepted_characterised_link_becomes_an_entry(tmp_path, monkeypatch):
    monkeypatch.setattr(bafu, "ACCEPTED_CHARACTERISED", frozenset({TOXIC["code"]}))
    made, reason = bafu.package_entry(TOXIC, state_for(tmp_path))
    assert reason is None and made["target"]["code"] == "ef-toxic"
    assert "no factor in any method" not in made["comment"]


def test_identical_factors_take_their_target(tmp_path):
    ours = entry(AMMONIA, "ef-ammonia", "ammonia")
    new, kind = bafu.retarget(ours, state_for(tmp_path, curated=[ours]))
    assert kind == "retargeted"
    assert new["target"]["code"] == "ef-ammonia-twin" and new["target"]["unit"] == "kilogram"
    assert new["source"] == AMMONIA
    assert new["comment"] == "target aligned with brightway-flows 1.1.0 (identical factors)"


def test_an_existing_comment_is_kept(tmp_path):
    ours = {**entry(AMMONIA, "ef-ammonia", "ammonia"), "comment": "old note"}
    new, _ = bafu.retarget(ours, state_for(tmp_path, curated=[ours]))
    assert new["comment"].startswith("old note; target aligned with brightway-flows 1.1.0")


def test_different_factors_leave_the_entry_alone(tmp_path):
    ours = entry(BENZENE, "ef-benzene", "benzene")
    new, kind = bafu.retarget(ours, state_for(tmp_path, curated=[ours]))
    assert new == ours and kind == "differ"


def test_an_entry_they_agree_with_is_untouched(tmp_path):
    ours = entry(AMMONIA, "ef-ammonia-twin", "ammonia")
    assert bafu.retarget(ours, state_for(tmp_path, curated=[ours])) == (ours, None)


def test_two_factorless_flows_take_their_target_and_say_so(tmp_path):
    ours = entry(BORAX, "ef-long", "Longterm")
    new, kind = bafu.retarget(ours, state_for(tmp_path, curated=[ours]))
    assert kind == "retargeted" and new["target"]["code"] == "ef-borax"
    assert new["comment"] == (
        "target aligned with brightway-flows 1.1.0 (neither flow carries a factor)"
    )


def test_identical_factors_with_another_conversion_leave_the_entry_alone(tmp_path):
    ours = entry(SCALED, "ef-ammonia", "ammonia")
    new, kind = bafu.retarget(ours, state_for(tmp_path, curated=[ours]))
    assert new == ours and kind == "unit_differs"
    scaled = {**ours, "conversion_factor": 2.0}
    new, kind = bafu.retarget(scaled, state_for(tmp_path, curated=[scaled]))
    assert kind == "retargeted" and new["conversion_factor"] == 2.0


def test_a_link_we_cannot_use_never_retargets(tmp_path):
    ours = entry(BROAD, "ef-ammonia", "ammonia")
    assert bafu.retarget(ours, state_for(tmp_path, curated=[ours])) == (ours, None)
