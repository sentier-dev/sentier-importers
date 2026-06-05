import hashlib

import pytest
from sentier_importers.core import fetch as fetch_mod
from sentier_importers.core.context import RunContext
from sentier_importers.core.errors import FetchError


def _ctx(tmp_path, **kw):
    return RunContext(cache_dir=tmp_path / "cache", output_dir=tmp_path / "out", **kw)


def test_fetch_file_url(tmp_path):
    src = tmp_path / "data.txt"
    src.write_bytes(b"hello")
    ctx = _ctx(tmp_path)
    raw = fetch_mod.fetch(f"file://{src}", ctx)
    assert raw.content == b"hello"
    assert raw.source_url == f"file://{src}"


def test_fetch_writes_cache(tmp_path):
    src = tmp_path / "data.txt"
    src.write_bytes(b"cached")
    ctx = _ctx(tmp_path)
    url = f"file://{src}"
    fetch_mod.fetch(url, ctx)
    key = hashlib.sha256(url.encode("utf-8")).hexdigest()
    assert (ctx.cache_dir / key).read_bytes() == b"cached"


def test_cache_hit_skips_source(tmp_path):
    src = tmp_path / "data.txt"
    src.write_bytes(b"first")
    ctx = _ctx(tmp_path)
    url = f"file://{src}"
    fetch_mod.fetch(url, ctx)
    src.write_bytes(b"second")  # change source after caching
    raw = fetch_mod.fetch(url, ctx)
    assert raw.content == b"first"  # served from cache


def test_offline_miss_raises(tmp_path):
    ctx = _ctx(tmp_path, offline=True)
    with pytest.raises(FetchError):
        fetch_mod.fetch("https://example.com/x", ctx)


def test_missing_file_raises(tmp_path):
    ctx = _ctx(tmp_path)
    with pytest.raises(FetchError):
        fetch_mod.fetch(f"file://{tmp_path / 'nope.txt'}", ctx)


def test_unsupported_scheme_raises(tmp_path):
    ctx = _ctx(tmp_path)
    with pytest.raises(FetchError):
        fetch_mod.fetch("ftp://example.com/x", ctx)


def test_http_fetch_uses_httpx(tmp_path, monkeypatch):
    class FakeResponse:
        content = b"net-bytes"

        def raise_for_status(self):
            return None

    monkeypatch.setattr(fetch_mod.httpx, "get", lambda *a, **k: FakeResponse())
    ctx = _ctx(tmp_path)
    raw = fetch_mod.fetch("https://example.com/data", ctx)
    assert raw.content == b"net-bytes"
    key = hashlib.sha256(b"https://example.com/data").hexdigest()
    assert (ctx.cache_dir / key).read_bytes() == b"net-bytes"


def test_offline_hit_succeeds(tmp_path):
    url = "https://example.com/offline"
    key = hashlib.sha256(url.encode("utf-8")).hexdigest()
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir(parents=True)
    (cache_dir / key).write_bytes(b"cached")
    ctx = RunContext(cache_dir=cache_dir, output_dir=tmp_path / "out", offline=True)
    raw = fetch_mod.fetch(url, ctx)
    assert raw.content == b"cached"
    assert raw.source_url == url
