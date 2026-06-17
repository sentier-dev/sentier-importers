"""Deliver stage: clone the target repo, branch, copy emitted files into the target's
path, commit, push, and open a PR via ``gh``. A no-op in dry-run mode."""

import shutil
import subprocess
from pathlib import Path

from sentier_importers.core.context import RunContext
from sentier_importers.core.errors import DeliveryError
from sentier_importers.core.targets import Target


def _run(cmd: list[str], cwd: Path | None = None) -> str:
    """Run a subprocess, returning stdout. Raises CalledProcessError on non-zero exit."""
    result = subprocess.run(cmd, cwd=cwd, check=True, capture_output=True, text=True)
    return result.stdout


def _remove_stale_same_stem(dest: Path) -> None:
    """Delete any sibling of ``dest`` with the same stem but a different extension.

    When a source's output format changes (e.g. ``foodex2.yaml`` -> ``foodex2.parquet``),
    this prevents the target repo from ending up with both the old and new files.
    """
    for sibling in dest.parent.glob(f"{dest.stem}.*"):
        if sibling.name != dest.name:
            sibling.unlink()


def deliver(
    files: list[Path],
    target: Target,
    *,
    branch: str,
    title: str,
    body: str,
    ctx: RunContext,
) -> str | None:
    """Open a PR adding ``files`` to ``target``. Returns the PR URL, or None in dry-run.

    In dry-run mode this performs no git/gh operations. Otherwise it clones the
    target into a working dir under the cache, creates ``branch``, copies the files
    into ``target.output_subdir``, commits, pushes, and runs ``gh pr create``.
    """
    if ctx.dry_run:
        return None

    workdir = ctx.cache_dir / "delivery" / target.name
    try:
        if workdir.exists():
            shutil.rmtree(workdir)
        workdir.parent.mkdir(parents=True, exist_ok=True)
        _run(["git", "clone", "--depth", "1", target.repo, str(workdir)])
        _run(["git", "checkout", "-b", branch], cwd=workdir)

        dest_dir = workdir / target.output_subdir
        dest_dir.mkdir(parents=True, exist_ok=True)
        for file in files:
            dest = dest_dir / file.name
            _remove_stale_same_stem(dest)
            shutil.copy2(file, dest)

        _run(["git", "add", target.output_subdir], cwd=workdir)
        _run(["git", "commit", "-m", title], cwd=workdir)
        _run(["git", "push", "-u", "origin", branch], cwd=workdir)
        url = _run(["gh", "pr", "create", "--title", title, "--body", body], cwd=workdir)
        return url.strip()
    except subprocess.CalledProcessError as exc:
        raise DeliveryError(f"delivery to {target.name} failed: {exc}") from exc
