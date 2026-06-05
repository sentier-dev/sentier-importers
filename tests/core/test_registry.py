import textwrap

import pytest
from sentier_importers.core import registry as registry_mod
from sentier_importers.core.errors import RegistryError
from sentier_importers.core.source import Source, SourceConfig


def _write_registry(tmp_path, body):
    path = tmp_path / "registry.yaml"
    path.write_text(textwrap.dedent(body), encoding="utf-8")
    return path


def test_load_registry_parses_entries(tmp_path):
    path = _write_registry(
        tmp_path,
        """
        sources:
          - name: demo
            module: sentier_importers.sources.example_csv.source
            target: sentier_inventory
            category: demo
            fetch:
              url: "file:///tmp/x.csv"
              format: csv
            output_format: json
            validate_against: null
            enabled: true
        """,
    )
    configs = registry_mod.load_registry(path)
    assert len(configs) == 1
    cfg = configs[0]
    assert isinstance(cfg, SourceConfig)
    assert cfg.name == "demo"
    assert cfg.fetch_url == "file:///tmp/x.csv"
    assert cfg.fetch_format == "csv"
    assert cfg.validate_against is None


def test_load_registry_parses_collection_and_dedup(tmp_path):
    path = _write_registry(
        tmp_path,
        """
        sources:
          - name: agribalyse-products
            module: m.a
            target: sentier_vocab
            category: products
            fetch: { url: "file:///a.xlsx", format: xlsx }
            output_format: yaml
            collection:
              class: ProductCollection
              items_key: products
              scheme: https://vocab.sentier.dev/products/
              schema_file: product
            validate_against: Product
            dedup:
              on_existing: error
              check_existing: false
        """,
    )
    cfg = registry_mod.load_registry(path)[0]
    assert cfg.collection_class == "ProductCollection"
    assert cfg.collection_items_key == "products"
    assert cfg.collection_scheme == "https://vocab.sentier.dev/products/"
    assert cfg.schema_file == "product"
    assert cfg.dedup_on_existing == "error"
    assert cfg.dedup_check_existing is False


def test_collection_and_dedup_default_when_absent(tmp_path):
    path = _write_registry(
        tmp_path,
        """
        sources:
          - name: plain
            module: m.a
            target: sentier_inventory
            category: c
            fetch: { url: "file:///a", format: csv }
            output_format: json
        """,
    )
    cfg = registry_mod.load_registry(path)[0]
    assert cfg.collection_class is None
    assert cfg.collection_items_key is None
    assert cfg.collection_scheme is None
    assert cfg.schema_file is None
    assert cfg.dedup_on_existing == "skip"
    assert cfg.dedup_check_existing is True


def test_get_config_by_name(tmp_path):
    path = _write_registry(
        tmp_path,
        """
        sources:
          - name: a
            module: m.a
            target: sentier_inventory
            category: c
            fetch: { url: "file:///a", format: csv }
            output_format: json
          - name: b
            module: m.b
            target: sentier_inventory
            category: c
            fetch: { url: "file:///b", format: csv }
            output_format: yaml
        """,
    )
    configs = registry_mod.load_registry(path)
    assert registry_mod.get_config("b", configs).output_format == "yaml"
    with pytest.raises(RegistryError):
        registry_mod.get_config("missing", configs)


def test_malformed_entry_raises(tmp_path):
    path = _write_registry(
        tmp_path,
        """
        sources:
          - name: broken
            module: m.x
        """,
    )
    with pytest.raises(RegistryError):
        registry_mod.load_registry(path)


def test_load_source_instantiates_subclass(tmp_path):
    # Reuse the real example_csv plugin module created in Task 13.
    cfg = SourceConfig(
        name="example-csv",
        module="sentier_importers.sources.example_csv.source",
        target="sentier_inventory",
        category="example",
        fetch_url="unused",
        fetch_format="csv",
        output_format="json",
    )
    source = registry_mod.load_source(cfg)
    assert isinstance(source, Source)
    assert source.config.name == "example-csv"
