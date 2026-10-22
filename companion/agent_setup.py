"""Connect Hermes or OpenClaw to the companion's tools (MCP), the same way for both.

    uv run companion agent-tools                         print what to add, for both agents
    uv run companion agent-tools --url http://100.x.y.z:8765/mcp
    uv run companion agent-tools --token                 create COMPANION_AGENT_TOKEN in .env
    uv run companion agent-tools --write-hermes ~/.hermes/config.yaml
    uv run companion agent-tools --write-openclaw ~/.openclaw/openclaw.json

Hermes reads ``mcp_servers`` in its config.yaml; OpenClaw ``mcp.servers`` in
openclaw.json (or ``openclaw mcp set``). Both expand ``${VAR}``, so the token
stays in the agent's own environment (Hermes: ~/.hermes/.env), not in the
file. Writing a file keeps a copy of it first (``.bak-companion``) and leaves
everything else in it as it was.
"""

from __future__ import annotations

import json
import re
import secrets
import shutil
from pathlib import Path
from typing import Any

import yaml

NAME = "companion"


def mcp_url(host: str, port: int) -> str:
    host = "127.0.0.1" if host in ("0.0.0.0", "::", "") else host
    return f"http://{host}:{port}/mcp"


def hermes_entry(url: str, token_env: str) -> dict[str, Any]:
    return {
        "url": url,
        "headers": {"Authorization": f"Bearer ${{{token_env}}}"},
        "timeout": 60,
        "connect_timeout": 10,
    }


def openclaw_entry(url: str, token_env: str) -> dict[str, Any]:
    return {
        "url": url,
        "transport": "streamable-http",
        "connectionTimeoutMs": 10000,
        "requestTimeoutMs": 60000,
        "headers": {"Authorization": f"Bearer ${{{token_env}}}"},
    }


def hermes_snippet(url: str, token_env: str) -> str:
    return yaml.safe_dump(
        {"mcp_servers": {NAME: hermes_entry(url, token_env)}}, sort_keys=False, allow_unicode=True
    )


def openclaw_snippet(url: str, token_env: str) -> str:
    return json.dumps({"mcp": {"servers": {NAME: openclaw_entry(url, token_env)}}}, indent=2)


def openclaw_command(url: str, token_env: str) -> str:
    entry = json.dumps(openclaw_entry(url, token_env), separators=(",", ":"))
    return f"openclaw mcp set {NAME} '{entry}'"


def _backup(path: Path) -> Path:
    copy = path.with_name(path.name + ".bak-companion")
    shutil.copy2(path, copy)
    return copy


def write_hermes(path: Path, url: str, token_env: str) -> str:
    """Add (or replace) the companion under ``mcp_servers`` in Hermes' config.yaml.

    Only lines belonging to this entry are touched; comments and the rest stay.
    """
    text = path.read_text(encoding="utf-8") if path.is_file() else ""
    data = yaml.safe_load(text) if text.strip() else {}
    if not isinstance(data, dict):
        raise ValueError(f"{path}: not a YAML mapping")
    if path.is_file():
        _backup(path)
    entry = yaml.safe_dump({NAME: hermes_entry(url, token_env)}, sort_keys=False)
    block = "".join(f"  {line}\n" for line in entry.splitlines())
    lines = text.splitlines(keepends=True)
    top = next(
        (i for i, line in enumerate(lines) if re.match(r"^mcp_servers:\s*(#.*)?$", line)), None
    )
    if top is None:
        if text and not text.endswith("\n"):
            text += "\n"
        text += f"\nmcp_servers:\n{block}"
        path.write_text(text, encoding="utf-8")
        return "added mcp_servers with the companion"
    if isinstance(data.get("mcp_servers"), dict) and NAME in data["mcp_servers"]:
        # Replace the old entry: from "  companion:" to the next line indented two or less.
        start = next(
            i for i in range(top + 1, len(lines)) if re.match(rf"^  {NAME}:\s*(#.*)?$", lines[i])
        )
        end = start + 1
        while end < len(lines) and (
            not lines[end].strip() or lines[end].startswith("   ") or lines[end].startswith("\t")
        ):
            end += 1
        lines[start:end] = [block]
        path.write_text("".join(lines), encoding="utf-8")
        return "replaced the companion under mcp_servers"
    lines.insert(top + 1, block)
    path.write_text("".join(lines), encoding="utf-8")
    return "added the companion under mcp_servers"


def write_openclaw(path: Path, url: str, token_env: str) -> str:
    """Set ``mcp.servers.companion`` in openclaw.json (other settings stay as they are)."""
    data = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
    if not isinstance(data, dict):
        raise ValueError(f"{path}: not a JSON object")
    if path.is_file():
        _backup(path)
    servers = data.setdefault("mcp", {}).setdefault("servers", {})
    replaced = NAME in servers
    servers[NAME] = openclaw_entry(url, token_env)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return ("replaced" if replaced else "added") + " mcp.servers.companion"


def run_cli(args: Any, config: Any, env_file: Path) -> int:
    from companion.core.envfile import read_values, set_values

    cfg = config.mcp
    token_env = cfg.token_env or "COMPANION_AGENT_TOKEN"
    url = args.url or mcp_url(config.server.host, config.server.port)
    if args.token:
        if read_values(env_file).get(token_env):
            print(f"{token_env} is already set in {env_file}")
        else:
            set_values(env_file, {token_env: secrets.token_urlsafe(32)})
            print(f"{token_env} written to {env_file}: copy the same value to the agent's env")
    for path, write, name in (
        (args.write_hermes, write_hermes, "Hermes"),
        (args.write_openclaw, write_openclaw, "OpenClaw"),
    ):
        if path:
            target = Path(path).expanduser()
            print(
                f"{name}: {write(target, url, token_env)} in {target} (copy kept as .bak-companion)"
            )
    if not (args.write_hermes or args.write_openclaw):
        print("The companion's tools, for MCP clients, at", url)
        if not cfg.enabled:
            print("(turn them on first: mcp.enabled: true)")
        print(f"\nBoth agents read the token from their environment as ${{{token_env}}}.")
        print("\nHermes: add to ~/.hermes/config.yaml, and the token to ~/.hermes/.env:\n")
        print(hermes_snippet(url, token_env))
        print("OpenClaw: in openclaw.json (or run the command below):\n")
        print(openclaw_snippet(url, token_env))
        print("\n" + openclaw_command(url, token_env))
    return 0
