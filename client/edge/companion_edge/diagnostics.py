"""Facts about the device for the dashboard's Diagnostics button.

A fixed list of read-only commands (no remote shell): versions, the same
checks as ``--check``, memory, disk, network and Wi-Fi state, the kernel's
last messages and the last crash, if any. Missing tools are reported as such.
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import json
import platform
import sys
from pathlib import Path

from companion_edge import __version__, settings
from companion_edge.config import EdgeConfig

SECTION_BYTES = 16 * 1024
COMMAND_S = 5.0

COMMANDS = {
    "uptime": "uptime",
    "memory": "free",
    "disk": "df -h /",
    "network": "ip addr",
    "routes": "ip route",
    "wifi": "wpa_cli -i wlan0 status 2>/dev/null || iw dev wlan0 link",
    "kernel": "dmesg | tail -n 60",
}


def _cut(text: str) -> str:
    if len(text) <= SECTION_BYTES:
        return text
    return "…\n" + text[-SECTION_BYTES:]


async def _run(command: str) -> str:
    try:
        proc = await asyncio.create_subprocess_shell(
            command, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT
        )
    except OSError as exc:
        return f"cannot run: {exc}"
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), COMMAND_S)
    except TimeoutError:
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
        return f"no answer within {COMMAND_S:.0f} s"
    return out.decode("utf-8", "replace").strip() or "(nothing)"


def _check(config: EdgeConfig) -> str:
    from companion_edge.__main__ import check

    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        check(config)
    return out.getvalue().strip()


async def collect(
    config: EdgeConfig, config_path: Path | None, log_file: Path | None = None
) -> dict[str, str]:
    sections: dict[str, str] = {
        "version": (
            f"companion-edge {__version__}\nPython {sys.version.split()[0]}\n"
            f"{platform.platform()} ({platform.machine()})"
        ),
        "check": await asyncio.to_thread(_check, config),
        "settings": json.dumps(settings.to_dict(config), indent=2, ensure_ascii=False)
        + (f"\n(from {config_path})" if config_path else "\n(no settings file)"),
    }
    results = await asyncio.gather(*(_run(c) for c in COMMANDS.values()))
    sections.update(zip(COMMANDS, results, strict=True))
    if log_file is not None:
        crash = log_file.with_name("crash.log")
        if crash.is_file() and crash.stat().st_size:
            sections["last crash"] = crash.read_text("utf-8", "replace")
    return {name: _cut(text) for name, text in sections.items()}
