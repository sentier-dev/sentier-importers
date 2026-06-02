"""Registry of target repositories that imported data is delivered to.

Adding a new target (e.g. a future bulk-data repo) is a single entry here.
``schema_ref`` pins the git ref/release used for validation; ``validator``
selects a strategy registered in :mod:`sentier_importers.core.validate`.
"""

from dataclasses import dataclass

from sentier_importers.core.errors import RegistryError


@dataclass(frozen=True)
class Target:
    """A repository imported data is delivered to."""

    name: str
    repo: str  # git/https remote URL
    output_subdir: str  # path within the target repo where emitted files land
    schema_ref: str | None = None  # pinned git ref/release for schema validation
    validator: str = "none"  # validation strategy id (see validate.py)


TARGETS: dict[str, Target] = {
    "sentier_vocab": Target(
        name="sentier_vocab",
        repo="https://github.com/sentier-dev/sentier_vocab.git",
        output_subdir="data",
        schema_ref="main",
        validator="linkml",
    ),
    "sentier_inventory": Target(
        name="sentier_inventory",
        repo="https://github.com/sentier-dev/sentier_inventory.git",
        output_subdir="data",
        schema_ref=None,
        validator="none",
    ),
    "sentier_methods": Target(
        name="sentier_methods",
        repo="https://github.com/sentier-dev/sentier_methods.git",
        output_subdir="data",
        schema_ref=None,
        validator="none",
    ),
}


def get_target(name: str) -> Target:
    """Return the registered :class:`Target` for ``name`` or raise ``RegistryError``."""
    try:
        return TARGETS[name]
    except KeyError:
        raise RegistryError(f"unknown target: {name!r}") from None
