"""BAFU-2026 process terms -> ``sentier_vocab`` (processes).

One ``Process`` term per dataset, keyed on the dataset UUID. Obsolete datasets
are kept — live processes link into them — and marked in the definition.
"""

from sentier_importers.core.source import Source
from sentier_importers.core.types import RawData, Record, Records, Rows
from sentier_importers.sources.bafu import ecospold
from sentier_importers.sources.bafu.provenance import BAFU_PROVENANCE_IRI

#: Base of every published Sentier process IRI.
PROCESSES_SCHEME = "https://vocab.sentier.dev/processes/"
#: IRI path segment namespacing this source's ids.
SOURCE_PREFIX = "bafu"
#: Definition text is truncated to keep 11,947 terms within sane payload size.
_DEFINITION_MAX = 500


def process_iri(dataset_uuid: str) -> str:
    """IRI for a BAFU process, keyed on its dataset UUID."""
    return f"{PROCESSES_SCHEME}{SOURCE_PREFIX}/{dataset_uuid}"


def _definition(record: Record) -> str | None:
    text = " ".join((record["general_comment"] or "").split())[:_DEFINITION_MAX]
    if record["obsolete"]:
        return f"[obsolete] {text}".strip()
    return text or None


class BafuVocabProcessesSource(Source):
    """Map BAFU datasets into ``Process`` terms, one content-named file per sector.

    ``emit_filename`` is the sector slug (``agriculture`` … ``obsolete``): each
    registry entry emits only the datasets routing to that sector, so the
    delivered files are named by content, never by source. Without
    ``emit_filename`` the source emits every dataset.
    """

    def parse(self, raw: RawData) -> Records:
        return ecospold.parse_ecospold_zip(raw)

    def transform(self, records: Records) -> Rows:
        sector = self.config.emit_filename
        rows: Rows = []
        for record in records:
            if sector and ecospold.sector_slug(ecospold.sector_for(record["category"])) != sector:
                continue
            row: Record = {
                "iri": process_iri(record["uuid"]),
                "pref_label": record["name"],
                "notation": record["uuid"],
                "process_type": "unit",
                "source": BAFU_PROVENANCE_IRI,
                "status": "draft",
            }
            definition = _definition(record)
            if definition:
                row["definition"] = definition
            rows.append(row)
        return rows
