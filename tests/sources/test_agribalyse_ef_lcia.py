import io

import pyarrow as pa
import pyarrow.parquet as pq
from sentier_importers.core.source import SourceConfig
from sentier_importers.core.types import RawData
from sentier_importers.sources.agribalyse.cfs import AgribalyseEfCfsSource
from sentier_importers.sources.agribalyse.ef_common import flow_iri, method_id, unit_for
from sentier_importers.sources.agribalyse.methods import AgribalyseEfMethodsSource


def _raw() -> RawData:
    cols = {
        "FLOW_uuid": ["u1", "u2", "u1"],
        "FLOW_name": ["Carbon dioxide", "Methane", "Carbon dioxide"],
        "LCIAMethod_name": ["Climate change", "Climate change", "Acidification"],
        "CF EF3.1": [1.0, 28.0, 0.5],
        "LCIAMethod_location": [None, None, None],
        "FLOW_class0": ["Emissions", "Emissions", "Emissions"],
        "FLOW_class1": ["air", "air", "air"],
        "FLOW_class2": [None, None, None],
    }
    table = pa.table(
        {
            "FLOW_uuid": pa.array(cols["FLOW_uuid"], pa.string()),
            "FLOW_name": pa.array(cols["FLOW_name"], pa.string()),
            "LCIAMethod_name": pa.array(cols["LCIAMethod_name"], pa.string()),
            "CF EF3.1": pa.array(cols["CF EF3.1"], pa.float64()),
            "LCIAMethod_location": pa.array(cols["LCIAMethod_location"], pa.string()),
            "FLOW_class0": pa.array(cols["FLOW_class0"], pa.string()),
            "FLOW_class1": pa.array(cols["FLOW_class1"], pa.string()),
            "FLOW_class2": pa.array(cols["FLOW_class2"], pa.string()),
        }
    )
    buf = io.BytesIO()
    pq.write_table(table, buf)
    return RawData(content=buf.getvalue(), source_url="file://cf.parquet")


def _cfg(name):
    return SourceConfig(
        name=name,
        module=f"sentier_importers.sources.agribalyse.{name}",
        target="sentier_methods",
        category="01-ef-3.1",
        fetch_url="unused://",
        fetch_format="parquet",
        output_format="parquet",
    )


def test_methods_one_row_per_impact_category_with_units():
    src = AgribalyseEfMethodsSource(_cfg("methods"))
    rows = src.transform(src.parse(_raw()))
    assert {r["impact_category"] for r in rows} == {"Climate change", "Acidification"}
    cc = next(r for r in rows if r["impact_category"] == "Climate change")
    assert cc["method_id"] == method_id("Climate change")
    assert cc["method_name"] == "EF v3.1"
    assert cc["unit"] == "kg CO2 eq"
    assert cc["datasource"] == "ef-3.1"


def test_cfs_one_row_per_cf_keyed_to_flow_and_method():
    src = AgribalyseEfCfsSource(_cfg("characterization-factors"))
    rows = src.transform(src.parse(_raw()))
    assert len(rows) == 3
    ch4 = next(r for r in rows if r["flow_name"] == "Methane")
    assert ch4["method_id"] == method_id("Climate change")
    assert ch4["impact_category"] == "Climate change"
    assert ch4["flow"] == flow_iri("u2")
    assert ch4["factor_value"] == 28.0
    assert ch4["unit"] == unit_for("Climate change") == "kg CO2 eq"
    assert ch4["flow_context"] == "Emissions / air"
