"""Saving settings edited in the dashboard.

Edits go to the ``*.local.yaml`` override next to the main config, never to
the main file: only the values that differ from it are written, so the
override stays small and the committed defaults stay untouched. The previous
override is kept as ``*.previous.local.yaml``, and a hand-written one (which may
hold comments the dashboard does not keep) also as ``*.original.local.yaml``,
once. All of them are git-ignored like the override itself.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from companion.core.config import (
    CompanionConfig,
    ConfigSource,
    changed_settings,
    deep_merge,
    get_setting,
    validation_errors,
)

HEADER = (
    "# Machine-specific overrides, deep-merged over {main}.\n"
    "# Written by the admin dashboard; the previous version is in {previous}.\n"
)


class ConfigEditError(Exception):
    """The edited settings cannot be saved; ``errors`` name the settings."""

    def __init__(self, errors: list[dict[str, str]]) -> None:
        super().__init__("; ".join(f"{e['path']}: {e['message']}" for e in errors))
        self.errors = errors


def _sibling(source: ConfigSource, tag: str) -> Path:
    return source.local_path.with_name(f"{source.path.stem}.{tag}.local{source.path.suffix}")


def previous_path(source: ConfigSource) -> Path:
    return _sibling(source, "previous")


def original_path(source: ConfigSource) -> Path:
    return _sibling(source, "original")


def _difference(base: Any, desired: Any) -> Any:
    """The parts of ``desired`` that differ from ``base`` (mappings recursively)."""
    if isinstance(base, dict) and isinstance(desired, dict):
        out = {}
        for key, value in desired.items():
            if key not in base:
                out[key] = value
            elif base[key] != value:
                out[key] = _difference(base[key], value)
        return out
    return desired


def build_override(source: ConfigSource, desired: dict[str, Any]) -> dict[str, Any]:
    """The smallest override that turns the main config into ``desired``.

    Raises :class:`ConfigEditError` when ``desired`` is invalid, or when an
    override cannot express it: deep-merging can add or change keys of a
    mapping such as ``extra_body`` but never remove one set in the main file.
    """
    try:
        wanted = CompanionConfig.model_validate(desired).model_dump(mode="json")
    except ValidationError as exc:
        raise ConfigEditError(validation_errors(exc)) from exc
    base_raw = source.read_base()
    try:
        base = CompanionConfig.model_validate(base_raw).model_dump(mode="json")
    except ValidationError as exc:
        raise ConfigEditError(
            [
                {"path": e["path"], "message": f"{source.path.name}: {e['message']}"}
                for e in validation_errors(exc)
            ]
        ) from exc

    override = _difference(base, wanted)
    result = CompanionConfig.model_validate(deep_merge(base_raw, override)).model_dump(mode="json")
    unexpressible = changed_settings(result, wanted)
    if unexpressible:
        raise ConfigEditError(
            [
                {
                    "path": path,
                    "message": f"set in {source.path.name}, which an override cannot remove "
                    f"(it would stay {get_setting(result, path)!r}); edit {source.path.name}",
                }
                for path in unexpressible
            ]
        )
    return override


def write_override(source: ConfigSource, override: dict[str, Any]) -> Path:
    """Replace the override file atomically, keeping the previous one."""
    local = source.local_path
    local.parent.mkdir(parents=True, exist_ok=True)
    previous = previous_path(source)
    if local.is_file():
        shutil.copy2(local, previous)
        original = original_path(source)
        written_by_us = local.read_text(encoding="utf-8").startswith(HEADER.splitlines()[0])
        if not written_by_us and not original.exists():
            shutil.copy2(local, original)
    text = HEADER.format(main=source.path.name, previous=previous.name)
    if override:
        text += yaml.safe_dump(override, sort_keys=False, allow_unicode=True)
    tmp = local.with_name(f".{local.name}.tmp")
    tmp.write_text(text, encoding="utf-8", newline="\n")
    os.replace(tmp, local)
    return local


def save_settings(source: ConfigSource, desired: dict[str, Any]) -> Path:
    """Validate ``desired`` (unresolved settings, no command-line overrides) and save it."""
    if not source.path.is_file():
        raise ConfigEditError(
            [{"path": "", "message": f"{source.path} does not exist; create it to save settings"}]
        )
    return write_override(source, build_override(source, desired))
