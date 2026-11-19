"""The synthetic SimaPro export of the Agribalyse tests, and the real inputs.

The real-data tests run only when ``SENTIER_DATA_ROOT`` names the directory that holds
the sibling checkouts (as the registry reads it); they are skipped otherwise.
"""

import os
from pathlib import Path

import pytest
from sentier_importers.core import registry as registry_mod
from sentier_importers.core.context import RunContext
from sentier_importers.core.errors import FetchError
from sentier_importers.core.fetch import fetch

#: Two processes and the five substance blocks, in the shape of the real export
#: (latin-1, CRLF, ``;`` separated): 11 flows, and one exchanged name without a
#: substance row.
MINI_CSV = Path(__file__).parent / "fixtures" / "agribalyse_mini.csv"
_REAL_CSV = "sentier-agribalyse/source/AGB32_ecoinvent_free.CSV.xz"
#: A registry block whose ``fetch`` is the pinned Brightway Flows file.
_BRIGHTWAY_FLOWS_BLOCK = "bafu-ef-brightway-flows"


def real_csv() -> bytes:
    """The public Agribalyse 3.2 export, or skip."""
    root = os.environ.get("SENTIER_DATA_ROOT")
    if not root:
        pytest.skip("SENTIER_DATA_ROOT is not set")
    path = Path(root) / _REAL_CSV
    if not path.exists():
        pytest.skip(f"{path} is not available")
    return path.read_bytes()


def real_brightway_flows(config: pytest.Config) -> bytes:
    """The pinned Brightway Flows file, fetched once through the registry URL into
    pytest's cache directory, or skip."""
    if not os.environ.get("SENTIER_DATA_ROOT"):
        pytest.skip("SENTIER_DATA_ROOT is not set")
    url = registry_mod.get_config(_BRIGHTWAY_FLOWS_BLOCK).fetch_url
    cache = Path(config.cache.mkdir("brightway-flows"))
    try:
        return fetch(url, RunContext(cache_dir=cache, output_dir=cache)).content
    except FetchError as exc:
        pytest.skip(f"Brightway Flows file not available: {exc}")
