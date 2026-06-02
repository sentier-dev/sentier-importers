"""Shared data types passed between pipeline stages."""

from dataclasses import dataclass
from typing import Any

# A record/row is a JSON-serializable mapping. `Records` (parse output) and `Rows`
# (transform output) are structurally identical but mark distinct pipeline stages.
Record = dict[str, Any]
Records = list[Record]
Rows = list[Record]


@dataclass(frozen=True)
class RawData:
    """Raw bytes retrieved by a fetch stage, with provenance metadata."""

    content: bytes
    source_url: str
    media_type: str | None = None
