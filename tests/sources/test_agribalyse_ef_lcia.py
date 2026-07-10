import io

import pyarrow as pa
import pyarrow.parquet as pq
from sentier_importers.core.source import SourceConfig
from sentier_importers.core.types import RawData
from sentier_importers.sources.agribalyse.cfs import AgribalyseEfCfsSource
from sentier_importers.sources.agribalyse.ef_common import METHOD_IRI, cf_iri, flow_iri, impact_iri
from sentier_importers.sources.agribalyse.impact_categories import (
    AgribalyseEfImpactCategoriesSource,
)
from sentier_importers.sources.agribalyse.methods import AgribalyseEfMethodsSource


def _raw() -> RawData:
    cols = {
        "FLOW_uuid": ["u1", "u2", "u1"],
        "LCIAMethod_name": ["Climate change", "Climate change", "Acidification"],
        "CF EF3.1": [1.0, 2.0, 0.5],
    }
    table = pa.table(
        {
            "FLOW_uuid": pa.array(cols["FLOW_uuid"], pa.string()),
            "LCIAMethod_name": pa.array(cols["LCIAMethod_name"], pa.string()),
            "CF EF3.1": pa.array(cols["CF EF3.1"], pa.float64()),
        }
    )
    buf = io.BytesIO()
    pq.write_table(table, buf)
    return RawData(content=buf.getvalue(), source_url="file://cf.parquet")


def _cfg(name, category, cls, items_key, scheme, schema_file, validate):
    return SourceConfig(
        name=name,
        module=f"sentier_importers.sources.agribalyse.{name}",
        target="sentier_vocab",
        category=category,
        fetch_url="unused://",
        fetch_format="parquet",
        output_format="parquet",
        collection_class=cls,
        collection_items_key=items_key,
        collection_scheme=scheme,
        schema_file=schema_file,
        validate_against=validate,
        dedup_check_existing=False,
    )


def test_methods_emits_one_method_linking_impact_categories():
    cfg = _cfg(
        "methods",
        "lcia-methods",
        "LCIAMethodCollection",
        "lcia_methods",
        "https://vocab.sentier.dev/lcia-methods/",
        "lcia-method",
        "LCIAMethod",
    )
    src = AgribalyseEfMethodsSource(cfg)
    rows = src.transform(src.parse(_raw()))
    assert len(rows) == 1
    assert rows[0]["iri"] == METHOD_IRI
    assert set(rows[0]["impact_categories"]) == {
        impact_iri("Climate change"),
        impact_iri("Acidification"),
    }


def test_impact_categories_one_per_distinct_method_name():
    cfg = _cfg(
        "impact_categories",
        "impact-categories",
        "ImpactCategoryCollection",
        "impact_categories",
        "https://vocab.sentier.dev/impact-categories/",
        "impact-category",
        "ImpactCategory",
    )
    src = AgribalyseEfImpactCategoriesSource(cfg)
    rows = src.transform(src.parse(_raw()))
    assert {r["pref_label"] for r in rows} == {"Climate change", "Acidification"}
    assert {r["iri"] for r in rows} == {
        impact_iri("Climate change"),
        impact_iri("Acidification"),
    }


def test_cfs_emit_one_per_row_keyed_to_flows():
    cfg = _cfg(
        "cfs",
        "characterization-factors",
        "CharacterizationFactorCollection",
        "characterization_factors",
        "https://vocab.sentier.dev/characterization-factors/",
        "characterization-factor",
        "CharacterizationFactor",
    )
    src = AgribalyseEfCfsSource(cfg)
    rows = src.transform(src.parse(_raw()))
    assert len(rows) == 3
    first = next(r for r in rows if r["iri"] == cf_iri("Climate change", "u1"))
    assert first["method"] == METHOD_IRI
    assert first["impact_category"] == impact_iri("Climate change")
    assert first["flow"] == flow_iri("u1")
    assert first["factor_value"] == 1.0
