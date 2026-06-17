import pytest
from sentier_importers.core import deliver as deliver_mod
from sentier_importers.core.context import RunContext
from sentier_importers.core.errors import DeliveryError
from sentier_importers.core.targets import Target

TARGET = Target(
    name="t", repo="https://github.com/o/r.git", output_subdir="data", validator="none"
)


def _ctx(tmp_path, dry_run):
    return RunContext(cache_dir=tmp_path / "cache", output_dir=tmp_path / "out", dry_run=dry_run)


def test_dry_run_is_noop(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(deliver_mod, "_run", lambda cmd, cwd=None: calls.append(cmd))
    result = deliver_mod.deliver(
        [tmp_path / "f.json"],
        TARGET,
        branch="b",
        title="t",
        body="x",
        ctx=_ctx(tmp_path, dry_run=True),
    )
    assert result is None
    assert calls == []  # no git/gh commands run in dry-run


def test_real_run_invokes_git_and_gh(tmp_path, monkeypatch):
    src = tmp_path / "out" / "f.json"
    src.parent.mkdir(parents=True)
    src.write_text("{}")
    calls = []

    def fake_run(cmd, cwd=None):
        calls.append(cmd)
        if cmd[:2] == ["gh", "pr"]:
            return "https://github.com/o/r/pull/1\n"
        return ""

    monkeypatch.setattr(deliver_mod, "_run", fake_run)
    url = deliver_mod.deliver(
        [src],
        TARGET,
        branch="import/x",
        title="Import x",
        body="body",
        ctx=_ctx(tmp_path, dry_run=False),
    )
    assert url == "https://github.com/o/r/pull/1"
    flat = [" ".join(c) for c in calls]
    assert any(c.startswith("git clone") for c in flat)
    assert any("checkout -b import/x" in c for c in flat)
    assert any(c.startswith("git commit") for c in flat)
    assert any(c.startswith("gh pr create") for c in flat)


def test_run_wrapper_failure_raises(tmp_path, monkeypatch):
    def boom(cmd, cwd=None):
        raise deliver_mod.subprocess.CalledProcessError(1, cmd)

    monkeypatch.setattr(deliver_mod, "_run", boom)
    with pytest.raises(DeliveryError):
        deliver_mod.deliver(
            [tmp_path / "f.json"],
            TARGET,
            branch="b",
            title="t",
            body="x",
            ctx=_ctx(tmp_path, dry_run=False),
        )


def test_remove_stale_same_stem(tmp_path):
    (tmp_path / "foodex2.yaml").write_text("old")
    (tmp_path / "foodex2.parquet").write_text("new")
    (tmp_path / "other.yaml").write_text("keep")
    deliver_mod._remove_stale_same_stem(tmp_path / "foodex2.parquet")
    assert not (tmp_path / "foodex2.yaml").exists()  # stale prior format removed
    assert (tmp_path / "foodex2.parquet").exists()  # incoming file untouched
    assert (tmp_path / "other.yaml").exists()  # unrelated file kept
