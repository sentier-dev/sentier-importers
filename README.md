# sentier-importers

```mermaid
flowchart LR
    src["External sources: EFSA, JRC EF 3.1, Agribalyse, BAFU"] -->|fetch| imp[sentier-importers]
    imp -->|PR| voc[sentier-vocab]
    imp -->|PR| inv[sentier-inventory]
    imp -->|PR| met[sentier-methods]
    imp -->|PR| map[sentier-mappings]
    voc -->|fetch schema| imp
```

## What it is

A plugin framework that imports external data into the Sentier data repos. Each data source is a plugin. The framework runs every plugin through one staged pipeline:

```mermaid
flowchart LR
    fetch --> parse --> transform --> dedup --> assemble --> validate --> emit --> deliver
```

- The framework owns the driver and the shared services: cached fetch, parsers, dedup, LinkML validation, YAML/JSON/Parquet writers, PR delivery.
- A source declares itself in `app/sentier_importers/registry.yaml` and implements `transform` in `app/sentier_importers/sources/<name>/source.py`.

## Install

Needs Python 3.10 or newer and `uv`.

```bash
uv sync --extra dev
```

## Use

List registered sources, their targets, and whether they are enabled:

```bash
uv run sentier-importers list
```

Run one source as a dry run. Files land in `output/<target>/<category>/`, no PR is opened:

```bash
uv run sentier-importers run <source>
```

`validate <source>` runs the same pipeline up to the validate step and emits nothing.

Options for `run`. The last three rows also apply to `validate`:

| Option | Effect |
|---|---|
| `--all` | run every enabled source instead of one; this is the CI smoke test |
| `--deliver` | also open a PR on the target repo; needs an authenticated `gh` |
| `--deliver-local <path>` | copy the emitted files into a local checkout of the target; no git, no PR |
| `--offline` | fail on any fetch cache miss |
| `--cache-dir`, `--output-dir` | override the fetch cache and the staging dir |
| `--schema-dir` | validate against local schemas instead of the target's pinned ref |

## Layout

```
app/sentier_importers/
  registry.yaml    the source registry, one commented block per source
  core/            pipeline driver, fetch, parse, dedup, validate, write, deliver, targets
  matching/        BAFU to EF flow matching, used by the bafu and eaternity sources
  sources/         one folder per source, example_csv/ is the reference plugin
scripts/           local BAFU regeneration driver and output checks
tests/             core/ framework tests, sources/ plugin tests
```

## Data

- Inputs: `file://` and `http(s)://` URLs. Fetches are cached in the user cache dir, `~/.cache/sentier_importers/` on Linux, keyed by the SHA-256 of the URL.
- Outputs: YAML or JSON for vocabulary terms, Parquet for bulk tables, randonneur JSON for mappings. Never TTL.
- Most sources are `enabled: false`. Big imports are opt-in so `run --all` stays fast.
- The Agribalyse, EF and BAFU sources read local `file://` paths. See "Local inputs" below.

### Local inputs

Every `file://` input in `registry.yaml` is written as `file://${SENTIER_DATA_ROOT}/<repo-or-folder>/...`.
The data root is resolved once per run, first hit wins:

1. `--data-root DIR` on `list`, `validate` and `run`.
2. The `SENTIER_DATA_ROOT` environment variable.
3. The parent directory of this checkout. Clone the sibling repos beside `sentier-importers` and nothing needs setting.

Each source declares who can hold its inputs. `sentier-importers list` prints it as the last column:

| `access`   | meaning |
|------------|---------|
| `public`   | http(s) inputs or a public `sentier-*` checkout beside this repo |
| `licensed` | at least one input you obtain from its provider under their terms, e.g. the BAFU-2026 v1 EcoSpold export at `<root>/sources/bafu-2026/` |
| `private`  | at least one DdS-internal artifact (`dds-agribalyse`, `dds-carbonminds-data`); these sources only run inside DdS |

A missing local input is a warning, not an error: `validate` and `run` print the path, the registry input, the root that was used, and the `access` class, then skip that source (`run --all` carries on with the rest). Any other fetch failure is still an error.

## Contributing

1. Add `app/sentier_importers/sources/<name>/source.py` with a `Source` subclass that implements `transform`. Override `fetch` or `parse` only if the input needs it.
2. Add a block to `registry.yaml`.
3. Add tests under `tests/sources/` with a cached fetch fixture. Tests run offline.

Format and test before committing:

```bash
uv run --extra dev pre-commit run --all-files
```

```bash
uv run --extra testing pytest
```

CI also runs the plugin tests with `uv run --extra testing pytest app/sentier_importers/sources/`.

## Licence

BSD 3-Clause. See `LICENSE`.
