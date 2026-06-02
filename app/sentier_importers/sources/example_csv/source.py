"""A minimal reference source.

Demonstrates the override hook: it reads a bundled CSV via the cached fetcher so the
pipeline runs fully offline (in CI). A typical real source would instead just declare
a ``fetch_url`` in ``registry.yaml`` and rely on the default ``fetch``.
"""

from pathlib import Path

from sentier_importers.core import fetch as fetch_mod
from sentier_importers.core.context import RunContext
from sentier_importers.core.source import Source
from sentier_importers.core.types import RawData, Records, Rows

_DATA = Path(__file__).with_name("data") / "example.csv"


class ExampleCsvSource(Source):
    """Normalize a small CSV of widgets into ``{id, label, region}`` rows."""

    def fetch(self, ctx: RunContext) -> RawData:
        return fetch_mod.fetch(f"file://{_DATA}", ctx)

    def transform(self, records: Records) -> Rows:
        return [
            {
                "id": record["id"].strip(),
                "label": record["name"].strip().title(),
                "region": record["region"].strip(),
            }
            for record in records
        ]
