"""Reading and updating the ``.env`` file without disturbing the rest of it.

``set_values`` replaces the line of each variable it is given (keeping its
position and the comments around it) and appends the ones the file lacks. A
missing ``.env`` starts as a copy of ``.env.example`` when there is one, so the
explanations for each variable stay with the user's file.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

from dotenv import dotenv_values

_LINE = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=")


def read_values(path: Path) -> dict[str, str]:
    if not path.is_file():
        return {}
    return {k: v for k, v in dotenv_values(path, encoding="utf-8").items() if v is not None}


def _quote(value: str) -> str:
    if value and re.fullmatch(r"[A-Za-z0-9_\-.:/+=@]+", value):
        return value
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def set_values(path: Path, values: dict[str, str], *, example: Path | None = None) -> None:
    """Write ``values`` into the env file at ``path`` (created if needed)."""
    if path.is_file():
        lines = path.read_text(encoding="utf-8").splitlines()
    elif example is not None and example.is_file():
        lines = example.read_text(encoding="utf-8").splitlines()
    else:
        lines = []
    pending = dict(values)
    for i, line in enumerate(lines):
        match = _LINE.match(line)
        if match and match.group(1) in pending:
            name = match.group(1)
            lines[i] = f"{name}={_quote(pending.pop(name))}"
    if pending:
        if lines and lines[-1].strip():
            lines.append("")
        lines.extend(f"{name}={_quote(value)}" for name, value in pending.items())
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")
    os.replace(tmp, path)
    if os.name != "nt":
        path.chmod(0o600)
    os.environ.update(values)
