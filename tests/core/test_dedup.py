import hashlib

import orjson
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
        category="products",
        fetch_url="u",
        fetch_format="xlsx",
        output_format="yaml",
        collection_items_key="products",
    )
    base.update(overrides)
    return SourceConfig(**base)


_VOCAB_TARGET = Target(
    name="sentier_vocab",
    repo="https://github.com/sentier-dev/sentier-vocab.git",
    output_subdir="data",
    schema_ref="main",
    validator="linkml",
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


def _patch_existing(monkeypatch, iris, labels):
    monkeypatch.setattr(
        dedup_mod,
        "_existing_index",
        lambda target, category, items_key, ctx: (set(iris), dict(labels)),
    )


def test_layer_b_skip_drops_existing(tmp_path, monkeypatch):
    _patch_existing(monkeypatch, {"x/a"}, {})
    rows = [{"iri": "x/a", "pref_label": "A"}, {"iri": "x/b", "pref_label": "B"}]
    out = dedup_mod.dedup(rows, _config(dedup_on_existing="skip"), _VOCAB_TARGET, _ctx(tmp_path))
    assert out == [{"iri": "x/b", "pref_label": "B"}]


def test_layer_b_error_raises_on_collision(tmp_path, monkeypatch):
    _patch_existing(monkeypatch, {"x/a"}, {})
    rows = [{"iri": "x/a", "pref_label": "A"}]
    with pytest.raises(ValidationError):
        dedup_mod.dedup(rows, _config(dedup_on_existing="error"), _VOCAB_TARGET, _ctx(tmp_path))


def test_layer_b_overwrite_keeps_imported(tmp_path, monkeypatch):
    _patch_existing(monkeypatch, {"x/a"}, {})
    rows = [{"iri": "x/a", "pref_label": "New A"}]
    out = dedup_mod.dedup(
        rows, _config(dedup_on_existing="overwrite"), _VOCAB_TARGET, _ctx(tmp_path)
    )
    assert out == [{"iri": "x/a", "pref_label": "New A"}]


def test_layer_b_cross_source_label_clash_warns(tmp_path, monkeypatch, warnings):
    _patch_existing(monkeypatch, {"x/existing"}, {"wheat": "x/existing"})
    rows = [{"iri": "x/new", "pref_label": "Wheat"}]
    out = dedup_mod.dedup(rows, _config(dedup_on_existing="skip"), _VOCAB_TARGET, _ctx(tmp_path))
    assert out == rows
    assert any("wheat" in msg.lower() for msg in warnings)


def test_layer_b_unknown_policy_raises(tmp_path, monkeypatch):
    _patch_existing(monkeypatch, {"x/a"}, {})
    rows = [{"iri": "x/a", "pref_label": "A"}]
    with pytest.raises(ValidationError):
        dedup_mod.dedup(rows, _config(dedup_on_existing="merge"), _VOCAB_TARGET, _ctx(tmp_path))


def test_layer_b_disabled_by_config(tmp_path, monkeypatch):
    called = []
    monkeypatch.setattr(
        dedup_mod, "_existing_index", lambda *a: called.append(True) or (set(), {})
    )
    rows = [{"iri": "x/a", "pref_label": "A"}]
    out = dedup_mod.dedup(rows, _config(dedup_check_existing=False), _VOCAB_TARGET, _ctx(tmp_path))
    assert out == rows
    assert called == []  # Layer B never consulted


def test_layer_b_skipped_for_target_without_schema_ref(tmp_path, monkeypatch):
    called = []
    monkeypatch.setattr(
        dedup_mod, "_existing_index", lambda *a: called.append(True) or (set(), {})
    )
    rows = [{"iri": "x/a", "pref_label": "A"}]
    out = dedup_mod.dedup(rows, _config(target="sentier_inventory"), _BULK_TARGET, _ctx(tmp_path))
    assert out == rows
    assert called == []


# --- Layer B fetch (_existing_index) via seeded offline cache -----------------


def _seed(cache_dir, url, payload: bytes):
    cache_dir.mkdir(parents=True, exist_ok=True)
    key = hashlib.sha256(url.encode("utf-8")).hexdigest()
    (cache_dir / key).write_bytes(payload)


def test_existing_index_reads_seeded_collections(tmp_path):
    ctx = _ctx(tmp_path, offline=True)
    listing_url = (
        "https://api.github.com/repos/sentier-dev/sentier-vocab/contents/data/products?ref=main"
    )
    file_url = "https://example.test/core.yaml"
    _seed(
        ctx.cache_dir,
        listing_url,
        orjson.dumps(
            [
                {"name": "core.yaml", "download_url": file_url},
                {"name": "README.md", "download_url": "https://example.test/README.md"},
            ]
        ),
    )
    _seed(
        ctx.cache_dir,
        file_url,
        b"scheme: https://vocab.sentier.dev/products/\n"
        b"products:\n"
        b"  - iri: https://vocab.sentier.dev/products/electricity\n"
        b"    pref_label: Electricity\n",
    )
    iris, labels = dedup_mod._existing_index(_VOCAB_TARGET, "products", "products", ctx)
    assert iris == {"https://vocab.sentier.dev/products/electricity"}
    assert labels == {"electricity": "https://vocab.sentier.dev/products/electricity"}
