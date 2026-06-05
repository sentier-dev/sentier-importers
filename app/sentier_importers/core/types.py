"""Shared data types passed between pipeline stages."""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

# A record/row is a JSON-serializable mapping. `Records` (parse output) and `Rows`
# (transform output) are structurally identical but mark distinct pipeline stages.
Record = dict[str, Any]
Records = list[Record]
Rows = list[Record]

# What the validate/emit stages handle: either a flat list of rows (bulk/per-item
# targets) or a single assembled collection mapping (vocab tree-root targets).
Collection = Mapping[str, Any]
Payload = Rows | Collection


@dataclass(frozen=True)
class RawData:
    """Raw bytes retrieved by a fetch stage, with provenance metadata."""

    content: bytes
    source_url: str
    media_type: str | None = None
