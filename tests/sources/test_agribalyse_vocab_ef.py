import io

import pyarrow as pa
import pyarrow.parquet as pq
from sentier_importers.core.source import SourceConfig
from sentier_importers.core.types import RawData
from sentier_importers.sources.agribalyse.ef_common import (
    EF31_SOURCE_IRI,
    VOCAB_METHOD_IRI,
    vocab_impact_iri,
)
from sentier_importers.sources.agribalyse.vocab_impact_categories import (
    AgribalyseEfImpactCategoriesSource,
)
from sentier_importers.sources.agribalyse.vocab_lcia_methods import AgribalyseEfLciaMethodSource


def _raw() -> RawData:
    cols = {
        "FLOW_uuid": ["u1", "u2"],
        "FLOW_name": ["CO2", "CH4"],
        "LCIAMethod_name": ["Climate change", "Acidification"],
        "CF EF3.1": [1.0, 0.5],
        "LCIAMethod_location": [None, None],
        "FLOW_class0": ["Emissions", "Emissions"],
        "FLOW_class1": ["air", "air"],
        "FLOW_class2": [None, None],
    }
    table = pa.table(
        {k: pa.array(v, pa.float64() if k == "CF EF3.1" else pa.string()) for k, v in cols.items()}
    )
    buf = io.BytesIO()
    pq.write_table(table, buf)
    return RawData(content=buf.getvalue(), source_url="file://cf.parquet")


def _cfg(name):
    return SourceConfig(
        name=name,
        module=f"sentier_importers.sources.agribalyse.{name}",
        target="sentier_vocab",
        category="x",
        fetch_url="unused://",
        fetch_format="parquet",
        output_format="yaml",
    )


def test_lcia_method_term_links_impact_categories():
    src = AgribalyseEfLciaMethodSource(_cfg("vocab_lcia_methods"))
    rows = src.transform(src.parse(_raw()))
    assert len(rows) == 1
    assert rows[0]["iri"] == VOCAB_METHOD_IRI
    assert set(rows[0]["impact_categories"]) == {
        vocab_impact_iri("Climate change"),
        vocab_impact_iri("Acidification"),
    }
    assert rows[0]["source"] == EF31_SOURCE_IRI


def test_impact_category_terms_one_per_name():
    src = AgribalyseEfImpactCategoriesSource(_cfg("vocab_impact_categories"))
    rows = src.transform(src.parse(_raw()))
    assert {r["pref_label"] for r in rows} == {"Climate change", "Acidification"}
    cc = next(r for r in rows if r["pref_label"] == "Climate change")
    assert cc["iri"] == vocab_impact_iri("Climate change")
    assert cc["method"] == VOCAB_METHOD_IRI
