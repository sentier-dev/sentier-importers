from copy import deepcopy

import pytest
import yaml
from sentier_importers.core.errors import RegistryError
from sentier_importers.core.lineage import DatasetRef
from sentier_importers.core.provenance import resolve_provenance
from sentier_importers.core.registry import get_config, load_registry


def declaration():
    return deepcopy(get_config("bafu-processes").input_provenance["source"])


def test_registry_resolves_reusable_declaration_for_each_role(tmp_path):
    metadata = declaration()
    path = tmp_path / "registry.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "source_datasets": {"bafu-v1": metadata},
                "sources": [
                    {
                        "name": "demo",
                        "module": "demo.source",
                        "target": "sentier_inventory",
                        "category": "demo",
                        "fetch": {"url": "file://original.zip", "format": "zip"},
                        "output_format": "parquet",
                        "inputs": {"mapping": "file://mapping.json"},
                        "provenance": {"source": "bafu-v1", "mapping": "bafu-v1"},
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    (config,) = load_registry(path)
    assert config.input_provenance == {"source": metadata, "mapping": metadata}
    config.input_provenance["source"]["title"] = "changed"
    assert config.input_provenance["mapping"]["title"] == metadata["title"]


@pytest.mark.parametrize("references", [{}, {"other": "bafu"}, {"source": "missing"}])
def test_invalid_provenance_reference_fails(references):
    with pytest.raises(RegistryError):
        resolve_provenance({"provenance": references}, {"bafu": declaration()})


@pytest.mark.parametrize(
    "key,value",
    [
        ("title", ""),
        ("publisher", 123),
        ("creators", "BAFU"),
        ("date", "2026"),
        ("private_path", "C:/Users/private"),
        ("source_url", "file:///C:/private.zip"),
        ("source_url", "https://user:secret@example.org/file"),
        ("source_url", "https://example.org/file?token=secret"),
    ],
)
def test_bad_declared_facts_and_private_locators_are_rejected(key, value):
    metadata = declaration()
    metadata[key] = value
    with pytest.raises(RegistryError):
        resolve_provenance({"provenance": {"source": "bafu"}}, {"bafu": metadata})


def test_legacy_entry_can_adopt_provenance_later():
    assert resolve_provenance({}, {}) == {}


def test_opted_in_jobs_must_describe_secondary_inputs():
    with pytest.raises(RegistryError, match="every named input"):
        resolve_provenance(
            {"provenance": {"source": "bafu"}, "inputs": {"mapping": "file://mapping"}},
            {"bafu": declaration()},
        )


def test_known_public_source_url_uses_standard_datasource_facet():
    metadata = declaration()
    metadata["source_url"] = "https://example.org/datasets/bafu-2026"
    ref = DatasetRef("sentier://inputs/example", "bafu.zip")
    ref.source_metadata = {"metadata": metadata, "observation": {}}
    facets = ref.as_event_value()["facets"]
    assert facets["dataSource"]["name"] == "bafu-2026"
    assert facets["dataSource"]["uri"] == metadata["source_url"]
    assert "url" not in facets["dataSource"]
    assert "ownership" not in facets  # Publishing does not establish ownership.
