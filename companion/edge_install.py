"""Set up a board over SSH: ``companion edge install`` and the dashboard's Add device.

Made for boards running a stock image that logs in as root and starts
services from ``/etc/init.d`` (the LicheeRV Nano with Sipeed's Buildroot
image); boards with systemd get a unit instead. The board needs Python 3.11+
with ``websockets`` (the stock LicheeRV image has both), nothing from the
Internet: the edge client is copied from this server's own files.

What it does, in order (each step reports through ``progress``):

1. log in: with this server's SSH key, else the password given, the one this
   server set before, or the stock ``root``;
2. put this server's public key into ``authorized_keys``;
3. replace the stock root password (or none) with a random one (the stock one is
   public and the board goes outdoors); the new one is kept in the device registry.
   A password of the board's own, or the one this server set, stays as it is;
4. enroll the device (a token of its own) and copy the edge client to
   ``/root/companion-edge/releases/<version>``, ``current`` pointing at it;
5. write ``edge.toml`` (from the hardware profile; an existing one keeps its
   settings and only gets the server and device id), ``edge.env`` with the token;
6. add the Wi-Fi networks given (with their passphrases hashed, never in plain
   text), on the boot partition too where the image reads them from there;
6b. if asked, put the board on Tailscale: copy Tailscale to it when it has none
   (downloaded and checked by this server, see ``companion/tailscale.py``) and
   join, with an auth key or through a login link shown as the step's detail;
7. install the service (``S96companion-edge`` or a systemd unit), run
   ``companion-edge --check``, start it and show the first lines of its log.
"""

from __future__ import annotations

import asyncio
import contextlib
import gzip
import hashlib
import io
import ipaddress
import json
import logging
import re
import secrets
import shlex
import socket
import subprocess
import sys
import tarfile
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import asyncssh

from companion.core.logging import kv
from companion.devices.registry import DeviceRegistry

log = logging.getLogger(__name__)
logging.getLogger("asyncssh").setLevel(logging.WARNING)  # one INFO line per packet exchange

PROJECT_ROOT = Path(__file__).resolve().parents[1]
EDGE_DIR = PROJECT_ROOT / "client" / "edge"
PROFILES_DIR = EDGE_DIR / "profiles"
BOARD_DIR = "/root/companion-edge"
STOCK_PASSWORD = "root"  # Sipeed's images (and many others) ship with root/root
SERVICE_NAME = "S96companion-edge"
PIDFILE = "/run/companion-edge-service.pid"
KEEP_RELEASES = 2  # the current one and the one before it, for going back
SERVER_CERT = f"{BOARD_DIR}/server.crt"  # the public listener's own certificate

Progress = Callable[[str, str, str], None]  # (step, status: run|ok|warn|fail, detail)

SERVICE_SH = f"""#!/bin/sh
# Runs the companion edge client and starts it again if it stops.
# Written by the Companion Server's installer; settings are in edge.toml.
DIR={BOARD_DIR}
set -a
. "$DIR/edge.env"
set +a
child=
trap 'kill "$child" 2>/dev/null; wait "$child"; exit 0' TERM INT
while true; do
  # After an update (update.pending names the version before it), the new version
  # has three starts to connect; then this goes back to the version before.
  if [ -f "$DIR/update.pending" ]; then
    tries=$(cat "$DIR/update.tries" 2>/dev/null || echo 0)
    tries=$((tries + 1))
    echo "$tries" > "$DIR/update.tries"
    if [ "$tries" -gt 3 ]; then
      ln -sfn "releases/$(cat "$DIR/update.pending")" "$DIR/current"
      rm -f "$DIR/update.pending" "$DIR/update.tries"
      echo "$(date): the update did not connect; went back" >> "$DIR/logs/update.log"
    fi
  fi
  # python3 -m imports from the current directory first: run from the release
  # (again each time, as an update moves current).
  cd "$DIR/current" || exit 1
  python3 -u -m companion_edge --config "$DIR/edge.toml" \\
    --log-file "$DIR/logs/edge.log" 2>"$DIR/logs/crash.log" &
  child=$!
  wait "$child"
  sleep 5
done
"""

INIT_SCRIPT = f"""#!/bin/sh
# Companion edge client at boot (installed by the Companion Server).
# Logs: {BOARD_DIR}/logs/edge.log
case "$1" in
  start)
    start-stop-daemon -S -q -b -m -p {PIDFILE} -x /bin/sh -- {BOARD_DIR}/service.sh
    echo "companion-edge started"
    ;;
  stop)
    start-stop-daemon -K -q -p {PIDFILE} && rm -f {PIDFILE}
    echo "companion-edge stopped"
    ;;
  restart)
    "$0" stop
    sleep 2
    "$0" start
    ;;
  *)
    echo "usage: $0 {{start|stop|restart}}"
    exit 1
    ;;
esac
"""

SYSTEMD_UNIT = f"""[Unit]
Description=Companion edge client (installed by the Companion Server)
Wants=network-online.target sound.target
After=network-online.target sound.target

[Service]
ExecStart=/bin/sh {BOARD_DIR}/service.sh
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
"""

# Sets root's password from stdin without chpasswd (not on every Buildroot).
SET_PASSWORD_PY = r"""
import crypt, sys
password = sys.stdin.read().strip()
hashed = crypt.crypt(password, crypt.mksalt(crypt.METHOD_SHA512))
lines = open("/etc/shadow").read().splitlines()
out = []
for line in lines:
    parts = line.split(":")
    if parts[0] == "root":
        parts[1] = hashed
        line = ":".join(parts)
    out.append(line)
with open("/etc/shadow.tmp", "w") as f:
    f.write("\n".join(out) + "\n")
import os
os.chmod("/etc/shadow.tmp", 0o600)
os.replace("/etc/shadow.tmp", "/etc/shadow")
"""

# Says what root's password is: "stock" (the stock one, or none), "known" (the one
# this server set) or "other" (the board's own); stdin: the stock one, the known one.
PASSWORD_STATE_PY = r"""
import crypt, sys
stock, known = (sys.stdin.read().split("\n") + [""])[:2]
def matches(word, hashed):
    try:
        return crypt.crypt(word, hashed) == hashed
    except OSError:  # a locked account ("!", "*") is no hash
        return False
for line in open("/etc/shadow"):
    parts = line.rstrip("\n").split(":")
    if parts[0] == "root":
        hashed = parts[1]
        if not hashed or matches(stock, hashed):
            print("stock")
        elif known and matches(known, hashed):
            print("known")
        else:
            print("other")
        break
"""


class InstallError(Exception):
    """The installation cannot go on; the message says why and what to do."""


@dataclass
class WifiNetwork:
    ssid: str
    password: str | None = None  # None: an open network
    priority: int = 0


@dataclass
class InstallPlan:
    device_id: str
    host: str
    server_url: str
    profile: str = "licheerv-nano"
    camera: bool | None = None  # None: as the profile (or the existing edge.toml) says
    rotate: int | None = None
    wifi: list[WifiNetwork] = field(default_factory=list)
    password: str | None = None  # the board's current root password, if not the stock one
    change_password: bool = True
    start: bool = True
    # The server's public address (server.public.url) and, unless that listener
    # has a certificate from a CA, its own certificate for the board to trust.
    public_url: str | None = None
    server_cert_pem: str | None = None
    # Update a board this server set up before: keep its token (and root password),
    # only bring its software, settings and Wi-Fi up to date. Works without network.
    keep_token: bool = False
    # Put the board on Tailscale (needs Internet on the board, through its Wi-Fi):
    # with this auth key, or else through a login link. tailscale_url is this
    # server's Tailscale address, which the board then also tries.
    tailscale: bool = False
    tailscale_auth_key: str | None = None
    tailscale_url: str | None = None


@dataclass
class InstallResult:
    device_id: str
    version: str
    root_password: str | None
    log_tail: str = ""
    check_output: str = ""


# -- helpers that need no board ------------------------------------------------


def edge_version() -> str:
    text = (EDGE_DIR / "companion_edge" / "__init__.py").read_text(encoding="utf-8")
    match = re.search(r'__version__\s*=\s*"([^"]+)"', text)
    if not match:
        raise InstallError("cannot read the edge client's version")
    return match[1]


def edge_package() -> bytes:
    """The edge client's Python files as a .tar.gz (``companion_edge/...``)."""
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w", format=tarfile.PAX_FORMAT) as tar:
        for path in sorted((EDGE_DIR / "companion_edge").glob("*.py")):
            data = path.read_bytes()
            info = tarfile.TarInfo(f"companion_edge/{path.name}")
            info.size = len(data)
            info.mtime = int(path.stat().st_mtime)
            info.mode = 0o644
            tar.addfile(info, io.BytesIO(data))
    return gzip.compress(raw.getvalue(), mtime=0)


def profiles() -> list[str]:
    return sorted(p.stem for p in PROFILES_DIR.glob("*.toml"))


def toml_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, list):
        return "[" + ", ".join(toml_value(item) for item in value) + "]"
    return json.dumps(str(value), ensure_ascii=False)


_HEADER = re.compile(r"^\s*\[([A-Za-z0-9_.-]+)\]\s*(#.*)?$")


def _section_body(lines: list[str], section: str | None) -> tuple[int, int] | None:
    """Line range of a section's keys (None: the top level), or None if absent."""

    def next_header(after: int) -> int:
        return next((j for j in range(after, len(lines)) if _HEADER.match(lines[j])), len(lines))

    if section is None:
        return 0, next_header(0)
    for i, line in enumerate(lines):
        header = _HEADER.match(line)
        if header and header[1] == section:
            return i + 1, next_header(i + 1)
    return None


def toml_set(text: str, section: str | None, key: str, value: Any) -> str:
    """Set ``key`` in ``[section]`` (None: the top level) of a TOML file's text.

    Replaces the key's line (its trailing comment stays only if the value does),
    or a commented-out ``# key = ...`` line; appends the key to the section if absent.
    Only for the flat files of the edge client (no arrays of tables).
    """
    lines = text.splitlines()
    setting = f"{key} = {toml_value(value)}"
    body = _section_body(lines, section)
    if body is None:
        lines += ["", f"[{section}]", setting]
        return "\n".join(lines) + "\n"
    start, end = body
    live = re.compile(rf'^(\s*){re.escape(key)}\s*=\s*(?:"(?:[^"\\]|\\.)*"|[^#]*?)\s*(#.*)?$')
    commented = re.compile(rf"^\s*#\s*{re.escape(key)}\s*=")
    replaced = next((i for i in range(start, end) if live.match(lines[i])), None)
    if replaced is not None:
        match = live.match(lines[replaced])
        assert match is not None
        # A trailing comment usually describes the value: keep it only if that stays.
        same = lines[replaced].split("#")[0].split("=", 1)[1].strip() == toml_value(value)
        lines[replaced] = f"{match[1]}{setting}" + (f"   {match[2]}" if match[2] and same else "")
    else:
        uncommented = next((i for i in range(start, end) if commented.match(lines[i])), None)
        if uncommented is not None:
            lines[uncommented] = setting
        else:
            insert = end
            while insert > start and not lines[insert - 1].strip():
                insert -= 1
            lines.insert(insert, setting)
    return "\n".join(lines) + "\n"


def edge_toml(plan: InstallPlan, existing: str | None) -> str:
    """The board's edge.toml: the existing one or the profile, with this server set."""
    if existing is None:
        path = PROFILES_DIR / f"{plan.profile}.toml"
        if not path.is_file():
            raise InstallError(f"no profile {plan.profile!r} (have: {', '.join(profiles())})")
        text = path.read_text(encoding="utf-8")
    else:
        text = existing
    text = toml_set(text, None, "server", plan.server_url)
    others = [plan.tailscale_url if plan.tailscale else None, plan.public_url]
    servers = [plan.server_url, *(u for u in others if u and u != plan.server_url)]
    text = toml_set(text, None, "servers", servers if len(servers) > 1 else [])
    text = toml_set(text, None, "server_cert", SERVER_CERT if plan.server_cert_pem else "")
    text = toml_set(text, None, "token_env", "COMPANION_TOKEN")
    text = toml_set(text, None, "device_id", plan.device_id)
    text = toml_set(text, None, "state_file", f"{BOARD_DIR}/state.json")
    if plan.camera is not None:
        text = toml_set(text, "camera", "enabled", plan.camera)
    if plan.rotate is not None:
        text = toml_set(text, "camera", "rotate", plan.rotate)
    return text


# Never restored from a remembered config: they say where and who the board is.
_NOT_RESTORED = {"server", "servers", "server_cert", "token_env", "device_id", "state_file"}


def restore_settings(text: str, remembered: dict[str, Any]) -> str:
    """``text`` (an edge.toml) with the settings a board reported before it was reflashed."""
    for key, value in remembered.items():
        if isinstance(value, dict):
            for name, item in value.items():
                if isinstance(item, (bool, int, float, str)):
                    text = toml_set(text, key, name, item)
        elif key not in _NOT_RESTORED and isinstance(value, (bool, int, float, str)):
            text = toml_set(text, None, key, value)
    return text


def wpa_psk(ssid: str, password: str) -> str:
    """The WPA2 pre-shared key, as wpa_passphrase computes it (no plain text on the board)."""
    if not 8 <= len(password) <= 63:
        raise InstallError(f"Wi-Fi {ssid!r}: a WPA password has 8 to 63 characters")
    return hashlib.pbkdf2_hmac("sha1", password.encode(), ssid.encode(), 4096, 32).hex()


_NETWORK = re.compile(r"network=\{(.*?)\n\s*\}", re.S)


def _ssid_of(block: str) -> str | None:
    match = re.search(r"^\s*ssid=(.+)$", block, re.M)
    if not match:
        return None
    value = match[1].strip()
    if value.startswith('"') and value.endswith('"'):
        return value[1:-1]
    with contextlib.suppress(ValueError):
        return bytes.fromhex(value).decode("utf-8", "replace")
    return value


def wpa_config(networks: list[WifiNetwork], existing: str | None) -> str:
    """wpa_supplicant.conf with ``networks`` first; other existing networks are kept."""
    names = {n.ssid for n in networks}
    lines = ["ctrl_interface=/var/run/wpa_supplicant", "update_config=0", ""]
    count = len(networks)
    for index, network in enumerate(networks):
        body = [f"\tssid={network.ssid.encode().hex()}", "\tscan_ssid=1"]
        if network.password:
            body.append(f"\tpsk={wpa_psk(network.ssid, network.password)}")
        else:
            body.append("\tkey_mgmt=NONE")
        body.append(f"\tpriority={network.priority or (count - index) + 10}")
        lines += [f"# {network.ssid}", "network={", *body, "}", ""]
    for match in _NETWORK.finditer(existing or ""):
        if _ssid_of(match[1]) not in names:
            lines += ["network={" + match[1], "}", ""]
    return "\n".join(lines)


def _output(command: list[str]) -> str:
    try:
        return subprocess.run(command, capture_output=True, text=True, timeout=10).stdout
    except (OSError, subprocess.SubprocessError):
        return ""


def local_ipv4_addresses() -> list[str]:
    found: set[str] = set()
    with contextlib.suppress(OSError):
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            found.add(str(info[4][0]))
    found.update(re.findall(r"inet (\d+\.\d+\.\d+\.\d+)/", _output(["ip", "-4", "-o", "addr"])))
    return sorted(found)


def _gateway_addresses() -> list[str]:
    """Addresses of the interfaces that have a default gateway (real networks,
    not virtual switches or USB links)."""
    if sys.platform == "win32":
        out = _output(
            [
                "powershell",
                "-NoProfile",
                "-Command",
                "Get-NetIPConfiguration | Where-Object { $_.IPv4DefaultGateway } | "
                "ForEach-Object { $_.IPv4Address.IPAddress }",
            ]
        )
        return re.findall(r"\d+\.\d+\.\d+\.\d+", out)
    addresses = []
    for dev in re.findall(r"dev (\S+)", _output(["ip", "-4", "route", "show", "default"])):
        out = _output(["ip", "-4", "-o", "addr", "show", "dev", dev])
        addresses += re.findall(r"inet (\d+\.\d+\.\d+\.\d+)/", out)
    return addresses


_OVERLAY = ipaddress.IPv4Network("100.64.0.0/10")  # carrier-grade NAT range; Tailscale uses it


def lan_addresses() -> list[str]:
    """This machine's addresses a board on the same Wi-Fi may reach, best guess first."""
    guesses = _gateway_addresses()
    with contextlib.suppress(OSError), socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.connect(("192.0.2.1", 9))  # picks the default route; no packet is sent
        guesses.append(str(s.getsockname()[0]))
    usb = {a for a in local_ipv4_addresses() if usb_board_candidates([a])}
    guesses += [a for a in local_ipv4_addresses() if a not in usb]
    out: list[str] = []
    for address in guesses:
        ip = ipaddress.IPv4Address(address)
        if ip.is_private and ip not in _OVERLAY and not ip.is_loopback and not ip.is_link_local:
            if address not in out:
                out.append(address)
    return out


def usb_board_candidates(addresses: list[str] | None = None) -> list[str]:
    """Boards on USB networks: the stock image serves DHCP on 10.x.y.0/24 from
    10.x.y.1 and gives this computer .100-.200."""
    out = []
    for address in addresses if addresses is not None else local_ipv4_addresses():
        ip = ipaddress.IPv4Address(address)
        if ip in ipaddress.IPv4Network("10.0.0.0/8") and 100 <= int(address.split(".")[3]) <= 200:
            out.append(".".join([*address.split(".")[:3], "1"]))
    return sorted(set(out))


async def _port_open(host: str, port: int, timeout_s: float) -> bool:
    try:
        _, writer = await asyncio.wait_for(asyncio.open_connection(host, port), timeout_s)
    except (OSError, TimeoutError):
        return False
    writer.close()
    with contextlib.suppress(OSError):
        await writer.wait_closed()
    return True


async def _host_key(host: str) -> str | None:
    try:
        key = await asyncio.wait_for(asyncssh.get_server_host_key(host, 22), 5)
    except (OSError, TimeoutError, asyncssh.Error):
        return None
    return key.get_fingerprint() if key is not None else None


async def discover(addresses: list[str] | None = None) -> list[str]:
    """Boards reachable over USB with SSH open, one address per board (a board
    with both RNDIS and NCM links shows up on two networks)."""
    candidates = usb_board_candidates(addresses)
    open_ = await asyncio.gather(*(_port_open(c, 22, 1.5) for c in candidates))
    reachable = [c for c, ok in zip(candidates, open_, strict=True) if ok]
    keys = await asyncio.gather(*(_host_key(c) for c in reachable))
    seen: set[str] = set()
    boards = []
    for host, key in zip(reachable, keys, strict=True):
        if key is None or key not in seen:
            boards.append(host)
        if key is not None:
            seen.add(key)
    return boards


def tailscale_addresses() -> list[str]:
    """This machine's Tailscale (100.64.0.0/10) addresses: reachable by a board on
    the same tailnet from anywhere."""
    return [a for a in local_ipv4_addresses() if ipaddress.IPv4Address(a) in _OVERLAY]


def server_addresses() -> list[str]:
    """Addresses to offer boards, the one that works everywhere (Tailscale) first."""
    return tailscale_addresses() + lan_addresses()


def default_server_url(port: int) -> str | None:
    addresses = server_addresses()
    return f"ws://{addresses[0]}:{port}/v1/realtime" if addresses else None


def server_key(data_dir: Path) -> asyncssh.SSHKey:
    """This server's SSH key for its boards (created on first use)."""
    path = data_dir / "ssh" / "id_ed25519"
    if path.is_file():
        return asyncssh.read_private_key(str(path))
    path.parent.mkdir(parents=True, exist_ok=True)
    key = asyncssh.generate_private_key("ssh-ed25519", comment="companion-server")
    key.write_private_key(str(path))
    key.write_public_key(str(path) + ".pub")
    with contextlib.suppress(OSError):
        path.chmod(0o600)
    return key


def public_reach(config: Any) -> tuple[str | None, str | None]:
    """(public URL, certificate PEM for boards to trust) from the server's config."""
    public = config.server.public
    if not public.enabled or not public.url:
        return None, None
    if public.cert_file:  # from a CA: boards check it like any website
        return public.url, None
    from companion.core.tls import ensure_certificate

    return public.url, ensure_certificate(config.storage.data_dir / "tls").pem()


# -- talking to the board ----------------------------------------------------------


class Board:
    def __init__(self, conn: asyncssh.SSHClientConnection) -> None:
        self.conn = conn

    async def run(self, command: str, *, input: str | None = None, check: bool = True) -> str:
        result = await self.conn.run(command, input=input, check=False)
        out = str(result.stdout or "") + str(result.stderr or "")
        if check and result.exit_status != 0:
            raise InstallError(
                f"`{command.splitlines()[0][:80]}` failed on the board: {out.strip()}"
            )
        return out

    async def read(self, path: str) -> str | None:
        async with self.conn.start_sftp_client() as sftp:
            try:
                async with sftp.open(path, "rb") as f:
                    return (await f.read()).decode("utf-8", "replace")
            except asyncssh.SFTPNoSuchFile:
                return None

    async def write(self, path: str, data: str | bytes, mode: int = 0o644) -> None:
        payload = data.encode() if isinstance(data, str) else data
        async with self.conn.start_sftp_client() as sftp:
            tmp = f"{path}.tmp"
            async with sftp.open(tmp, "wb") as f:
                await f.write(payload)
            await sftp.chmod(tmp, mode)
            await sftp.posix_rename(tmp, path)

    async def put(self, local: Path, path: str) -> None:
        """Copy a (large) file without reading it into memory."""
        async with self.conn.start_sftp_client() as sftp:
            await sftp.put(str(local), f"{path}.tmp")
            await sftp.posix_rename(f"{path}.tmp", path)

    async def exists(self, path: str) -> bool:
        out = await self.run(f"test -e {shlex.quote(path)} && echo yes || echo no")
        return out.strip() == "yes"


async def connect(
    host: str, key: asyncssh.SSHKey, passwords: list[str | None], *, timeout_s: float = 10
) -> tuple[asyncssh.SSHClientConnection, str | None]:
    """Log in as root: the key first, then each password; returns the one that worked."""
    last: Exception | None = None
    for password in passwords or [None]:
        try:
            conn = await asyncssh.connect(
                host,
                username="root",
                client_keys=[key],
                password=password,
                known_hosts=None,  # a freshly flashed board has a new host key
                connect_timeout=timeout_s,
                agent_path=None,
            )
            return conn, password
        except asyncssh.PermissionDenied as exc:
            last = exc
        except (OSError, asyncssh.Error) as exc:
            raise InstallError(f"cannot reach {host} over SSH: {exc}") from exc
    raise InstallError(
        f"{host} refused the login (tried this server's key and {len(passwords)} password(s)); "
        "give the board's root password"
    ) from last


async def _board_token(board: Board, registry: DeviceRegistry, device_id: str) -> str:
    """The token already on the board, if it is this server's token for ``device_id``."""
    env = await board.read(f"{BOARD_DIR}/edge.env") or ""
    match = re.search(r"^COMPANION_TOKEN=(\S+)$", env, re.M)
    enrolled = registry.identify(match[1]) if match else None
    if enrolled is None or enrolled.device_id != device_id:
        raise InstallError(
            f"this board has no token from this server for {device_id!r}; "
            "set it up instead of updating it"
        )
    return match[1]


TAILSCALE_LOGIN_S = 300.0  # how long the installer waits for someone to open the login link


async def _board_tailscale(board: Board, args: str, input: str | None = None) -> dict[str, Any]:
    """Run the edge client's Tailscale helper on the board; its JSON report."""
    out = await board.run(
        f"cd {BOARD_DIR}/current && python3 -m companion_edge.tailscale {args}",
        input=input,
        check=False,
    )
    try:
        report = json.loads(out.strip().splitlines()[-1])
    except (ValueError, IndexError):
        raise InstallError(f"Tailscale on the board: {out.strip()[:300] or 'no answer'}") from None
    if "error" in report:
        raise InstallError(str(report["error"]))
    return report


async def _join_tailscale(
    board: Board, plan: InstallPlan, data_dir: Path, report: Callable[..., None]
) -> None:
    from companion import tailscale

    report("tailscale", "run")
    state = await _board_tailscale(board, "status")
    if not state["installed"]:
        if not state.get("arch"):
            raise InstallError(f"Tailscale has no build for this board ({state.get('machine')})")
        report("tailscale", "run", f"getting Tailscale for {state['arch']} from pkgs.tailscale.com")
        try:
            package = await tailscale.package(state["arch"], data_dir)
        except tailscale.PackageError as exc:
            raise InstallError(str(exc)) from exc
        report("tailscale", "run", f"copying {package.name} to the board")
        remote = f"{BOARD_DIR}/{package.name}"
        await board.put(package.path, remote)
        try:
            state = await _board_tailscale(board, f"install {shlex.quote(remote)} {package.sha256}")
        finally:
            await board.run(f"rm -f {shlex.quote(remote)}", check=False)
    key = plan.tailscale_auth_key
    report("tailscale", "run", "joining the tailnet")
    state = await _board_tailscale(
        board,
        f"up --hostname {shlex.quote(plan.device_id)}" + (" --auth-key-stdin" if key else ""),
        input=key or "",
    )
    deadline = time.monotonic() + TAILSCALE_LOGIN_S
    while state.get("state") != "Running" and time.monotonic() < deadline:
        if state.get("auth_url"):
            report("tailscale", "run", f"Open this link to let the board in: {state['auth_url']}")
        await asyncio.sleep(3.0)
        state = await _board_tailscale(board, "status")
    if state.get("state") == "Running":
        where = ", ".join(state.get("ips") or [])
        report("tailscale", "ok", f"on the tailnet as {state.get('hostname')} ({where})")
    else:
        report(
            "tailscale",
            "warn",
            f"not on the tailnet yet ({state.get('state') or 'not running'}): finish it under "
            "Devices, Maintenance once the board is connected",
        )


async def install(
    plan: InstallPlan,
    *,
    registry: DeviceRegistry,
    data_dir: Path,
    progress: Progress | None = None,
    connected: Callable[[str], bool] | None = None,
    wait_s: float = 45.0,
) -> InstallResult:
    """Set up the board at ``plan.host``; see the module docstring for the steps."""

    def report(step: str, status: str, detail: str = "") -> None:
        if status != "run":
            log.debug("edge install", extra=kv(step=step, status=status, detail=detail[:200]))
        if progress is not None:
            progress(step, status, detail)

    enrolled = registry.get(plan.device_id)
    passwords: list[str | None] = []
    for candidate in (
        plan.password,
        enrolled.root_password if enrolled else None,
        STOCK_PASSWORD,
    ):
        if candidate and candidate not in passwords:
            passwords.append(candidate)

    report("connect", "run", plan.host)
    key = server_key(data_dir)
    conn, _ = await connect(plan.host, key, passwords)
    board = Board(conn)
    try:
        system = await board.run("uname -m; cat /etc/os-release 2>/dev/null | head -1", check=False)
        python = await board.run(
            "python3 -c 'import sys, websockets; "
            "print(sys.version.split()[0], websockets.__version__)'",
            check=False,
        )
        if not re.match(r"^3\.(1[1-9]|[2-9]\d)", python.strip()):
            raise InstallError(
                "the board needs Python 3.11+ with websockets "
                f"(python3 says: {python.strip()[:200] or 'not found'})"
            )
        systemd = (await board.run("command -v systemctl || true")).strip() != ""
        if not systemd and not await board.exists("/etc/init.d"):
            raise InstallError("the board has neither systemd nor /etc/init.d")
        report("connect", "ok", f"{' '.join(system.split())[:80]}; Python {python.strip()}")

        report("ssh key", "run")
        public = key.export_public_key().decode().strip()
        await board.run(
            "mkdir -p /root/.ssh && chmod 700 /root/.ssh && touch /root/.ssh/authorized_keys && "
            f"(grep -qF {shlex.quote(public.split()[1])} /root/.ssh/authorized_keys || "
            f"echo {shlex.quote(public)} >> /root/.ssh/authorized_keys) && "
            "chmod 600 /root/.ssh/authorized_keys"
        )
        report("ssh key", "ok", "this server can log in with its own key from now on")

        token: str | None = None
        if plan.keep_token:
            token = await _board_token(board, registry, plan.device_id)
            report("token", "ok", "kept the token it has")

        root_password = enrolled.root_password if enrolled else None
        if plan.change_password:
            # Asked on the board: a login with the key says nothing about the password.
            report("password", "run")
            state = await board.run(
                f"python3 -c {shlex.quote(PASSWORD_STATE_PY)}",
                input=f"{STOCK_PASSWORD}\n{root_password or ''}",
                check=False,
            )
            if state.strip() == "stock":
                new_password = secrets.token_urlsafe(12)
                out = await board.run(
                    f"python3 -c {shlex.quote(SET_PASSWORD_PY)}", input=new_password, check=False
                )
                if "Traceback" in out:
                    report("password", "warn", "could not change it; change it by hand (passwd)")
                else:
                    root_password = new_password
                    report("password", "ok", "replaced the stock root password (see the dashboard)")
            elif state.strip() == "known":
                report("password", "ok", "kept the one this server set")
            elif state.strip() == "other":
                root_password = None  # what this server had is not the board's any more
                report("password", "ok", "kept the board's own password")
            else:
                report("password", "warn", "could not read it; left it as it is")

        version = edge_version()
        if token is not None:
            registry.update(
                plan.device_id,
                address=plan.host,
                root_password=root_password,
                installed_version=version,
            )
        else:
            report("enroll", "run", plan.device_id)
            token = registry.enroll(
                plan.device_id,
                profile=plan.profile,
                address=plan.host,
                root_password=root_password,
                installed_version=version,
            )
            report("enroll", "ok", "the device has a token of its own")

        report("stop", "run")
        await board.run(f"/etc/init.d/{SERVICE_NAME} stop >/dev/null 2>&1 || true", check=False)
        if systemd:
            await board.run("systemctl stop companion-edge >/dev/null 2>&1 || true", check=False)
        # The service's client takes a moment to shut down; what is left after
        # that was started some other way (by hand, or an older setup).
        pids: list[str] = []
        for _ in range(10):
            others = await board.run(
                "ps | grep '[c]ompanion_edge' | awk '{print $1}' || true", check=False
            )
            pids = [p for p in others.split() if p.isdigit()]
            if not pids:
                break
            await asyncio.sleep(0.5)
        if pids:
            await board.run(f"kill {' '.join(pids)} 2>/dev/null || true", check=False)
            report("stop", "warn", f"stopped another companion-edge (pid {', '.join(pids)})")
        else:
            report("stop", "ok")

        report("copy", "run", f"companion-edge {version}")
        release = f"{BOARD_DIR}/releases/{version}"
        await board.run(f"mkdir -p {release} {BOARD_DIR}/logs")
        await board.write("/tmp/companion-edge.tar.gz", edge_package())
        await board.run(
            f"rm -rf {release}/companion_edge"
            f" && gzip -dc /tmp/companion-edge.tar.gz | tar -xf - -C {release}"  # busybox: no -z
            " && rm -f /tmp/companion-edge.tar.gz"
            f" && ln -sfn releases/{version} {BOARD_DIR}/current"
        )
        listing = await board.run(f"ls -1t {BOARD_DIR}/releases", check=False)
        for old in [v for v in listing.split() if v != version][KEEP_RELEASES - 1 :]:
            await board.run(f"rm -rf {shlex.quote(f'{BOARD_DIR}/releases/{old}')}", check=False)
        report("copy", "ok", release)

        report("settings", "run")
        existing = await board.read(f"{BOARD_DIR}/edge.toml")
        if existing is not None:
            stamp = time.strftime("%Y%m%d-%H%M%S")
            await board.run(
                f"cp {BOARD_DIR}/edge.toml {BOARD_DIR}/edge.toml.before-install-{stamp}"
            )
        if plan.server_cert_pem:
            await board.write(SERVER_CERT, plan.server_cert_pem)
        text = edge_toml(plan, existing)
        remembered = enrolled.settings if enrolled is not None else None
        if existing is None and remembered:  # reflashed: bring back what it had
            text = edge_toml(plan, restore_settings(text, remembered))
        await board.write(f"{BOARD_DIR}/edge.toml", text)
        await board.write(f"{BOARD_DIR}/edge.env", f"COMPANION_TOKEN={token}\n", mode=0o600)
        await board.write(f"{BOARD_DIR}/service.sh", SERVICE_SH, mode=0o755)
        kept = (
            "kept the board's settings"
            if existing
            else "restored the settings it had before"
            if remembered
            else f"profile {plan.profile}"
        )
        reach = f"; from outside {plan.public_url}" if plan.public_url else ""
        report("settings", "ok", f"{kept}; server {plan.server_url}{reach}")

        if plan.wifi:
            report("wifi", "run", ", ".join(n.ssid for n in plan.wifi))
            current = await board.read("/etc/wpa_supplicant.conf")
            config = wpa_config(plan.wifi, current)
            await board.write("/etc/wpa_supplicant.conf", config, mode=0o600)
            boot = await board.exists("/boot/wifi.sta") or await board.exists("/boot/usb.dev")
            if boot:  # Sipeed's image copies it from the boot partition at every start
                await board.write("/boot/wpa_supplicant.conf", config, mode=0o600)
                await board.run("touch /boot/wifi.sta", check=False)
            await board.run(
                "wpa_cli -i wlan0 reconfigure >/dev/null 2>&1 || "
                "(test -x /etc/init.d/S30wifi && "
                "/etc/init.d/S30wifi restart >/dev/null 2>&1) || true",
                check=False,
            )
            report("wifi", "ok", "saved" + (" (also on the boot partition)" if boot else ""))

        if plan.tailscale:
            try:
                await _join_tailscale(board, plan, data_dir, report)
            except InstallError as exc:  # the board works without it, at home
                report("tailscale", "warn", str(exc))

        report("service", "run")
        if systemd:
            await board.write("/etc/systemd/system/companion-edge.service", SYSTEMD_UNIT)
            await board.run("systemctl daemon-reload && systemctl enable companion-edge")
        else:
            await board.write(f"/etc/init.d/{SERVICE_NAME}", INIT_SCRIPT, mode=0o755)
        report("service", "ok", "starts at boot")

        report("check", "run")
        check_output = await board.run(
            f"set -a && . {BOARD_DIR}/edge.env && set +a && cd {BOARD_DIR}/current && "
            f"python3 -m companion_edge --version && "
            f"python3 -m companion_edge --config {BOARD_DIR}/edge.toml --check",
            check=False,
        )
        failed = "MISSING" in check_output or "NOT FOUND" in check_output
        report("check", "warn" if failed else "ok", check_output.strip())

        log_tail = ""
        if plan.start:
            report("start", "run")
            if systemd:
                await board.run("systemctl restart companion-edge")
            else:
                await board.run(f"/etc/init.d/{SERVICE_NAME} start")
            deadline = time.monotonic() + (wait_s if connected else 8.0)
            joined = False
            while time.monotonic() < deadline:
                await asyncio.sleep(1.0)
                if connected is not None and connected(plan.device_id):
                    joined = True
                    break
            log_tail = await board.run(f"tail -n 15 {BOARD_DIR}/logs/edge.log", check=False)
            if connected is None:
                report("start", "ok", "started; see the log below")
            elif joined:
                report("start", "ok", "connected to this server")
            else:
                report("start", "warn", "started but not connected yet (Wi-Fi? see the log)")
        return InstallResult(plan.device_id, version, root_password, log_tail, check_output)
    finally:
        conn.close()


# -- when a board cannot connect at all: look at it over USB -------------------------

DOCTOR = [
    (
        "version",
        f"cd {BOARD_DIR}/current 2>/dev/null && python3 -m companion_edge --version"
        " || echo 'not set up by a Companion Server'",
    ),
    ("running", "ps | grep '[c]ompanion_edge' || echo 'companion-edge is not running'"),
    (
        "check",
        f"set -a; . {BOARD_DIR}/edge.env 2>/dev/null; set +a; cd {BOARD_DIR}/current"
        f" && python3 -m companion_edge --config {BOARD_DIR}/edge.toml --check",
    ),
    ("settings", f"cat {BOARD_DIR}/edge.toml"),
    ("log", f"tail -n 40 {BOARD_DIR}/logs/edge.log"),
    ("last crash", f"cat {BOARD_DIR}/logs/crash.log 2>/dev/null"),
    (
        "updates",
        f"cat {BOARD_DIR}/logs/update.log 2>/dev/null; ls -l {BOARD_DIR}/current;"
        f" ls {BOARD_DIR}/releases",
    ),
    ("wifi", "wpa_cli -i wlan0 status 2>/dev/null; ip addr show wlan0"),
    ("clock", "date"),
]


async def doctor(
    host: str, *, registry: DeviceRegistry, data_dir: Path, password: str | None = None
) -> list[tuple[str, str]]:
    """What a board says about itself, read over SSH (USB): for boards that cannot connect."""
    passwords: list[str | None] = [password] if password else []
    passwords += [d.root_password for d in registry.list() if d.root_password]
    passwords.append(STOCK_PASSWORD)
    conn, _ = await connect(host, server_key(data_dir), list(dict.fromkeys(passwords)))
    board = Board(conn)
    try:
        return [
            (title, (await board.run(command, check=False)).strip() or "(nothing)")
            for title, command in DOCTOR
        ]
    finally:
        conn.close()


# -- command line ------------------------------------------------------------------


def run_cli(args: Any, config: Any) -> int:
    """``companion edge discover|install`` (see ``companion/__main__.py``)."""
    import getpass

    if args.edge_command == "doctor":
        host = args.host or next(iter(asyncio.run(discover())), None)
        if not host:
            print("No board found on USB; give --host.")
            return 1
        data_dir = config.storage.data_dir
        try:
            sections = asyncio.run(
                doctor(host, registry=DeviceRegistry(data_dir / "devices.json"), data_dir=data_dir)
            )
        except InstallError as exc:
            print(f"FAIL {exc}")
            return 1
        for title, text in sections:
            print(f"== {title}")
            print(text)
            print()
        return 0

    if (
        args.edge_command == "update"
        and DeviceRegistry(config.storage.data_dir / "devices.json").get(args.name) is None
    ):
        print(f"This server has not set up {args.name!r}; use  companion edge install.")
        return 1

    if args.edge_command == "discover":
        found = asyncio.run(discover())
        if not found:
            print("No board found on USB. Plug it in, wait for it to boot (~30 s), try again.")
            return 1
        for host in found:
            print(host)
        return 0

    host = args.host
    if not host:
        found = asyncio.run(discover())
        if len(found) != 1:
            print(
                "No board found on USB; give --host."
                if not found
                else f"Several boards found ({', '.join(found)}); pick one with --host."
            )
            return 1
        host = found[0]
    server_url = args.server or default_server_url(config.server.port)
    if not server_url:
        print(
            "Cannot tell this machine's network address; give --server ws://ADDRESS:PORT/v1/realtime"
        )
        return 1
    wifi = []
    for ssid in args.wifi:
        password = getpass.getpass(f"Password for Wi-Fi {ssid!r} (Enter for an open network): ")
        wifi.append(WifiNetwork(ssid, password or None))
    password = (
        getpass.getpass("Board's root password (Enter: try the stock one): ")
        if (args.ask_password)
        else None
    )
    auth_key = (
        getpass.getpass("Tailscale auth key (Enter: open a login link instead): ").strip()
        if args.tailscale
        else ""
    )
    public_url, cert_pem = public_reach(config)
    tailnet = tailscale_addresses()
    plan = InstallPlan(
        device_id=args.name,
        host=host,
        server_url=server_url,
        public_url=public_url,
        server_cert_pem=cert_pem,
        profile=args.profile,
        camera=args.camera,
        rotate=args.rotate,
        wifi=wifi,
        password=password or None,
        change_password=not args.keep_password,
        keep_token=args.edge_command == "update",
        tailscale=args.tailscale,
        tailscale_auth_key=auth_key or None,
        tailscale_url=f"ws://{tailnet[0]}:{config.server.port}/v1/realtime" if tailnet else None,
    )
    links: set[str] = set()

    def show(step: str, status: str, detail: str) -> None:
        if status == "run":
            if "https://" in detail and detail not in links:  # a link to open now
                links.add(detail)
                print(f"     {step:<9} {detail}")
            return
        mark = {"ok": "ok  ", "warn": "WARN", "fail": "FAIL"}.get(status, status)
        first, *rest = detail.splitlines() or [""]
        print(f"{mark} {step:<9} {first}")
        for line in rest:
            print(f"               {line}")

    data_dir = config.storage.data_dir
    try:
        result = asyncio.run(
            install(
                plan,
                registry=DeviceRegistry(data_dir / "devices.json"),
                data_dir=data_dir,
                progress=show,
            )
        )
    except InstallError as exc:
        print(f"FAIL {exc}")
        return 1
    if result.log_tail.strip():
        print("\nThe board's log:\n" + result.log_tail.rstrip())
    if result.root_password:
        print(
            f"\nThe board's root password is now: {result.root_password}\n"
            "(also in the dashboard under Devices). This server logs in with its own key."
        )
    return 0
