"""Deliver stage: clone the target repo, branch, copy emitted files into the target's
path, commit, push, and open a PR via ``gh``. A no-op in dry-run mode.

Delivery copies over whatever the target already holds at the same path, no merge.
For inventory targets the category folder is the source folder (``data/<source>-
<source_version>/``), so a full overwrite is the intended semantics: a run for a
source rewrites its own folder whole, and sources never share a folder or a file.
sentier-vocab is delivered one release at a time (``place_release`` /
``deliver_release``): the release's files in ``terms/`` and ``relations/`` are removed
and the staged ones copied in, so a release that now needs fewer shards leaves none behind.
"""

import shutil
import subprocess
from collections.abc import Callable, Mapping
from pathlib import Path

from sentier_importers.core import vocab_layers
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


def deliver_local(files: list[Path], target: Target, *, category: str, root: Path) -> list[Path]:
    """Copy emitted ``files`` into a local checkout of ``target`` — no git, no PR.

    Places each file at ``root/<output_subdir>/<category>/<name>``, preserving the
    category folder (unlike :func:`deliver`, which currently flattens). This is the
    regenerate-locally delivery path for sources whose data cannot be pushed to a
    public repo yet; ``root`` must be an existing directory. Existing files at the
    destination are overwritten: for inventory targets the category is the source
    folder, which a delivery is meant to replace whole.
    """
    if not root.is_dir():
        raise DeliveryError(f"local delivery root is not a directory: {root}")
    dest_dir = root / target.output_subdir / category
    dest_dir.mkdir(parents=True, exist_ok=True)
    dests: list[Path] = []
    for file in files:
        dest = dest_dir / file.name
        _remove_stale_same_stem(dest)
        shutil.copy2(file, dest)
        dests.append(dest)
    return dests


def place_release(written: Mapping[str, list[Path]], root: Path, release: str) -> list[Path]:
    """Replace ``release``'s files in a local sentier-vocab checkout — no git, no PR.

    ``written`` maps a layer folder (``terms``, ``relations``) to the staged files of
    that layer, as returned by ``vocab_layers.write_layers``. In each layer folder
    under ``root`` every file of ``release`` is deleted before the staged files are
    copied in; files of other releases are not touched. ``root`` must be an existing
    directory.
    """
    if not root.is_dir():
        raise DeliveryError(f"local delivery root is not a directory: {root}")
    dests: list[Path] = []
    for layer, files in written.items():
        dest_dir = root / layer
        dest_dir.mkdir(parents=True, exist_ok=True)
        for old in dest_dir.glob("*.parquet"):
            if vocab_layers.release_of(old) == release:
                old.unlink()
        for file in files:
            dests.append(Path(shutil.copy2(file, dest_dir / file.name)))
    return dests


def _open_pr(
    target: Target,
    *,
    branch: str,
    title: str,
    body: str,
    ctx: RunContext,
    place: Callable[[Path], list[str]],
) -> str:
    """Clone ``target``, let ``place`` put files into the clone, and open a PR.

    ``place`` receives the clone's root and returns the paths (relative to it) to
    stage; deletions under those paths are staged too.
    """
    workdir = ctx.cache_dir / "delivery" / target.name
    try:
        if workdir.exists():
            shutil.rmtree(workdir)
        workdir.parent.mkdir(parents=True, exist_ok=True)
        _run(["git", "clone", "--depth", "1", target.repo, str(workdir)])
        _run(["git", "checkout", "-b", branch], cwd=workdir)
        _run(["git", "add", "--all", *place(workdir)], cwd=workdir)
        _run(["git", "commit", "-m", title], cwd=workdir)
        _run(["git", "push", "-u", "origin", branch], cwd=workdir)
        url = _run(["gh", "pr", "create", "--title", title, "--body", body], cwd=workdir)
        return url.strip()
    except subprocess.CalledProcessError as exc:
        raise DeliveryError(f"delivery to {target.name} failed: {exc}") from exc


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

    def place(workdir: Path) -> list[str]:
        dest_dir = workdir / target.output_subdir
        dest_dir.mkdir(parents=True, exist_ok=True)
        for file in files:
            dest = dest_dir / file.name
            _remove_stale_same_stem(dest)
            shutil.copy2(file, dest)
        return [target.output_subdir]

    return _open_pr(target, branch=branch, title=title, body=body, ctx=ctx, place=place)


def deliver_release(
    written: Mapping[str, list[Path]],
    target: Target,
    release: str,
    *,
    branch: str,
    title: str,
    body: str,
    ctx: RunContext,
) -> str | None:
    """Open a PR replacing ``release`` in sentier-vocab. Returns the PR URL, or None in dry-run."""
    if ctx.dry_run:
        return None

    def place(workdir: Path) -> list[str]:
        place_release(written, workdir, release)
        return list(written)

    return _open_pr(target, branch=branch, title=title, body=body, ctx=ctx, place=place)
