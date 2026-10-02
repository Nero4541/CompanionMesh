"""Optional on-disk image archive (``vision.store_images``), with retention."""

from __future__ import annotations

import logging
import time
from pathlib import Path

from companion.core.logging import kv
from companion.vision.observation import VisualObservation

log = logging.getLogger(__name__)


class ImageArchive:
    def __init__(self, root: Path, retention_days: float) -> None:
        self.root = root
        self.retention_days = retention_days

    def path_for(self, obs: VisualObservation) -> Path:
        day = obs.timestamp.strftime("%Y-%m-%d")
        return self.root / day / f"{obs.session_id or 'none'}_{obs.id}.jpg"

    def save(self, obs: VisualObservation, jpeg: bytes) -> Path:
        path = self.path_for(obs)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(jpeg)
        return path

    def purge(self) -> int:
        """Delete stored images older than the retention period."""
        if not self.root.is_dir():
            return 0
        cutoff = time.time() - self.retention_days * 86400
        removed = 0
        for path in self.root.glob("*/*.jpg"):
            if path.stat().st_mtime < cutoff:
                path.unlink(missing_ok=True)
                removed += 1
        for day in self.root.iterdir():
            if day.is_dir() and not any(day.iterdir()):
                day.rmdir()
        if removed:
            log.info("purged stored images", extra=kv(count=removed))
        return removed
