"""Source plugin contract.

A source's declarative configuration lives in ``registry.yaml`` and is parsed into a
:class:`SourceConfig`. Behavior lives in a :class:`Source` subclass that implements
``transform`` (and may override ``fetch``/``parse`` for non-trivial inputs). The
framework supplies validate → emit → deliver via the pipeline driver.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass

from sentier_importers.core import fetch as fetch_mod
from sentier_importers.core import parse as parse_mod
from sentier_importers.core.context import RunContext
from sentier_importers.core.types import RawData, Records, Rows


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
    validate_against: str | None = None
    enabled: bool = True


class Source(ABC):
    """Base class for all source plugins.

    Default ``fetch`` and ``parse`` delegate to the shared services using the
    declared ``fetch_url`` / ``fetch_format``. Only ``transform`` is required.
    """

    def __init__(self, config: SourceConfig) -> None:
        self.config = config

    def fetch(self, ctx: RunContext) -> RawData:
        """Retrieve raw data (default: cached fetch of ``config.fetch_url``)."""
        return fetch_mod.fetch(self.config.fetch_url, ctx)

    def parse(self, raw: RawData) -> Records:
        """Turn raw bytes into records (default: parser for ``config.fetch_format``)."""
        return parse_mod.parse(raw, self.config.fetch_format)

    @abstractmethod
    def transform(self, records: Records) -> Rows:
        """Map parsed records to rows shaped for the target. Source-specific."""
