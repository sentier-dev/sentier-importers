"""Load the source manifest (``registry.yaml``) into :class:`SourceConfig`s and
instantiate the corresponding :class:`Source` plugins by import."""

import importlib
import inspect
from pathlib import Path

import yaml
from sentier_importers.core.errors import RegistryError
from sentier_importers.core.source import Source, SourceConfig

REGISTRY_PATH = Path(__file__).resolve().parents[1] / "registry.yaml"


def _to_config(entry: dict) -> SourceConfig:
    try:
        fetch = entry["fetch"]
        collection = entry.get("collection") or {}
        dedup = entry.get("dedup") or {}
        package = entry.get("package") or {}
        return SourceConfig(
            name=entry["name"],
            module=entry["module"],
            target=entry["target"],
            category=entry["category"],
            fetch_url=fetch["url"],
            fetch_format=fetch["format"],
            output_format=entry["output_format"],
            validate_against=entry.get("validate_against"),
            enabled=entry.get("enabled", True),
            collection_class=collection.get("class"),
            collection_items_key=collection.get("items_key"),
            collection_scheme=collection.get("scheme"),
            schema_file=collection.get("schema_file"),
            dedup_on_existing=dedup.get("on_existing", "skip"),
            dedup_check_existing=dedup.get("check_existing", True),
            package_name=package.get("name"),
            package_version=package.get("version"),
            package_verb=package.get("verb"),
        )
    except (KeyError, TypeError) as exc:
        raise RegistryError(f"invalid source entry {entry!r}: {exc}") from exc


def load_registry(path: Path = REGISTRY_PATH) -> list[SourceConfig]:
    """Parse ``registry.yaml`` into a list of :class:`SourceConfig`."""
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return [_to_config(entry) for entry in raw.get("sources", [])]


def get_config(name: str, configs: list[SourceConfig] | None = None) -> SourceConfig:
    """Return the config named ``name`` (loading the registry if not provided)."""
    if configs is None:
        configs = load_registry()
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
