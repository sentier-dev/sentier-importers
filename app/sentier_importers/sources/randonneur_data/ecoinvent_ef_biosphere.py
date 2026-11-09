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
import math
import zlib
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
from sentier_importers.core import fetch as fetch_mod
from sentier_importers.core.context import RunContext
from sentier_importers.core.errors import MissingInputError, ParseError, ValidationError
from sentier_importers.core.source import Source
from sentier_importers.core.types import RawData, Record, Records, Rows

#: Registry input naming sentier-vocab's ``terms/`` folder (a directory, read locally).
VOCAB_DIR = "ef_vocab"
FLOW_TYPE = "elementary-flow"
#: Comment of an entry whose EF target the vocabulary does not list.
NO_VOCAB_TERM = "target has no sentier-vocab term"
#: Upstream spellings of one unit, used to compare the two sides only.
_UNIT_ALIASES = {"m2*year": "m2*a"}


def vocab_flow_codes(terms_dir: Path) -> frozenset[str]:
    """Last IRI segment of every elementary-flow term under ``terms_dir``.

    An empty result would flag every entry as lacking a vocab term, so a folder
    without flow terms is a missing input, not an empty vocabulary.
    """
    codes: set[str] = set()
    for path in sorted(Path(terms_dir).glob("*.parquet")):
        try:
            table = pq.read_table(path, columns=["iri"], filters=[("type", "==", FLOW_TYPE)])
        except (pa.ArrowInvalid, KeyError) as exc:
            raise ParseError(f"{path} is not a sentier-vocab term file: {exc}") from exc
        codes.update(iri.rsplit("/", 1)[-1] for iri in table.column("iri").to_pylist())
    if not codes:
        raise MissingInputError(
            f"no elementary-flow term under {terms_dir}: {VOCAB_DIR} must be the terms/ "
            "folder of a sentier-vocab checkout",
            url=f"file://{terms_dir}",
        )
    return frozenset(codes)


def _same_unit(left: str, right: str) -> bool:
    return _UNIT_ALIASES.get(left, left) == _UNIT_ALIASES.get(right, right)


def _factor(value: object, code: str) -> float:
    try:
        factor = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        factor = math.nan
    if not math.isfinite(factor) or factor <= 0:
        raise ValidationError(f"{code}: conversion_factor {value!r} is not a positive number")
    return factor


def _entry(record: Record, vocab_codes: frozenset[str]) -> Record:
    source, target = record.get("source"), record.get("target")
    if not isinstance(source, dict) or not isinstance(target, dict):
        raise ValidationError(f"upstream entry without a source and a target: {record!r}")
    code, target_code = source.get("uuid"), target.get("uuid")
    if not code or not target_code:
        raise ValidationError(f"upstream entry without a uuid on both sides: {record!r}")
    name, context = target.get("name"), target.get("context")
    source_unit = str(source.get("unit") or "").strip()
    target_unit = str(target.get("unit_name") or "").strip()
    if not name or not source_unit or not target_unit or not isinstance(context, list):
        raise ValidationError(
            f"{code}: upstream entry needs a unit on both sides and a target name and context"
        )

    entry: Record = {
        "source": {"code": code},
        "target": {"code": target_code, "name": name, "context": context, "unit": target_unit},
    }
    comments: list[str] = []
    if record.get("conversion_factor") is not None:
        entry["conversion_factor"] = _factor(record["conversion_factor"], code)
        # The upstream comment of a converted entry quotes the ecoinvent flow name, which
        # this package does not carry; the two units are what a reader needs here.
        comments.append(
            f"source unit {source_unit}, target unit {target_unit}; "
            "conversion factor from the upstream mapping"
        )
    elif not _same_unit(source_unit, target_unit):
        raise ValidationError(
            f"{code}: source unit {source_unit!r} and target unit {target_unit!r} differ "
            "without a conversion_factor"
        )
    elif record.get("comment"):
        comments.append(str(record["comment"]))
    if target_code not in vocab_codes:
        comments.append(NO_VOCAB_TERM)
    if comments:
        entry["comment"] = "; ".join(comments)
    return entry


class EcoinventEfBiosphereSource(Source):
    """Reshape the upstream ``replace`` entries into sentier-mappings entries."""

    def _terms_dir(self) -> Path:
        return fetch_mod.local_path(self.config.inputs.get(VOCAB_DIR), VOCAB_DIR)

    def fetch(self, ctx: RunContext) -> RawData:
        """Fetch the upstream package; ``ef_vocab`` is a directory, read in ``transform``."""
        terms_dir = self._terms_dir()
        if not terms_dir.is_dir():
            raise MissingInputError(f"folder not found: {terms_dir}", url=f"file://{terms_dir}")
        return fetch_mod.fetch(self.config.fetch_url, ctx)

    def parse(self, raw: RawData) -> Records:
        """Gunzip the upstream package and return its ``replace`` entries."""
        try:
            entries = json.loads(gzip.decompress(raw.content))["replace"]
        except (OSError, EOFError, zlib.error, ValueError, KeyError, TypeError) as exc:
            raise ParseError(
                f"{raw.source_url} is not a gzipped randonneur package: {exc!r}"
            ) from exc
        if not isinstance(entries, list) or not all(isinstance(e, dict) for e in entries):
            raise ParseError(f"{raw.source_url}: 'replace' is not a list of entries")
        return entries

    def transform(self, records: Records) -> Rows:
        vocab_codes = vocab_flow_codes(self._terms_dir())
        rows: Rows = []
        seen: set[str] = set()
        for record in records:
            entry = _entry(record, vocab_codes)
            code = entry["source"]["code"]
            if code in seen:
                raise ValidationError(f"upstream maps ecoinvent flow {code} more than once")
            seen.add(code)
            rows.append(entry)
        return rows
