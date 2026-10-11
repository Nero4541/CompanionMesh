from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, ValidationError

from companion.core.config import ConfigError

_NAME = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

# Shipped with the project and tracked in git: shown, copied, never rewritten.
BUILTIN_PERSONAS = frozenset({"default"})

_HEADER = "# Persona written by the admin dashboard.\n"


class Persona(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    display_name: str | None = None
    language: str | None = None
    voice: str | None = None
    system_prompt: str


def valid_name(name: str) -> bool:
    return bool(_NAME.match(name))


def is_builtin(name: str) -> bool:
    return name.lower() in BUILTIN_PERSONAS


def persona_path(personas_dir: Path, name: str) -> Path:
    if not valid_name(name):
        raise ConfigError(f"invalid persona name: {name!r} (letters, digits, - and _)")
    return personas_dir / f"{name}.yaml"


def load_persona(personas_dir: Path, name: str) -> Persona:
    path = persona_path(personas_dir, name)
    if not path.is_file():
        raise ConfigError(f"persona not found: {path}")
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        return Persona.model_validate({"name": name, **data})
    except (yaml.YAMLError, ValidationError) as exc:
        raise ConfigError(f"{path}: invalid persona: {exc}") from exc


def list_personas(personas_dir: Path) -> list[str]:
    if not personas_dir.is_dir():
        return []
    return sorted(p.stem for p in personas_dir.glob("*.yaml") if p.is_file() and valid_name(p.stem))


class _BlockDumper(yaml.SafeDumper):
    """Multi-line strings (system prompts) as readable ``|`` blocks."""


def _str(dumper: yaml.SafeDumper, value: str) -> Any:
    style = "|" if "\n" in value else None
    return dumper.represent_scalar("tag:yaml.org,2002:str", value, style=style)


_BlockDumper.add_representer(str, _str)


def save_persona(personas_dir: Path, persona: Persona) -> Path:
    """Write a persona file atomically. Built-in personas are never rewritten."""
    if is_builtin(persona.name):
        raise ConfigError(f"{persona.name!r} ships with the project; save a copy instead")
    path = persona_path(personas_dir, persona.name)
    personas_dir.mkdir(parents=True, exist_ok=True)
    data = persona.model_dump(exclude={"name"})
    text = _HEADER + yaml.dump(data, Dumper=_BlockDumper, sort_keys=False, allow_unicode=True)
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(text, encoding="utf-8", newline="\n")
    os.replace(tmp, path)
    return path


def delete_persona(personas_dir: Path, name: str) -> None:
    if is_builtin(name):
        raise ConfigError(f"{name!r} ships with the project and cannot be deleted")
    path = persona_path(personas_dir, name)
    if not path.is_file():
        raise ConfigError(f"persona not found: {path}")
    path.unlink()
