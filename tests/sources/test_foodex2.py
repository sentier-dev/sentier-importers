"""Tests for the FoodEx2 importer reading EFSA's primary MTX catalogue.

Covers: ``.ecf`` (zip) unwrapping + streaming XML parse, ``termType`` routing,
parity field mapping (labels, scope-note stripping, master-hierarchy broader, facet
``related``, legacy ``additional_notations``, status), and a full offline pipeline run
per category. The fixture is a tiny hand-built ``MTX.ecf`` — no network.
"""

import zipfile
from pathlib import Path

import pytest
import yaml
from sentier_importers.core import pipeline
from sentier_importers.core.context import RunContext
from sentier_importers.core.source import SourceConfig
from sentier_importers.sources.foodex2.provenance import PROVENANCE_IRI
from sentier_importers.sources.foodex2.source import Foodex2Source, _facet_value_codes

FIXTURES = Path(__file__).parent / "fixtures"
CONCEPT_SCHEMA = FIXTURES / "concept_collection.yaml"

ROOT = "https://vocab.sentier.dev"
PRODUCTS = f"{ROOT}/products/"

# Per-category collection wiring, so one helper can build any slice.
_CATEGORY = {
    "products": ("ProductCollection", "products", "product", "Product"),
    "organisms": ("OrganismCollection", "organisms", "organism", "Organism"),
    "qualifiers": ("QualifierCollection", "qualifiers", "qualifier", "Qualifier"),
}


# --- a tiny MTX.ecf fixture ----------------------------------------------------


def _attr(code: str, values: list[str]) -> str:
    vals = "".join(f"<attributeValue>{v}</attributeValue>" for v in values)
    return (
        f"<implicitAttribute><attributeCode>{code}</attributeCode>"
        f"<attributeValues>{vals}</attributeValues></implicitAttribute>"
    )


def _term(
    code,
    termtype,
    *,
    name=None,
    parent=None,
    short=None,
    scope=None,
    common=(),
    sci=(),
    facets=None,
    legacy=None,
    status="APPROVED",
) -> str:
    desc = f"<termCode>{code}</termCode>"
    if name is not None:
        desc += f"<termExtendedName>{name}</termExtendedName>"
    if short:
        desc += f"<termShortName>{short}</termShortName>"
    if scope:
        desc += f"<termScopeNote>{scope}</termScopeNote>"
    parent_code = parent or ""  # root terms carry an empty parentCode
    ha = (
        "<hierarchyAssignment><hierarchyCode>MTX</hierarchyCode>"
        f"<parentCode>{parent_code}</parentCode></hierarchyAssignment>"
    )
    attrs = [_attr("termType", [termtype])]
    if common:
        attrs.append(_attr("A02", list(common)))
    if sci:
        attrs.append(_attr("A01", list(sci)))
    if facets:
        attrs.append(_attr("allFacets", [facets]))
    for field, value in (legacy or {}).items():
        attrs.append(_attr(field, [value]))
    return (
        f"<term><termDesc>{desc}</termDesc>"
        f"<termVersion><status>{status}</status></termVersion>"
        f"<hierarchyAssignments>{ha}</hierarchyAssignments>"
        f"<implicitAttributes>{''.join(attrs)}</implicitAttributes></term>"
    )


MTX_XML = (
    '<?xml version="1.0" encoding="UTF-8"?><message><catalogue>'
    + _term("FOOD", "g", name="Food")  # products root
    + _term("FEED", "g", name="Feed")  # products root
    + _term(
        "A000A",
        "r",
        name="Teff grain",
        parent="FOOD",
        short="TEFF",
        scope="Cereal grains from the species Eragrostis tef (Zucc.) Trotter.£http://example.org/teff",
        common=["Tef grain", "Teff grain"],  # "Teff grain" == pref_label, dropped
        sci=["Eragrostis tef (Zucc.) Trotter"],
        facets="A000A#F01.ORG1$F28.FAC1$F27.A000A",  # F27 self-ref dropped
        legacy={"foodexOldCode": "A.01.000028", "GEMSCode": "GC0652"},
    )
    + _term("A001B", "d", name="Obsolete millet term", parent="FEED", status="DEPRECATED")
    + _term("NSRC", "n", name="Natural sources")  # organisms root
    + _term("ORG1", "n", name="Frog (as animal)", parent="NSRC")
    + _term("FACR", "f", name="Facets")  # qualifiers root
    + _term("FAC1", "f", name="Process", parent="FACR")
    + _term("A999Z", "r", parent="FOOD")  # no extended name -> skipped
    + "</catalogue></message>"
)


@pytest.fixture(scope="module")
def sample_ecf(tmp_path_factory) -> Path:
    path = tmp_path_factory.mktemp("mtx") / "MTX.ecf"
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("MTX.xml", MTX_XML.encode("utf-8"))
    return path


def _config(ecf: Path, category="products", **overrides) -> SourceConfig:
    cls, items_key, schema_file, validate = _CATEGORY[category]
    base = dict(
        name=f"foodex2-{category}",
        module="sentier_importers.sources.foodex2.source",
        target="sentier_vocab",
        category=category,
        fetch_url=f"file://{ecf}",
        fetch_format="ecf",
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


def _source(ecf: Path, category="products", **overrides) -> Foodex2Source:
    return Foodex2Source(_config(ecf, category, **overrides))


def _ctx(tmp_path, **kw) -> RunContext:
    return RunContext(cache_dir=tmp_path / "c", output_dir=tmp_path / "o", **kw)


def _rows(ecf, tmp_path, category="products"):
    src = _source(ecf, category)
    return src.transform(src.parse(src.fetch(_ctx(tmp_path))))


# --- parsing & routing --------------------------------------------------------


def test_parse_streams_terms_from_ecf(sample_ecf, tmp_path):
    src = _source(sample_ecf)
    records = src.parse(src.fetch(_ctx(tmp_path)))
    assert isinstance(records, list)
    assert {r["termCode"] for r in records} == {
        "FOOD",
        "FEED",
        "A000A",
        "A001B",
        "NSRC",
        "ORG1",
        "FACR",
        "FAC1",
        "A999Z",
    }


def test_classify_routes_by_termtype(sample_ecf, tmp_path):
    src = _source(sample_ecf)
    by_code = {t["termCode"]: t for t in src.parse(src.fetch(_ctx(tmp_path)))}
    assert src.classify(by_code["A000A"]) == "products"  # r
    assert src.classify(by_code["A001B"]) == "products"  # d
    assert src.classify(by_code["ORG1"]) == "organisms"  # n
    assert src.classify(by_code["FAC1"]) == "qualifiers"  # f


def test_each_block_emits_only_its_category(sample_ecf, tmp_path):
    assert {r["notation"] for r in _rows(sample_ecf, tmp_path, "products")} == {
        "FOOD",
        "A000A",
        "FEED",
        "A001B",
    }
    assert {r["notation"] for r in _rows(sample_ecf, tmp_path, "organisms")} == {"NSRC", "ORG1"}
    assert {r["notation"] for r in _rows(sample_ecf, tmp_path, "qualifiers")} == {"FACR", "FAC1"}


def test_malformed_term_without_label_is_skipped(sample_ecf, tmp_path):
    iris = {r["iri"] for r in _rows(sample_ecf, tmp_path)}
    assert f"{PRODUCTS}foodex2/A999Z" not in iris


# --- field mapping ------------------------------------------------------------


def test_iri_is_category_namespaced(sample_ecf, tmp_path):
    teff = next(r for r in _rows(sample_ecf, tmp_path) if r["notation"] == "A000A")
    assert teff["iri"] == f"{PRODUCTS}foodex2/A000A"
    frog = next(r for r in _rows(sample_ecf, tmp_path, "organisms") if r["notation"] == "ORG1")
    assert frog["iri"] == f"{ROOT}/organisms/foodex2/ORG1"


def test_definition_strips_scope_note_sources(sample_ecf, tmp_path):
    teff = next(r for r in _rows(sample_ecf, tmp_path) if r["notation"] == "A000A")
    assert teff["definition"] == "Cereal grains from the species Eragrostis tef (Zucc.) Trotter."


def test_alt_labels_merge_short_common_scientific_without_dupes(sample_ecf, tmp_path):
    teff = next(r for r in _rows(sample_ecf, tmp_path) if r["notation"] == "A000A")
    # shortName TEFF + commonNames + scientificNames; "Teff grain" == pref_label, dropped.
    assert teff["alt_labels"] == ["TEFF", "Tef grain", "Eragrostis tef (Zucc.) Trotter"]


def test_broader_minted_from_master_hierarchy(sample_ecf, tmp_path):
    teff = next(r for r in _rows(sample_ecf, tmp_path) if r["notation"] == "A000A")
    assert teff["broader"] == f"{PRODUCTS}foodex2/FOOD"


def test_root_term_has_no_broader(sample_ecf, tmp_path):
    food = next(r for r in _rows(sample_ecf, tmp_path) if r["notation"] == "FOOD")
    assert "broader" not in food


def test_facets_become_cross_category_related(sample_ecf, tmp_path):
    teff = next(r for r in _rows(sample_ecf, tmp_path) if r["notation"] == "A000A")
    # F01->ORG1 (organisms), F28->FAC1 (qualifiers); F27->A000A self-ref dropped.
    assert teff["related"] == [
        f"{ROOT}/organisms/foodex2/ORG1",
        f"{ROOT}/qualifiers/foodex2/FAC1",
    ]


def test_legacy_codes_preserved_as_additional_notations(sample_ecf, tmp_path):
    teff = next(r for r in _rows(sample_ecf, tmp_path) if r["notation"] == "A000A")
    assert teff["additional_notations"] == ["foodex1:A.01.000028", "gems:GC0652"]


def test_status_mapping(sample_ecf, tmp_path):
    rows = {r["notation"]: r for r in _rows(sample_ecf, tmp_path)}
    assert rows["A000A"]["status"] == "draft"  # APPROVED -> draft
    assert rows["A001B"]["status"] == "deprecated"  # DEPRECATED -> deprecated


def test_every_term_links_to_foodex2_source(sample_ecf, tmp_path):
    for category in _CATEGORY:
        rows = _rows(sample_ecf, tmp_path, category)
        assert rows and all(r["source"] == PROVENANCE_IRI for r in rows)


def test_facet_value_codes_parsing():
    assert _facet_value_codes("A0CTY#F01.A04TB$F02.A0EME$F27.A0CTY") == [
        "A04TB",
        "A0EME",
        "A0CTY",
    ]


# --- full pipeline ------------------------------------------------------------


def test_run_products_emits_valid_collection(sample_ecf, tmp_path, monkeypatch):
    monkeypatch.setattr(pipeline.schema_provider, "resolve_schema", lambda *a, **k: CONCEPT_SCHEMA)
    out = pipeline.run_source(_source(sample_ecf, "products"), _ctx(tmp_path))
    assert out == tmp_path / "o" / "sentier_vocab" / "products" / "foodex2-products.yaml"

    loaded = yaml.safe_load(out.read_text())
    assert loaded["scheme"] == PRODUCTS
    assert {p["pref_label"] for p in loaded["products"]} == {
        "Food",
        "Teff grain",
        "Feed",
        "Obsolete millet term",
    }
