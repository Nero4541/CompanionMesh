"""Device health for heartbeats, read from /proc and /sys (Linux; best effort)."""

from __future__ import annotations

import glob
from pathlib import Path
from typing import Any


def _read(path: str) -> str | None:
    try:
        return Path(path).read_text().strip()
    except OSError:
        return None


def cpu_temp_c() -> float | None:
    temps = []
    for zone in glob.glob("/sys/class/thermal/thermal_zone*/temp"):
        raw = _read(zone)
        if raw and raw.lstrip("-").isdigit():
            temps.append(int(raw) / 1000)
    return round(max(temps), 1) if temps else None


def mem_free_mb() -> int | None:
    meminfo = _read("/proc/meminfo")
    if not meminfo:
        return None
    for line in meminfo.splitlines():
        if line.startswith("MemAvailable:"):
            return int(line.split()[1]) // 1024
    return None


def wifi_signal_dbm() -> float | None:
    wireless = _read("/proc/net/wireless")
    if not wireless:
        return None
    for line in wireless.splitlines()[2:]:
        parts = line.split()
        if len(parts) >= 4:
            try:
                return float(parts[3].rstrip("."))
            except ValueError:
                return None
    return None


def uptime_s() -> int | None:
    raw = _read("/proc/uptime")
    return int(float(raw.split()[0])) if raw else None


def snapshot(**extra: Any) -> dict[str, Any]:
    status = {
        "cpu_temp_c": cpu_temp_c(),
        "mem_free_mb": mem_free_mb(),
        "wifi_signal_dbm": wifi_signal_dbm(),
        "uptime_s": uptime_s(),
        **extra,
    }
    return {k: v for k, v in status.items() if v is not None}
