"""Canonical package names for the ``bafu-2026-v1 -> ef-3.1`` pair in sentier-mappings.

One name per package file in ``data/bafu-2026-v1__ef-3.1/``, in build order and
precedence: each later package was built over what the earlier ones leave unmapped
(see ``docs/repos/sentier-mappings.md``). Defined once here and imported by
``mappings_biosphere_nomenclature``, ``mappings_biosphere_coverage`` and the registry
tests, so the coverage sidecar's ``package`` field and the runtime messages always
spell them the same way as the registry's ``emit_filename`` values.
"""

from __future__ import annotations

CURATED = "biosphere-1-curated"
INFERRED = "biosphere-2-inferred"
MATCHED = "biosphere-3-matched"
NOMENCLATURE = "biosphere-4-nomenclature"
