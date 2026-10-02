"""Structured logging on top of the standard library.

Use ``log.info("event name", extra={"fields": {...}})`` or the ``kv`` helper.
"""

from __future__ import annotations

import json
import logging
import sys
from typing import Any


def kv(**fields: Any) -> dict[str, Any]:
    return {"fields": fields}


class _TextFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        base = f"{self.formatTime(record, '%H:%M:%S')} {record.levelname:<7} {record.name}: "
        base += record.getMessage()
        fields = getattr(record, "fields", None)
        if fields:
            base += " " + " ".join(f"{k}={v!r}" for k, v in fields.items())
        if record.exc_info:
            base += "\n" + self.formatException(record.exc_info)
        return base


class _JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        out: dict[str, Any] = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        out.update(getattr(record, "fields", None) or {})
        if record.exc_info:
            out["exc"] = self.formatException(record.exc_info)
        return json.dumps(out, ensure_ascii=False, default=str)


def setup_logging(level: str = "INFO", fmt: str = "text") -> None:
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(_JsonFormatter() if fmt == "json" else _TextFormatter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)
    for noisy in ("httpx", "httpcore", "faster_whisper"):
        logging.getLogger(noisy).setLevel(max(logging.WARNING, root.level))
