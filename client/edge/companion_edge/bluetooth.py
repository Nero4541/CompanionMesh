"""A Bluetooth speaker for the board (BlueZ + bluez-alsa), set up from the dashboard.

Pairing runs ``bluetoothctl`` with an agent that needs no input (speakers have
no PIN). Some boards (the LicheeRV Nano's RTL8733BS) find nothing with an
inquiry scan, so the speaker is also paged directly (``hcitool cc``) before
pairing. Once paired, the board keeps it connected with ``bluetooth.sh`` (a
loop started at boot: bluealsa, then reconnect whenever the speaker is
off or drops) and plays to it through the ALSA device ``bluetooth``, defined
in /etc/asound.conf (read whatever $HOME a service has).

``bluetoothctl info`` crashes on some BlueZ builds (5.68 on Sipeed's image),
so only the device lists are read.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

PCM = "bluetooth"  # the ALSA device name the edge plays to
LEAD_IN_MS = 300  # Bluetooth speakers chop the first fraction of a second
ASOUND = Path("/etc/asound.conf")
SYSFS = Path("/sys/class/bluetooth")
INIT_SCRIPT = Path("/etc/init.d/S95companion-bluetooth")
SYSTEMD_UNIT = Path("/etc/systemd/system/companion-bluetooth.service")
PIDFILE = "/run/companion-bluetooth.pid"
STATE = "bluetooth.json"  # next to edge.toml: the speaker and the output before it
SCRIPT = "bluetooth.sh"
_BEGIN, _END = "# companion-edge bluetooth begin", "# companion-edge bluetooth end"
ADDRESS = re.compile(r"^[0-9A-F]{2}(:[0-9A-F]{2}){5}$")
_DEVICE = re.compile(r"Device ([0-9A-Fa-f:]{17}) ?(.*)$")
_ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]|[\x01\x02\r]")

KEEPALIVE_SH = r"""#!/bin/sh
# Keep the Bluetooth speaker connected (written by companion-edge; started at boot).
# The speaker may be off at boot or drop later: page it and connect again.
MAC="$1"
DIR=$(dirname "$0")
LOG="$DIR/logs/bluetooth.log"
mkdir -p "$DIR/logs"
log() { echo "$(date '+%F %T') $*" >> "$LOG"; }
[ -n "$MAC" ] || { log "no speaker address given"; exit 1; }

hciconfig hci0 up 2>/dev/null
bluetoothctl power on >/dev/null 2>&1
# bluealsa plays to the speaker; a system that runs it as a service has it already.
bluealsa -p a2dp-source --sbc-quality=medium >/dev/null 2>&1 &
BA=$!
sleep 1
if ! kill -0 "$BA" 2>/dev/null; then  # an older bluealsa without --sbc-quality, or running already
  bluealsa -p a2dp-source >/dev/null 2>&1 &
  BA=$!
fi
trap 'kill "$BA" 2>/dev/null; exit 0' TERM INT

# Wi-Fi and Bluetooth often share one chip: with Wi-Fi power save on, Wi-Fi
# traffic makes the audio stutter. Drivers turn it back on after a reconnect.
wifi_ps_off() {
  if iw dev wlan0 get power_save 2>/dev/null | grep -q ": on"; then
    iw dev wlan0 set power_save off && log "Wi-Fi power save off"
  fi
  return 0
}

state=""
while :; do
  wifi_ps_off
  [ "$(wc -c < "$LOG" 2>/dev/null || echo 0)" -gt 200000 ] && : > "$LOG"
  if bluetoothctl devices Connected 2>/dev/null | grep -q "$MAC"; then
    [ "$state" = up ] || log "connected $MAC"
    state=up
    sleep 15 & wait $!
    continue
  fi
  if ! bluetoothctl devices Paired 2>/dev/null | grep -q "$MAC"; then
    [ "$state" = unpaired ] || log "$MAC is not paired any more; pair it again from the dashboard"
    state=unpaired
    sleep 30 & wait $!
    continue
  fi
  [ "$state" = up ] && log "disconnected $MAC"
  state=down
  hcitool cc "$MAC" >/dev/null 2>&1
  bluetoothctl connect "$MAC" >/dev/null 2>&1
  sleep 10 & wait $!
done
"""

INIT_SH = """#!/bin/sh
# The Bluetooth speaker (written by companion-edge): keep it connected.
DIR={dir}
PIDFILE={pidfile}

case "$1" in
  start)
    [ -f "$DIR/{state}" ] || exit 0
    MAC=$(sed -n 's/.*"speaker": *"\\([0-9A-F:]*\\)".*/\\1/p' "$DIR/{state}")
    start-stop-daemon -S -q -b -m -p "$PIDFILE" -x /bin/sh -- "$DIR/{script}" "$MAC"
    ;;
  stop)
    start-stop-daemon -K -q -p "$PIDFILE" && rm -f "$PIDFILE"
    ;;
  restart)
    "$0" stop; sleep 1; "$0" start
    ;;
  *)
    echo "usage: $0 {{start|stop|restart}}"; exit 1
    ;;
esac
"""

UNIT = """[Unit]
Description=Companion edge: keep the Bluetooth speaker connected
After=bluetooth.target
Wants=bluetooth.target

[Service]
ExecStart=/bin/sh {dir}/{script} {mac}
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
"""


class BluetoothError(Exception):
    pass


def _run(args: list[str], timeout: float = 15) -> str:
    try:
        done = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return ""
    return _ANSI.sub("", done.stdout or "")


def _devices(text: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in text.splitlines():
        match = _DEVICE.search(line)
        if match:
            out[match[1].upper()] = match[2].strip()
    return out


def _systemd() -> bool:
    return shutil.which("systemctl") is not None and Path("/run/systemd/system").is_dir()


def check_address(address: str) -> str:
    address = address.strip().upper().replace("-", ":")
    if not ADDRESS.match(address):
        raise BluetoothError(f"{address!r} is not a Bluetooth address (like AA:BB:CC:DD:EE:FF)")
    return address


def load_state(base: Path) -> dict[str, Any]:
    try:
        data = json.loads((base / STATE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def state(base: Path) -> dict[str, Any]:
    """The controller, the paired devices (which are connected) and the speaker."""
    controller = SYSFS.is_dir() and any(SYSFS.glob("hci*"))
    tools = {name: shutil.which(name) is not None for name in ("bluetoothctl", "bluealsa")}
    paired: dict[str, str] = {}
    connected: dict[str, str] = {}
    if controller and tools["bluetoothctl"]:
        paired = _devices(_run(["bluetoothctl", "devices", "Paired"]))
        if not paired:  # BlueZ before 5.65
            paired = _devices(_run(["bluetoothctl", "paired-devices"]))
        connected = _devices(_run(["bluetoothctl", "devices", "Connected"]))
    saved = load_state(base)
    speaker = saved.get("speaker")
    return {
        "controller": controller,
        "tools": tools,
        "speaker": {
            "address": speaker,
            "name": paired.get(speaker) or saved.get("name") or "",
            "paired": speaker in paired,
            "connected": speaker in connected,
        }
        if speaker
        else None,
        "devices": [
            {"address": a, "name": n, "paired": True, "connected": a in connected}
            for a, n in sorted(paired.items(), key=lambda item: item[1].lower())
        ],
        "pcm": PCM,
    }


def _ready() -> None:
    if not (SYSFS.is_dir() and any(SYSFS.glob("hci*"))):
        raise BluetoothError(
            "this board has no Bluetooth controller (nothing in /sys/class/bluetooth)"
        )
    for tool in ("bluetoothctl", "bluealsa"):
        if shutil.which(tool) is None:
            raise BluetoothError(
                f"{tool} is not installed: the board's system cannot play to Bluetooth "
                "speakers (it needs BlueZ and bluez-alsa)"
            )


class _Ctl:
    """One interactive bluetoothctl, fed commands; its output is collected as it comes."""

    def __init__(self) -> None:
        self.proc = subprocess.Popen(
            ["bluetoothctl"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        self.lines: list[str] = []
        self.lock = threading.Lock()
        self.reader = threading.Thread(target=self._read, daemon=True)
        self.reader.start()

    def _read(self) -> None:
        assert self.proc.stdout is not None
        for line in self.proc.stdout:
            with self.lock:
                self.lines.append(_ANSI.sub("", line).strip())

    def send(self, command: str) -> int:
        """Send a command; returns the output position to look for its answer from."""
        with self.lock:
            mark = len(self.lines)
        assert self.proc.stdin is not None
        try:
            self.proc.stdin.write(command + "\n")
            self.proc.stdin.flush()
        except OSError:
            pass
        return mark

    def expect(self, mark: int, patterns: list[str], timeout: float) -> re.Match[str] | None:
        """The first line after ``mark`` matching one of ``patterns``, or None at the timeout."""
        compiled = [re.compile(p) for p in patterns]
        deadline = time.monotonic() + timeout
        while True:
            with self.lock:
                lines = self.lines[mark:]
            for line in lines:
                for pattern in compiled:
                    match = pattern.search(line)
                    if match:
                        return match
            if time.monotonic() >= deadline or self.proc.poll() is not None:
                return None
            time.sleep(0.2)

    def found(self) -> dict[str, str]:
        with self.lock:
            return _devices("\n".join(self.lines))

    def close(self) -> None:
        self.send("quit")
        try:
            self.proc.wait(3)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait()


def scan(seconds: float = 10.0) -> list[dict[str, Any]]:
    """Devices in reach (named ones first). Speakers show only while in pairing mode."""
    _ready()
    ctl = _Ctl()
    try:
        ctl.send("power on")
        ctl.send("scan on")
        time.sleep(seconds)
        ctl.send("scan off")
        mark = ctl.send("devices")
        ctl.expect(mark, [r"Device "], 2.0)
        found = ctl.found()
    finally:
        ctl.close()
    paired = _devices(_run(["bluetoothctl", "devices", "Paired"]))
    unnamed = re.compile(r"^([0-9A-F]{2}-){5}[0-9A-F]{2}$", re.I)
    return sorted(
        (
            {"address": a, "name": "" if unnamed.match(n) else n, "paired": a in paired}
            for a, n in found.items()
        ),
        key=lambda d: (not d["name"], d["name"].lower(), d["address"]),
    )


def pair(
    address: str, base: Path, *, previous_output: str, previous_lead_in: int
) -> dict[str, Any]:
    """Pair, trust and connect the speaker at ``address``; make it the board's speaker."""
    address = check_address(address)
    _ready()
    ctl = _Ctl()
    try:
        ctl.send("power on")
        mark = ctl.send("agent NoInputNoOutput")
        ctl.expect(mark, [r"Agent registered", r"already registered"], 3.0)
        ctl.send("default-agent")
        mark = ctl.send("scan on")
        # Some boards' inquiry finds nothing: page the speaker so BlueZ learns of it.
        if ctl.expect(mark, [rf"Device {address}"], 3.0) is None:
            _run(["hcitool", "cc", address], timeout=10)
            ctl.expect(mark, [rf"Device {address}"], 5.0)
        ctl.send("scan off")
        mark = ctl.send(f"pair {address}")
        answer = ctl.expect(
            mark,
            [r"Pairing successful", r"Failed to pair: (\S+)", r"not available"],
            30.0,
        )
        if answer is None:
            raise BluetoothError("pairing did not finish in 30 s: is the speaker in pairing mode?")
        if "not available" in answer[0]:
            raise BluetoothError(
                f"the board cannot find {address}: switch the speaker on, put it in pairing mode "
                "(usually: hold its Bluetooth button until it flashes) and disconnect it from "
                "phones nearby"
            )
        if answer[0].startswith("Failed") and "AlreadyExists" not in answer[1]:
            raise BluetoothError(f"pairing failed ({answer[1]}): put the speaker in pairing mode")
        mark = ctl.send(f"trust {address}")
        ctl.expect(mark, [r"trust succeeded", r"Changing .* succeeded"], 5.0)
    finally:
        ctl.close()
    paired = _devices(_run(["bluetoothctl", "devices", "Paired"]))
    saved = load_state(base)
    # Pairing another speaker: what to go back to is still the output before the first.
    if saved.get("speaker"):
        previous_output = saved.get("previous_output", previous_output)
        previous_lead_in = saved.get("previous_lead_in_ms", previous_lead_in)
    data = {
        "speaker": address,
        "name": paired.get(address, ""),
        "previous_output": previous_output,
        "previous_lead_in_ms": previous_lead_in,
    }
    (base / STATE).write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    write_pcm(address)
    # Connect only once bluealsa runs (the keep-alive starts it): without it the
    # speaker has no audio profile to connect to (br-connection-profile-unavailable).
    keep_connected(base, address)
    time.sleep(2.0)
    _run(["bluetoothctl", "connect", address], timeout=25)
    return state(base)


def write_pcm(address: str | None, path: Path | None = None) -> None:
    """Define (or with None, remove) the ALSA device ``bluetooth`` in /etc/asound.conf."""
    path = path or ASOUND
    text = path.read_text(encoding="utf-8") if path.is_file() else ""
    text = re.sub(rf"\n?{_BEGIN}\n.*?{_END}\n?", "\n", text, flags=re.S).strip("\n")
    if address:
        block = (
            f"{_BEGIN}\n"
            f"pcm.{PCM} {{\n"
            "    type plug\n"
            "    slave.pcm {\n"
            "        type bluealsa\n"
            f'        device "{address}"\n'
            '        profile "a2dp"\n'
            "    }\n"
            "}\n"
            f"{_END}\n"
        )
        text = f"{text}\n\n{block}" if text else block
    elif text:
        text += "\n"
    path.write_text(text, encoding="utf-8")


def keep_connected(base: Path, address: str) -> None:
    """Install and (re)start the loop that keeps the speaker connected, also at boot."""
    script = base / SCRIPT
    script.write_text(KEEPALIVE_SH, encoding="utf-8")
    script.chmod(0o755)
    if _systemd():
        SYSTEMD_UNIT.write_text(UNIT.format(dir=base, script=SCRIPT, mac=address), encoding="utf-8")
        _run(["systemctl", "daemon-reload"])
        _run(["systemctl", "enable", "companion-bluetooth"])
        _run(["systemctl", "restart", "companion-bluetooth"])
    else:
        INIT_SCRIPT.write_text(
            INIT_SH.format(dir=base, pidfile=PIDFILE, state=STATE, script=SCRIPT), encoding="utf-8"
        )
        INIT_SCRIPT.chmod(0o755)
        _run([str(INIT_SCRIPT), "restart"], timeout=10)


def remove(address: str, base: Path) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """Forget a paired device. For the speaker: also its keep-alive and ALSA device.

    Returns the new state and, if it was the speaker, the saved state from
    before (with the output to go back to).
    """
    address = check_address(address)
    saved = load_state(base)
    was_speaker = saved.get("speaker") == address
    if was_speaker:
        if _systemd():
            _run(["systemctl", "disable", "--now", "companion-bluetooth"])
            SYSTEMD_UNIT.unlink(missing_ok=True)
        elif INIT_SCRIPT.exists():
            _run([str(INIT_SCRIPT), "stop"], timeout=10)
            INIT_SCRIPT.unlink()
        (base / STATE).unlink(missing_ok=True)
        write_pcm(None)
    _run(["bluetoothctl", "remove", address])
    return state(base), saved if was_speaker else None
