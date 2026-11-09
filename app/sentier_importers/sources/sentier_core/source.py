"""The Sentier-curated terms -> ``sentier_vocab`` (release ``sentier-core``).

Units, unit groups, flow properties, model terms, sources, contacts and the few pilot
terms Sentier.dev curates itself are authored in ``data/seed.yaml``, one list of flat
rows per term type, and delivered like any other source release. sentier-vocab holds
parquet only, so a change to one of these terms is an edit of the seed and a new
delivery.
"""

from pathlib import Path

import yaml
from sentier_importers.core.context import RunContext
from sentier_importers.core.errors import ParseError
from sentier_importers.core.source import Source
from sentier_importers.core.types import RawData, Records, Rows

SEED = Path(__file__).with_name("data") / "seed.yaml"


class SentierCoreSource(Source):
    """Emit every seed row with its term type in a ``type`` slot (``term_type: per-row``)."""

    def fetch(self, ctx: RunContext) -> RawData:
        # Read from disk, not through the URL-keyed fetch cache: an edited seed must
        # never be served from an older cached copy.
        return RawData(content=SEED.read_bytes(), source_url=f"file://{SEED}")

    def parse(self, raw: RawData) -> Records:
        seed = yaml.safe_load(raw.content)
        if not isinstance(seed, dict) or not all(isinstance(v, list) for v in seed.values()):
            raise ParseError(f"{raw.source_url}: expected one list of rows per term type")
        return [{**row, "type": term_type} for term_type, rows in seed.items() for row in rows]

    def transform(self, records: Records) -> Rows:
        return list(records)
