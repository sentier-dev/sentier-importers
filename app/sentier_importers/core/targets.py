"""Registry of target repositories that imported data is delivered to.

Adding a new target (e.g. a future bulk-data repo) is a single entry here.
``schema_ref`` pins the git ref/release the target's contracts are read from;
``validator`` names how a delivery is checked: ``vocab_contract`` (the term and
relation contracts of sentier-vocab, see :mod:`sentier_importers.core.vocab_contract`)
or ``none`` (bulk-data repos that validate in their own CI).
"""

from dataclasses import dataclass

from sentier_importers.core.errors import RegistryError

#: Validator id of a target delivered as term and relation layers, one release at a time.
VOCAB_CONTRACT = "vocab_contract"


@dataclass(frozen=True)
class Target:
    """A repository imported data is delivered to."""

    name: str
    repo: str  # git/https remote URL
    output_subdir: str  # path within the target repo where emitted files land
    schema_ref: str | None = None  # pinned git ref/release the contracts are read from
    validator: str = "none"  # "vocab_contract" or "none"


TARGETS: dict[str, Target] = {
    "sentier_vocab": Target(
        name="sentier_vocab",
        repo="https://github.com/sentier-dev/sentier-vocab.git",
        output_subdir="",  # the layer folders terms/ and relations/ sit at the repo root
        schema_ref="main",
        validator=VOCAB_CONTRACT,
    ),
    "sentier_inventory": Target(
        name="sentier_inventory",
        repo="https://github.com/sentier-dev/sentier-inventory.git",
        output_subdir="data",
        schema_ref=None,
        validator="none",
    ),
    "sentier_methods": Target(
        name="sentier_methods",
        repo="https://github.com/sentier-dev/sentier-methods.git",
        output_subdir="data",
        schema_ref=None,
        validator="none",
    ),
    "sentier_mappings": Target(
        name="sentier_mappings",
        repo="https://github.com/sentier-dev/sentier-mappings.git",
        output_subdir="data",
        schema_ref=None,  # randonneur JSON Schema, validated in-source
        validator="none",
    ),
}


def get_target(name: str) -> Target:
    """Return the registered :class:`Target` for ``name`` or raise ``RegistryError``."""
    try:
        return TARGETS[name]
    except KeyError:
        raise RegistryError(f"unknown target: {name!r}") from None
