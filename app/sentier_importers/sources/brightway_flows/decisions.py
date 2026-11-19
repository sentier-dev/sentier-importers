"""Reviewed decisions on the Brightway Flows links of the BAFU pair that change a score.

``bafu_decisions.yaml`` holds one decision per BAFU flow code, made by the project
owner on correctness (2026-11-19): ``theirs`` (take the Brightway Flows target),
``ours`` (keep our target) or ``hold`` (no entry). Flows are filed under a group that
carries the decision and its reason; ``name``, ``context`` and ``unit`` repeat the
flow for the reader and let ``bafu.py`` refuse a decision whose flow has changed.

The file is read strictly: a key it does not expect, a flow or group written twice,
or a decision that is not one of the three is a ``ValidationError``.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml
from sentier_importers.core.errors import ValidationError

DECISIONS_PATH = Path(__file__).with_name("bafu_decisions.yaml")
#: The day the decisions were made; emitted comments name it.
REVIEWED = "2026-11-19"
THEIRS, OURS, HOLD = "theirs", "ours", "hold"
_DECISIONS = (THEIRS, OURS, HOLD)
_TOP_FIELDS = {"groups", "flows"}
_GROUP_FIELDS = {"decision", "reason"}
_FLOW_FIELDS = {"group", "name", "context", "unit"}


@dataclass(frozen=True)
class FlowDecision:
    """The decision on one BAFU flow, with the flow record it was made for."""

    code: str
    group: str
    decision: str
    reason: str
    name: str
    context: tuple[str, ...]
    unit: str


class _DuplicateKey(Exception):
    pass


class _UniqueKeyLoader(yaml.SafeLoader):
    """A safe loader that refuses a mapping key written twice (YAML keeps the last)."""

    def construct_mapping(self, node, deep=False):
        seen = set()
        for key_node, _ in node.value:
            key = self.construct_object(key_node, deep=True)
            if key in seen:
                raise _DuplicateKey(key)
            seen.add(key)
        return super().construct_mapping(node, deep)


def _text(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _groups(raw: object, path: Path) -> dict[str, tuple[str, str]]:
    if not isinstance(raw, dict):
        raise ValidationError(f"{path}: 'groups' must be a mapping")
    groups: dict[str, tuple[str, str]] = {}
    for name, body in raw.items():
        if not isinstance(body, dict) or set(body) != _GROUP_FIELDS:
            raise ValidationError(
                f"{path}: group {name!r} must have exactly {sorted(_GROUP_FIELDS)}"
            )
        if body["decision"] not in _DECISIONS:
            raise ValidationError(
                f"{path}: group {name!r} has decision {body['decision']!r}, "
                f"not one of {list(_DECISIONS)}"
            )
        if not _text(body["reason"]):
            raise ValidationError(f"{path}: group {name!r} has a blank or non-string reason")
        groups[name] = (body["decision"], body["reason"].strip())
    return groups


def _flow(code: object, body: object, groups: dict, path: Path) -> FlowDecision:
    if not _text(code):
        raise ValidationError(f"{path}: flow code {code!r} is not a string")
    if not isinstance(body, dict) or set(body) != _FLOW_FIELDS:
        raise ValidationError(f"{path}: flow {code} must have exactly {sorted(_FLOW_FIELDS)}")
    if body["group"] not in groups:
        raise ValidationError(f"{path}: flow {code} names the unknown group {body['group']!r}")
    for field in ("name", "unit"):
        if not _text(body[field]):
            raise ValidationError(f"{path}: flow {code} has a blank or non-string {field}")
    context = body["context"]
    if not isinstance(context, list) or not context or not all(_text(c) for c in context):
        raise ValidationError(f"{path}: flow {code}: context must be a list of strings")
    decision, reason = groups[body["group"]]
    return FlowDecision(
        code=code,
        group=body["group"],
        decision=decision,
        reason=reason,
        name=body["name"],
        context=tuple(context),
        unit=body["unit"],
    )


def load_decisions(path: Path = DECISIONS_PATH) -> dict[str, FlowDecision]:
    """``{BAFU flow code: FlowDecision}`` from a decisions file."""
    try:
        data = yaml.load(path.read_text(encoding="utf-8"), Loader=_UniqueKeyLoader)  # noqa: S506
    except _DuplicateKey as exc:
        raise ValidationError(f"{path}: {exc.args[0]!r} is written more than once") from exc
    except yaml.YAMLError as exc:
        raise ValidationError(f"{path}: not valid YAML: {exc}") from exc
    if not isinstance(data, dict) or set(data) != _TOP_FIELDS:
        raise ValidationError(f"{path}: must be a mapping with exactly {sorted(_TOP_FIELDS)}")
    groups = _groups(data["groups"], path)
    if not isinstance(data["flows"], dict):
        raise ValidationError(f"{path}: 'flows' must be a mapping")
    return {code: _flow(code, body, groups, path) for code, body in data["flows"].items()}
