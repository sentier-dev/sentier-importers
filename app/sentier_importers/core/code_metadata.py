"""Capture job-scoped code evidence without machine-specific paths."""

import hashlib
import inspect
import subprocess
import textwrap
from pathlib import Path
from urllib.parse import quote

ROOT = Path(__file__).resolve().parents[3]
PRODUCER = "https://github.com/sentier-dev/sentier-importers"
FACETS = (
    "https://raw.githubusercontent.com/sentier-dev/sentier-importers/main/openlineage/facets/1-0-0"
)


def git(*args, root=None):
    try:
        return subprocess.run(
            ["git", *args], cwd=root or ROOT, capture_output=True, text=True, check=True
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return None


def capture(source, dependencies=(), include_source=False):
    commit = git("rev-parse", "HEAD")
    evidence = {
        "_producer": PRODUCER,
        "_schemaURL": f"{FACETS}/SentierCodeJobFacet.json",
        "commit": commit,
        "codeDirty": None,
        "scope": "declared-files",
        "files": [],
    }
    facets = {"sentier_code": evidence}
    try:
        obj = inspect.unwrap(source)
        filename = Path(inspect.getsourcefile(obj) or inspect.getfile(obj)).resolve()
        relative = filename.relative_to(ROOT).as_posix()
        lines, line = inspect.getsourcelines(obj)
    except (OSError, TypeError, ValueError):
        return facets
    paths = [relative, *dependencies]
    states = []
    for name in dict.fromkeys(paths):
        path = (ROOT / name).resolve()
        if not path.is_relative_to(ROOT):
            raise ValueError("Code dependencies must be repository-relative files")
        rel = path.relative_to(ROOT).as_posix()
        state = git("status", "--porcelain", "--untracked-files=all", "--", rel)
        try:
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
        except OSError:
            digest = None
        known = digest is not None and commit is not None and state is not None
        states.append(bool(state) if known else None)
        evidence["files"].append(
            {
                "path": rel,
                "sha256": digest,
                "dirty": states[-1],
            }
        )
    evidence["codeDirty"] = True if True in states else (None if None in states else False)
    committed = commit and git("cat-file", "-e", f"{commit}:{relative}") is not None
    if committed:
        facets["sourceCodeLocation"] = {
            "_producer": PRODUCER,
            "_schemaURL": "https://openlineage.io/spec/facets/1-1-0/SourceCodeLocationJobFacet.json",
            "type": "git",
            "repoUrl": f"{PRODUCER}.git",
            "path": relative,
            "version": commit,
            "url": f"{PRODUCER}/blob/{commit}/{quote(relative)}#L{line}",
        }
        evidence["linkRepresents"] = (
            "recorded-source" if states[0] is False else "committed-baseline"
        )
    if include_source:
        facets["sourceCode"] = {
            "_producer": PRODUCER,
            "_schemaURL": "https://openlineage.io/spec/facets/1-0-1/SourceCodeJobFacet.json",
            "language": "python",
            "sourceCode": textwrap.dedent("".join(lines)),
        }
    return facets
