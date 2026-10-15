"""The device's own log lines, kept in memory and sent to the server (``device.log``).

Lines logged while offline (no Wi-Fi, server unreachable: the ones that explain
why) wait here and go out once the device is connected again. Only the newest
``CAPACITY`` are kept, so a long time offline costs a fixed amount of memory.
"""

from __future__ import annotations

import itertools
import logging
import threading
import uuid
from collections import deque
from typing import Any

CAPACITY = 500
BATCH = 100  # lines per device.log message


class LogBuffer(logging.Handler):
    def __init__(self, capacity: int = CAPACITY) -> None:
        super().__init__(level=logging.INFO)
        self.boot = uuid.uuid4().hex[:8]  # tells the server a new process started
        self._lines: deque[dict[str, Any]] = deque(maxlen=capacity)
        self._seq = itertools.count()
        self._guard = threading.Lock()

    def emit(self, record: logging.LogRecord) -> None:
        try:
            msg = record.getMessage()
            if record.exc_info and record.exc_info[1] is not None:
                msg = f"{msg}: {type(record.exc_info[1]).__name__}: {record.exc_info[1]}"
            with self._guard:
                self._lines.append(
                    {
                        "seq": next(self._seq),
                        "t": round(record.created, 3),
                        "level": record.levelname,
                        "logger": record.name,
                        "msg": msg[:2000],
                    }
                )
        except Exception:  # a log handler must never raise
            self.handleError(record)

    def after(self, seq: int, limit: int = BATCH) -> list[dict[str, Any]]:
        """Lines newer than ``seq`` (oldest first)."""
        with self._guard:
            return [line for line in self._lines if line["seq"] > seq][:limit]


BUFFER = LogBuffer()
