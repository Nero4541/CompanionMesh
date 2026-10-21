"""Face tracks per camera: who is where, since when, and whether their lips move.

Ported from the experiments (.workspace prototype ``pipeline.py``):

- Faces of consecutive frames are linked by box overlap (IoU >= 0.3, best first).
- A shot cut or camera move (mean difference of 64 x 36 grey thumbnails > 30)
  ends every track, so a face is never carried across to someone else.
- A track's identity is a vote over its frames: one confident frame names the
  whole track (side views then inherit the name).
- Each track keeps a series of mouth gaps (inner-lip opening / mouth width);
  the spread of that series over an utterance says who was talking (needs
  >= 15 frames a second).

Times are plain seconds (wall clock on the server, recording time in replays).
"""

from __future__ import annotations

from collections import Counter, deque
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from companion.perception.registry import Match

IOU_LINK = 0.3
SHOT_CUT = 30.0  # mean |difference| of 64 x 36 grey thumbnails that counts as a cut
MOUTH_TALKING = 0.035  # spread of the mouth gap over an utterance that counts as talking
MOUTH_LEAD = 1.3  # and this many times the next track's
HISTORY_S = 300.0  # ended tracks are kept this long, for who was there during an utterance
FRONTAL_YAW = 0.35  # |yaw| below this is a face turned to the camera, good as a sample


@dataclass(slots=True)
class FaceObs:
    """One face in one frame, as the perception engine describes it."""

    box: tuple[float, float, float, float]
    score: float
    embedding: np.ndarray
    mouth: float | None = None
    yaw: float | None = None

    @property
    def height(self) -> float:
        return self.box[3] - self.box[1]


def iou(a: Iterable[float], b: Iterable[float]) -> float:
    ax0, ay0, ax1, ay1 = a
    bx0, by0, bx1, by1 = b
    x0, y0, x1, y1 = max(ax0, bx0), max(ay0, by0), min(ax1, bx1), min(ay1, by1)
    inter = max(0.0, x1 - x0) * max(0.0, y1 - y0)
    union = (ax1 - ax0) * (ay1 - ay0) + (bx1 - bx0) * (by1 - by0) - inter
    return inter / (union + 1e-6)


@dataclass
class Track:
    id: str
    camera: str
    shot: int
    first_t: float
    last_t: float
    box: tuple[float, float, float, float]
    frames: int = 0
    votes: Counter[str] = field(default_factory=Counter)
    names: dict[str, tuple[str, str]] = field(default_factory=dict)  # id -> (name, role)
    best_score: float = 0.0  # best similarity to anyone known (for strangers: how close)
    kind: str = "person"  # person | picture
    picture_checked: bool = False
    ended: bool = False
    yaw: float | None = None
    mouths: deque[tuple[float, float]] = field(default_factory=lambda: deque(maxlen=1800))
    boxes: deque[tuple[float, tuple[float, float, float, float]]] = field(
        default_factory=lambda: deque(maxlen=600)
    )
    _sum: np.ndarray | None = None
    samples: list[np.ndarray] = field(default_factory=list)  # good, frontal faces (for enrolling)
    stranger_id: str | None = None

    @property
    def person_id(self) -> str | None:
        if not self.votes:
            return None
        return self.votes.most_common(1)[0][0]

    @property
    def name(self) -> str | None:
        pid = self.person_id
        return self.names[pid][0] if pid else None

    @property
    def role(self) -> str:
        pid = self.person_id
        return self.names[pid][1] if pid else "stranger"

    @property
    def embedding(self) -> np.ndarray | None:
        if self._sum is None:
            return None
        return self._sum / (np.linalg.norm(self._sum) + 1e-12)

    @property
    def duration_s(self) -> float:
        return self.last_t - self.first_t

    def seen_during(self, start: float, end: float) -> bool:
        return self.first_t <= end and self.last_t >= start

    def mouth_spread(self, start: float, end: float, min_frames: int) -> float | None:
        """Standard deviation of the mouth gap over [start, end], or None with too few frames."""
        gaps = [g for t, g in self.mouths if start <= t <= end]
        if len(gaps) < max(3, min_frames):
            return None
        return float(np.std(gaps))

    def describe(self) -> dict[str, Any]:
        return {
            "track_id": self.id,
            "camera": self.camera,
            "person_id": self.person_id,
            "name": self.name,
            "role": self.role,
            "kind": self.kind,
            "since": round(self.first_t, 3),
            "last_seen": round(self.last_t, 3),
            "frames": self.frames,
            "box": [round(v, 1) for v in self.box],
            "yaw": None if self.yaw is None else round(self.yaw, 3),
        }


Matcher = Callable[[np.ndarray], Match]


class FaceTracker:
    """The tracks of one camera."""

    def __init__(
        self,
        camera: str,
        matcher: Matcher,
        *,
        lost_s: float = 0.5,
        min_face_px: int = 40,
        history_s: float = HISTORY_S,
    ) -> None:
        self.camera = camera
        self.matcher = matcher
        self.lost_s = lost_s
        self.min_face_px = min_face_px
        self.history_s = history_s
        self.tracks: dict[str, Track] = {}
        self.shot = 0
        self.fps = 0.0  # frames a second actually seen (moving average)
        self._thumb: np.ndarray | None = None
        self._last_t: float | None = None
        self._count = 0

    @property
    def active(self) -> list[Track]:
        return [t for t in self.tracks.values() if not t.ended]

    def _new_id(self) -> str:
        self._count += 1
        return f"{self.camera}-{self._count}"

    def update(
        self, t: float, faces: list[FaceObs], thumb: np.ndarray | None = None
    ) -> tuple[list[Track], list[Track]]:
        """Take one frame's faces; returns (tracks started, tracks ended) by this frame."""
        if self._last_t is not None and t > self._last_t:
            rate = 1.0 / (t - self._last_t)
            self.fps = rate if self.fps == 0 else 0.9 * self.fps + 0.1 * rate
        self._last_t = t
        ended: list[Track] = []
        if thumb is not None:
            if self._thumb is not None and float(np.abs(thumb - self._thumb).mean()) > SHOT_CUT:
                self.shot += 1
                for track in self.active:
                    track.ended = True
                    ended.append(track)
            self._thumb = thumb
        started: list[Track] = []
        used: set[str] = set()
        active = self.active
        for face in sorted(faces, key=lambda f: -f.score):
            best, best_iou = None, IOU_LINK
            for track in active:
                if track.id in used:
                    continue
                overlap = iou(face.box, track.box)
                if overlap > best_iou:
                    best, best_iou = track, overlap
            if best is None:
                best = Track(self._new_id(), self.camera, self.shot, t, t, face.box)
                self.tracks[best.id] = best
                started.append(best)
            used.add(best.id)
            self._add(best, t, face)
        # A track not seen for lost_s (longer at low frame rates) has ended.
        lost = max(self.lost_s, 2.5 / self.fps) if self.fps > 0 else self.lost_s
        for track in self.active:
            if track.id not in used and t - track.last_t > lost:
                track.ended = True
                ended.append(track)
        for key in [
            k for k, tr in self.tracks.items() if tr.ended and t - tr.last_t > self.history_s
        ]:
            del self.tracks[key]
        return started, ended

    def _add(self, track: Track, t: float, face: FaceObs) -> None:
        track.last_t = t
        track.box = face.box
        track.frames += 1
        track.yaw = face.yaw
        track.boxes.append((t, face.box))
        if face.mouth is not None:
            track.mouths.append((t, face.mouth))
        emb = np.asarray(face.embedding, np.float32)
        track._sum = emb.copy() if track._sum is None else track._sum + emb
        match = self.matcher(emb)
        track.best_score = max(track.best_score, match.score)
        if match.person_id is not None and match.name is not None:
            track.votes[match.person_id] += 1
            track.names[match.person_id] = (match.name, match.role)
        frontal = face.yaw is None or abs(face.yaw) < FRONTAL_YAW
        if face.height >= self.min_face_px and frontal and len(track.samples) < 200:
            if not track.samples or float(max(s @ emb for s in track.samples[-20:])) < 0.95:
                track.samples.append(emb)

    def visible_between(self, start: float, end: float) -> list[Track]:
        return [t for t in self.tracks.values() if t.kind == "person" and t.seen_during(start, end)]

    def end_all(self) -> list[Track]:
        ended = self.active
        for track in ended:
            track.ended = True
        return ended


def thumbnail(rgb: np.ndarray) -> np.ndarray:
    """64 x 36 grey picture of a frame, for spotting cuts and camera moves."""
    from PIL import Image

    return np.asarray(
        Image.fromarray(rgb).convert("L").resize((64, 36), Image.Resampling.BILINEAR), np.float32
    )


def jpeg_thumbnail(jpeg: bytes) -> np.ndarray:
    import io

    from PIL import Image

    with Image.open(io.BytesIO(jpeg)) as image:
        image.draft("L", (128, 72))
        small = image.convert("L").resize((64, 36), Image.Resampling.BILINEAR)
        return np.asarray(small, np.float32)


def talking_track(
    tracks: list[Track], start: float, end: float, fps: float
) -> tuple[Track | None, list[tuple[Track, float]]]:
    """The track whose lips clearly moved during [start, end], and every visible track's spread.

    Clear: spread >= MOUTH_TALKING and MOUTH_LEAD times the next track's (as tested).
    Needs about 40 % of the frames of the span at ``fps``.
    """
    min_frames = int(0.4 * (end - start) * fps)
    scored = []
    for track in tracks:
        spread_ = track.mouth_spread(start, end, min_frames)
        if spread_ is not None:
            scored.append((track, spread_))
    scored.sort(key=lambda x: -x[1])
    if not scored or scored[0][1] < MOUTH_TALKING:
        return None, scored
    if len(scored) > 1 and scored[0][1] < MOUTH_LEAD * scored[1][1]:
        return None, scored
    return scored[0][0], scored
