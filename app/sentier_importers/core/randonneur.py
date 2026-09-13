"""Helpers over randonneur mapping packages."""

from __future__ import annotations


def codes_of(package: dict) -> set[str]:
    """Source codes named by every ``replace``/``update`` entry in a mapping package."""
    return {
        e["source"]["code"]
        for verb in ("replace", "update")
        for e in package.get(verb, [])
        if e.get("source", {}).get("code")
    }
