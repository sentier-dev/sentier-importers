# sentier_importers

A source-centric, format-agnostic framework for importing external data into Sentier
target repositories (`sentier_vocab`, `sentier_inventory`, `sentier_methods`, …).

Each data source is a plugin implementing a uniform staged contract:

    fetch → parse → transform → validate → emit → deliver

The framework owns the driver and shared services (cached fetch, input parsers,
JSON/YAML/Parquet writers, schema validation, PR delivery). A source declares itself
in `app/sentier_importers/registry.yaml` and implements `transform` in
`app/sentier_importers/sources/<name>/source.py`.

## Install

```bash
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
```

## CLI

```bash
python -m sentier_importers list                 # list registered sources
python -m sentier_importers validate <source>    # fetch/parse/transform/validate only
python -m sentier_importers run <source>          # full pipeline, stage to output/ (dry-run)
python -m sentier_importers run <source> --deliver # also open a PR to the target repo
python -m sentier_importers run --all             # run every enabled source (dry-run)
```

## Adding a source

1. Create `app/sentier_importers/sources/<name>/source.py` with a `Source` subclass
   implementing `transform` (override `fetch`/`parse` if the input is non-trivial).
2. Add a block to `registry.yaml`.
3. Add `tests/` next to the source with a cached fetch fixture.

See `app/sentier_importers/sources/example_csv/` for a reference.

## Output formats

- **YAML / JSON** — human-reviewable vocabulary and structured terms.
- **Parquet** — bulk quantitative data (characterization factors, LCIA datasets).

The framework reads external RDF/OWL/TTL on the input side but never emits TTL.
