"""The elementary flows of a SimaPro CSV export (Agribalyse 3.2).

A flow is one ``(compartment, name, sub-compartment)`` that a ``Process`` block
exchanges under one of the five elementary sections. Its unit is the unit of the
substance row of the same name in the block SimaPro writes for that compartment after
the processes, not the unit of an exchange row (an exchange of a ``kg`` substance may
be written in ``g``).

The export is latin-1 with ``;`` between fields. A line ends with a line feed, with or
without a carriage return, and with nothing else: a comment may hold the byte 0x85,
which ``str.splitlines`` would take for a line break. Only the first fields of a row
are read, so the separators inside a quoted comment do no harm; a quoted name would,
and is refused.
"""

from __future__ import annotations

import lzma
from dataclasses import dataclass

from sentier_importers.core.errors import ParseError

_ENCODING = "latin-1"
_XZ_MAGIC = b"\xfd7zXZ\x00"
_SEPARATOR = ";"
_PROCESS = "Process"
_END = "End"
#: Section of a process block -> heading of the substance block of that compartment.
SUBSTANCE_HEADING = {
    "Resources": "Raw materials",
    "Emissions to air": "Airborne emissions",
    "Emissions to water": "Waterborne emissions",
    "Emissions to soil": "Emissions to soil",
    "Final waste flows": "Final waste flows",
}
_COMPARTMENT_OF_HEADING = {heading: section for section, heading in SUBSTANCE_HEADING.items()}


@dataclass(frozen=True, order=True)
class FlowRecord:
    """One elementary flow, in the spelling of the export."""

    name: str
    compartment: str
    subcompartment: str
    unit: str


def _lines(content: bytes) -> list[str]:
    if content.startswith(_XZ_MAGIC):
        try:
            content = lzma.decompress(content)
        except lzma.LZMAError as exc:
            raise ParseError(f"not a readable xz file: {exc}") from exc
    return content.decode(_ENCODING).replace("\r\n", "\n").split("\n")


def _fields(line: str, wanted: int, number: int) -> list[str]:
    """The first ``wanted`` fields of a row (fewer when the row is shorter)."""
    if line.startswith('"'):
        raise ParseError(f"line {number}: quoted flow names are not supported: {line[:80]!r}")
    return line.split(_SEPARATOR, wanted)[:wanted]


def _scan(lines: list[str]) -> tuple[set[tuple[str, str, str]], dict[tuple[str, str], str]]:
    """``(exchanged, units)``: the ``(compartment, name, sub-compartment)`` keys the
    processes exchange, and ``{(compartment, name): unit}`` from the substance blocks."""
    exchanged: set[tuple[str, str, str]] = set()
    units: dict[tuple[str, str], str] = {}
    block: str | None = None  # heading of the block being read
    section: str | None = None  # elementary section being read, in a process block
    for number, line in enumerate(lines, start=1):
        if block is None:
            if line and not line.startswith("{"):
                block, section = line, None
        elif line == _END:
            block = None
        elif block == _PROCESS:
            if section is None:
                section = line if line in SUBSTANCE_HEADING else None
            elif not line:
                section = None
            else:
                name, *rest = _fields(line, 2, number)
                exchanged.add((section, name, rest[0] if rest else ""))
        elif block in _COMPARTMENT_OF_HEADING and line:
            fields = _fields(line, 2, number)
            if len(fields) < 2:
                raise ParseError(f"line {number}: substance row without a unit: {line[:80]!r}")
            key = (_COMPARTMENT_OF_HEADING[block], fields[0])
            if units.setdefault(key, fields[1]) != fields[1]:
                raise ParseError(
                    f"line {number}: substance {fields[0]!r} of {block!r} is written with "
                    f"two units: {units[key]!r} and {fields[1]!r}"
                )
    return exchanged, units


def read_flows(content: bytes) -> tuple[list[FlowRecord], int]:
    """``(flows, dropped)`` from an export, xz-compressed or plain.

    ``flows`` are sorted by name, compartment and sub-compartment. ``dropped`` counts
    the exchanged names that have no substance row in the block of their compartment,
    and so no unit.
    """
    exchanged, units = _scan(_lines(content))
    if not exchanged:
        raise ParseError("no elementary flow found: not a SimaPro process export")
    flows = [
        FlowRecord(name, compartment, subcompartment, units[(compartment, name)])
        for compartment, name, subcompartment in exchanged
        if (compartment, name) in units
    ]
    return sorted(flows), len(exchanged) - len(flows)
