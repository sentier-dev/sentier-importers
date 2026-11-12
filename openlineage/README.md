# Import lineage

The importer records OpenLineage events for inventory, vocabulary, methods and
mappings. Each import is a parent job; framework stages and declared source
transformations are child jobs connected through their input and output datasets.
Recordings describe source provenance, code, execution times, schemas, counts and
previews. Marquez displays the recorded graph without re-running the import.

See [this graph](inventory-import-workflow.md) for an inventory import example.

```text
fetch -> parse -> source transformations -> deduplicate -> assemble -> validate -> emit
                                                                                -> deliver (optional)
```

## Run and inspect

Run commands from the `sentier-importers` checkout. Python 3.10+ and uv are required:

```sh
uv sync
uv run sentier-importers list
uv run sentier-importers run <source>
```

Replace `<source>` with a registry name. Imports stage data under
`output/<target>/<category>/` by default. They record lineage locally without
contacting Marquez or opening a PR. `validate <source>` records execution through
validation but does not emit an artifact or create an inspectable artifact bundle.

Vocabulary uses the current term/relation release contract. A single source stages
both layers under `output/sentier_vocab/parts/<source>/` and cannot be delivered
alone. To build and deliver the whole release, use:

```sh
uv run sentier-importers run --release <release> --deliver
```

The release stages `terms/<release>[-NN].parquet` and
`relations/<release>.parquet`. Each file has its own adjacent verified bundle.
The release graph links these outputs to the recorded source parts. Delivery
replaces the release's files and bundles together, preserving other releases.
Vocabulary files retain their import identities when copied to a target checkout.

Inputs come from `registry.yaml`. `--data-root DIR` overrides their base directory;
otherwise `SENTIER_DATA_ROOT` or the parent of this checkout is used. For a single
source, `--input-file FILE` selects a local primary input without changing the
registry. `--schema-dir DIR` selects local validation schemas instead of the target's
pinned schema reference. Obtain private or licensed inputs from their providers.

To inspect a successfully emitted file, start a Marquez deployment, then run:

```sh
uv run inspect-lineage --artifact path/to/output.parquet
```

Inspection verifies the artifact, events and available upstream bundles, submits
events upstream first, checks that the output graph exists, and prints its browser
URL. It reads local files; clone remote artifacts together with their lineage before
inspection. It does not upload the source file or emitted artifact. Datasets without
a compatible bundle report `No lineage information available for this endpoint`.

Inventory outputs can also be selected by source folder and endpoint:

```sh
uv run inspect-lineage <source-folder> --processes
uv run inspect-lineage <source-folder> --exchanges
```

These selectors use the sibling `sentier-inventory` checkout by default.
`--inventory-root DIR` selects another checkout or inventory staging root.
Use `--artifact` for an exact file path or outputs from the other targets.
Inspection defaults to graph depth 8; `--depth N` overrides it.

## Recording locations

| Location | Contents and lifecycle |
|---|---|
| `output/lineage/<source>.openlineage.jsonl` | Working event log; replaced by the next run of the same source. `--lineage-dir DIR` changes its directory. |
| `<artifact-directory>/.lineage/<filename>/` | Complete bundle for the current artifact: `manifest.json`, `events.jsonl` and available `upstream/` bundles. Replaced when a new successful run replaces the artifact. |
| `output/lineage/runs/<run-id>/` | Separate immutable history for each successful single-artifact import. Multi-artifact runs use a compact key per run/output pair. `--lineage-store DIR` relocates this archive; it does not move the current bundle. |

The manifest records the producing run and checksums binding the artifact to its
events. Keep the artifact and its adjacent `.lineage/<filename>/` directory
together. Inspection does not need the working log or historical archive, even
when `--lineage-store` selects an external directory. `--output-dir DIR` changes
the staging base, default working-log directory and default historical archive.
`--lineage-dir` affects only the working log. Older pointer-based bundles remain
readable; a new run writes the adjacent layout and removes its superseded pointer.
Failed runs retain their working events but do not get successful artifact bundles.

## Deliver data and lineage together

```sh
uv run sentier-importers run <source> --deliver
```

Delivery opens one PR containing the selected artifact and its complete current
verified import history. Git/GitHub CLI authentication and permission to push a PR
branch on the target repository are required. Category folders are preserved.
For inventory, the delivered layout is:

```text
data/<source-folder>/<filename>
data/<source-folder>/.lineage/<filename>/manifest.json
data/<source-folder>/.lineage/<filename>/events.jsonl
data/<source-folder>/.lineage/<filename>/upstream/  # available verified input history
.gitattributes                   # preserve artifact and lineage bytes across clones
```

A new run replaces the artifact and its matching current bundle. Historical runs
remain in the contributor's archive; delivery does not copy that archive into the
target. Delivery stages verified current bundles even when ignored by the target.
Git attributes prevent
newline conversion from breaking checksums. Delivered bundles do not depend on the
contributor's staging directory, cache or external lineage store.

The import completes before Git/PR operations. A PR failure does not change the
successful import recording. Bundles retain the original import dataset identities;
opening a PR does not record a merge or publication on main.

`--deliver-local CHECKOUT` copies the artifact and portable lineage into an existing
target checkout without Git operations. Both delivery modes retain the staged output
and apply to all four targets and supported output formats. They deliver the selected
artifact; complete release metadata and target-repository validation are separate
requirements. Review captured code and source previews before publishing.

## Marquez connection

The default API is `http://localhost:5000`; the default UI is
`http://localhost:3000`. Inspection requires an already running server.
For a deployment with one API/UI base URL:

```sh
uv run inspect-lineage --artifact path/to/output.parquet --marquez-url https://lineage.example.org
```

For separate API and UI addresses, use `--marquez-api` and `--marquez-web`; these
values override `--marquez-url`. A base URL may include a path prefix or end in
`/api/v1`. Connection options do not deploy a server or configure authentication.

To inspect an existing server index without submitting events:

```sh
uv run inspect-lineage --artifact path/to/output.parquet --marquez-url https://lineage.example.org --no-upload
```

This still verifies the local artifact and bundle, then requests the dataset graph.
If the server has no matching graph, inspection reports that lineage is not indexed.
The URL above is illustrative. Delivery does not automatically index events on any
Marquez server.

## Source and transformation contract

Registry entries select a source module, primary URL/format, target, category,
output format and optional validation contract. `enabled: false` sources remain
runnable by name.

A module exposes one `Source` subclass. Inherited `fetch()` reads/caches the registry
URL and named inputs; override `parse()` for source-specific extraction. Declare
meaningful source operations with `transform_steps()`:

```python
def transform_steps(self):
    return (
        TransformStep(
            "map-process-rows", "Map source records to inventory process rows.",
            self.map_rows,
            inputs=("mapping",),
            code_dependencies=("app/sentier_importers/sources/example/helpers.py",),
        ),
    )
```

Step names are unique lower-kebab identifiers. Framework stage names are reserved.
Each step receives the previous result. `transform()` remains the compatibility entry
point; legacy plugins appear as one `transform` job. An empty step sequence passes
parsed records through. Declare directly used helper files for job-scoped code evidence.

Secondary inputs belong in registry `inputs`. Consume them through `self.inputs[name]`
or `self.input_path(name)` during a step, and declare their roles in `TransformStep.inputs`.
The framework fingerprints consumed bytes/directories, adds upstream graph edges and
preserves verified available upstream bundles. Undeclared reads through these interfaces
fail. Untracked filesystem/network reads bypass this contract and must not be used
for dependencies that affect output. Missing upstream history is `not-recorded`.

## Source provenance contract

Registry `source_datasets` defines reusable dataset descriptions. A source's
`provenance` maps its primary role `source` and every declared secondary input role
to those descriptions. Existing sources can adopt this incrementally. Once opted in,
invalid references, missing roles and malformed metadata fail registry loading.

Required keys are `source`, `source_version`, `title`, `publisher`, `license` and
`citation`; unknown values can be null except source ID/title. Optional fields are
`creators`, publication `date`, `identifier`, public `source_url`, `license_url`,
`format`, `format_version` and textual `coverage`. Vocabulary field meanings follow
`sentier-vocab`. Public reference URLs must exclude credentials/query tokens.

The framework freezes the resolved declaration in `sentier_source` alongside the
observed SHA-256, byte size, read/observation timestamps and acquisition method.
Only a fresh HTTP acquisition records `downloadedAt`; cache/local reads do not infer
a historical download date. Import time is never substituted for publication time.
Provider attribution is separate from the optional public run contributor.

`--source-provenance KEY` selects an existing declaration for an explicitly chosen
input; it does not create one or infer its correctness from a filename. Named inputs
must already have provenance declarations. `--input-file` and `--source-provenance`
apply to one source, not `--all`.

## Recorded evidence and privacy

- Every job emits `START` then `COMPLETE`, or `FAIL`. Failure facets exclude raw
  exception messages and tracebacks that may reveal local data.
- Dataset names are filenames. Abstract namespaces distinguish workspace, target,
  release, run and SHA-256 without exporting physical paths, OS users or hostnames.
- Code facets include transformation callables, relative implementation paths, file
  hashes, Git commit and job-scoped `codeDirty`. When local code differs, a source
  link points to the committed baseline; captured source and hashes describe execution.
- Schemas, row counts, transformation statistics, previews of up to five rows, and
  up to three distinct non-null examples per schema field support inspection.
  These describe processing stages, not arbitrary row-level tracing.
- Object-valued mapping `source`/`target` preview cells use declared JSON text
  encoding to avoid Marquez search-index conflicts. Emitted data types stay structured.

Only an explicitly provided `--contributor PUBLIC_ID` is recorded as a contributor.
Keep secrets, personal details and sensitive paths out of captured code. Previews
contain real source values and are subject to the data's terms.

Custom facet schemas live in [`facets/`](facets/). Recording, bundle inspection and
source provenance are implemented in
[`core/lineage.py`](../app/sentier_importers/core/lineage.py),
[`core/inventory_lineage.py`](../app/sentier_importers/core/inventory_lineage.py)
and [`core/provenance.py`](../app/sentier_importers/core/provenance.py) respectively.
This code repository excludes generated
artifacts and recordings; delivery includes the selected artifact and verified
history in the target repository.
