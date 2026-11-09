"""The ecoinvent-ef-biosphere plugin turns Brightway's ecoinvent -> EF 3.1 randonneur
package into a sentier-mappings package."""

import gzip
import json
import re
from dataclasses import replace
from pathlib import Path

import jsonschema
import pytest
from sentier_importers.core import registry as registry_mod
from sentier_importers.core import vocab_layers
from sentier_importers.core.context import RunContext
from sentier_importers.core.errors import FetchError, ParseError, ValidationError
from sentier_importers.core.pipeline import _assemble
from sentier_importers.core.source import SourceConfig
from sentier_importers.sources.randonneur_data.ecoinvent_ef_biosphere import (
    NO_VOCAB_TERM,
    EcoinventEfBiosphereSource,
)

_SCHEMA = Path(__file__).parent / "fixtures" / "randonneur-package.schema.json"
FLOWS = "https://vocab.sentier.dev/flows/"
SOURCE = "https://vocab.sentier.dev/sources/ef-3.1"
MODULE = "sentier_importers.sources.randonneur_data.ecoinvent_ef_biosphere"
PAIR = "ecoinvent-3.11-biosphere__ef-3.1"


def _upstream(source_unit, target, *, uuid="eco-1", **extra):
    entry = {
        "source": {
            "name": "Ethylene carbonate",
            "unit": source_unit,
            "uuid": uuid,
            "formula": "C3H4O3",
            "context": ["water", "unspecified"],
        },
        "target": {
            "name": "1,3-dioxolan-2-one",
            "CAS number": "000096-49-1",
            "uuid": "ef-in-vocab",
            "unit_group_flow_property": "93a60a56-a3c8-11da-a746-0800200b9a66",
            "unit_scaling": 1.0,
            "unit_name": "kg",
            "context": ["Emissions", "Emissions to water", "Emissions to water, unspecified"],
        },
    }
    entry["target"].update(target)
    entry.update(extra)
    return entry


ENTRIES = [
    _upstream("kg", {}, comment="Name match based on ecoinvent LCIA 3.11 EF 3.1 implementation"),
    _upstream("kg", {"uuid": "ef-not-in-vocab", "name": "fenpropimorph"}, uuid="eco-2"),
    _upstream(
        "kg",
        {"uuid": "ef-coal", "name": "brown coal", "unit_name": "MJ"},
        uuid="eco-3",
        conversion_factor=9.41,
        comment="Conversion factor from EF 3.1 LCIA method for 'Coal, brown'",
    ),
    _upstream("m2*year", {"unit_name": "m2*a"}, uuid="eco-4"),
]


def _write_inputs(tmp_path, entries=ENTRIES):
    package = {"name": "ecoinvent-3.11-biosphere-EF-3.1-biosphere", "replace": entries}
    upstream = tmp_path / "upstream.gz"
    upstream.write_bytes(gzip.compress(json.dumps(package).encode()))
    terms = [
        {"iri": iri, "type": term_type, "pref_label": "x", "source": SOURCE, "status": "draft"}
        for iri, term_type in (
            (FLOWS + "ef-in-vocab", "elementary-flow"),
            (FLOWS + "ef-coal", "elementary-flow"),
            (SOURCE, "source"),
        )
    ]
    vocab_layers.write_layers(terms, [], tmp_path / "vocab", "ef-3.1")
    return upstream, tmp_path / "vocab" / "terms"


def _config(upstream, terms_dir) -> SourceConfig:
    return SourceConfig(
        name="ecoinvent-ef-biosphere",
        module=MODULE,
        target="sentier_mappings",
        category=PAIR,
        fetch_url=f"file://{upstream}",
        fetch_format="json",
        output_format="json",
        emit_filename="biosphere",
        package_name=f"{PAIR}-biosphere",
        package_version="0.1.0",
        package_verb="replace",
        inputs={"ef_vocab": f"file://{terms_dir}"},
    )


def _rows(tmp_path, entries=ENTRIES):
    config = _config(*_write_inputs(tmp_path, entries))
    src = EcoinventEfBiosphereSource(config)
    ctx = RunContext(cache_dir=tmp_path / "cache", output_dir=tmp_path / "out")
    return src.transform(src.parse(src.fetch(ctx))), config


def test_ecoinvent_side_carries_only_the_code(tmp_path):
    rows, _ = _rows(tmp_path)
    assert [r["source"] for r in rows] == [
        {"code": "eco-1"},
        {"code": "eco-2"},
        {"code": "eco-3"},
        {"code": "eco-4"},
    ]


def test_ef_side_is_the_full_open_identifier(tmp_path):
    rows, _ = _rows(tmp_path)
    assert rows[0]["target"] == {
        "code": "ef-in-vocab",
        "name": "1,3-dioxolan-2-one",
        "context": ["Emissions", "Emissions to water", "Emissions to water, unspecified"],
        "unit": "kg",
    }


def test_match_rule_comment_is_kept(tmp_path):
    rows, _ = _rows(tmp_path)
    assert rows[0]["comment"] == "Name match based on ecoinvent LCIA 3.11 EF 3.1 implementation"
    assert "conversion_factor" not in rows[0]


def test_target_without_a_vocab_term_is_flagged(tmp_path):
    rows, _ = _rows(tmp_path)
    assert rows[1]["comment"] == NO_VOCAB_TERM
    assert "comment" not in rows[3]


def test_conversion_names_both_units_and_no_ecoinvent_flow(tmp_path):
    rows, _ = _rows(tmp_path)
    coal = rows[2]
    assert coal["conversion_factor"] == 9.41
    assert "kg" in coal["comment"] and "MJ" in coal["comment"]
    assert "Coal, brown" not in coal["comment"]


def test_same_unit_in_another_spelling_needs_no_factor(tmp_path):
    rows, _ = _rows(tmp_path)
    assert rows[3]["target"]["unit"] == "m2*a"
    assert "conversion_factor" not in rows[3]


def test_unit_change_without_a_factor_is_an_error(tmp_path):
    entries = [_upstream("m3", {"unit_name": "kg"})]
    with pytest.raises(ValidationError, match="'m3'.*'kg'"):
        _rows(tmp_path, entries)


def test_duplicate_source_code_is_an_error(tmp_path):
    entries = [_upstream("kg", {}), _upstream("kg", {"uuid": "ef-coal"})]
    with pytest.raises(ValidationError, match="eco-1"):
        _rows(tmp_path, entries)


def test_entry_without_a_uuid_is_an_error(tmp_path):
    entries = [_upstream("kg", {"uuid": None})]
    with pytest.raises(ValidationError, match="uuid"):
        _rows(tmp_path, entries)


def test_vocab_folder_must_be_declared(tmp_path):
    upstream, _ = _write_inputs(tmp_path)
    src = EcoinventEfBiosphereSource(replace(_config(upstream, "unused"), inputs={}))
    ctx = RunContext(cache_dir=tmp_path / "cache", output_dir=tmp_path / "out")
    with pytest.raises(FetchError, match="ef_vocab"):
        src.parse(src.fetch(ctx))


def test_assembled_package_matches_randonneur_schema(tmp_path):
    rows, config = _rows(tmp_path)
    package = _assemble(rows, config)
    assert package["name"] == f"{PAIR}-biosphere"
    assert len(package["replace"]) == 4
    jsonschema.validate(package, json.loads(_SCHEMA.read_text()))


def test_registry_block_pins_the_upstream_file_by_commit():
    (config,) = [c for c in registry_mod.load_registry() if c.module == MODULE]
    assert config.target == "sentier_mappings"
    assert config.category == PAIR
    assert config.emit_filename == "biosphere"
    assert config.package_verb == "replace"
    assert set(config.inputs) == {"ef_vocab"}
    assert re.search(
        r"^https://raw\.githubusercontent\.com/brightway-lca/randonneur_data/[0-9a-f]{40}/",
        config.fetch_url,
    )


def test_payload_that_is_not_a_gzipped_package_is_a_parse_error(tmp_path):
    upstream, terms_dir = _write_inputs(tmp_path)
    upstream.write_bytes(b"<html>not found</html>")
    src = EcoinventEfBiosphereSource(_config(upstream, terms_dir))
    ctx = RunContext(cache_dir=tmp_path / "cache", output_dir=tmp_path / "out")
    with pytest.raises(ParseError, match="gzipped randonneur package"):
        src.parse(src.fetch(ctx))
