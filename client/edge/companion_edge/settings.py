"""Remote settings: report the config to the server and take changes from it.

The server's dashboard sees what ``describe`` reports and sends back a whole
config; ``apply_update`` checks it, ``save`` writes it into the TOML file,
changing only the lines whose values changed (comment lines stay), and keeps
the previous file as ``<name>.bak``. Settings that could lock the device out or
change who it is (server, servers, server_cert, token_env, device_id,
state_file) are read-only.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import fields, replace
from pathlib import Path
from typing import Any

from companion_edge.config import AudioConfig, CameraConfig, EdgeConfig, load

READ_ONLY = frozenset({"server", "servers", "server_cert", "token_env", "device_id", "state_file"})
SECTIONS = {"audio": AudioConfig, "camera": CameraConfig}
CHOICES = {
    "audio.codec": ["opus", "pcm"],
    "audio.opus_encoder": ["auto", "libopus", "ffmpeg"],
    "audio.aec": ["none", "device"],
    "camera.mode": ["change", "periodic"],
    "camera.driver": ["v4l2", "cvi"],
    "camera.rotate": [0, 90, 180, 270],
    "camera.stream_format": ["mjpeg", "h264"],
}
# Numbers that must be positive (the rest may be 0 or more).
POSITIVE = frozenset(
    {
        "heartbeat_s",
        "audio.opus_bitrate",
        "audio.packets_per_frame",
        "camera.analysis_fps",
        "camera.heartbeat_s",
        "camera.min_interval_s",
        "camera.max_interval_s",
        "camera.quality",
        "camera.stream_fps",
    }
)
_HEADER = "# Changed from the Companion dashboard; the previous version is in {bak}.\n"


def _kind(value: Any) -> str:
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, int):
        return "int"
    if isinstance(value, float):
        return "float"
    if isinstance(value, list):
        return "list"
    return "str"


def _plain(value: Any) -> Any:
    return str(value) if isinstance(value, Path) else value


def to_dict(config: EdgeConfig) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for f in fields(config):
        value = getattr(config, f.name)
        if f.name in SECTIONS:
            out[f.name] = {g.name: _plain(getattr(value, g.name)) for g in fields(value)}
        else:
            out[f.name] = _plain(value)
    return out


def schema() -> dict[str, dict[str, Any]]:
    """Every setting: its type, allowed values and whether the server may change it.

    Types come from the defaults: a file saying ``heartbeat_s = 30`` still
    holds a number of seconds that may have a fraction.
    """
    out: dict[str, dict[str, Any]] = {}
    for key, value in to_dict(EdgeConfig()).items():
        if key in SECTIONS:
            for name, item in value.items():
                path = f"{key}.{name}"
                out[path] = {"type": _kind(item), "editable": True}
                if path in CHOICES:
                    out[path]["choices"] = CHOICES[path]
        else:
            out[key] = {"type": _kind(value), "editable": key not in READ_ONLY}
    return out


def describe(config: EdgeConfig, path: Path | None) -> dict[str, Any]:
    """Payload of ``device.config``: the settings, their schema, and whether they can be saved."""
    return {
        "config": to_dict(config),
        "schema": schema(),
        "editable": path is not None,
        "file": str(path) if path else None,
    }


def _check(path: str, value: Any, kind: str) -> Any:
    if kind == "bool":
        if not isinstance(value, bool):
            raise ValueError(f"{path}: expected true or false")
    elif kind == "int":
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError(f"{path}: expected a whole number")
    elif kind == "float":
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"{path}: expected a number")
        value = float(value)
    elif not isinstance(value, str):
        raise ValueError(f"{path}: expected text")
    if kind in ("int", "float"):
        if value < 0 or (value == 0 and path in POSITIVE):
            raise ValueError(f"{path}: must be {'above' if path in POSITIVE else 'at least'} 0")
    if path in CHOICES and value not in CHOICES[path]:
        raise ValueError(f"{path}: one of {', '.join(map(str, CHOICES[path]))}")
    return value


def apply_update(current: EdgeConfig, update: dict[str, Any]) -> EdgeConfig:
    """``current`` with the changes in ``update`` (a whole or partial config).

    Raises ValueError for unknown settings, wrong types or values, and for any
    change to a read-only setting.
    """
    kinds = schema()
    now = to_dict(current)
    changes: dict[str, Any] = {}
    sections: dict[str, dict[str, Any]] = {}
    for key, value in update.items():
        if key in SECTIONS:
            if not isinstance(value, dict):
                raise ValueError(f"{key}: expected a table of settings")
            for name, item in value.items():
                path = f"{key}.{name}"
                if path not in kinds:
                    raise ValueError(f"unknown setting {path}")
                sections.setdefault(key, {})[name] = _check(path, item, kinds[path]["type"])
        elif key not in kinds:
            raise ValueError(f"unknown setting {key}")
        elif key in READ_ONLY:
            if value != now[key]:
                raise ValueError(f"{key} can only be changed on the device itself")
        else:
            changes[key] = _check(key, value, kinds[key]["type"])
    for key, values in sections.items():
        changes[key] = replace(getattr(current, key), **values)
    return replace(current, **changes)


def with_server(current: EdgeConfig, url: str) -> EdgeConfig:
    """``current`` also trying ``url``, after the addresses it has.

    Not a remote setting (the addresses are read-only): the device adds the
    server's Tailscale address itself once it has joined the tailnet, so the
    address it reaches the server by now stays first.
    """
    urls = current.server_urls()
    if url in urls:
        return current
    return replace(current, servers=[*urls, url])


def _toml_value(value: Any) -> str:
    if isinstance(value, list):
        return "[" + ", ".join(_toml_value(v) for v in value) + "]"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return repr(value)
    text = str(value).replace("\\", "\\\\").replace('"', '\\"')
    return f'"{text}"'


def render(text: str, old: EdgeConfig, new: EdgeConfig) -> str:
    """``text`` (a TOML config) with the values that differ between ``old`` and ``new``.

    Changed keys are rewritten in place. Comment lines stay; a changed line's
    own trailing comment goes, as it usually described the old value. Keys the
    file did not set yet go at the end of their table.
    """
    before, after = to_dict(old), to_dict(new)
    pending: dict[str, dict[str, Any]] = {
        "": {k: v for k, v in after.items() if k not in SECTIONS and before.get(k) != v}
    }
    for name in SECTIONS:
        pending[name] = {k: v for k, v in after[name].items() if before[name].get(k) != v}

    out: list[str] = []
    section = ""
    seen = {""}

    def flush(name: str) -> None:
        additions = [f"{k} = {_toml_value(v)}" for k, v in pending.get(name, {}).items()]
        pending[name] = {}
        trailing: list[str] = []
        while out and not out[-1].strip():
            trailing.append(out.pop())
        out.extend(additions)
        out.extend(trailing)

    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("["):
            flush(section)
            section = stripped[1 : stripped.find("]")].strip()
            seen.add(section)
            out.append(line)
            continue
        if stripped and not stripped.startswith("#") and "=" in stripped:
            key = stripped.split("=", 1)[0].strip()
            if key in pending.get(section, {}):
                value = pending[section].pop(key)
                indent = line[: len(line) - len(line.lstrip())]
                out.append(f"{indent}{key} = {_toml_value(value)}")
                continue
        out.append(line)
    flush(section)
    for name in SECTIONS:
        if pending[name]:
            out.extend(["", f"[{name}]"])
            flush(name)
    return "\n".join(out) + "\n"


def _changes(old: EdgeConfig, new: EdgeConfig) -> dict[str, Any]:
    before, after = to_dict(old), to_dict(new)
    out: dict[str, Any] = {}
    for key, value in after.items():
        if key in SECTIONS:
            out.update({f"{key}.{n}": v for n, v in value.items() if before[key].get(n) != v})
        elif before.get(key) != value:
            out[key] = value
    return out


def save(path: Path, old: EdgeConfig, new: EdgeConfig) -> None:
    """Write the changes from ``old`` to ``new`` into the TOML file at ``path``.

    ``old`` is the running config, which may differ from the file where the
    command line overrides it (``--server``); only changed settings are written.
    The previous file is kept as ``.bak``.
    """
    existed = path.exists()
    original = path.read_text(encoding="utf-8") if existed else ""
    bak = path.with_name(path.name + ".bak")
    text = render(original, old, new)
    if not text.startswith("# Changed from the Companion dashboard"):
        text = _HEADER.format(bak=bak.name) + text
    tomllib.loads(text)  # never write a file the device cannot read back
    if existed:
        bak.write_text(original, encoding="utf-8")
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)
    reread = to_dict(load(path))
    for setting, value in _changes(old, new).items():
        section, _, name = setting.rpartition(".")
        if (reread[section][name] if section else reread[setting]) != value:
            if existed:
                bak.replace(path)  # put the previous file back
            else:
                path.unlink()
            raise ValueError(f"{setting}: the saved file did not read back as {value!r}")
