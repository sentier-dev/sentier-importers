"""Content-addressed cached fetcher for file:// and http(s):// sources."""

import hashlib
from pathlib import Path

import httpx

from sentier_importers.core.context import RunContext
from sentier_importers.core.errors import FetchError
from sentier_importers.core.types import RawData


def _cache_key(url: str) -> str:
    return hashlib.sha256(url.encode("utf-8")).hexdigest()


def fetch(url: str, ctx: RunContext) -> RawData:
    """Retrieve ``url`` with on-disk content caching.

    The cache is keyed by the SHA-256 of the URL under ``ctx.cache_dir``. A cache
    hit short-circuits the source. In offline mode a cache miss raises
    ``FetchError``. Supports ``file://`` and ``http(s)://`` schemes.
    """
    cache_path = ctx.cache_dir / _cache_key(url)
    if cache_path.exists():
        return RawData(content=cache_path.read_bytes(), source_url=url)

    if ctx.offline:
        raise FetchError(f"offline: no cache entry for {url}")

    if url.startswith("file://"):
        local = Path(url[len("file://") :])
        if not local.exists():
            raise FetchError(f"file not found: {local}")
        content = local.read_bytes()
    elif url.startswith(("http://", "https://")):
        try:
            response = httpx.get(url, follow_redirects=True, timeout=60)
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise FetchError(f"failed to fetch {url}: {exc}") from exc
        content = response.content
    else:
        raise FetchError(f"unsupported URL scheme: {url}")

    ctx.cache_dir.mkdir(parents=True, exist_ok=True)
    cache_path.write_bytes(content)
    return RawData(content=content, source_url=url)
