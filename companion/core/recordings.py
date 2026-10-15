"""Utterances as the server heard them (``stt.keep_audio``), for checking recognition.

Each utterance that reached speech recognition is written as a 16 kHz mono WAV
next to a small JSON file with what was recognized and what became of it
(a turn, dropped as the companion's own voice, or nothing recognized). The
dashboard lists them so "it misheard me" can be checked by listening to what
the microphone actually delivered. Files older than ``keep_audio_days`` go.
"""

from __future__ import annotations

import json
import logging
import re
import time
import uuid
import wave
from dataclasses import asdict, dataclass
from pathlib import Path

import numpy as np

from companion.core.logging import kv

log = logging.getLogger(__name__)

SAMPLE_RATE = 16_000
RECORDING_ID = re.compile(r"^\d{8}-\d{6}-[0-9a-f]{8}$")


@dataclass
class Recording:
    id: str
    timestamp: float
    session_id: str | None
    device_id: str | None
    duration_s: float
    text: str
    outcome: str  # turn | echo | empty
    stt_s: float | None = None


class RecordingStore:
    def __init__(self, root: Path) -> None:
        self.root = root

    def save(
        self,
        audio: np.ndarray,
        *,
        session_id: str | None,
        device_id: str | None,
        text: str,
        outcome: str,
        stt_s: float | None = None,
    ) -> Recording:
        now = time.time()
        rid = f"{time.strftime('%Y%m%d-%H%M%S', time.localtime(now))}-{uuid.uuid4().hex[:8]}"
        pcm = (np.clip(audio, -1.0, 1.0) * 32767).astype("<i2")
        rec = Recording(
            id=rid,
            timestamp=now,
            session_id=session_id,
            device_id=device_id,
            duration_s=round(pcm.size / SAMPLE_RATE, 2),
            text=text,
            outcome=outcome,
            stt_s=round(stt_s, 2) if stt_s is not None else None,
        )
        self.root.mkdir(parents=True, exist_ok=True)
        with wave.open(str(self.root / f"{rid}.wav"), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(SAMPLE_RATE)
            w.writeframes(pcm.tobytes())
        (self.root / f"{rid}.json").write_text(
            json.dumps(asdict(rec), ensure_ascii=False), encoding="utf-8"
        )
        return rec

    def list(self, *, limit: int = 100) -> list[Recording]:
        if not self.root.is_dir():
            return []
        out = []
        for path in sorted(self.root.glob("*.json"), reverse=True)[:limit]:
            try:
                out.append(Recording(**json.loads(path.read_text(encoding="utf-8"))))
            except (OSError, ValueError, TypeError):
                continue
        return out

    def audio_path(self, rid: str) -> Path | None:
        if not RECORDING_ID.match(rid):
            return None
        path = self.root / f"{rid}.wav"
        return path if path.is_file() else None

    def purge(self, older_than_days: float) -> int:
        if not self.root.is_dir():
            return 0
        cutoff = time.time() - older_than_days * 86400
        removed = 0
        for path in self.root.iterdir():
            if path.suffix in (".wav", ".json") and path.stat().st_mtime < cutoff:
                path.unlink(missing_ok=True)
                removed += path.suffix == ".wav"
        if removed:
            log.info("purged recordings", extra=kv(count=removed))
        return removed
