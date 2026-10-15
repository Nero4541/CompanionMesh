"""The board's Wi-Fi networks (wpa_supplicant), changed from the dashboard.

Networks are stored with their hashed key (the server computes it), never the
password. On Sipeed's image the file is also written to the boot partition,
from where the image copies it at every start. The network the board is
connected through now cannot be removed remotely (that would cut it off).
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import Any

CONFIG = Path("/etc/wpa_supplicant.conf")
BOOT_COPY = Path("/boot/wpa_supplicant.conf")
_NETWORK = re.compile(r"network=\{(.*?)\n\s*\}", re.S)


class WifiError(Exception):
    pass


def _ssid(block: str) -> str | None:
    match = re.search(r"^\s*ssid=(.+)$", block, re.M)
    if not match:
        return None
    value = match[1].strip()
    if value.startswith('"') and value.endswith('"'):
        return value[1:-1]
    try:
        return bytes.fromhex(value).decode("utf-8", "replace")
    except ValueError:
        return value


def networks(text: str) -> list[tuple[str, str]]:
    """(ssid, block text) of each network in a wpa_supplicant.conf."""
    out = []
    for match in _NETWORK.finditer(text):
        ssid = _ssid(match[1])
        if ssid is not None:
            out.append((ssid, match[1]))
    return out


def current_ssid() -> str | None:
    try:
        out = subprocess.run(
            ["wpa_cli", "-i", "wlan0", "status"], capture_output=True, text=True, timeout=5
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    match = re.search(r"^ssid=(.*)$", out, re.M)
    return match[1] if match else None


def render(add: list[dict[str, Any]], keep: list[tuple[str, str]]) -> str:
    lines = ["ctrl_interface=/var/run/wpa_supplicant", "update_config=0", ""]
    for index, network in enumerate(add):
        ssid = str(network["ssid"])
        psk = network.get("psk")
        body = [f"\tssid={ssid.encode().hex()}", "\tscan_ssid=1"]
        if psk:
            if not re.fullmatch(r"[0-9a-fA-F]{64}", str(psk)):
                raise WifiError(f"{ssid}: the key must be 64 hex digits")
            body.append(f"\tpsk={psk}")
        else:
            body.append("\tkey_mgmt=NONE")
        body.append(f"\tpriority={len(add) - index + 10}")
        lines += [f"# {ssid}", "network={", *body, "}", ""]
    for _, block in keep:
        lines += ["network={" + block, "}", ""]
    return "\n".join(lines)


def state(config: Path | None = None) -> dict[str, Any]:
    config = config or CONFIG
    text = config.read_text(encoding="utf-8") if config.is_file() else ""
    return {"networks": [ssid for ssid, _ in networks(text)], "current": current_ssid()}


def change(
    add: list[dict[str, Any]],
    remove: list[str],
    *,
    config: Path | None = None,
    boot_copy: Path | None = None,
    reload: bool = True,
) -> dict[str, Any]:
    """Add (or replace) and remove networks; returns the new state."""
    config, boot_copy = config or CONFIG, boot_copy or BOOT_COPY
    if not config.is_file():
        raise WifiError(f"{config} not found: this board's Wi-Fi is not set up through it")
    now = current_ssid()
    if now and now in remove:
        raise WifiError(f"{now!r} is the network the board uses now; it cannot be removed remotely")
    text = config.read_text(encoding="utf-8")
    replaced = {str(n["ssid"]) for n in add} | set(remove)
    keep = [(ssid, block) for ssid, block in networks(text) if ssid not in replaced]
    new = render(add, keep)
    tmp = config.with_name(config.name + ".tmp")
    tmp.write_text(new, encoding="utf-8")
    tmp.chmod(0o600)
    tmp.replace(config)
    if boot_copy.parent.is_dir() and (
        boot_copy.exists() or (boot_copy.parent / "wifi.sta").exists()
    ):
        boot_copy.write_text(new, encoding="utf-8")
    if reload:
        subprocess.run(
            ["wpa_cli", "-i", "wlan0", "reconfigure"], capture_output=True, timeout=10, check=False
        )
    return state(config)
