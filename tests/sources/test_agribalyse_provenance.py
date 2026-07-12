from sentier_importers.core.source import SourceConfig
from sentier_importers.sources.agribalyse.provenance import (
    PROVENANCE_IRI,
    AgribalyseEf31Provenance,
)


def _config():
    return SourceConfig(
        name="agribalyse-ef31-source",
        module="sentier_importers.sources.agribalyse.provenance",
        target="sentier_vocab",
        category="sources",
        fetch_url="static://agribalyse-ef31-source",
        fetch_format="json",
        output_format="yaml",
        collection_class="SourceCollection",
        collection_items_key="sources",
        collection_scheme="https://vocab.sentier.dev/sources/",
        schema_file="source",
        validate_against="Source",
    )


def test_provenance_emits_single_ef31_source_row():
    rows = AgribalyseEf31Provenance(_config()).transform([])
    assert len(rows) == 1
    row = rows[0]
    assert row["iri"] == PROVENANCE_IRI == "https://vocab.sentier.dev/sources/ef-3.1"
    assert row["pref_label"] == "Environmental Footprint 3.1"
    assert row["publisher"] == "European Commission, Joint Research Centre"
    assert row["status"] == "published"
    assert "EF" in row["citation"]
