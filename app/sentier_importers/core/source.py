"""Source plugin contract.

A source's declarative configuration lives in ``registry.yaml`` and is parsed into a
:class:`SourceConfig`. Behavior lives in a :class:`Source` subclass that implements
``transform`` (and may override ``fetch``/``parse`` for non-trivial inputs). The
framework supplies validate → emit → deliver via the pipeline driver.
"""

import re
from abc import ABC, abstractmethod
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from sentier_importers.core import fetch as fetch_mod
from sentier_importers.core import parse as parse_mod
from sentier_importers.core.context import RunContext
from sentier_importers.core.errors import SourceContractError
from sentier_importers.core.types import RawData, Records, Rows

_STEP_NAME = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_FRAMEWORK_STEP_NAMES = {
    "fetch",
    "parse",
    "deduplicate",
    "assemble",
    "validate",
    "emit",
    "deliver",
}


@dataclass(frozen=True)
class TransformStep:
    """One source-owned, observable transformation in an ordered pipeline."""

    name: str
    description: str
    function: Callable[[Records], Records]
    inputs: tuple[str, ...] = ()
    code_dependencies: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not _STEP_NAME.fullmatch(self.name):
            raise SourceContractError(
                f"transform step name {self.name!r} must use lower-kebab spelling"
            )
        if not self.description.strip():
            raise SourceContractError(f"transform step {self.name!r} needs a description")
        if not callable(self.function):
            raise SourceContractError(f"transform step {self.name!r} function is not callable")


@dataclass(frozen=True)
class SourceConfig:
    """Declarative configuration for one source, parsed from ``registry.yaml``."""

    name: str
    module: str
    target: str
    category: str
    fetch_url: str
    fetch_format: str
    output_format: str
    enabled: bool = True
    # Emitted file name override (without extension). ``None`` ⇒ the source name.
    # Needed when several sources fill one target folder with contract-named files
    # (e.g. sentier-inventory's ``processes.parquet`` / ``exchanges.parquet``).
    emit_filename: str | None = None
    # sentier-vocab sources only: the term type every row of this source gets in the
    # ``type`` column (``elementary-flow``, ``process``, …). ``category`` is then the
    # source release, the stem of ``terms/<release>.parquet``.
    term_type: str | None = None
    # Deterministic dedup against the target vocab (see core.dedup).
    dedup_on_existing: str = "skip"  # skip | error
    dedup_check_existing: bool = True  # Layer B on/off (vocab sources only)
    # Randonneur mapping-package emission (sentier_mappings target). ``package_verb`` set ⇒
    # wrap rows into ``{name, version, <verb>: rows}``.
    package_name: str | None = None
    package_version: str | None = None
    # replace | update | delete | create | review | coverage (sidecars)
    package_verb: str | None = None
    # Named secondary inputs (``{name: url}``) a source joins against — bridges that
    # compose several upstream artifacts. Fetched through the same cache as
    # ``fetch_url`` and exposed as ``Source.inputs[name]``.
    inputs: dict[str, str] = field(default_factory=dict)
    # Who can hold this source's local inputs: ``public`` (http(s) or a public
    # sentier-* checkout), ``licensed`` (obtainable under the provider's terms, e.g.
    # the BAFU EcoSpold export), ``private`` (a DdS-internal artifact). Drives the
    # ``list`` column and the missing-input error message (spec 2026-09-28).
    access: str = "public"
    # Role -> resolved reusable bibliographic declaration; absent for legacy jobs.
    input_provenance: dict[str, dict] = field(default_factory=dict)


class ObservedInputs(dict):
    """Report successful named-input reads only while a pipeline step is active."""

    def __init__(self, values, owner):
        super().__init__(values)
        self.owner = owner

    def __getitem__(self, name):
        value = super().__getitem__(name)
        if self.owner._input_observer is not None:
            self.owner._input_observer(name, value)
        return value

    def get(self, name, default=None):
        return self[name] if name in self else default

    def items(self):
        return ((name, self[name]) for name in self)

    def values(self):
        return (self[name] for name in self)


class Source(ABC):
    """Base class for all source plugins.

    Default ``fetch`` and ``parse`` delegate to the shared services using the
    declared ``fetch_url`` / ``fetch_format``. Only ``transform`` is required.
    """

    def __init__(self, config: SourceConfig) -> None:
        self.config = config
        self._input_observer = None
        #: ``config.inputs`` resolved to bytes by :meth:`fetch`, keyed by input name.
        self.inputs: dict[str, RawData] = {}

    @property
    def inputs(self):
        return self._inputs

    @inputs.setter
    def inputs(self, values):
        self._inputs = ObservedInputs(values, self)

    def input_path(self, name: str) -> Path:
        """Declare consumption of a configured directory/file read directly by a step."""
        path = fetch_mod.local_path(self.config.inputs.get(name), name)
        if self._input_observer is not None:
            self._input_observer(name, path)
        return path

    def fetch(self, ctx: RunContext) -> RawData:
        """Retrieve raw data (default: cached fetch of ``config.fetch_url``).

        Named ``config.inputs`` are fetched the same way and stored on
        :attr:`inputs` for ``parse``/``transform`` to join against.
        """
        self.inputs = {name: fetch_mod.fetch(url, ctx) for name, url in self.config.inputs.items()}
        return fetch_mod.fetch(self.config.fetch_url, ctx)

    def parse(self, raw: RawData) -> Records:
        """Turn raw bytes into records (default: parser for ``config.fetch_format``)."""
        return parse_mod.parse(raw, self.config.fetch_format)

    @abstractmethod
    def transform(self, records: Records) -> Rows:
        """Map parsed records to rows shaped for the target. Source-specific."""

    def transform_steps(self) -> Sequence[TransformStep]:
        """Return ordered, observable source transformations.

        This default adapter preserves existing plugins as one ``transform`` child.
        New and migrated plugins should declare each meaningful operation separately.
        """
        description = self.transform.__doc__ or f"Transform {self.config.name} records."
        return (
            TransformStep(
                "transform", description.strip(), self.transform, inputs=tuple(self.config.inputs)
            ),
        )

    def declared_transform_steps(self) -> tuple[TransformStep, ...]:
        """Validate and freeze transformations; an empty sequence passes parsed records through."""
        steps = tuple(self.transform_steps())
        names = [step.name for step in steps]
        duplicates = sorted({name for name in names if names.count(name) > 1})
        if duplicates:
            raise SourceContractError(
                f"source {self.config.name!r} repeats transform step names: {duplicates}"
            )
        reserved = sorted(set(names) & _FRAMEWORK_STEP_NAMES)
        if reserved:
            raise SourceContractError(
                f"source {self.config.name!r} uses framework step names: {reserved}"
            )
        for step in steps:
            unknown = set(step.inputs) - self.config.inputs.keys()
            if unknown:
                raise SourceContractError(
                    f"step {step.name!r} declares unknown inputs: {sorted(unknown)}"
                )
        return steps
