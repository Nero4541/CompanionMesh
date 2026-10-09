"""Visual change detection on tiny grayscale frames, in plain Python (no numpy).

Mirrors ``companion.vision.motion`` on the server: ffmpeg scales the camera
down to 64x48 grey, and only frames where something moved, or the scene
changed and stayed changed, are sent upstream. At 3072 pixels a frame this
costs a few milliseconds even on a single-core RISC-V board.
"""

from __future__ import annotations

from dataclasses import dataclass

WIDTH, HEIGHT = 64, 48
FRAME_BYTES = WIDTH * HEIGHT


@dataclass(frozen=True)
class MotionSettings:
    pixel_threshold: int = 25
    motion_threshold: float = 0.02
    scene_threshold: float = 0.25
    min_frames: int = 2
    cooldown_s: float = 10.0
    background_rate: float = 0.05


@dataclass(frozen=True)
class ChangeEvent:
    kind: str  # "motion" | "scene_change"
    score: float
    area: float

    def to_payload(self) -> dict[str, object]:
        return {"kind": self.kind, "score": round(self.score, 3), "area": round(self.area, 4)}


class MotionDetector:
    def __init__(self, settings: MotionSettings | None = None) -> None:
        self.settings = settings or MotionSettings()
        self._prev: bytes | None = None
        self._background: list[float] | None = None
        self._streak = {"motion": 0, "scene_change": 0}
        self._last_event = {"motion": -1e9, "scene_change": -1e9}

    def reset(self) -> None:
        self._prev = None
        self._background = None
        self._streak = {"motion": 0, "scene_change": 0}

    def feed(self, gray: bytes, at: float) -> ChangeEvent | None:
        """One grayscale frame (one byte per pixel); ``at`` in seconds."""
        s = self.settings
        if self._prev is None or self._background is None or len(gray) != len(self._prev):
            self._prev = gray
            self._background = [float(v) for v in gray]
            return None
        t = s.pixel_threshold
        rate = s.background_rate
        prev, background = self._prev, self._background
        moved = 0
        differs = 0
        for i, v in enumerate(gray):
            if abs(v - prev[i]) > t:
                moved += 1
            b = background[i]
            if abs(v - b) > t:
                differs += 1
            background[i] = b + rate * (v - b)
        n = len(gray) or 1
        motion, scene = moved / n, differs / n
        self._prev = gray

        if self._sustained("scene_change", scene >= s.scene_threshold, at):
            self._background = [float(v) for v in gray]
            self._streak["motion"] = 0
            return ChangeEvent("scene_change", min(1.0, 0.5 + scene), scene)
        if self._sustained("motion", motion >= s.motion_threshold, at):
            return ChangeEvent("motion", min(1.0, motion / (4 * s.motion_threshold)), motion)
        return None

    def _sustained(self, kind: str, over: bool, at: float) -> bool:
        self._streak[kind] = self._streak[kind] + 1 if over else 0
        if self._streak[kind] < self.settings.min_frames:
            return False
        if at - self._last_event[kind] < self.settings.cooldown_s:
            return False
        self._last_event[kind] = at
        self._streak[kind] = 0
        return True
