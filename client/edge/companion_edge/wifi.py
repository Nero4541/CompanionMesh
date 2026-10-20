"""The board's Wi-Fi networks (wpa_supplicant), changed from the dashboard.

Networks are stored with their hashed key (the server computes it), never the
password. On Sipeed's image the file is also written to the boot partition,
from where the image copies it at every start. The network the board is
connected through now cannot be removed remotely (that would cut it off).
"""

from __future__ import annotations

import os
import re
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

CONFIG = Path("/etc/wpa_supplicant.conf")
BOOT_COPY = Path("/boot/wpa_supplicant.conf")
INTERFACE = "wlan0"
PROC = Path("/proc")
# udhcpc: SIGUSR2 releases the lease, SIGUSR1 renews (Linux numbers where Python lacks them).
RELEASE = getattr(signal, "SIGUSR2", 12)
RENEW = getattr(signal, "SIGUSR1", 10)
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


def link(interface: str = INTERFACE) -> str | None:
    """The network the board is connected to (wpa_state COMPLETED), or None."""
    try:
        out = subprocess.run(
            ["wpa_cli", "-i", interface, "status"], capture_output=True, text=True, timeout=5
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    if not re.search(r"^wpa_state=COMPLETED$", out, re.M):
        return None
    match = re.search(r"^ssid=(.*)$", out, re.M)
    return match[1] if match else None


def dhcp_clients(interface: str = INTERFACE, proc: Path | None = None) -> list[int]:
    """The udhcpc processes that look after ``interface``."""
    pids = []
    for entry in (proc or PROC).iterdir():
        if not entry.name.isdigit():
            continue
        try:
            args = (entry / "cmdline").read_bytes().split(b"\0")
        except OSError:
            continue
        names = [os.path.basename(a.decode(errors="replace")) for a in args if a]
        if names and names[0] == "udhcpc" and any(interface in a for a in names[1:]):
            pids.append(int(entry.name))
    return pids


def renew_address(interface: str = INTERFACE) -> int:
    """Have udhcpc give up the address it has and ask the network for a new one.

    Moved to another network, wpa_supplicant does not tell udhcpc, which keeps
    the old network's lease (and only renews it with that network's server), so
    the board is cut off. SIGUSR2 releases the lease, SIGUSR1 then asks afresh
    (a DISCOVER). The pause between them matters: pending signals are delivered
    lowest number first, so sent together the renew would come before the release.
    Returns how many udhcpc processes were asked.
    """
    pids = dhcp_clients(interface)
    for pid in pids:
        try:
            os.kill(pid, RELEASE)
        except OSError:
            pass
    if pids:
        time.sleep(1.0)
    for pid in pids:
        try:
            os.kill(pid, RENEW)
        except OSError:
            pass
    return len(pids)


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


def main(argv: list[str] | None = None) -> int:
    """For the server's installer over SSH: ``link`` prints the network, ``renew``
    asks udhcpc for a new address (printing how many it asked)."""
    args = sys.argv[1:] if argv is None else argv
    if args == ["link"]:
        print(link() or "")
    elif args == ["renew"]:
        print(renew_address())
    else:
        print("usage: python3 -m companion_edge.wifi link|renew", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
