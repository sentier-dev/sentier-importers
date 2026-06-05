import dataclasses

import pytest
from sentier_importers.core import targets as targets_mod
from sentier_importers.core.errors import RegistryError
from sentier_importers.core.targets import Target


def test_known_targets_present():
    for name in ("sentier_vocab", "sentier_inventory", "sentier_methods"):
        assert name in targets_mod.TARGETS


def test_vocab_target_uses_linkml_and_is_pinned():
    vocab = targets_mod.get_target("sentier_vocab")
    assert vocab.validator == "linkml"
    assert vocab.schema_ref is not None


def test_bulk_target_has_no_schema():
    inv = targets_mod.get_target("sentier_inventory")
    assert inv.validator == "none"
    assert inv.schema_ref is None


def test_target_is_frozen():
    with pytest.raises(dataclasses.FrozenInstanceError):
        targets_mod.get_target("sentier_vocab").name = "x"


def test_unknown_target_raises():
    with pytest.raises(RegistryError):
        targets_mod.get_target("does_not_exist")


def test_target_dataclass_fields():
    t = Target(name="t", repo="https://github.com/o/r.git", output_subdir="data")
    assert t.schema_ref is None
    assert t.validator == "none"
