"""Tests for the FoodEx2 esfc importer: multi-category routing, field mapping,
cross-category links, and a full offline pipeline run for each category."""

from pathlib import Path

import yaml
from sentier_importers.core import pipeline
from sentier_importers.core.context import RunContext
from sentier_importers.core.source import SourceConfig
from sentier_importers.sources.foodex2.provenance import PROVENANCE_IRI
from sentier_importers.sources.foodex2.source import Foodex2Source

FIXTURES = Path(__file__).parent / "fixtures"
SAMPLE = FIXTURES / "foodex2-sample.json"
CONCEPT_SCHEMA = FIXTURES / "concept_collection.yaml"

ROOT = "https://vocab.sentier.dev"
PRODUCTS = f"{ROOT}/products/"

# Per-category collection wiring, so one helper can build any slice.
_CATEGORY = {
    "products": ("ProductCollection", "products", "product", "Product"),
    "organisms": ("OrganismCollection", "organisms", "organism", "Organism"),
    "qualifiers": ("QualifierCollection", "qualifiers", "qualifier", "Qualifier"),
}


def _config(category="products", **overrides) -> SourceConfig:
    cls, items_key, schema_file, validate = _CATEGORY[category]
    base = dict(
        name=f"foodex2-{category}",
        module="sentier_importers.sources.foodex2.source",
        target="sentier_vocab",
        category=category,
        fetch_url=f"file://{SAMPLE}",
        fetch_format="json",
        output_format="yaml",
        collection_class=cls,
        collection_items_key=items_key,
        collection_scheme=f"{ROOT}/{category}/",
        schema_file=schema_file,
        validate_against=validate,
        dedup_check_existing=False,  # Layer B needs network; off for unit tests
    )
    base.update(overrides)
    return SourceConfig(**base)


def _source(category="products", **overrides) -> Foodex2Source:
    return Foodex2Source(_config(category, **overrides))


def _ctx(tmp_path, **kw) -> RunContext:
    return RunContext(cache_dir=tmp_path / "c", output_dir=tmp_path / "o", **kw)


def _rows(tmp_path, category="products"):
    src = _source(category)
    return src.transform(src.parse(src.fetch(_ctx(tmp_path))))


# --- parsing & routing --------------------------------------------------------


def test_parse_unwraps_terms_envelope(tmp_path):
    records = _source().parse(_source().fetch(_ctx(tmp_path)))
    assert isinstance(records, list)
    assert {r["id"] for r in records} == {
        "FOOD",
        "A000A",
        "FEED",
        "A001B",
        "NSRC",
        "ORG1",
        "FACR",
        "FAC1",
        "A999Z",
    }


def test_classify_routes_by_list(tmp_path):
    src = _source()
    by_id = {t["id"]: t for t in src.parse(src.fetch(_ctx(tmp_path)))}
    assert src.classify(by_id["A000A"]) == "products"  # Food
    assert src.classify(by_id["A001B"]) == "products"  # Feed
    assert src.classify(by_id["ORG1"]) == "organisms"  # Natural sources
    assert src.classify(by_id["FAC1"]) == "qualifiers"  # Facets


def test_each_block_emits_only_its_category(tmp_path):
    products = {r["notation"] for r in _rows(tmp_path, "products")}
    assert products == {"FOOD", "A000A", "FEED", "A001B"}
    assert {r["notation"] for r in _rows(tmp_path, "organisms")} == {"NSRC", "ORG1"}
    assert {r["notation"] for r in _rows(tmp_path, "qualifiers")} == {"FACR", "FAC1"}


def test_malformed_term_without_label_is_skipped(tmp_path):
    iris = {r["iri"] for r in _rows(tmp_path)}
    assert f"{PRODUCTS}foodex2/A999Z" not in iris


# --- field mapping ------------------------------------------------------------


def test_iri_is_category_namespaced(tmp_path):
    teff = next(r for r in _rows(tmp_path) if r["notation"] == "A000A")
    assert teff["iri"] == f"{PRODUCTS}foodex2/A000A"
    frog = next(r for r in _rows(tmp_path, "organisms") if r["notation"] == "ORG1")
    assert frog["iri"] == f"{ROOT}/organisms/foodex2/ORG1"


def test_alt_labels_merge_common_and_scientific_names_without_dupes(tmp_path):
    teff = next(r for r in _rows(tmp_path) if r["notation"] == "A000A")
    # shortName TEFF + commonNames + scientificNames; "Teff grain" == pref_label, dropped.
    assert teff["alt_labels"] == ["TEFF", "Tef grain", "Eragrostis tef (Zucc.) Trotter"]


def test_broader_minted_from_hierarchy_code(tmp_path):
    teff = next(r for r in _rows(tmp_path) if r["notation"] == "A000A")
    assert teff["broader"] == f"{PRODUCTS}foodex2/FOOD"  # Z0001.0001.0001 -> Z0001.0001


def test_root_term_has_no_broader(tmp_path):
    food = next(r for r in _rows(tmp_path) if r["notation"] == "FOOD")
    assert "broader" not in food


def test_facets_become_cross_category_related(tmp_path):
    teff = next(r for r in _rows(tmp_path) if r["notation"] == "A000A")
    # F01->ORG1 (organisms), F28->FAC1 (qualifiers); F27->A000A self-ref dropped.
    assert teff["related"] == [
        f"{ROOT}/organisms/foodex2/ORG1",
        f"{ROOT}/qualifiers/foodex2/FAC1",
    ]


def test_legacy_codes_preserved_as_additional_notations(tmp_path):
    teff = next(r for r in _rows(tmp_path) if r["notation"] == "A000A")
    assert teff["additional_notations"] == ["foodex1:A.01.000028", "gems:GC0652"]  # matrix empty


def test_status_mapping(tmp_path):
    rows = {r["notation"]: r for r in _rows(tmp_path)}
    assert rows["A000A"]["status"] == "draft"  # active -> draft
    assert rows["A001B"]["status"] == "deprecated"


def test_every_term_links_to_foodex2_source(tmp_path):
    for category in _CATEGORY:
        rows = _rows(tmp_path, category)
        assert rows and all(r["source"] == PROVENANCE_IRI for r in rows)


# --- full pipeline ------------------------------------------------------------


def test_run_products_emits_valid_collection(tmp_path, monkeypatch):
    monkeypatch.setattr(pipeline.schema_provider, "resolve_schema", lambda *a, **k: CONCEPT_SCHEMA)
    out = pipeline.run_source(_source("products"), _ctx(tmp_path))
    assert out == tmp_path / "o" / "sentier_vocab" / "products" / "foodex2-products.yaml"

    loaded = yaml.safe_load(out.read_text())
    assert loaded["scheme"] == PRODUCTS
    assert {p["pref_label"] for p in loaded["products"]} == {
        "Food",
        "Teff grain",
        "Feed",
        "Obsolete millet term",
    }
