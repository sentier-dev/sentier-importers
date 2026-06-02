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
    """

    cache_dir: Path
    output_dir: Path
    dry_run: bool = True
    offline: bool = False
