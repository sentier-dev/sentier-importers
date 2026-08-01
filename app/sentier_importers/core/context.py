"""Per-run configuration shared across all pipeline stages."""

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class RunContext:
    """Configuration for one importer run.

    - ``cache_dir``: content-addressed fetch cache location.
    - ``output_dir``: where emitted files are staged before delivery.
    - ``dry_run``: when True (default), never open a PR — stage locally only.
    - ``offline``: when True, a fetch cache miss is an error (used in tests/CI).
    - ``schema_dir``: when set, validate against schemas in this local directory instead
      of fetching the target repo's pinned ref — for co-developing data and schema before
      the schema change is pushed.
    - ``deliver_local_root``: when set, copy emitted files into this local checkout of
      the target repo (no git/gh involved) — the regenerate-locally flow for sources
      whose data cannot be delivered upstream yet.
    """

    cache_dir: Path
    output_dir: Path
    dry_run: bool = True
    offline: bool = False
    schema_dir: Path | None = None
    deliver_local_root: Path | None = None
