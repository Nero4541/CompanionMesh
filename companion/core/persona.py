from __future__ import annotations

import re
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, ValidationError

from companion.core.config import ConfigError

_NAME = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


class Persona(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    display_name: str | None = None
    language: str | None = None
    voice: str | None = None
    system_prompt: str


def load_persona(personas_dir: Path, name: str) -> Persona:
    if not _NAME.match(name):
        raise ConfigError(f"invalid persona name: {name!r}")
    path = personas_dir / f"{name}.yaml"
    if not path.is_file():
        raise ConfigError(f"persona not found: {path}")
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        return Persona.model_validate({"name": name, **data})
    except (yaml.YAMLError, ValidationError) as exc:
        raise ConfigError(f"{path}: invalid persona: {exc}") from exc
