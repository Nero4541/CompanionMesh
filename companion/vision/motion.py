"""Low-cost visual change detection on tiny grayscale frames (v0.5).

Runs where the camera is (the IP camera bridge here, the edge client on a
board) so only frames worth a look travel upstream:

    camera -> 64x48 gray -> motion / scene-change score -> interesting? -> send

* motion: share of pixels that changed since the previous frame.
* scene change: share of pixels that differ from a slowly adapting background,
  i.e. something moved in and stayed (or left).

An event needs ``min_frames`` consecutive frames over the threshold (one-frame
flicker is noise) and events of the same kind are at least ``cooldown_s``
apart. The algorithm is mirrored, without numpy, in ``companion_edge.motion``.
"""

from __future__ import annotations

import io
from dataclasses import dataclass

import numpy as np

SIZE = (64, 48)


@dataclass(frozen=True, slots=True)
class MotionSettings:
    pixel_threshold: int = 25  # 0-255 grey levels a pixel must change by
    motion_threshold: float = 0.02  # share of changed pixels that counts as motion
    scene_threshold: float = 0.25  # share differing from the background = new scene
    min_frames: int = 2
    cooldown_s: float = 10.0
    background_rate: float = 0.05  # how fast the background absorbs a change


@dataclass(frozen=True, slots=True)
class ChangeEvent:
    kind: str  # "motion" | "scene_change"
    score: float  # 0..1, larger = bigger change
    area: float  # share of pixels involved

    def to_payload(self) -> dict[str, float | str]:
        return {"kind": self.kind, "score": round(self.score, 3), "area": round(self.area, 4)}


class MotionDetector:
    def __init__(self, settings: MotionSettings | None = None) -> None:
        self.settings = settings or MotionSettings()
        self._prev: np.ndarray | None = None
        self._background: np.ndarray | None = None
        self._streak = {"motion": 0, "scene_change": 0}
        self._last_event = {"motion": -1e9, "scene_change": -1e9}

    def reset(self) -> None:
        self._prev = None
        self._background = None
        self._streak = {"motion": 0, "scene_change": 0}

    def feed(self, gray: np.ndarray, at: float) -> ChangeEvent | None:
        """Feed one small grayscale frame (uint8, any size); ``at`` in seconds."""
        s = self.settings
        frame = gray.astype(np.float32)
        if self._prev is None or self._prev.shape != frame.shape:
            self._prev = frame
            self._background = frame.copy()
            return None
        assert self._background is not None
        motion = float(np.mean(np.abs(frame - self._prev) > s.pixel_threshold))
        scene = float(np.mean(np.abs(frame - self._background) > s.pixel_threshold))
        self._prev = frame
        self._background += s.background_rate * (frame - self._background)

        event: ChangeEvent | None = None
        if self._sustained("scene_change", scene >= s.scene_threshold, at):
            event = ChangeEvent("scene_change", min(1.0, 0.5 + scene), scene)
            self._background = frame.copy()  # the new view is the new normal
            self._streak["motion"] = 0
        elif self._sustained("motion", motion >= s.motion_threshold, at):
            event = ChangeEvent("motion", min(1.0, motion / (4 * s.motion_threshold)), motion)
        return event

    def _sustained(self, kind: str, over: bool, at: float) -> bool:
        self._streak[kind] = self._streak[kind] + 1 if over else 0
        if self._streak[kind] < self.settings.min_frames:
            return False
        if at - self._last_event[kind] < self.settings.cooldown_s:
            return False
        self._last_event[kind] = at
        self._streak[kind] = 0
        return True


def tiny_gray(jpeg: bytes, size: tuple[int, int] = SIZE) -> np.ndarray:
    """Decode a JPEG straight to a tiny grayscale frame (JPEG draft mode: cheap)."""
    from PIL import Image

    with Image.open(io.BytesIO(jpeg)) as image:
        image.draft("L", (size[0] * 2, size[1] * 2))
        small = image.convert("L").resize(size)
        return np.asarray(small, dtype=np.uint8)


def frame_priority(reason: str, event: dict[str, object] | None) -> float:
    """How much a frame deserves analysis (0..1): asked-for > changed > routine."""
    if reason in ("manual", "question"):
        return 1.0
    if event:
        score = event.get("score")
        value = float(score) if isinstance(score, (int, float)) else 0.5
        bonus = 0.1 if event.get("kind") == "scene_change" else 0.0
        return max(0.3, min(0.95, 0.4 + 0.5 * value + bonus))
    if reason == "change":
        return 0.5
    return 0.2
