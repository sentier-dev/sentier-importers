# Contributing

## Setup

```bash
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
pre-commit install
```

## Quality bar

- `ruff` (line-length 99), `black`, `isort` via pre-commit.
- `pytest` with ≥80% coverage on `app/sentier_importers/core/`.
- No live network in tests or CI — sources ship cached fetch fixtures.

## Running tests

```bash
pytest                                              # framework tests + coverage on core/
pytest app/sentier_importers/sources/<name>/tests/  # a source's own tests
```

## Adding a source

Create `sources/<name>/source.py` (a `Source` subclass implementing `transform`),
add a `registry.yaml` block, and ship `tests/` with offline fixtures. Keep
source-specific logic in `transform`; reuse the shared fetch/parse/write/validate
services. Validate against the target repo's pinned schema before delivery.
