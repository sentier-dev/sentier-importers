"""BAFU-2026 elementary flows -> ``sentier_vocab`` (elementary-flows).

The distinct biosphere flows across all exchanges (name × category ×
subCategory), with the deterministic uuid5 id shared with the inventory
``exchanges.flow`` column. Dedup is ``on_existing: skip`` in the registry so
curated EF 3.1/ENVO terms are never clobbered.
"""

from sentier_importers.core.source import Source
from sentier_importers.core.types import RawData, Record, Records, Rows
from sentier_importers.sources.bafu import ecospold
from sentier_importers.sources.bafu.provenance import BAFU_PROVENANCE_IRI

#: Base of every published Sentier flow IRI.
FLOWS_SCHEME = "https://vocab.sentier.dev/flows/"


class BafuVocabFlowsSource(Source):
    """Map the distinct BAFU biosphere flows into ``ElementaryFlow`` terms."""

    def parse(self, raw: RawData) -> Records:
        return ecospold.parse_ecospold_zip(raw)

    def transform(self, records: Records) -> Rows:
        rows: Rows = []
        seen: set[str] = set()
        for record in records:
            for exchange in record["exchanges"]:
                if exchange["group_code"] != 4:  # FromNature / ToNature only
                    continue
                fid = ecospold.flow_id(
                    exchange["name"], exchange["category"], exchange["subcategory"]
                )
                if fid in seen:
                    continue
                seen.add(fid)
                row: Record = {
                    "iri": f"{FLOWS_SCHEME}{fid}",
                    "pref_label": exchange["name"],
                    "notation": fid,
                    "compartment": exchange["category"],
                    "sub_compartment": exchange["subcategory"],
                    "source": BAFU_PROVENANCE_IRI,
                    "status": "draft",
                }
                if exchange["cas"]:
                    row["cas_number"] = exchange["cas"]
                rows.append(row)
        return rows
