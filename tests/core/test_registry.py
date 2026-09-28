import re
import textwrap
from pathlib import Path

import pytest
from sentier_importers.core import registry as registry_mod
from sentier_importers.core.errors import RegistryError
from sentier_importers.core.source import Source, SourceConfig
from sentier_importers.sources.bafu.packages import CURATED, INFERRED, MATCHED, NOMENCLATURE

#: Folder = pair convention (sentier-mappings 2026-09-14 restructure): every
#: sentier_mappings ``category`` must be ``<source>__<target>``, lower-kebab,
#: version-suffixed datasource ids, no numeric rank prefix.
_PAIR_CATEGORY_RE = re.compile(r"^(?!\d)[a-z0-9][a-z0-9.-]*__(?!\d)[a-z0-9][a-z0-9.-]*$")


def test_pair_category_pattern_rejects_the_old_rank_prefixed_spelling():
    assert _PAIR_CATEGORY_RE.match("bafu-2026-v1__ef-3.1")
    assert _PAIR_CATEGORY_RE.match("agribalyse-3.2__ecoinvent-3.9.1")
    assert not _PAIR_CATEGORY_RE.match("03-bafu-2026-v1__ef-3.1")
    assert not _PAIR_CATEGORY_RE.match("bafu-2026-v1__08-ef-3.1")
    assert not _PAIR_CATEGORY_RE.match("-__-")


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


def test_load_registry_parses_named_inputs(tmp_path):
    path = _write_registry(
        tmp_path,
        """
        sources:
          - name: demo
            module: sentier_importers.sources.example_csv.source
            target: sentier_mappings
            category: demo
            fetch:
              url: "file:///tmp/primary.json"
              format: json
            inputs:
              curated: "file:///tmp/curated.json"
              ecospold: "file:///tmp/bafu.zip"
            output_format: json
        """,
    )
    cfg = registry_mod.load_registry(path)[0]
    assert cfg.inputs == {
        "curated": "file:///tmp/curated.json",
        "ecospold": "file:///tmp/bafu.zip",
    }


def test_sentier_mappings_categories_are_pair_folder_names():
    # sentier-mappings 2026-09-14: no more global numeric rank prefix. Every
    # sentier_mappings entry's category must be exactly the pair folder name
    # (``<source>__<target>``), never a ``<NN>-`` prefixed one.
    mappings = [c for c in registry_mod.load_registry() if c.target == "sentier_mappings"]
    assert mappings, "expected at least one sentier_mappings source in the registry"
    for cfg in mappings:
        assert _PAIR_CATEGORY_RE.match(
            cfg.category
        ), f"{cfg.name!r} category {cfg.category!r} is not a pair-folder name"


def test_bafu_pair_emit_filenames_are_the_four_numbered_packages_plus_sidecars():
    # data/bafu-2026-v1__ef-3.1/ holds exactly biosphere-1-curated.json,
    # biosphere-2-inferred.json, biosphere-3-matched.json, biosphere-4-nomenclature.json,
    # plus the coverage.json and inference_review.json sidecars -- no bare "biosphere"
    # or rank-numbered name survives the restructure.
    bafu_pair = [
        c
        for c in registry_mod.load_registry()
        if c.target == "sentier_mappings" and c.category == "bafu-2026-v1__ef-3.1"
    ]
    assert {c.emit_filename for c in bafu_pair} == {
        CURATED,
        INFERRED,
        MATCHED,
        NOMENCLATURE,
        "coverage",
        "inference_review",
    }


# --- configurable data root (issue #12, spec 2026-09-28) --------------------


def _root_registry(tmp_path, access_line=""):
    return _write_registry(
        tmp_path,
        f"""
        sources:
          - name: rooted
            module: sentier_importers.sources.example_csv.source
            target: sentier_inventory
            category: demo
            {access_line}
            fetch:
              url: "file://${{SENTIER_DATA_ROOT}}/sub/main.csv"
              format: csv
            inputs:
              side: "file://${{SENTIER_DATA_ROOT}}/other/side.json"
              plain: "file:///abs/untouched.json"
            output_format: json
        """,
    )


def test_placeholder_expands_in_fetch_url_and_every_input(tmp_path):
    cfg = registry_mod.load_registry(_root_registry(tmp_path), data_root=Path("/data"))[0]
    assert cfg.fetch_url == "file:///data/sub/main.csv"
    assert cfg.inputs == {
        "side": "file:///data/other/side.json",
        "plain": "file:///abs/untouched.json",
    }


def test_unknown_placeholder_is_a_registry_error_naming_source_and_token(tmp_path):
    path = _write_registry(
        tmp_path,
        """
        sources:
          - name: typo
            module: sentier_importers.sources.example_csv.source
            target: sentier_inventory
            category: demo
            fetch:
              url: "file://${SENTIER_DATA_ROT}/x.csv"
              format: csv
            output_format: json
        """,
    )
    with pytest.raises(RegistryError, match=r"typo.*\$\{SENTIER_DATA_ROT\}"):
        registry_mod.load_registry(path, data_root=Path("/data"))


def test_resolve_data_root_precedence(tmp_path, monkeypatch):
    monkeypatch.setenv(registry_mod.DATA_ROOT_VAR, str(tmp_path / "env"))
    assert registry_mod.resolve_data_root(tmp_path / "flag") == (tmp_path / "flag").resolve()
    assert registry_mod.resolve_data_root() == (tmp_path / "env").resolve()
    monkeypatch.delenv(registry_mod.DATA_ROOT_VAR)
    assert registry_mod.resolve_data_root() == registry_mod.DEFAULT_DATA_ROOT


def test_default_data_root_is_the_checkout_parent():
    checkout = registry_mod.REGISTRY_PATH.parents[2]
    assert (checkout / "pyproject.toml").is_file()
    assert registry_mod.DEFAULT_DATA_ROOT == checkout.parent


def test_env_root_is_expanded_from_tilde(monkeypatch):
    monkeypatch.setenv(registry_mod.DATA_ROOT_VAR, "~/dds-elsewhere")
    assert registry_mod.resolve_data_root() == Path("~/dds-elsewhere").expanduser().resolve()


def test_access_defaults_to_public_and_rejects_unknown_values(tmp_path):
    assert (
        registry_mod.load_registry(_root_registry(tmp_path), data_root=Path("/d"))[0].access
        == "public"
    )
    cfg = registry_mod.load_registry(
        _root_registry(tmp_path, "access: licensed"), data_root=Path("/d")
    )[0]
    assert cfg.access == "licensed"
    with pytest.raises(RegistryError, match="access"):
        registry_mod.load_registry(
            _root_registry(tmp_path, "access: secret"), data_root=Path("/d")
        )


def test_real_registry_has_no_machine_paths():
    text = registry_mod.REGISTRY_PATH.read_text(encoding="utf-8")
    assert "/home/" not in text
    for url in re.findall(r'"(file://[^"]*)"', text):
        assert url.startswith("file://${SENTIER_DATA_ROOT}/"), url


def test_real_registry_access_matches_input_origin():
    for cfg in registry_mod.load_registry(data_root=Path("/root")):
        urls = [cfg.fetch_url, *cfg.inputs.values()]
        if any("/dds-carbonminds-data/" in u or "/dds-agribalyse/" in u for u in urls):
            assert cfg.access == "private", cfg.name
        elif any("/root/sources/" in u for u in urls):
            assert cfg.access == "licensed", cfg.name
        else:
            assert cfg.access == "public", cfg.name
