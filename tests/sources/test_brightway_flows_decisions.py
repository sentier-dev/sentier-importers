from collections import Counter
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest
import yaml
from sentier_importers.core.errors import ValidationError
from sentier_importers.sources.brightway_flows.decisions import (
    DECISIONS_PATH,
    FlowDecision,
    load_decisions,
)

CODE = "3f7b2453-30a4-58dd-a851-7b9c3add5c4f"


def good():
    return {
        "groups": {"air": {"decision": "theirs", "reason": "same substance"}},
        "flows": {
            CODE: {
                "group": "air",
                "name": "Atrazine",
                "context": ["emissions to air", "unspecified"],
                "unit": "kg",
            }
        },
    }


def write(tmp_path, doc):
    path = tmp_path / "decisions.yaml"
    path.write_text(doc if isinstance(doc, str) else yaml.safe_dump(doc))
    return path


def test_a_decision_carries_its_group_and_the_flow_it_was_made_for(tmp_path):
    decisions = load_decisions(write(tmp_path, good()))
    assert decisions == {
        CODE: FlowDecision(
            code=CODE,
            group="air",
            decision="theirs",
            reason="same substance",
            name="Atrazine",
            context=("emissions to air", "unspecified"),
            unit="kg",
        )
    }
    with pytest.raises(FrozenInstanceError):
        decisions[CODE].decision = "ours"


def test_the_shipped_file_loads(tmp_path):
    decisions = load_decisions()
    assert DECISIONS_PATH.name == "bafu_decisions.yaml"
    assert Counter(d.decision for d in decisions.values()) == {"theirs": 38, "ours": 15, "hold": 5}
    assert all(d.reason and d.name and d.context and d.unit for d in decisions.values())


def test_the_file_ships_with_the_package():
    pyproject = Path(__file__).parents[2] / "pyproject.toml"
    assert '"sources/brightway_flows/*.yaml"' in pyproject.read_text()


def _without(doc, *path):
    node = doc
    for key in path[:-1]:
        node = node[key]
    del node[path[-1]]
    return doc


def _with(doc, value, *path):
    node = doc
    for key in path[:-1]:
        node = node[key]
    node[path[-1]] = value
    return doc


@pytest.mark.parametrize(
    "doc,message",
    [
        ("- a list\n", "mapping with exactly"),
        (_with(good(), 1, "reviewed"), "mapping with exactly"),
        (_without(good(), "flows"), "mapping with exactly"),
        (_with(good(), [], "groups"), "'groups' must be a mapping"),
        (_with(good(), [], "flows"), "'flows' must be a mapping"),
        (_with(good(), "maybe", "groups", "air", "decision"), "decision 'maybe'"),
        (_with(good(), " ", "groups", "air", "reason"), "reason"),
        (_without(good(), "groups", "air", "reason"), "group 'air' must have exactly"),
        (_with(good(), "x", "groups", "air", "note"), "group 'air' must have exactly"),
        (_with(good(), "water", "flows", CODE, "group"), "unknown group 'water'"),
        (_with(good(), "x", "flows", CODE, "cas"), "must have exactly"),
        (_without(good(), "flows", CODE, "unit"), "must have exactly"),
        (_with(good(), "emissions to air", "flows", CODE, "context"), "context"),
        (_with(good(), [1, 2], "flows", CODE, "context"), "context"),
        (_with(good(), "", "flows", CODE, "name"), "name"),
        (_with(good(), 3, "flows", CODE, "unit"), "unit"),
    ],
)
def test_a_malformed_file_is_refused(tmp_path, doc, message):
    with pytest.raises(ValidationError, match=message):
        load_decisions(write(tmp_path, doc))


def test_a_flow_decided_twice_is_refused(tmp_path):
    flow = '{group: air, name: "Atrazine", context: ["emissions to air", "unspecified"], unit: kg}'
    text = (
        "groups:\n  air: {decision: theirs, reason: same substance}\n"
        f"flows:\n  {CODE}: {flow}\n  {CODE}: {flow}\n"
    )
    with pytest.raises(ValidationError, match="more than once"):
        load_decisions(write(tmp_path, text))


def test_a_group_defined_twice_is_refused(tmp_path):
    text = (
        "groups:\n  air: {decision: theirs, reason: a}\n  air: {decision: ours, reason: b}\n"
        "flows: {}\n"
    )
    with pytest.raises(ValidationError, match="more than once"):
        load_decisions(write(tmp_path, text))


def test_text_that_is_not_yaml_is_refused(tmp_path):
    with pytest.raises(ValidationError, match="not valid YAML"):
        load_decisions(write(tmp_path, "groups: [unclosed\n"))
