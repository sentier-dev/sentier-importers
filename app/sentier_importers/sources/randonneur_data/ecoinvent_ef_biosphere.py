"""ecoinvent biosphere → EF 3.1 elementary flows as a randonneur mapping package.

Source: ``ecoinvent-3.11-biosphere-EF-3.1-biosphere`` from Brightway's randonneur_data
(CC-BY-4.0, Chris Mutel with the UNEP GLAD mapping taskforce), pinned by commit in the
registry. Emits ``biosphere.json`` for the ``ecoinvent-3.11-biosphere__ef-3.1`` pair in
``sentier-mappings``.

These are the correspondences sentier-vocab carried as ``exact_match`` on the EF 3.1 flows
until the term and relation layers (sentier-vocab issue #34): a link to another identifier
space is a mappings bridge, not a vocabulary relation.

- The ecoinvent side is the elementary exchange UUID alone (``source.code``), the same
  opaque-code boundary sentier-mappings applies to every ecoinvent identifier.
- The EF side is the full open identifier: UUID, JRC name, context path and unit.
- A target that has no term in sentier-vocab says so in its ``comment``; it is still an
  EF 3.1 flow key (sentier-methods holds factors for flows the vocabulary does not list).
"""

import gzip
import json
from pathlib import Path

import pyarrow.parquet as pq
from sentier_importers.core import fetch as fetch_mod
from sentier_importers.core.context import RunContext
from sentier_importers.core.errors import ParseError, ValidationError
from sentier_importers.core.source import Source
from sentier_importers.core.types import RawData, Record, Records, Rows

#: Registry input naming sentier-vocab's ``terms/`` folder (a directory, read locally).
VOCAB_DIR = "ef_vocab"
FLOW_TYPE = "elementary-flow"
#: Comment of an entry whose EF target the vocabulary does not list.
NO_VOCAB_TERM = "target has no sentier-vocab term"
#: Upstream spellings of one unit: no conversion between the two sides.
_UNIT_ALIASES = {"m2*year": "m2*a"}


def vocab_flow_codes(terms_dir: Path) -> frozenset[str]:
    """Last IRI segment of every elementary-flow term under ``terms_dir``."""
    codes: set[str] = set()
    for path in sorted(Path(terms_dir).glob("*.parquet")):
        table = pq.read_table(path, columns=["iri"], filters=[("type", "==", FLOW_TYPE)])
        codes.update(iri.rsplit("/", 1)[-1] for iri in table.column("iri").to_pylist())
    return frozenset(codes)


def _unit(value: object) -> str:
    unit = str(value or "").strip()
    return _UNIT_ALIASES.get(unit, unit)


def _entry(record: Record, vocab_codes: frozenset[str]) -> Record:
    source, target = record.get("source") or {}, record.get("target") or {}
    code, target_code = source.get("uuid"), target.get("uuid")
    if not code or not target_code:
        raise ValidationError(f"upstream entry without a uuid on both sides: {record!r}")
    source_unit, target_unit = _unit(source.get("unit")), _unit(target.get("unit_name"))
    factor = record.get("conversion_factor")
    comments: list[str] = []
    if factor is not None:
        comments.append(
            f"source unit {source_unit}, target unit {target_unit}; "
            "conversion factor from the upstream mapping"
        )
    elif source_unit != target_unit:
        raise ValidationError(
            f"{code}: source unit {source_unit!r} and target unit {target_unit!r} differ "
            "without a conversion_factor"
        )
    elif record.get("comment"):
        comments.append(str(record["comment"]))
    if target_code not in vocab_codes:
        comments.append(NO_VOCAB_TERM)

    entry: Record = {
        "source": {"code": code},
        "target": {
            "code": target_code,
            "name": target.get("name"),
            "context": list(target.get("context") or []),
            "unit": target_unit,
        },
    }
    if factor is not None:
        entry["conversion_factor"] = float(factor)
    if comments:
        entry["comment"] = "; ".join(comments)
    return entry


class EcoinventEfBiosphereSource(Source):
    """Reshape the upstream ``replace`` entries into sentier-mappings entries."""

    def __init__(self, config) -> None:
        super().__init__(config)
        self._vocab_codes: frozenset[str] = frozenset()

    def fetch(self, ctx: RunContext) -> RawData:
        """Fetch the upstream package; ``ef_vocab`` is a directory, read in ``parse``."""
        return fetch_mod.fetch(self.config.fetch_url, ctx)

    def parse(self, raw: RawData) -> Records:
        """Gunzip the upstream package and return its ``replace`` entries."""
        terms_dir = fetch_mod.local_path(self.config.inputs.get(VOCAB_DIR), VOCAB_DIR)
        self._vocab_codes = vocab_flow_codes(terms_dir)
        try:
            return list(json.loads(gzip.decompress(raw.content))["replace"])
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise ParseError(
                f"{raw.source_url} is not a gzipped randonneur package: {exc!r}"
            ) from exc

    def transform(self, records: Records) -> Rows:
        rows: Rows = []
        seen: set[str] = set()
        for record in records:
            entry = _entry(record, self._vocab_codes)
            code = entry["source"]["code"]
            if code in seen:
                raise ValidationError(f"upstream maps ecoinvent flow {code} more than once")
            seen.add(code)
            rows.append(entry)
        return rows
