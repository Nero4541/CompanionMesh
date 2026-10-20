"""Tailscale on the board: install it, join a tailnet, report how it is doing.

Boards without a package manager (Sipeed's LicheeRV Nano image) get the static
build from pkgs.tailscale.com, which the server downloads and checks; it goes
to /usr/sbin with its state in /var/lib/tailscale (``/run`` is cleared at every
start) and starts at boot (init script or systemd unit). A board that has
Tailscale already keeps its own.

Joining runs ``tailscale up`` in the background: with an auth key it is done
in seconds, without one ``status`` shows the login link (``auth_url``) until
someone opens it. The board does not take the tailnet's DNS
(``--accept-dns=false``): it reaches the server by its 100.x address and keeps
its own name servers.

Also run by the server's installer over SSH (``python3 -m companion_edge.tailscale``),
which prints the same reports as JSON.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import platform
import re
import shutil
import ssl
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.request
from pathlib import Path
from typing import Any

BIN_DIR = Path("/usr/sbin")
STATE_DIR = Path("/var/lib/tailscale")
INIT_SCRIPT = Path("/etc/init.d/S45tailscale")
SYSTEMD_UNIT = Path("/etc/systemd/system/tailscaled.service")
DEFAULTS = Path("/etc/default/tailscaled")
TUN = Path("/dev/net/tun")
PIDFILE = "/run/tailscaled.pid"
UP_TIMEOUT = "15m"  # how long a `tailscale up` waits for someone to log in
# uname -m -> the name pkgs.tailscale.com uses
ARCHES = {
    "x86_64": "amd64",
    "amd64": "amd64",
    "i386": "386",
    "i686": "386",
    "aarch64": "arm64",
    "arm64": "arm64",
    "armv6l": "arm",
    "armv7l": "arm",
    "riscv64": "riscv64",
    "mips": "mips",
    "mipsel": "mipsle",
    "mips64": "mips64",
}
PACKAGE = re.compile(r"^tailscale_[0-9][0-9.]*_[a-z0-9]+\.tgz$")
HOSTNAME = re.compile(r"[^a-z0-9-]+")

INIT_SH = f"""#!/bin/sh
# Tailscale (static build in {BIN_DIR}; written by companion-edge).
# State is kept in {STATE_DIR} because /run and /tmp are cleared at every start.
DAEMON={BIN_DIR}/tailscaled
PIDFILE={PIDFILE}

case "$1" in
  start)
    [ -e {TUN} ] || modprobe tun 2>/dev/null
    mkdir -p {STATE_DIR} /var/run/tailscale
    start-stop-daemon -S -q -b -m -p "$PIDFILE" -x "$DAEMON" -- \\
      --state={STATE_DIR}/tailscaled.state --port=41641
    ;;
  stop)
    start-stop-daemon -K -q -p "$PIDFILE" -x "$DAEMON" && rm -f "$PIDFILE"
    "$DAEMON" --cleanup >/dev/null 2>&1
    ;;
  restart)
    "$0" stop; sleep 1; "$0" start
    ;;
  *)
    echo "usage: $0 {{start|stop|restart}}"; exit 1
    ;;
esac
"""

_up: subprocess.Popen[bytes] | None = None  # the `tailscale up` running in the background


class TailscaleError(Exception):
    pass


def arch() -> str | None:
    return ARCHES.get(platform.machine().lower())


def binary() -> str | None:
    found = shutil.which("tailscale")
    if found:
        return found
    path = BIN_DIR / "tailscale"
    return str(path) if path.exists() else None


def hostname(device_id: str) -> str:
    """A tailnet machine name from the device id (lower case, letters, digits, '-')."""
    name = HOSTNAME.sub("-", device_id.lower()).strip("-")
    return name[:63] or "companion-edge"


def _systemd() -> bool:
    return shutil.which("systemctl") is not None and Path("/run/systemd/system").is_dir()


def _cli(*args: str, timeout: float = 15) -> subprocess.CompletedProcess[str]:
    path = binary()
    if path is None:
        raise TailscaleError("Tailscale is not installed")
    try:
        return subprocess.run([path, *args], capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        raise TailscaleError(f"`tailscale {args[0]}` did not answer in {timeout:.0f} s") from exc


def status() -> dict[str, Any]:
    """installed, running (the daemon), state (BackendState), auth_url, ips, ..."""
    global _up
    if _up is not None and _up.poll() is not None:
        _up = None  # finished (or gave up): no zombie left behind
    report: dict[str, Any] = {
        "installed": binary() is not None,
        "arch": arch(),
        "machine": platform.machine(),
        "running": False,
        "state": None,
        "auth_url": None,
        "ips": [],
        "hostname": None,
        "dns_name": None,
        "tailnet": None,
        "version": None,
        "tun": TUN.exists(),
        "joining": _up is not None,
    }
    if not report["installed"]:
        return report
    try:
        done = _cli("status", "--json")
        data = json.loads(done.stdout)
    except (TailscaleError, OSError, ValueError):
        return report  # the daemon is not running
    me = data.get("Self") or {}
    tailnet = data.get("CurrentTailnet") or {}
    report.update(
        running=True,
        state=data.get("BackendState"),
        auth_url=data.get("AuthURL") or None,
        ips=list(data.get("TailscaleIPs") or me.get("TailscaleIPs") or []),
        hostname=me.get("HostName"),
        dns_name=(me.get("DNSName") or "").rstrip(".") or None,
        tailnet=tailnet.get("Name"),
        version=(data.get("Version") or "").split("-")[0] or None,
    )
    return report


def download(url: str, token: str | None, context: ssl.SSLContext | None, dest: Path) -> None:
    """Fetch ``url`` into ``dest`` piece by piece (the package is far larger than spare RAM)."""
    request = urllib.request.Request(url)
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    part = dest.with_name(dest.name + ".part")
    try:
        with urllib.request.urlopen(request, timeout=60, context=context) as response:
            with part.open("wb") as out:
                shutil.copyfileobj(response, out, 1 << 16)
        part.replace(dest)
    finally:
        part.unlink(missing_ok=True)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(1 << 16), b""):
            digest.update(block)
    return digest.hexdigest()


def install(archive: Path, sha256: str, *, bin_dir: Path | None = None) -> None:
    """Put ``tailscale`` and ``tailscaled`` from a checked package into /usr/sbin."""
    bin_dir = bin_dir or BIN_DIR
    if _sha256(archive) != sha256.lower():
        raise TailscaleError("the Tailscale package does not match its checksum")
    with tarfile.open(archive, "r:gz") as tar:
        members = {}
        for member in tar.getmembers():
            parts = member.name.split("/")
            if len(parts) == 2 and parts[1] in ("tailscale", "tailscaled") and member.isfile():
                members[parts[1]] = member
            elif len(parts) == 3 and parts[1] == "systemd" and member.isfile():
                members[f"systemd/{parts[2]}"] = member
        if "tailscale" not in members or "tailscaled" not in members:
            raise TailscaleError("the package has no tailscale and tailscaled programs")
        need = members["tailscale"].size + members["tailscaled"].size
        free = shutil.disk_usage(bin_dir).free
        if free < need + (8 << 20):
            raise TailscaleError(
                f"not enough room in {bin_dir}: Tailscale needs {need >> 20} MB, "
                f"{free >> 20} MB are free"
            )
        for name in ("tailscale", "tailscaled"):
            source = tar.extractfile(members[name])
            assert source is not None
            target = bin_dir / name
            tmp = bin_dir / f".{name}.new"
            with source, tmp.open("wb") as out:
                shutil.copyfileobj(source, out, 1 << 16)
            tmp.chmod(0o755)
            os.replace(tmp, target)  # also while the old one runs
        if _systemd() and bin_dir == BIN_DIR:
            for name, path in (
                ("systemd/tailscaled.service", SYSTEMD_UNIT),
                ("systemd/tailscaled.defaults", DEFAULTS),
            ):
                source = tar.extractfile(members[name]) if name in members else None
                if source is not None and not path.exists():
                    path.parent.mkdir(parents=True, exist_ok=True)
                    with source:
                        path.write_bytes(source.read())


def _service() -> list[str] | None:
    """How this board starts tailscaled: a command, or None when nothing is set up."""
    if _systemd():
        done = subprocess.run(
            ["systemctl", "cat", "tailscaled"], capture_output=True, text=True, check=False
        )
        return ["systemctl", "enable", "--now", "tailscaled"] if done.returncode == 0 else None
    scripts = sorted(Path("/etc/init.d").glob("S*tailscale*"))
    return [str(scripts[0]), "start"] if scripts else None


def start() -> dict[str, Any]:
    """Start the daemon (setting up its start at boot when nothing does); returns the status."""
    if binary() is None:
        raise TailscaleError("Tailscale is not installed")
    report = status()
    if report["running"]:
        return report
    if not TUN.exists():
        subprocess.run(["modprobe", "tun"], capture_output=True, check=False)
        if not TUN.exists():
            raise TailscaleError(
                "this board's system has no TUN device (/dev/net/tun), which Tailscale needs"
            )
    command = _service()
    if command is None:
        if _systemd():
            raise TailscaleError("tailscaled has no systemd unit on this board")
        INIT_SCRIPT.write_text(INIT_SH, encoding="utf-8")
        INIT_SCRIPT.chmod(0o755)
        command = [str(INIT_SCRIPT), "start"]
    subprocess.run(command, capture_output=True, timeout=30, check=False)
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        report = status()
        if report["running"]:
            return report
        time.sleep(1)
    raise TailscaleError("tailscaled did not start (see its log: tailscaled in the system log)")


def up(name: str, auth_key: str | None = None, *, wait_s: float = 25) -> dict[str, Any]:
    """Join a tailnet as ``name``; returns once joined, or once there is a login link."""
    global _up
    start()
    if _up is not None and _up.poll() is None:
        _up.terminate()  # an earlier try still waiting for its login
        with contextlib.suppress(subprocess.TimeoutExpired):
            _up.wait(5)
    path = binary()
    assert path is not None
    command = [
        path,
        "up",
        "--reset",  # settings from another setup would make `up` refuse
        f"--hostname={hostname(name)}",
        "--accept-dns=false",
        f"--timeout={UP_TIMEOUT}",
    ]
    key_file: Path | None = None
    if auth_key:
        # A file, so the key does not show in the process list.
        fd, tmp = tempfile.mkstemp(prefix="ts-key-")
        with os.fdopen(fd, "w") as fh:
            fh.write(auth_key.strip())
        key_file = Path(tmp)
        command.append(f"--auth-key=file:{key_file}")
    log = Path(tempfile.gettempdir()) / "tailscale-up.log"
    with log.open("wb") as out:
        _up = subprocess.Popen(
            command, stdout=out, stderr=subprocess.STDOUT, start_new_session=True
        )
    try:
        deadline = time.monotonic() + wait_s
        while time.monotonic() < deadline:
            time.sleep(1)
            report = status()
            if report["state"] == "Running" and report["ips"]:
                return report
            if report["auth_url"] and not auth_key:
                return report
            if _up is not None and _up.poll() not in (None, 0):
                text = log.read_text(encoding="utf-8", errors="replace").strip()
                raise TailscaleError(f"tailscale up failed: {text[-400:] or 'no output'}")
        return status()
    finally:
        if key_file is not None:
            key_file.unlink(missing_ok=True)


def logout() -> dict[str, Any]:
    global _up
    if _up is not None and _up.poll() is None:
        _up.terminate()
    _up = None
    done = _cli("logout", timeout=30)
    if done.returncode != 0:
        raise TailscaleError(f"tailscale logout failed: {(done.stdout + done.stderr).strip()}")
    return status()


def main(argv: list[str] | None = None) -> int:
    """For the server's installer: each command prints a status report (JSON)."""
    parser = argparse.ArgumentParser(prog="python3 -m companion_edge.tailscale")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("status")
    unpack = commands.add_parser("install", help="install a package the server checked")
    unpack.add_argument("archive", type=Path)
    unpack.add_argument("sha256")
    join = commands.add_parser("up", help="join (an auth key, if any, comes on stdin)")
    join.add_argument("--hostname", required=True)
    join.add_argument("--auth-key-stdin", action="store_true")
    join.add_argument("--wait", type=float, default=25)
    commands.add_parser("logout")
    args = parser.parse_args(argv)
    try:
        if args.command == "install":
            install(args.archive, args.sha256)
            report = start()
        elif args.command == "up":
            key = sys.stdin.read().strip() if args.auth_key_stdin else None
            report = up(args.hostname, key or None, wait_s=args.wait)
        elif args.command == "logout":
            report = logout()
        else:
            report = status()
    except (TailscaleError, OSError, tarfile.TarError) as exc:
        print(json.dumps({"error": str(exc)}))
        return 1
    print(json.dumps(report))
    return 0


if __name__ == "__main__":
    sys.exit(main())
