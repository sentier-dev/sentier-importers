"""bafu-2026-v1 -> EF 3.1 CF keys inferred from Eaternity's biosphere3 pair family.

Primary input: the rank-4 ``ecoinvent-biosphere3 -> eaternity-bafu-ext`` package
(Eaternity, sentier-mappings PR #7). Named inputs:

- ``rank3``: the rank-3 ``bafu-2026-v1 -> ef-3.1`` package, whose source codes are
  excluded (rank 3 keeps precedence; this bridge only fills its gaps);
- ``ecospold``: the BAFU-2026 v1 ecoSpold zip, the flow-identity authority;
- ``ef_flows``: sentier-methods' EF 3.1 CF table, for EF flow names and contexts;
- ``method_cfs``: a *directory* of ``<method>/cfs.parquet`` tables (dds-carbonminds-data
  layout) holding the EF v3.1 factors matched onto both flow universes. Read from
  the local path directly, not through the fetch cache.

Emits the ``biosphere.json`` of the rank-6 ``06-bafu-2026-v1__ef-3.1`` bridge. The
sibling :mod:`inference_review` source emits the withheld pairs. See ``inference.py``.
"""

from __future__ import annotations

import io

import orjson
import pyarrow.parquet as pq
from sentier_importers.core import fetch as fetch_mod
from sentier_importers.core.context import RunContext
from sentier_importers.core.source import Source
from sentier_importers.core.types import RawData, Records, Rows
from sentier_importers.sources.bafu.ecospold import parse_ecospold_zip
from sentier_importers.sources.bafu.mappings_biosphere_matched import codes_of
from sentier_importers.sources.eaternity.bridge import BafuFlowIndex
from sentier_importers.sources.eaternity.cf_identity import CfVectors, EfLabels
from sentier_importers.sources.eaternity.inference import Inference, Inputs, infer

_METHOD_CFS = "method_cfs"
_EF_COLUMNS = ["flow", "flow_name", "flow_context"]


_REQUIRED_INPUTS = ("rank3", "ecospold", "ef_flows", _METHOD_CFS)


class EaternityInferredBafuEfSource(Source):
    """Compose rank 4 with CF identity into ``bafu-2026-v1 -> ef-3.1`` replace entries."""

    def fetch(self, ctx: RunContext) -> RawData:
        # every input but the CF directory goes through the cached fetcher
        self.inputs = {
            name: fetch_mod.fetch(url, ctx)
            for name, url in self.config.inputs.items()
            if name != _METHOD_CFS
        }
        return fetch_mod.fetch(self.config.fetch_url, ctx)

    def parse(self, raw: RawData) -> Records:
        """One record holding every parsed input; ``transform`` composes them."""
        missing = [
            name for name in _REQUIRED_INPUTS if name != _METHOD_CFS and name not in self.inputs
        ]
        if missing:
            raise RuntimeError(
                f"inputs {missing} not fetched: fetch() must run before parse(), and the "
                f"registry entry must declare inputs {list(_REQUIRED_INPUTS)}"
            )
        rank3 = orjson.loads(self.inputs["rank3"].content)
        ef_rows = pq.read_table(
            io.BytesIO(self.inputs["ef_flows"].content), columns=_EF_COLUMNS
        ).to_pylist()
        inputs = Inputs(
            rank4=orjson.loads(raw.content),
            rank3_codes=frozenset(codes_of(rank3)),
            bafu=BafuFlowIndex.from_ecospold(parse_ecospold_zip(self.inputs["ecospold"])),
            vectors=CfVectors.from_directory(
                fetch_mod.local_path(self.config.inputs.get(_METHOD_CFS), _METHOD_CFS)
            ),
            labels=EfLabels.from_rows(ef_rows),
        )
        return [{"inputs": inputs}]

    def infer(self, records: Records) -> Inference:
        (record,) = records
        return infer(record["inputs"])

    def transform(self, records: Records) -> Rows:
        return self.infer(records).entries
