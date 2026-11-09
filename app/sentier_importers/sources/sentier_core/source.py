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
from yaml.constructor import ConstructorError

SEED = Path(__file__).with_name("data") / "seed.yaml"


class _UniqueKeyLoader(yaml.SafeLoader):
    """A ``SafeLoader`` that refuses a repeated mapping key.

    PyYAML keeps the last of two equal keys, so a second ``unit:`` block further down
    the seed would silently drop every row of the first.
    """

    def construct_mapping(self, node, deep=False):
        seen = set()
        for key_node, _ in node.value:
            key = self.construct_object(key_node, deep=deep)
            if key in seen:
                raise ConstructorError(None, None, f"duplicate key {key!r}", key_node.start_mark)
            seen.add(key)
        return super().construct_mapping(node, deep=deep)


class SentierCoreSource(Source):
    """Emit every seed row with its term type in a ``type`` slot (``term_type: per-row``)."""

    def fetch(self, ctx: RunContext) -> RawData:
        # Read from disk, not through the URL-keyed fetch cache: an edited seed must
        # never be served from an older cached copy.
        return RawData(content=SEED.read_bytes(), source_url=f"file://{SEED}")

    def parse(self, raw: RawData) -> Records:
        try:
            seed = yaml.load(raw.content, Loader=_UniqueKeyLoader)  # noqa: S506
        except (yaml.YAMLError, TypeError) as exc:
            raise ParseError(f"{raw.source_url}: {exc}") from exc
        if not isinstance(seed, dict) or not all(isinstance(v, list) for v in seed.values()):
            raise ParseError(f"{raw.source_url}: expected one list of rows per term type")
        records: Records = []
        for term_type, rows in seed.items():
            for row in rows:
                if not isinstance(row, dict):
                    raise ParseError(
                        f"{raw.source_url}: a row under {term_type!r} is not a mapping: {row!r}"
                    )
                if row.get("type", term_type) != term_type:
                    raise ParseError(
                        f"{raw.source_url}: row {row.get('iri')!r} sits under {term_type!r} "
                        f"but says type {row['type']!r}"
                    )
                records.append({**row, "type": term_type})
        return records

    def transform(self, records: Records) -> Rows:
        return list(records)
