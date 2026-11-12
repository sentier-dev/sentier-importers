"""Helpers over randonneur mapping packages."""

from __future__ import annotations

import math


def codes_of(package: dict) -> set[str]:
    """Source codes named by every ``replace``/``update`` entry in a mapping package."""
    return {
        e["source"]["code"]
        for verb in ("replace", "update")
        for e in package.get(verb, [])
        if e.get("source", {}).get("code")
    }


def positive_finite_factor(value: object, name: str) -> float:
    """A mapping conversion factor, refused unless it is positive and finite.

    ``name`` labels the offending source row in the error.
    """
    try:
        factor = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"{name}: unit_conversion must be positive and finite") from exc
    if not math.isfinite(factor) or factor <= 0:
        raise ValueError(f"{name}: unit_conversion must be positive and finite")
    return factor
