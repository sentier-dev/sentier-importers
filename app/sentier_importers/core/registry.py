"""Load the source manifest (``registry.yaml``) into :class:`SourceConfig`s and
instantiate the corresponding :class:`Source` plugins by import."""

import importlib
import inspect
import os
import re
from pathlib import Path

import yaml
from sentier_importers.core.errors import RegistryError
from sentier_importers.core.source import Source, SourceConfig

REGISTRY_PATH = Path(__file__).resolve().parents[1] / "registry.yaml"

#: Environment variable naming the directory every ``file://`` input is relative to.
DATA_ROOT_VAR = "SENTIER_DATA_ROOT"
#: The one placeholder ``registry.yaml`` may use inside ``fetch.url`` / ``inputs``.
PLACEHOLDER = "${" + DATA_ROOT_VAR + "}"
#: Fallback root: the parent of this checkout, i.e. the ``~/dds`` sibling layout
#: (``app/sentier_importers/registry.yaml`` -> repo root -> its parent).
DEFAULT_DATA_ROOT = REGISTRY_PATH.parents[2].parent
#: Allowed values of a source's ``access`` key.
ACCESS_VALUES = ("public", "licensed", "private")

_ANY_PLACEHOLDER_RE = re.compile(r"\$\{[^}]*\}")


def resolve_data_root(explicit: Path | None = None) -> Path:
    """The data root, first hit wins: ``explicit`` (the ``--data-root`` flag), the
    ``SENTIER_DATA_ROOT`` environment variable, then :data:`DEFAULT_DATA_ROOT`.

    The root does not have to exist; a missing input is reported per file at fetch
    time with the root that was used.
    """
    if explicit is not None:
        return Path(explicit).expanduser().resolve()
    from_env = os.environ.get(DATA_ROOT_VAR)
    if from_env:
        return Path(from_env).expanduser().resolve()
    return DEFAULT_DATA_ROOT


def _expand(url: str, data_root: Path, source_name: str) -> str:
    """Substitute :data:`PLACEHOLDER` in ``url``; any other ``${...}`` token is a
    ``RegistryError`` so typos fail at load time instead of becoming a mystery path."""
    expanded = url.replace(PLACEHOLDER, str(data_root))
    leftover = _ANY_PLACEHOLDER_RE.search(expanded)
    if leftover:
        raise RegistryError(
            f"source {source_name!r}: unknown placeholder {leftover.group(0)} in {url!r} "
            f"(only {PLACEHOLDER} is supported)"
        )
    return expanded


def _to_config(entry: dict, data_root: Path) -> SourceConfig:
    try:
        name = entry["name"]
        fetch = entry["fetch"]
        access = entry.get("access", "public")
        if access not in ACCESS_VALUES:
            raise RegistryError(
                f"source {name!r}: access must be one of {ACCESS_VALUES}, got {access!r}"
            )
        collection = entry.get("collection") or {}
        dedup = entry.get("dedup") or {}
        package = entry.get("package") or {}
        return SourceConfig(
            name=name,
            module=entry["module"],
            target=entry["target"],
            category=entry["category"],
            fetch_url=_expand(fetch["url"], data_root, name),
            fetch_format=fetch["format"],
            output_format=entry["output_format"],
            validate_against=entry.get("validate_against"),
            enabled=entry.get("enabled", True),
            emit_filename=entry.get("emit_filename"),
            collection_class=collection.get("class"),
            collection_items_key=collection.get("items_key"),
            collection_scheme=collection.get("scheme"),
            schema_file=collection.get("schema_file"),
            dedup_on_existing=dedup.get("on_existing", "skip"),
            dedup_check_existing=dedup.get("check_existing", True),
            package_name=package.get("name"),
            package_version=package.get("version"),
            package_verb=package.get("verb"),
            inputs={
                key: _expand(value, data_root, name)
                for key, value in (entry.get("inputs") or {}).items()
            },
            access=access,
        )
    except (KeyError, TypeError) as exc:
        raise RegistryError(f"invalid source entry {entry!r}: {exc}") from exc


def load_registry(path: Path = REGISTRY_PATH, data_root: Path | None = None) -> list[SourceConfig]:
    """Parse ``registry.yaml`` into a list of :class:`SourceConfig`, with every
    ``${SENTIER_DATA_ROOT}`` expanded to ``data_root`` (see :func:`resolve_data_root`)."""
    root = resolve_data_root(data_root)
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return [_to_config(entry, root) for entry in raw.get("sources", [])]


def get_config(
    name: str, configs: list[SourceConfig] | None = None, data_root: Path | None = None
) -> SourceConfig:
    """Return the config named ``name`` (loading the registry if not provided)."""
    if configs is None:
        configs = load_registry(data_root=data_root)
    for config in configs:
        if config.name == name:
            return config
    raise RegistryError(f"no source named {name!r}")


def load_source(config: SourceConfig) -> Source:
    """Import ``config.module`` and instantiate its single :class:`Source` subclass."""
    module = importlib.import_module(config.module)
    classes = [
        obj
        for _, obj in inspect.getmembers(module, inspect.isclass)
        if obj.__module__ == config.module and issubclass(obj, Source) and obj is not Source
    ]
    if len(classes) != 1:
        raise RegistryError(
            f"module {config.module!r} must define exactly one Source subclass, "
            f"found {len(classes)}"
        )
    return classes[0](config)
