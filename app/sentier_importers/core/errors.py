"""Exception hierarchy for the importer framework."""


class SentierImporterError(Exception):
    """Base class for all importer errors."""


class FetchError(SentierImporterError):
    """Raised when a fetch stage cannot retrieve data.

    ``url`` is the input that failed, when known, so the pipeline can name the
    registry input it came from.
    """

    def __init__(self, message: str, url: str | None = None) -> None:
        super().__init__(message)
        self.url = url


class MissingInputError(FetchError):
    """A ``file://`` input is not present on this machine.

    Not a bug in the source: the file is a licensed or DdS-private artifact the
    operator has not placed under the data root. The CLI reports it as a warning
    and skips the source instead of failing the run.
    """


class ParseError(SentierImporterError):
    """Raised when raw data cannot be parsed into records."""


class ValidationError(SentierImporterError):
    """Raised when transformed rows fail target-schema validation."""


class DeliveryError(SentierImporterError):
    """Raised when the deliver stage fails."""


class RegistryError(SentierImporterError):
    """Raised when the source registry is malformed or a source is missing."""
