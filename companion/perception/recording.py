"""A recorded session for the replay harness: video, audio and hand labels.

Labels are one JSON file::

    {
      "recording": {"video": "living.mp4", "audio": "living.m4a", "start": 0, "end": 1800},
      "people": {"yukari": {"role": "user"}, "kenta": {"role": "acquaintance"}},
      "utterances": [
        {"start": 12.4, "end": 14.1, "speaker": "yukari", "addressed": true, "text": "..."},
        {"start": 20.0, "end": 23.5, "speaker": "media", "text": "(TV)"}
      ],
      "faces": [{"t": 15.0, "box": [410, 120, 520, 260], "who": "kenta"}]
    }

``speaker`` is a key of ``people``, ``stranger``, ``media`` (TV, radio, a video)
or ``unknown``; ``addressed`` says whether the line was meant for the companion
(default false). ``faces`` are spot checks: who the face inside ``box`` at time
``t`` (seconds) is, a key of ``people``, ``stranger`` or ``picture`` (a poster
or a face on a screen). Paths in ``recording`` are relative to the labels file;
``audio`` may be left out when the video file has the sound.

Video is decoded with PyAV (no ffmpeg needed) at a fixed rate, audio as 16 kHz
mono; both are deterministic for the same file and window.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

SAMPLE_RATE = 16000
SPECIAL_SPEAKERS = ("stranger", "media", "unknown")
SPECIAL_FACES = ("stranger", "picture")
ROLES = ("user", "acquaintance")


class LabelsError(ValueError):
    """The labels file is not in the expected shape."""


@dataclass(frozen=True, slots=True)
class LabelledUtterance:
    start: float
    end: float
    speaker: str  # a person key, stranger, media or unknown
    addressed: bool = False
    text: str = ""

    @property
    def duration_s(self) -> float:
        return self.end - self.start


@dataclass(frozen=True, slots=True)
class LabelledFace:
    t: float
    box: tuple[float, float, float, float]
    who: str  # a person key, stranger or picture


@dataclass
class Labels:
    people: dict[str, str] = field(default_factory=dict)  # key -> role
    utterances: list[LabelledUtterance] = field(default_factory=list)
    faces: list[LabelledFace] = field(default_factory=list)
    video: Path | None = None
    audio: Path | None = None
    start: float = 0.0
    end: float | None = None

    def role_of(self, who: str) -> str:
        """user, acquaintance, or the special label itself (stranger, media, ...)."""
        return self.people.get(who, who)


def _number(value: Any, where: str) -> float:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise LabelsError(f"{where}: a number is expected")
    return float(value)


def parse_labels(data: Any, base: Path | None = None) -> Labels:
    """Check and convert the JSON of a labels file."""
    if not isinstance(data, dict):
        raise LabelsError("the labels file must hold a JSON object")
    labels = Labels()
    people = data.get("people") or {}
    if not isinstance(people, dict):
        raise LabelsError("people: an object {key: {role}} is expected")
    for key, entry in people.items():
        role = entry.get("role") if isinstance(entry, dict) else entry
        if role not in ROLES:
            raise LabelsError(f"people.{key}: role must be user or acquaintance")
        if key in SPECIAL_SPEAKERS or key in SPECIAL_FACES:
            raise LabelsError(f"people.{key}: this name is reserved")
        labels.people[str(key)] = str(role)
    known = set(labels.people)
    for i, row in enumerate(data.get("utterances") or []):
        where = f"utterances[{i}]"
        if not isinstance(row, dict):
            raise LabelsError(f"{where}: an object is expected")
        start, end = _number(row.get("start"), f"{where}.start"), _number(row.get("end"), where)
        if end <= start:
            raise LabelsError(f"{where}: end must be after start")
        speaker = str(row.get("speaker") or "unknown")
        if speaker not in known and speaker not in SPECIAL_SPEAKERS:
            raise LabelsError(f"{where}.speaker: {speaker!r} is not in people")
        labels.utterances.append(
            LabelledUtterance(
                start, end, speaker, bool(row.get("addressed", False)), str(row.get("text") or "")
            )
        )
    for i, row in enumerate(data.get("faces") or []):
        where = f"faces[{i}]"
        if not isinstance(row, dict):
            raise LabelsError(f"{where}: an object is expected")
        box = row.get("box")
        if not (isinstance(box, list) and len(box) == 4):
            raise LabelsError(f"{where}.box: [x0, y0, x1, y1] is expected")
        who = str(row.get("who") or "")
        if who not in known and who not in SPECIAL_FACES:
            raise LabelsError(f"{where}.who: {who!r} is not in people")
        coords = tuple(_number(v, f"{where}.box") for v in box)
        labels.faces.append(LabelledFace(_number(row.get("t"), f"{where}.t"), coords, who))  # type: ignore[arg-type]
    labels.utterances.sort(key=lambda u: (u.start, u.end))
    labels.faces.sort(key=lambda f: f.t)
    recording = data.get("recording") or {}
    if not isinstance(recording, dict):
        raise LabelsError("recording: an object is expected")
    for key in ("video", "audio"):
        if recording.get(key):
            path = Path(str(recording[key]))
            setattr(labels, key, path if path.is_absolute() or base is None else base / path)
    if recording.get("start") is not None:
        labels.start = _number(recording["start"], "recording.start")
    if recording.get("end") is not None:
        labels.end = _number(recording["end"], "recording.end")
    return labels


def load_labels(path: Path) -> Labels:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise LabelsError(f"{path}: {exc}") from exc
    return parse_labels(data, path.parent)


@dataclass(slots=True)
class VideoFrame:
    t: float  # seconds into the recording
    rgb: np.ndarray  # height x width x 3, uint8


def video_frames(path: Path, start: float, end: float | None, fps: float) -> Iterator[VideoFrame]:
    """Frames of ``path`` between ``start`` and ``end`` at ``fps`` (the first frame at or after
    each step), with their own timestamps."""
    import av

    step = 1.0 / fps
    next_t = start
    with av.open(str(path)) as container:
        stream = container.streams.video[0]
        if start > 0 and stream.time_base is not None:
            container.seek(int(max(start - 2.0, 0.0) / stream.time_base), stream=stream)
        for frame in container.decode(stream):
            t = frame.time
            if t is None or t < next_t - 1e-3:
                continue
            if end is not None and t >= end:
                break
            while next_t <= t + 1e-9:
                next_t += step
            yield VideoFrame(round(float(t), 3), frame.to_ndarray(format="rgb24"))


def load_audio(path: Path, start: float = 0.0, end: float | None = None) -> np.ndarray:
    """Mono float32 at 16 kHz from ``start`` to ``end`` seconds (sample 0 = ``start``)."""
    import av

    chunks: list[np.ndarray] = []
    with av.open(str(path)) as container:
        stream = container.streams.audio[0]
        if start > 0 and stream.time_base is not None:
            container.seek(int(max(start - 1.0, 0.0) / stream.time_base), stream=stream)
        resampler = av.AudioResampler(format="flt", layout="mono", rate=SAMPLE_RATE)
        first: float | None = None
        for frame in container.decode(stream):
            if first is None:
                first = float(frame.time or 0.0)
            for out in resampler.resample(frame):
                chunks.append(out.to_ndarray().reshape(-1))
            if end is not None and frame.time is not None and frame.time > end + 1.0:
                break
        for out in resampler.resample(None):
            chunks.append(out.to_ndarray().reshape(-1))
    audio = np.concatenate(chunks).astype(np.float32) if chunks else np.zeros(0, np.float32)
    offset = round((start - (first or 0.0)) * SAMPLE_RATE)
    audio = audio[max(offset, 0) :]
    if end is not None:
        audio = audio[: round((end - start) * SAMPLE_RATE)]
    return audio


def audio_span(audio: np.ndarray, origin: float, start: float, end: float) -> np.ndarray:
    """The samples of [start, end) from audio that begins at ``origin`` seconds."""
    a = max(round((start - origin) * SAMPLE_RATE), 0)
    b = max(round((end - origin) * SAMPLE_RATE), a)
    return audio[a:b]
