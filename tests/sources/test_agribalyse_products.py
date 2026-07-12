import io

import pyarrow as pa
import pyarrow.parquet as pq
from sentier_importers.core.source import SourceConfig
from sentier_importers.core.types import RawData
from sentier_importers.sources.agribalyse.agribalyse_provenance import (
    AGRIBALYSE_PROVENANCE_IRI,
    AgribalyseProvenance,
)
from sentier_importers.sources.agribalyse.products import (
    AgribalyseProductsSource,
    group_iri,
    product_iri,
    subgroup_iri,
)


def _raw_parquet() -> RawData:
    # Mimic the real file: 6 columns "0".."5", a 2-row preamble, header at row 2, data from row 3.
    cols = {
        "0": [None, None, "Code\nAGB", "11172", "6582"],
        "1": [None, None, "Code\nCIQUAL", "11172", "6582"],
        "2": [
            None,
            "preamble",
            "Groupe d'aliment",
            "aides culinaires et ingrédients divers",
            "viandes, œufs, poissons",
        ],
        "3": [None, None, "Sous-groupe d'aliment", "aides culinaires", "viandes cuites"],
        "4": [
            None,
            None,
            "Nom du Produit en Français",
            "Court-bouillon pour poissons",
            "Veau, tête, bouillie",
        ],
        "5": [None, None, "LCI Name", "Aromatic stock cube", "Calf head boiled"],
    }
    table = pa.table({k: pa.array(v, type=pa.string()) for k, v in cols.items()})
    buf = io.BytesIO()
    pq.write_table(table, buf)
    return RawData(content=buf.getvalue(), source_url="file://test.parquet")


def _config():
    return SourceConfig(
        name="agribalyse-products",
        module="sentier_importers.sources.agribalyse.products",
        target="sentier_vocab",
        category="products",
        fetch_url="unused://",
        fetch_format="parquet",
        output_format="parquet",
        collection_class="ProductCollection",
        collection_items_key="products",
        collection_scheme="https://vocab.sentier.dev/products/",
        schema_file="product",
        validate_against="Product",
        dedup_check_existing=False,
    )


def test_parse_skips_preamble_and_names_fields():
    records = AgribalyseProductsSource(_config()).parse(_raw_parquet())
    assert [r["agb"] for r in records] == ["11172", "6582"]
    assert records[0]["group"] == "aides culinaires et ingrédients divers"
    assert records[0]["name_fr"] == "Court-bouillon pour poissons"


def test_transform_builds_group_subgroup_product_hierarchy():
    rows = AgribalyseProductsSource(_config()).transform(
        AgribalyseProductsSource(_config()).parse(_raw_parquet())
    )
    by_iri = {r["iri"]: r for r in rows}

    prod = by_iri[product_iri("11172")]
    assert prod["pref_label"] == "Court-bouillon pour poissons"
    assert prod["notation"] == "11172"
    assert prod["alt_labels"] == ["Aromatic stock cube"]
    assert prod["additional_notations"] == ["agb:11172"]
    assert prod["source"] == AGRIBALYSE_PROVENANCE_IRI

    sub = by_iri[subgroup_iri("aides culinaires")]
    assert sub["broader"] == group_iri("aides culinaires et ingrédients divers")
    assert prod["broader"] == subgroup_iri("aides culinaires")

    grp = by_iri[group_iri("aides culinaires et ingrédients divers")]
    assert "broader" not in grp
    # 2 groups + 2 subgroups + 2 products, all unique IRIs
    assert len(rows) == 6
    assert len({r["iri"] for r in rows}) == 6


def test_provenance_emits_single_agribalyse_source():
    rows = AgribalyseProvenance(_config()).transform([])
    assert len(rows) == 1
    assert rows[0]["iri"] == "https://vocab.sentier.dev/sources/agribalyse-3.2"
    assert rows[0]["publisher"] == "ADEME"
    assert rows[0]["status"] == "published"
