"""Log lines that devices send about themselves (``device.log``).

Kept per device: the most recent lines in memory for the dashboard, and all of
them in ``data/devices/<device_id>/edge.log`` (rotated), so a board's history
is still on the server after it is lost or reflashed. A device resends lines
it is not sure arrived; ``(boot, seq)`` tells which are new.
"""

from __future__ import annotations

import itertools
import logging
import logging.handlers
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from companion.devices.registry import DEVICE_ID

KEEP = 1000  # lines per device in memory
FILE_BYTES = 1024 * 1024
FILE_BACKUPS = 3
MAX_MSG = 2000


@dataclass
class _DeviceLog:
    lines: deque[dict[str, Any]] = field(default_factory=lambda: deque(maxlen=KEEP))
    boot: str | None = None
    seq: int = -1
    writer: logging.Handler | None = None


class DeviceLogs:
    def __init__(self, root: Path) -> None:
        self.root = root
        self._devices: dict[str, _DeviceLog] = {}
        self._ids = itertools.count(1)

    def _writer(self, device_id: str) -> logging.Handler | None:
        if not DEVICE_ID.match(device_id):
            return None
        folder = self.root / device_id
        folder.mkdir(parents=True, exist_ok=True)
        handler = logging.handlers.RotatingFileHandler(
            folder / "edge.log", maxBytes=FILE_BYTES, backupCount=FILE_BACKUPS, encoding="utf-8"
        )
        handler.setFormatter(logging.Formatter("%(message)s"))
        return handler

    def add(self, device_id: str, payload: dict[str, Any]) -> int:
        """Store the new lines of a ``device.log`` payload; returns how many were new."""
        records = payload.get("records")
        if not isinstance(records, list):
            return 0
        log = self._devices.setdefault(device_id, _DeviceLog())
        boot = str(payload.get("boot") or "")
        if boot != log.boot:
            log.boot, log.seq = boot, -1
        added = 0
        for record in records:
            if not isinstance(record, dict):
                continue
            seq = record.get("seq")
            if isinstance(seq, int):
                if seq <= log.seq:
                    continue
                log.seq = seq
            t = record.get("t")
            line = {
                "id": next(self._ids),
                "t": float(t) if isinstance(t, (int, float)) else time.time(),
                "level": str(record.get("level", "INFO"))[:10],
                "logger": str(record.get("logger", ""))[:60],
                "msg": str(record.get("msg", ""))[:MAX_MSG],
                "received": time.time(),
            }
            log.lines.append(line)
            added += 1
            if log.writer is None:
                log.writer = self._writer(device_id)
            if log.writer is not None:
                stamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(line["t"]))
                text = f"{stamp} {line['level']:<7} {line['logger']}: {line['msg']}"
                log.writer.emit(logging.makeLogRecord({"msg": text}))
        return added

    def recent(self, device_id: str, *, after: int = 0, limit: int = 500) -> list[dict[str, Any]]:
        log = self._devices.get(device_id)
        if log is None:
            return []
        return [line for line in log.lines if line["id"] > after][-limit:]

    def known(self) -> list[str]:
        on_disk = (
            [p.name for p in self.root.iterdir() if (p / "edge.log").is_file()]
            if self.root.is_dir()
            else []
        )
        return sorted(set(self._devices) | set(on_disk))

    def file(self, device_id: str) -> Path | None:
        if not DEVICE_ID.match(device_id):
            return None
        path = self.root / device_id / "edge.log"
        return path if path.is_file() else None

    def close(self) -> None:
        for log in self._devices.values():
            if log.writer is not None:
                log.writer.close()
