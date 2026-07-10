import io

import pyarrow as pa
import pyarrow.parquet as pq
from sentier_importers.core.source import SourceConfig
from sentier_importers.core.types import RawData
from sentier_importers.sources.agribalyse.processes import AgribalyseProcessesSource, process_iri
from sentier_importers.sources.agribalyse.products import product_iri
from sentier_importers.sources.agribalyse.provenance import PROVENANCE_IRI  # noqa: F401


def _raw() -> RawData:
    cols = {
        "0": [None, None, "Code\nAGB", "11172", "6582"],
        "1": [None, None, "Code\nCIQUAL", "11172", "6582"],
        "2": [None, "x", "Groupe d'aliment", "aides", "viandes"],
        "3": [None, None, "Sous-groupe d'aliment", "aides", "viandes cuites"],
        "4": [None, None, "Nom du Produit en Français", "Court-bouillon", "Veau"],
        "5": [None, None, "LCI Name", "Aromatic stock cube", "Calf head"],
    }
    table = pa.table({k: pa.array(v, pa.string()) for k, v in cols.items()})
    buf = io.BytesIO()
    pq.write_table(table, buf)
    return RawData(content=buf.getvalue(), source_url="file://p.parquet")


def _config():
    return SourceConfig(
        name="agribalyse-processes",
        module="sentier_importers.sources.agribalyse.processes",
        target="sentier_vocab",
        category="processes",
        fetch_url="unused://",
        fetch_format="parquet",
        output_format="parquet",
        collection_class="ProcessCollection",
        collection_items_key="processes",
        collection_scheme="https://vocab.sentier.dev/processes/",
        schema_file="process",
        validate_against="Process",
    )


def test_processes_one_per_agb_code_linked_to_product():
    src = AgribalyseProcessesSource(_config())
    rows = src.transform(src.parse(_raw()))
    assert [r["iri"] for r in rows] == [process_iri("11172"), process_iri("6582")]

    p = rows[0]
    assert p["pref_label"] == "Aromatic stock cube"  # prefers LCI Name
    assert p["process_type"] == "lci_result"
    assert p["related"] == [product_iri("11172")]
    assert p["additional_notations"] == ["agb:11172"]
    assert p["alt_labels"] == ["Court-bouillon"]  # French name as alt
