"""Exception hierarchy for the importer framework."""


class SentierImporterError(Exception):
    """Base class for all importer errors."""


class FetchError(SentierImporterError):
    """Raised when a fetch stage cannot retrieve data."""


class ParseError(SentierImporterError):
    """Raised when raw data cannot be parsed into records."""


class ValidationError(SentierImporterError):
    """Raised when transformed rows fail target-schema validation."""


class DeliveryError(SentierImporterError):
    """Raised when the deliver stage fails."""


class RegistryError(SentierImporterError):
    """Raised when the source registry is malformed or a source is missing."""
