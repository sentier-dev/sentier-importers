"""Pipeline driver: run a source through
fetch → parse → transform → dedup → assemble → validate → emit → deliver.

A sentier-vocab source is one *part* of a source release: the release is what gets
delivered, as ``terms/<release>[-NN].parquet`` and ``relations/<release>.parquet``.
``run_source`` stages a part for inspection only; ``run_release`` runs every part of
a release, validates the whole against the vocab contracts and delivers it.
"""

from dataclasses import replace
from pathlib import Path

import pyarrow.parquet as pq
from sentier_importers.core import dedup as dedup_mod
from sentier_importers.core import deliver as deliver_mod
from sentier_importers.core import inventory_lineage
from sentier_importers.core import lineage as lineage_mod
from sentier_importers.core import schema_provider, vocab_contract, vocab_layers
from sentier_importers.core import write as write_mod
from sentier_importers.core.context import RunContext
from sentier_importers.core.errors import (
    DeliveryError,
    FetchError,
    RegistryError,
    SourceContractError,
)
from sentier_importers.core.registry import DATA_ROOT_VAR
from sentier_importers.core.source import Source, SourceConfig
from sentier_importers.core.targets import VOCAB_CONTRACT, Target, get_target
from sentier_importers.core.types import Payload, Rows

#: Staging folder, under ``<output_dir>/<target>/``, of single vocab parts.
PARTS_DIR = "parts"


def _assemble(rows: Rows, config: SourceConfig) -> Payload:
    """Wrap rows into a randonneur mapping package when ``package_verb`` is set,
    else pass them through."""
    if config.package_verb is not None:
        return {
            "name": config.package_name,
            "version": config.package_version,
            config.package_verb: rows,
        }
    return rows


def _is_vocab(target: Target) -> bool:
    return target.validator == VOCAB_CONTRACT


def _contract(target: Target, ctx: RunContext) -> vocab_contract.Contract:
    """The vocab term and relation contracts at the target's pinned ref."""
    return vocab_contract.load_contract(
        schema_provider.resolve_schema(target, "term", ctx),
        schema_provider.resolve_schema(target, "relation", ctx),
    )


#: What to do about a missing local input, by the source's ``access`` class.
ACCESS_HINTS = {
    "public": "This input is a public sentier-* checkout: clone it beside this repo "
    "or point the data root at it.",
    "licensed": "This input is obtainable from its provider under the provider's "
    "terms; place it at the path above.",
    "private": "This input is a DdS-internal artifact and is not distributed with " "the repo.",
}


def _input_name(config: SourceConfig, url: str | None) -> str:
    """The registry key (``fetch.url`` or an ``inputs`` name) that declares ``url``."""
    if url == config.fetch_url:
        return "fetch.url"
    for name, candidate in config.inputs.items():
        if candidate == url:
            return name
    return "an input"


def _with_input_context(exc: FetchError, config: SourceConfig, ctx: RunContext) -> FetchError:
    """Re-wrap a fetch failure with the source, the registry input, the data root
    that was used, and what the ``access`` class says about obtaining the file.
    The exception type is preserved so the CLI can tell a missing local input
    (warn and skip) from any other fetch failure (error)."""
    root = ctx.data_root if ctx.data_root is not None else "(not set)"
    lines = [
        str(exc),
        f"  input:  {_input_name(config, exc.url)} of source {config.name!r}",
        f"  root:   {root}  (override with --data-root or {DATA_ROOT_VAR})",
        f"  access: {config.access}. {ACCESS_HINTS[config.access]}",
    ]
    return type(exc)("\n".join(lines), url=exc.url)


def _fetch(source: Source, ctx: RunContext):
    try:
        return source.fetch(ctx)
    except FetchError as exc:
        raise _with_input_context(exc, source.config, ctx) from exc


def _produce(
    source: Source, ctx: RunContext, recorder: lineage_mod.LineageRecorder
) -> tuple[Rows, Payload, Target, lineage_mod.DatasetRef]:
    """Run fetch → parse → transform → dedup → assemble → validate.

    Returns deduped rows, emit-ready payload, target, and validated dataset identity.
    """
    config = source.config
    target = get_target(config.target)
    external = recorder._inputs

    def fetch():
        raw = _fetch(source, ctx)
        if isinstance(raw, lineage_mod.RawData):
            external.append(
                inventory_lineage.observe_dependency(recorder, "source", config.fetch_url, raw)
            )
        return raw

    def observed(stage, allowed=None):
        def run(operation, inputs):
            direct_reads = []

            def consume(name, value):
                if name not in config.inputs or (allowed is not None and name not in allowed):
                    raise SourceContractError(f"step {stage!r} used undeclared input {name!r}")
                ref = inventory_lineage.observe_dependency(
                    recorder, name, config.inputs[name], value
                )
                if isinstance(value, Path):
                    direct_reads.append((value, ref.version))
                if not any(
                    item.namespace == ref.namespace and item.name == ref.name for item in inputs
                ):
                    inputs.append(ref)
                if not any(
                    item.namespace == ref.namespace and item.name == ref.name
                    for item in recorder._inputs
                ):
                    recorder._inputs.append(ref)

            source._input_observer = consume
            try:
                result = operation()
                for path, expected in direct_reads:
                    if "sha256:" + inventory_lineage.path_fingerprint(path)[0] != expected:
                        raise SourceContractError(
                            "Direct input changed while the step was running"
                        )
                return result
            finally:
                source._input_observer = None

        return run

    fetched = recorder.intermediate("fetched-source")
    raw = recorder.run_child(
        "fetch",
        "Fetch and cache the primary source and its named inputs.",
        external,
        [fetched],
        fetch,
        source=source.fetch,
    )

    parsed = recorder.intermediate("parsed-records")
    parse_inputs = [fetched]
    records = recorder.run_child(
        "parse",
        f"Parse the fetched {config.fetch_format} source into records.",
        parse_inputs,
        [parsed],
        lambda: observed("parse")(lambda: source.parse(raw), parse_inputs),
        source=source.parse,
    )

    current = parsed
    transformed = records
    for step in source.declared_transform_steps():
        output = recorder.intermediate(step.name)
        step_inputs = [current]
        transformed = recorder.run_child(
            step.name,
            step.description,
            step_inputs,
            [output],
            lambda step=step, transformed=transformed: observed(step.name, step.inputs)(
                lambda: step.function(transformed), step_inputs
            ),
            source=step.function,
            code_dependencies=step.code_dependencies,
            include_source=True,
        )
        current = output

    deduped = recorder.intermediate("deduplicated-rows")
    dedup_inputs = [current]

    def framework_input(role, url, raw):
        ref = inventory_lineage.observe_dependency(recorder, role, url, raw)
        dedup_inputs.append(ref)
        recorder._inputs.append(ref)

    dedup_ctx = replace(ctx, dependency_observer=framework_input)
    rows = recorder.run_child(
        "deduplicate",
        "Deduplicate transformed rows according to the source registry policy.",
        dedup_inputs,
        [deduped],
        lambda: dedup_mod.dedup(transformed, config, target, dedup_ctx),
        source=dedup_mod.dedup,
    )

    assembled = recorder.intermediate("assembled-payload")
    payload = recorder.run_child(
        "assemble",
        "Assemble rows into the payload shape required by the target.",
        [deduped],
        [assembled],
        lambda: _assemble_payload(rows, config, target, ctx),
        source=_assemble_payload,
    )

    validated = recorder.intermediate("validated-payload")

    def validate() -> Payload:
        _validate(payload, config, target, ctx)
        return payload

    recorder.run_child(
        "validate",
        "Validate the assembled payload against the configured target contract.",
        [assembled],
        [validated],
        validate,
        source=_validate,
    )
    return rows, payload, target, validated


def validate_source(source: Source, ctx: RunContext) -> int:
    """Run the pipeline up to and including validation; return the row count."""
    config = source.config
    recorder = lineage_mod.LineageRecorder(
        config, lineage_mod.lineage_path(config, ctx), parent_source=type(source), ctx=ctx
    )
    recorder.start_parent([])
    try:
        rows, payload, target, validated = _produce(source, ctx, recorder)
        recorder.complete_parent([validated])
        return len(payload["terms"]) if _is_vocab(target) else len(rows)
    except BaseException as error:
        recorder.fail_parent(error)
        raise
    finally:
        recorder.close()


def run_source(source: Source, ctx: RunContext) -> Path:
    """Run the full pipeline. Emits to ``output_dir``; delivers a PR unless dry-run."""
    config = source.config
    target = get_target(config.target)
    if _is_vocab(target):
        return _run_vocab_part(source, target, ctx)
    archive_lineage = config.target in (
        "sentier_inventory",
        "sentier_vocab",
        "sentier_methods",
        "sentier_mappings",
    )
    filename = config.emit_filename or config.name
    out_path = (
        ctx.output_dir
        / config.target
        / config.category
        / f"{filename}{write_mod.EXTENSIONS[config.output_format]}"
    )
    recorder = lineage_mod.LineageRecorder(
        config, lineage_mod.lineage_path(config, ctx), parent_source=type(source), ctx=ctx
    )
    recorder.start_parent([])
    completed = False
    try:
        _, payload, target, validated = _produce(source, ctx, recorder)
        emitted = recorder.artifact(out_path)

        def emit() -> Payload:
            write_mod.write(payload, out_path, config.output_format)
            if archive_lineage:
                recorder.describe_artifact(emitted, out_path)
            return payload

        recorder.run_child(
            "emit",
            f"Write the validated payload as {config.output_format}.",
            [validated],
            [emitted],
            emit,
            source=write_mod.write,
        )

        final = emitted
        if ctx.deliver_local_root is not None:
            local_path = (
                ctx.deliver_local_root / target.output_subdir / config.category / out_path.name
            )
            target_ref = lineage_mod.target_dataset(config, target.repo, out_path.name)
            if archive_lineage:
                target_ref = recorder.artifact(local_path, "delivered")

            def deliver() -> Payload:
                deliver_mod.deliver_local(
                    [out_path], target, category=config.category, root=ctx.deliver_local_root
                )
                if archive_lineage:
                    recorder.describe_artifact(target_ref, local_path)
                return payload

            recorder.run_child(
                "deliver",
                "Deliver the emitted artifact to its configured target repository.",
                [emitted],
                [target_ref],
                deliver,
                source=deliver_mod.deliver_local,
            )
            final = target_ref

        recorder.complete_parent([final])
        completed = True
        if archive_lineage:
            inventory_lineage.preserve(
                out_path, recorder, ctx.lineage_store or ctx.output_dir / "lineage" / "runs"
            )
            if ctx.deliver_local_root is not None:
                inventory_lineage.export_bundle(out_path, local_path, ctx.deliver_local_root)
    except BaseException as error:
        if not completed:
            recorder.fail_parent(error)
        raise
    finally:
        recorder.close()
    # A PR packages a completed import. A Git/PR failure must not rewrite that
    # successful history as an import failure or claim publication on main.
    if not ctx.dry_run:
        deliver_mod.deliver(
            [out_path],
            target,
            category=config.category,
            with_lineage=archive_lineage,
            branch=f"import/{config.name}",
            title=f"Import {config.name}",
            body=f"Automated import of {config.name} by sentier_importers.",
            ctx=ctx,
        )
    return out_path


def _assemble_payload(rows, config, target, ctx):
    if not _is_vocab(target):
        return _assemble(rows, config)
    terms, relations = vocab_layers.to_layers(
        rows, term_type=config.term_type, release=config.category, contract=_contract(target, ctx)
    )
    return {"terms": terms, "relations": relations}


def _validate(payload, config, target, ctx):
    # Upstream bulk targets validate in their own CI; vocab uses its current contracts.
    if _is_vocab(target):
        vocab_contract.validate(
            payload["terms"],
            payload["relations"],
            _contract(target, ctx),
            release=config.category,
            complete=False,
        )


def _emit_layers(payload, root, config, recorder, validated):
    written = {}
    outputs = []

    def emit():
        written.update(
            vocab_layers.write_layers(
                payload["terms"], payload["relations"], root, config.category
            )
        )
        for layer, files in written.items():
            for path in files:
                ref = recorder.artifact(path, f"staged/{layer}")
                recorder.describe_artifact(ref, path)
                ref.capture(pq.read_table(path).to_pylist())
                outputs.append(ref)

    recorder.run_child(
        "emit",
        "Write the validated vocabulary term and relation layers.",
        [validated],
        outputs,
        emit,
        source=vocab_layers.write_layers,
        capture_outputs=False,
    )
    return written, outputs


def _preserve_layers(written, recorder, ctx):
    for files in written.values():
        for path in files:
            inventory_lineage.preserve(
                path, recorder, ctx.lineage_store or ctx.output_dir / "lineage" / "runs"
            )


def _record_vocab_part(source, target, ctx):
    config = source.config
    recorder = lineage_mod.LineageRecorder(
        config, lineage_mod.lineage_path(config, ctx), parent_source=type(source), ctx=ctx
    )
    recorder.start_parent([])
    completed = False
    try:
        _, payload, _, validated = _produce(source, ctx, recorder)
        written, outputs = _emit_layers(
            payload,
            ctx.output_dir / config.target / PARTS_DIR / config.name,
            config,
            recorder,
            validated,
        )
        recorder.complete_parent(outputs)
        completed = True
        _preserve_layers(written, recorder, ctx)
        return payload["terms"], payload["relations"], written
    except BaseException as error:
        if not completed:
            recorder.fail_parent(error)
        raise
    finally:
        recorder.close()


def _run_vocab_part(source: Source, target: Target, ctx: RunContext) -> Path:
    config = source.config
    if ctx.deliver_local_root is not None or not ctx.dry_run:
        raise DeliveryError(
            f"source {config.name!r} is one part of the vocab release {config.category!r}; "
            f"a release is delivered whole: run --release {config.category}"
        )
    _, _, written = _record_vocab_part(source, target, ctx)
    return written[vocab_layers.TERMS_DIR][0]


def run_release(release: str, sources: list[Source], ctx: RunContext) -> list[Path]:
    """Run every part, validate the whole vocab release, and deliver its two layers."""
    targets = {source.config.target for source in sources}
    if len(targets) != 1 or any(source.config.category != release for source in sources):
        raise RegistryError(
            f"release {release!r} needs at least one source, all with category "
            f"{release!r} and the same target"
        )
    target = get_target(targets.pop())
    if not _is_vocab(target):
        raise RegistryError(f"target {target.name!r} is not delivered per release")
    config = replace(
        sources[0].config,
        name=f"release-{release}",
        term_type=None,
        inputs={},
        input_provenance={},
    )
    recorder = lineage_mod.LineageRecorder(
        config, lineage_mod.lineage_path(config, ctx), parent_source=run_release, ctx=ctx
    )
    recorder.start_parent([])
    completed = False
    try:
        terms: Rows = []
        relations: Rows = []
        for source in sources:
            part_terms, part_relations, part_files = _record_vocab_part(source, target, ctx)
            terms.extend(part_terms)
            relations.extend(part_relations)
            for layer, files in part_files.items():
                for path in files:
                    ref = inventory_lineage.observe_dependency(
                        recorder, f"{source.config.name}/{layer}", f"file://{path}", path
                    )
                    recorder._inputs.append(ref)

        assembled = recorder.intermediate("assembled-release")
        payload = recorder.run_child(
            "assemble",
            "Combine all source parts into one vocabulary release.",
            recorder._inputs,
            [assembled],
            lambda: {"terms": terms, "relations": relations},
            source=run_release,
        )
        validated = recorder.intermediate("validated-release")

        def validate():
            vocab_contract.validate(
                terms, relations, _contract(target, ctx), release=release, complete=True
            )
            return payload

        recorder.run_child(
            "validate",
            "Validate the complete vocabulary release.",
            [assembled],
            [validated],
            validate,
            source=vocab_contract.validate,
        )
        written, outputs = _emit_layers(
            payload, ctx.output_dir / target.name, config, recorder, validated
        )
        recorder.complete_parent(outputs)
        completed = True
        _preserve_layers(written, recorder, ctx)
    except BaseException as error:
        if not completed:
            recorder.fail_parent(error)
        raise
    finally:
        recorder.close()

    # Copying or opening a PR packages the completed import; it does not publish on main.
    if ctx.deliver_local_root is not None:
        deliver_mod.place_release(written, ctx.deliver_local_root, release, with_lineage=True)
    deliver_mod.deliver_release(
        written,
        target,
        release,
        branch=f"import/{release}",
        title=f"Import {release}",
        body=f"Automated import of the {release} release by sentier_importers.",
        ctx=ctx,
        with_lineage=True,
    )
    return [path for files in written.values() for path in files]
