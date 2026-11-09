import hashlib
import io

import orjson
import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from loguru import logger
from sentier_importers.core import dedup as dedup_mod
from sentier_importers.core.context import RunContext
from sentier_importers.core.errors import ValidationError
from sentier_importers.core.source import SourceConfig
from sentier_importers.core.targets import Target


@pytest.fixture
def warnings():
    """Capture loguru WARNING-level messages (loguru bypasses pytest's caplog)."""
    messages: list[str] = []
    sink_id = logger.add(lambda m: messages.append(str(m)), level="WARNING")
    yield messages
    logger.remove(sink_id)


def _ctx(tmp_path, **kw):
    return RunContext(cache_dir=tmp_path / "cache", output_dir=tmp_path / "out", **kw)


def _config(**overrides):
    base = dict(
        name="s",
        module="m",
        target="sentier_vocab",
        category="demo-1",
        term_type="product",
        fetch_url="u",
        fetch_format="xlsx",
        output_format="parquet",
    )
    base.update(overrides)
    return SourceConfig(**base)


_VOCAB_TARGET = Target(
    name="sentier_vocab",
    repo="https://github.com/sentier-dev/sentier-vocab.git",
    output_subdir="",
    schema_ref="main",
    validator="vocab_contract",
)
_BULK_TARGET = Target(
    name="sentier_inventory",
    repo="https://github.com/sentier-dev/sentier-inventory.git",
    output_subdir="data",
)


# --- slugify -----------------------------------------------------------------


def test_slugify_is_url_safe_and_lowercase():
    assert dedup_mod.slugify("Wheat, durum (raw)") == "wheat-durum-raw"


def test_slugify_strips_accents():
    assert dedup_mod.slugify("Café Crème") == "cafe-creme"


def test_slugify_is_idempotent():
    once = dedup_mod.slugify("Electricity, high voltage")
    assert dedup_mod.slugify(once) == once


# --- Layer A: intra-source ---------------------------------------------------


def test_intra_identical_duplicates_collapse(tmp_path):
    rows = [
        {"iri": "x/a", "pref_label": "A"},
        {"iri": "x/a", "pref_label": "A"},
    ]
    out = dedup_mod.dedup(rows, _config(dedup_check_existing=False), _VOCAB_TARGET, _ctx(tmp_path))
    assert out == [{"iri": "x/a", "pref_label": "A"}]


def test_intra_conflicting_same_iri_raises(tmp_path):
    rows = [
        {"iri": "x/a", "pref_label": "A"},
        {"iri": "x/a", "pref_label": "Different"},
    ]
    with pytest.raises(ValidationError):
        dedup_mod.dedup(rows, _config(dedup_check_existing=False), _VOCAB_TARGET, _ctx(tmp_path))


def test_intra_label_clash_warns_but_keeps_both(tmp_path, warnings):
    rows = [
        {"iri": "x/a", "pref_label": "Wheat"},
        {"iri": "x/b", "pref_label": "wheat"},  # same normalized label, different iri
    ]
    out = dedup_mod.dedup(rows, _config(dedup_check_existing=False), _VOCAB_TARGET, _ctx(tmp_path))
    assert len(out) == 2
    assert any("wheat" in msg.lower() for msg in warnings)


def test_rows_without_iri_pass_through(tmp_path):
    rows = [{"pref_label": "A"}, {"pref_label": "B"}]
    out = dedup_mod.dedup(rows, _config(dedup_check_existing=False), _VOCAB_TARGET, _ctx(tmp_path))
    assert out == rows


# --- Layer B: against existing target ----------------------------------------


def _patch_existing(monkeypatch, iris):
    monkeypatch.setattr(dedup_mod, "_existing_iris", lambda target, release, ctx: set(iris))


def test_layer_b_skip_drops_existing(tmp_path, monkeypatch):
    _patch_existing(monkeypatch, {"x/a"})
    rows = [{"iri": "x/a", "pref_label": "A"}, {"iri": "x/b", "pref_label": "B"}]
    out = dedup_mod.dedup(rows, _config(dedup_on_existing="skip"), _VOCAB_TARGET, _ctx(tmp_path))
    assert out == [{"iri": "x/b", "pref_label": "B"}]


def test_layer_b_error_raises_on_collision(tmp_path, monkeypatch):
    _patch_existing(monkeypatch, {"x/a"})
    rows = [{"iri": "x/a", "pref_label": "A"}]
    with pytest.raises(ValidationError):
        dedup_mod.dedup(rows, _config(dedup_on_existing="error"), _VOCAB_TARGET, _ctx(tmp_path))


def test_layer_b_asks_for_the_other_releases_of_the_target(tmp_path, monkeypatch):
    asked = []
    monkeypatch.setattr(
        dedup_mod, "_existing_iris", lambda target, release, ctx: asked.append(release) or set()
    )
    dedup_mod.dedup([{"iri": "x/a"}], _config(), _VOCAB_TARGET, _ctx(tmp_path))
    assert asked == ["demo-1"]


def test_layer_b_unknown_policy_raises(tmp_path, monkeypatch):
    _patch_existing(monkeypatch, {"x/a"})
    rows = [{"iri": "x/a", "pref_label": "A"}]
    for policy in ("merge", "overwrite"):  # an IRI cannot live in two releases
        with pytest.raises(ValidationError, match="unknown on_existing policy"):
            dedup_mod.dedup(rows, _config(dedup_on_existing=policy), _VOCAB_TARGET, _ctx(tmp_path))


def test_layer_b_disabled_by_config(tmp_path, monkeypatch):
    called = []
    monkeypatch.setattr(dedup_mod, "_existing_iris", lambda *a: called.append(True) or set())
    rows = [{"iri": "x/a", "pref_label": "A"}]
    out = dedup_mod.dedup(rows, _config(dedup_check_existing=False), _VOCAB_TARGET, _ctx(tmp_path))
    assert out == rows
    assert called == []  # Layer B never consulted


def test_layer_b_skipped_for_target_without_schema_ref(tmp_path, monkeypatch):
    called = []
    monkeypatch.setattr(dedup_mod, "_existing_iris", lambda *a: called.append(True) or set())
    rows = [{"iri": "x/a", "pref_label": "A"}]
    out = dedup_mod.dedup(
        rows, _config(target="sentier_inventory", term_type=None), _BULK_TARGET, _ctx(tmp_path)
    )
    assert out == rows
    assert called == []


# --- Layer B fetch (_existing_iris) --------------------------------------------


def _seed(cache_dir, url, payload: bytes):
    cache_dir.mkdir(parents=True, exist_ok=True)
    key = hashlib.sha256(url.encode("utf-8")).hexdigest()
    (cache_dir / key).write_bytes(payload)


def _term_file(*iris) -> bytes:
    buffer = io.BytesIO()
    pq.write_table(pa.table({"iri": list(iris), "type": ["product"] * len(iris)}), buffer)
    return buffer.getvalue()


def test_existing_iris_reads_the_other_releases_from_a_seeded_cache(tmp_path):
    ctx = _ctx(tmp_path, offline=True)
    listing_url = "https://api.github.com/repos/sentier-dev/sentier-vocab/contents/terms?ref=main"
    base = "https://example.test/terms/"
    _seed(
        ctx.cache_dir,
        listing_url,
        orjson.dumps(
            [
                {"name": "other-01.parquet", "download_url": base + "other-01.parquet"},
                {"name": "other-02.parquet", "download_url": base + "other-02.parquet"},
                # this release's own files are replaced by the delivery: never fetched
                {"name": "demo-1.parquet", "download_url": base + "demo-1.parquet"},
                {"name": "demo-1-02.parquet", "download_url": base + "demo-1-02.parquet"},
                {"name": "README.md", "download_url": base + "README.md"},
            ]
        ),
    )
    _seed(ctx.cache_dir, base + "other-01.parquet", _term_file("x/a", "x/b"))
    _seed(ctx.cache_dir, base + "other-02.parquet", _term_file("x/c"))
    assert dedup_mod._existing_iris(_VOCAB_TARGET, "demo-1", ctx) == {"x/a", "x/b", "x/c"}


def test_existing_iris_reads_the_terms_folder_beside_a_local_schema_dir(tmp_path):
    checkout = tmp_path / "sentier-vocab"
    (checkout / "schemas").mkdir(parents=True)
    (checkout / "terms").mkdir()
    (checkout / "terms" / "other.parquet").write_bytes(_term_file("x/a"))
    (checkout / "terms" / "demo-1.parquet").write_bytes(_term_file("x/own"))
    ctx = _ctx(tmp_path, offline=True, schema_dir=checkout / "schemas")
    assert dedup_mod._existing_iris(_VOCAB_TARGET, "demo-1", ctx) == {"x/a"}
