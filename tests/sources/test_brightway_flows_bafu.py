import json
import math
from dataclasses import replace

import pytest
from sentier_importers.core.context import RunContext
from sentier_importers.core.errors import ValidationError
from sentier_importers.core.source import SourceConfig
from sentier_importers.matching.ef_index import LabelDefect
from sentier_importers.sources.bafu.ecospold import flow_id
from sentier_importers.sources.brightway_flows import bafu
from sentier_importers.sources.brightway_flows.decisions import FlowDecision
from sentier_importers.sources.brightway_flows.identifiers import bafu_candidates

from tests.matching.ef_fixtures import AIR_RURAL, AIR_UNSPEC, cf_row, vocab_row, write_ef_inputs
from tests.sources.brightway_flows_fixture import document, flow, gz, link

AIR = ["emissions to air", "unspecified"]
AIR_LT = ["emissions to air", "low. pop., long-term"]
WATER_LT = ["emissions to water", "groundwater, long-term"]
AIR_LT_PATH = "Emissions / Emissions to air / Emissions to air, unspecified (long-term)"
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
#: Long-term sources: onto a long-term EF leaf, and onto targets whose index context is
#: uncertain (the Brightway Flows context code then says whether it is long-term).
LONG_OK, DEEP = src("Longok", AIR_LT), src("Deep", WATER_LT)
SHALLOW, DRIFT = src("Shallow", WATER_LT), src("Drift", AIR_LT)
ZERO, NOMULT, REDIR = src("Zero"), src("Nomult"), src("Redir")
#: BAFU water to air in kg: ours goes to the m3 flow with a conversion, theirs to the kg one.
VAPOUR = src("Vapour")
UNIVERSE = [AMMONIA, BENZENE, BORAX, HEXANE, BROAD, LONG, THORIUM, TOXIC, ODD]
BORAX_CAS = "1303-96-4"

#: code, name, context, method, value; a row with value None is a factorless vocab term
CF = [
    cf_row("ef-ammonia", "ammonia", AIR_UNSPEC, TOX, 1.0),
    cf_row("ef-ammonia-twin", "ammonia", AIR_RURAL, TOX, 1.0),
    cf_row("ef-benzene", "benzene", AIR_UNSPEC, TOX, 2.0),
    cf_row("ef-benzene-other", "benzene", AIR_RURAL, TOX, 9.0),
    cf_row("ef-toxic", "toxic", AIR_UNSPEC, TOX, 4.0),
    cf_row("ef-thorium-k", "thorium-232", AIR_UNSPEC, RAD, 1.0),
    cf_row("ef-zero", "zero", AIR_UNSPEC, TOX, 0.0),
    cf_row("ef-longok", "longok", AIR_LT_PATH, TOX, 3.0),
]
VOCAB = [
    vocab_row("ef-ammonia", "Ammonia"),
    vocab_row("ef-ammonia-twin", "Ammonia"),
    vocab_row("ef-benzene", "Benzene"),
    vocab_row("ef-benzene-other", "Benzene"),
    vocab_row("ef-toxic", "Toxic"),
    vocab_row("ef-thorium-k", "Thorium-232"),
    # only an ef-context notation, like the Brightway Flows 1.1 part of the ef-3.1
    # release: these sources read it (read_ef_context=True), our own matcher does not
    vocab_row("ef-borax", "Borax", cas=BORAX_CAS, ef_context=AIR_UNSPEC),
    vocab_row("ef-thorium", "Thorium-232", bw="envi-air-unkn"),
    vocab_row("ef-long", "Longterm", bw="envi-air-unkn"),
    # factorless and without a bw-context notation: listed in the vocab, never indexed
    vocab_row("ef-unplaced", "Unplaced"),
    vocab_row("ef-zero", "Zero"),
    vocab_row("ef-longok", "Longok"),
    # ambiguous bw-context codes: the index marks these contexts uncertain
    vocab_row("ef-deep", "Deep", bw="envi-wate-unkn"),
    vocab_row("ef-shallow", "Shallow", bw="envi-wate-unkn"),
    vocab_row("ef-drift", "Drift", bw="envi-air-grle-ru10pesq"),
    # EF has the same factorless flow once per unit
    vocab_row("ef-vapour-m3", "Water vapour", bw="envi-air-unkn"),
    vocab_row("ef-vapour-kg", "Water vapour", bw="envi-air-unkn"),
]
#: Unit symbol of the consensus flows that are not in kg (a factorless target takes
#: its unit from the Brightway Flows flow, the index has none for it).
BW_UNIT = {"ef-thorium": "kBq", "ef-thorium-k": "kBq", "ef-vapour-m3": "m3"}
#: Context code of the consensus flows that are not on ``envi-air-unkn``.
BW_CONTEXT = {
    "ef-deep": "envi-wate-lote",
    "ef-shallow": "envi-wate-unkn",
    "ef-drift": "envi-air-grle-ru10pesq",
}


def bw_document(merged=()):
    """``merged``: ``(old, new)`` EF codes; Brightway Flows lists only ``new`` and links
    ``old`` to it in its ef-3.1 correspondence (an EF flow merged into another one)."""

    def to(source, target, **kw):
        return link("bafu/2026-v1", their_id(source), target, **kw)

    gone = {old for old, _ in merged}
    return document(
        flows=[
            flow(
                code,
                r["pref_label"],
                context_code=BW_CONTEXT.get(code, "envi-air-unkn"),
                unit=BW_UNIT.get(code, "kg"),
            )
            for r in VOCAB
            for code in [r["iri"].rsplit("/", 1)[-1]]
            if code not in gone
        ]
        + [
            flow("add-hexane", "Hexane", source="bafu algorithm addition"),
            flow("ef-not-in-vocab", "Ghost"),
        ],
        redirects=[("old-borax", "ef-borax")],
        links_by_list={
            "ef-3.1": [link("ef-3.1", old, new) for old, new in merged],
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
                to(LONG_OK, "ef-longok"),
                to(DEEP, "ef-deep"),
                to(SHALLOW, "ef-shallow"),
                to(DRIFT, "ef-drift"),
                to(ZERO, "ef-zero"),
                to(VAPOUR, "ef-vapour-kg"),
                to(NOMULT, "ef-ammonia-twin", multiplier=0, basis="unit-table"),
                to(REDIR, "old-borax", match="broadMatch"),
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


def stage(tmp_path, curated=(), doc=None, universe=UNIVERSE, inferred=()):
    """Write the Brightway Flows file, the EF inputs, coverage.json and four packages."""
    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / "bw.json.gz").write_bytes(gz(doc or bw_document()))
    write_ef_inputs(tmp_path, CF, VOCAB)
    coverage = {"name": "c", "version": "0", "coverage": [{"source": s} for s in universe]}
    (tmp_path / "coverage.json").write_text(json.dumps(coverage))
    packages = {
        "curated": list(curated),
        "inferred": list(inferred),
        "matched": [],
        "nomenclature": [],
    }
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
    decisions_path = None

    def transform(self, records):
        return records


def decided(source, decision, reason="because"):
    """One reviewed decision on ``source``, as ``State.decisions`` holds it."""
    return FlowDecision(
        code=source["code"],
        group=f"g-{decision}",
        decision=decision,
        reason=reason,
        name=source["name"],
        context=tuple(source["context"]),
        unit=source["unit"],
    )


def with_decisions(state, *pairs):
    """``state`` with the decisions ``(source, decision)`` in place of its own."""
    return replace(state, decisions={s["code"]: decided(s, d) for s, d in pairs})


def write_decisions(path, *pairs):
    """A decisions file for ``(source, decision)`` pairs, one group per decision."""
    doc = {
        "groups": {f"g-{d}": {"decision": d, "reason": "because"} for _, d in pairs},
        "flows": {
            s["code"]: {
                "group": f"g-{d}",
                "name": s["name"],
                "context": s["context"],
                "unit": s["unit"],
            }
            for s, d in pairs
        },
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(doc))  # JSON is YAML
    return path


def state_for(tmp_path, curated=(), decisions_path=None, **stage_kw):
    probe = _Probe(config(stage(tmp_path, curated, **stage_kw)))
    probe.decisions_path = decisions_path
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
        (LONG_OK, None),  # a long-term source onto a long-term EF leaf
        # the index context is uncertain: the Brightway Flows context code decides
        (DEEP, None),
        (SHALLOW, "long_term_onto_immediate"),
        (DRIFT, "long_term_onto_immediate"),
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


def test_a_scoring_link_decided_theirs_becomes_an_entry(tmp_path):
    state = with_decisions(state_for(tmp_path), (TOXIC, "theirs"))
    made, reason = bafu.package_entry(TOXIC, state)
    assert reason is None and made["target"]["code"] == "ef-toxic"
    assert made["comment"] == (
        "brightway-flows 1.1.0, skos:closeMatch; reviewed 2026-11-19: because"
    )


@pytest.mark.parametrize("decision", ["ours", "hold"])
def test_a_scoring_link_decided_otherwise_stays_held(tmp_path, decision):
    state = with_decisions(state_for(tmp_path), (TOXIC, decision))
    assert bafu.package_entry(TOXIC, state) == (None, "characterised_held_for_review")


def test_a_decision_adds_nothing_to_a_link_that_changes_no_score(tmp_path):
    state = state_for(tmp_path)
    plain, _ = bafu.package_entry(BORAX, state)
    assert bafu.package_entry(BORAX, with_decisions(state, (BORAX, "theirs"))) == (plain, None)


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
    state = state_for(tmp_path)
    ours = entry(SCALED, "ef-ammonia", "ammonia")
    new, kind = bafu.retarget(ours, state)
    assert new == ours and kind == "unit_differs"
    scaled = {**ours, "conversion_factor": 2.0}
    new, kind = bafu.retarget(scaled, state)
    assert kind == "retargeted" and new["conversion_factor"] == 2.0


def test_a_link_we_cannot_use_never_retargets(tmp_path):
    ours = entry(BROAD, "ef-ammonia", "ammonia")
    assert bafu.retarget(ours, state_for(tmp_path, curated=[ours])) == (ours, None)


UNCERTAIN = "EF context inferred from the Brightway context code, branch/sub-compartment uncertain"


def test_an_uncertain_target_context_is_left_out_and_disclosed(tmp_path):
    state = state_for(tmp_path)
    made, reason = bafu.package_entry(DEEP, state)
    assert reason is None and "context" not in made["target"]
    assert UNCERTAIN in made["comment"]
    certain, _ = bafu.package_entry(BORAX, state)
    assert "context" in certain["target"] and UNCERTAIN not in certain["comment"]


def test_a_retarget_onto_an_uncertain_context_drops_ours_and_discloses_it(tmp_path):
    ours = entry(DEEP, "ef-long", "Longterm")
    new, kind = bafu.retarget(ours, state_for(tmp_path))
    assert kind == "retargeted" and new["target"]["code"] == "ef-deep"
    assert "context" not in new["target"]
    assert new["comment"].count(UNCERTAIN) == 1
    again, _ = bafu.retarget({**ours, "comment": UNCERTAIN}, state_for(tmp_path))
    assert again["comment"].count(UNCERTAIN) == 1  # never said twice


def test_a_target_whose_factors_are_all_zero_ships_and_says_so(tmp_path):
    made, reason = bafu.package_entry(ZERO, state_for(tmp_path))
    assert reason is None and made["target"]["code"] == "ef-zero"
    assert made["target"]["unit"] == "kilogram"
    assert "all factors are zero in EF 3.1" in made["comment"]
    assert "no factor in any method" not in made["comment"]


@pytest.mark.parametrize(
    "source_unit,target_unit,multiplier,expected",
    [
        ("kg", "kilogram", None, (None, None)),
        ("kg", "kilogram", 1.0, (None, None)),  # a multiplier of one is no conversion
        ("Bq", "kBq", 0.001, (0.001, None)),
        ("Bq", "kBq", None, (None, "unit_mismatch")),  # other scale, no multiplier
        ("Bq", "kBq", 1000.0, (None, "unit_mismatch")),  # contradicts the unit scale
        ("kWh", "megajoule", 3.6, (3.6, None)),
        ("kg", "cubic meter", 0.001, (0.001, None)),  # dimension crossing, their multiplier
        ("kg", "cubic meter", None, (None, "unit_mismatch")),
        ("kg", "cubic meter", 0, (None, "unit_mismatch")),
        ("Bq", "kBq", 0, (None, "unit_mismatch")),
        ("kg", "cubic meter", -0.001, (None, "unit_mismatch")),
        ("kg", "kilogram", -1.0, (None, "unit_mismatch")),
        ("kg", "cubic meter", math.nan, (None, "unit_mismatch")),
        ("kg", "cubic meter", math.inf, (None, "unit_mismatch")),
        ("tkm", "kilogram", None, (None, "unit_unknown")),
        ("kg", "sm3", 1.0, (None, "unit_unknown")),
        ("", "kilogram", None, (None, "unit_unknown")),
    ],
)
def test_conversion(source_unit, target_unit, multiplier, expected):
    assert bafu._conversion(source_unit, target_unit, multiplier) == expected


def test_a_multiplier_of_zero_is_not_read_as_one(tmp_path):
    ours = entry(NOMULT, "ef-ammonia", "ammonia")
    assert bafu.retarget(ours, state_for(tmp_path)) == (ours, "unit_differs")


def test_their_factorless_target_against_our_scoring_one_is_left_alone(tmp_path):
    ours = entry(BORAX, "ef-toxic", "toxic")
    assert bafu.retarget(ours, state_for(tmp_path)) == (ours, "theirs_zero")


def test_their_scoring_target_against_our_factorless_one_is_left_alone(tmp_path):
    ours = entry(TOXIC, "ef-borax", "Borax")
    assert bafu.retarget(ours, state_for(tmp_path)) == (ours, "ours_zero")


def test_zero_factors_and_no_factors_count_as_identical(tmp_path):
    ours = entry(BORAX, "ef-zero", "zero")
    new, kind = bafu.retarget(ours, state_for(tmp_path))
    assert kind == "retargeted" and new["target"]["code"] == "ef-borax"
    assert "neither flow carries a factor" in new["comment"]


def test_an_entry_without_target_code_is_reported_not_skipped(tmp_path):
    ours = {"source": AMMONIA, "target": {"name": "ammonia", "unit": "kilogram"}}
    assert bafu.retarget(ours, state_for(tmp_path)) == (ours, "ours_without_code")
    unusable = {"source": BROAD, "target": {"name": "ammonia"}}
    assert bafu.retarget(unusable, state_for(tmp_path)) == (unusable, None)


def test_a_merged_ef_flow_with_identical_factors_takes_their_code(tmp_path):
    doc = bw_document(merged=[("ef-ammonia", "ef-ammonia-twin")])
    ours = entry(AMMONIA, "ef-ammonia", "ammonia")
    new, kind = bafu.retarget(ours, state_for(tmp_path, doc=doc))
    assert kind == "retargeted" and new["target"]["code"] == "ef-ammonia-twin"


def test_a_merged_ef_flow_with_other_factors_is_a_disagreement(tmp_path):
    doc = bw_document(merged=[("ef-benzene", "ef-benzene-other")])
    ours = entry(BENZENE, "ef-benzene", "benzene")
    assert bafu.retarget(ours, state_for(tmp_path, doc=doc)) == (ours, "differ")


def test_a_source_mapped_by_two_packages_is_refused(tmp_path):
    ours = entry(AMMONIA, "ef-ammonia", "ammonia")
    with pytest.raises(ValidationError, match="more than one package"):
        state_for(tmp_path, curated=[ours], inferred=[ours])


def test_a_package_entry_outside_the_coverage_universe_is_refused(tmp_path):
    with pytest.raises(ValidationError, match="not in coverage"):
        state_for(tmp_path, curated=[entry(GHOST, "ef-ammonia", "ammonia")])


def test_a_source_record_that_differs_from_coverage_is_refused(tmp_path):
    other = {**AMMONIA, "unit": "g"}
    with pytest.raises(ValidationError, match="differs from its coverage record"):
        state_for(tmp_path, curated=[entry(other, "ef-ammonia", "ammonia")])


def test_an_undeclared_input_is_a_missing_input(tmp_path):
    cfg = config(stage(tmp_path))
    del cfg.inputs["nomenclature"]
    probe = _Probe(cfg)
    ctx = RunContext(cache_dir=tmp_path / "cache", output_dir=tmp_path / "out")
    with pytest.raises(RuntimeError, match="nomenclature"):
        probe.parse(probe.fetch(ctx))


ALIGNED_UNIT = "target and unit aligned with brightway-flows 1.1.0 (neither flow carries a factor)"
NO_FACTOR = "uncharacterised in EF 3.1: no factor in any method"


def vapour_entry():
    """Ours, as the nomenclature package writes it: the m3 flow, kg rescaled to m3."""
    return {
        **entry(VAPOUR, "ef-vapour-m3", "Water vapour", unit="cubic meter"),
        "conversion_factor": 0.001,
        "comment": "target unit is the EF convention for volume; amount rescaled by 0.001",
    }


def test_factorless_flows_with_another_conversion_take_their_unit(tmp_path):
    ours = vapour_entry()
    new, kind = bafu.retarget(ours, state_for(tmp_path))
    assert kind == "retargeted"
    assert new["source"] == VAPOUR
    assert new["target"]["code"] == "ef-vapour-kg" and new["target"]["unit"] == "kilogram"
    assert new["target"]["context"] == [
        "Emissions",
        "Emissions to air",
        "Emissions to air, unspecified",
    ]
    assert "conversion_factor" not in new
    # the old comment states a convention and a rescaling that no longer hold
    assert new["comment"] == f"{NO_FACTOR}; {ALIGNED_UNIT}"
    assert ours["conversion_factor"] == 0.001 and ours["target"]["unit"] == "cubic meter"


def test_a_unit_aligned_entry_is_left_alone_the_next_time(tmp_path):
    state = state_for(tmp_path)
    new, _ = bafu.retarget(vapour_entry(), state)
    assert bafu.retarget(new, state) == (new, None)


def test_their_multiplier_replaces_our_conversion_when_nothing_scores(tmp_path):
    ours = {**entry(THORIUM, "ef-long", "Longterm"), "comment": "old note"}
    new, kind = bafu.retarget(ours, state_for(tmp_path))
    assert kind == "retargeted"
    assert new["target"]["code"] == "ef-thorium" and new["target"]["unit"] == "kBq"
    assert new["conversion_factor"] == 0.001
    assert new["comment"] == (
        f"{NO_FACTOR}; {ALIGNED_UNIT}; amount rescaled by 0.001 (unit-table)"
    )
    assert list(new) == ["source", "target", "conversion_factor", "comment"]


def test_a_unit_aligned_comment_names_zero_factors_and_an_uncertain_context(tmp_path):
    state = state_for(tmp_path)
    zero, _ = bafu.retarget(
        {**entry(ZERO, "ef-long", "Longterm"), "conversion_factor": 0.5}, state
    )
    assert zero["comment"] == f"all factors are zero in EF 3.1; {ALIGNED_UNIT}"
    deep, _ = bafu.retarget(
        {**entry(DEEP, "ef-long", "Longterm"), "conversion_factor": 0.5}, state
    )
    assert "context" not in deep["target"] and "conversion_factor" not in deep
    assert deep["comment"] == f"{NO_FACTOR}; {ALIGNED_UNIT}; {UNCERTAIN}"


def test_their_unit_is_not_taken_when_their_conversion_is_not_usable(tmp_path):
    # Bq onto a kg flow without a multiplier: nothing to convert with
    ours = {**entry(ODD, "ef-long", "Longterm"), "conversion_factor": 0.5}
    assert bafu.retarget(ours, state_for(tmp_path)) == (ours, "unit_differs")


def test_their_unit_is_not_taken_when_it_is_unknown(tmp_path):
    state = state_for(tmp_path)
    flows = dict(state.bw.flows)
    flows["ef-vapour-kg"] = replace(flows["ef-vapour-kg"], unit="sm3")
    unknown = replace(state, bw=replace(state.bw, flows=flows))
    ours = vapour_entry()
    assert bafu.retarget(ours, unknown) == (ours, "unit_differs")


def test_scoring_flows_with_another_conversion_are_never_unit_aligned(tmp_path):
    # identical non-zero factors: another conversion changes the amount characterised
    ours = {**entry(SCALED, "ef-ammonia", "ammonia"), "conversion_factor": 0.5}
    assert bafu.retarget(ours, state_for(tmp_path)) == (ours, "unit_differs")


REVIEWED = "target aligned with brightway-flows 1.1.0 (reviewed 2026-11-19: because)"


def test_a_state_without_a_decisions_file_holds_no_decision(tmp_path):
    assert state_for(tmp_path).decisions == {}


def test_parse_loads_the_decisions_file(tmp_path):
    path = write_decisions(tmp_path / "d" / "decisions.yaml", (BENZENE, "theirs"), (TOXIC, "hold"))
    state = state_for(tmp_path, decisions_path=path)
    assert state.decisions[BENZENE["code"]] == decided(BENZENE, "theirs")
    assert state.decisions[TOXIC["code"]].decision == "hold"


def test_a_decision_on_a_flow_outside_coverage_is_refused(tmp_path):
    path = write_decisions(tmp_path / "d" / "decisions.yaml", (GHOST, "theirs"))
    with pytest.raises(ValidationError, match="not in coverage"):
        state_for(tmp_path, decisions_path=path)


@pytest.mark.parametrize(
    "change", [{"name": "Benzol"}, {"context": ["emissions to air", "indoor"]}, {"unit": "g"}]
)
def test_a_decision_that_no_longer_describes_its_flow_is_refused(tmp_path, change):
    stale = {**BENZENE, **change}  # same code, another record than coverage holds
    path = write_decisions(tmp_path / "d" / "decisions.yaml", (stale, "theirs"))
    with pytest.raises(ValidationError, match="differs from its coverage record"):
        state_for(tmp_path, decisions_path=path)


def test_a_differing_target_decided_theirs_is_taken(tmp_path):
    ours = {**entry(BENZENE, "ef-benzene", "benzene"), "comment": "old note"}
    state = with_decisions(state_for(tmp_path), (BENZENE, "theirs"))
    new, kind = bafu.retarget(ours, state)
    assert kind == "retargeted"
    assert new["source"] == BENZENE and new["target"]["code"] == "ef-benzene-other"
    assert new["target"]["unit"] == "kilogram"
    assert new["target"]["context"] == [
        "Emissions",
        "Emissions to air",
        "Emissions to non-urban air or from high stacks",
    ]
    assert new["comment"] == f"old note; {REVIEWED}"
    assert bafu.retarget(new, state) == (new, None)  # and it stays so
    bare, _ = bafu.retarget(entry(BENZENE, "ef-benzene", "benzene"), state)
    assert bare["comment"] == REVIEWED


@pytest.mark.parametrize("decision", ["ours", "hold"])
def test_a_differing_target_decided_otherwise_is_left_alone(tmp_path, decision):
    ours = entry(BENZENE, "ef-benzene", "benzene")
    state = with_decisions(state_for(tmp_path), (BENZENE, decision))
    assert bafu.retarget(ours, state) == (ours, "differ")


def test_a_decision_theirs_also_takes_a_target_only_one_side_scores_on(tmp_path):
    state = with_decisions(state_for(tmp_path), (BORAX, "theirs"), (TOXIC, "theirs"))
    theirs_zero, kind = bafu.retarget(entry(BORAX, "ef-toxic", "toxic"), state)
    assert kind == "retargeted" and theirs_zero["target"]["code"] == "ef-borax"
    ours_zero, kind = bafu.retarget(entry(TOXIC, "ef-borax", "Borax"), state)
    assert kind == "retargeted" and ours_zero["target"]["code"] == "ef-toxic"
    assert ours_zero["comment"] == REVIEWED


def test_a_decision_never_overrides_a_unit_problem(tmp_path):
    # their link doubles the amount, our entry does not: a reviewed target is not enough
    ours = entry(SCALED, "ef-benzene", "benzene")
    state = with_decisions(state_for(tmp_path), (SCALED, "theirs"))
    assert bafu.retarget(ours, state) == (ours, "unit_differs")
    without = state_for(tmp_path)
    assert bafu.retarget(ours, without) == (ours, "differ")


def test_decision_gaps_lists_unneeded_decisions_and_undecided_cases(tmp_path):
    curated = [
        entry(AMMONIA, "ef-ammonia", "ammonia"),  # identical factors: no decision needed
        entry(BENZENE, "ef-benzene", "benzene"),  # differ
        entry(BROAD, "ef-ammonia", "ammonia"),  # no usable link
    ]
    state = state_for(tmp_path, curated=curated)
    unneeded, undecided = bafu.decision_gaps(state)
    assert unneeded == []
    assert [s["name"] for s in undecided] == ["Benzene", "Toxic"]  # mapped differ, held link
    full = with_decisions(state, (BENZENE, "ours"), (TOXIC, "hold"))
    assert bafu.decision_gaps(full) == ([], [])
    extra = with_decisions(
        state,
        (BENZENE, "theirs"),
        (AMMONIA, "theirs"),
        (BROAD, "ours"),
        (BORAX, "theirs"),  # unmapped, ships without a decision
        (HEXANE, "hold"),  # unmapped, no EF target
    )
    unneeded, undecided = bafu.decision_gaps(extra)
    assert [s["name"] for s in unneeded] == ["Ammonia", "Borax", "Broad", "Hexane"]
    assert [s["name"] for s in undecided] == ["Toxic"]


def test_a_decision_already_applied_is_not_reported_as_unneeded(tmp_path):
    aligned = entry(BENZENE, "ef-benzene-other", "benzene")
    state = with_decisions(state_for(tmp_path, curated=[aligned]), (BENZENE, "theirs"))
    assert bafu.decision_gaps(state)[0] == []
    kept = with_decisions(state, (BENZENE, "ours"))  # "ours", yet we are on their target
    assert [s["name"] for s in bafu.decision_gaps(kept)[0]] == ["Benzene"]


def test_a_reviewed_target_that_scores_drops_what_the_old_comment_said_of_no_factor(tmp_path):
    # ours carried no factor and said so; theirs scores, so those sentences turn false
    old = (
        "uncharacterised in EF 3.1: no factor in any method; target unit is the EF "
        "convention for this dimension (EF states no reference unit for this flow); kept note"
    )
    state = with_decisions(state_for(tmp_path), (TOXIC, "theirs"), (BORAX, "theirs"))
    new, kind = bafu.retarget({**entry(TOXIC, "ef-borax", "Borax"), "comment": old}, state)
    assert kind == "retargeted" and new["comment"] == f"kept note; {REVIEWED}"
    zero = {**entry(TOXIC, "ef-zero", "zero"), "comment": "all factors are zero in EF 3.1"}
    assert bafu.retarget(zero, state)[0]["comment"] == REVIEWED
    # their target carries no factor: nothing the old comment says of ours is touched
    kept, _ = bafu.retarget({**entry(BORAX, "ef-toxic", "toxic"), "comment": old}, state)
    assert kept["comment"] == f"{old}; {REVIEWED}"
