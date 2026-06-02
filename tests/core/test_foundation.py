"""Tests for the core exception hierarchy, shared types, and run context."""

import dataclasses
from pathlib import Path

import pytest

from sentier_importers.core.context import RunContext
from sentier_importers.core.errors import (
    DeliveryError,
    FetchError,
    ParseError,
    RegistryError,
    SentierImporterError,
    ValidationError,
)
from sentier_importers.core.types import RawData


def test_error_hierarchy():
    for error in (FetchError, ParseError, ValidationError, DeliveryError, RegistryError):
        assert issubclass(error, SentierImporterError)


def test_rawdata_is_frozen():
    raw = RawData(content=b"abc", source_url="file:///x")
    assert raw.content == b"abc"
    assert raw.source_url == "file:///x"
    assert raw.media_type is None
    typed = RawData(content=b"x", source_url="file:///y", media_type="text/csv")
    assert typed.media_type == "text/csv"
    with pytest.raises(dataclasses.FrozenInstanceError):
        raw.content = b"changed"


def test_runcontext_defaults():
    ctx = RunContext(cache_dir=Path("/tmp/cache"), output_dir=Path("/tmp/out"))
    assert ctx.dry_run is True
    assert ctx.offline is False
    with pytest.raises(dataclasses.FrozenInstanceError):
        ctx.dry_run = False
