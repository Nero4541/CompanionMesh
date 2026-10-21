"""Replay harness: feed a recorded session through the perception steps offline.

    uv run companion replay labels.json [--fps 10] [--out report.json] [--repeat 2]

The recording's frames (decoded at ``--fps``) and its labelled utterances go
through a list of steps in time order, exactly as they would arrive live: an
utterance is handled once the frames up to its end have been seen. Each step
adds its findings to the utterances and a summary (scores against the hand
labels) to the report.

Everything a step reports is rounded and ordered, and the report carries a
``fingerprint`` (SHA-256 of all of it except timings): two runs of the same
recording with the same people and settings must give the same fingerprint.
``--repeat`` runs it several times and says whether they agree.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

import numpy as np

from companion.perception.recording import (
    SAMPLE_RATE,
    LabelledUtterance,
    Labels,
    VideoFrame,
    audio_span,
    load_audio,
    video_frames,
)


@dataclass
class ReplayContext:
    """Shared by the steps of one run: the labels and anything a step offers the others."""

    labels: Labels
    shared: dict[str, Any] = field(default_factory=dict)
    frames: int = 0
    last_t: float | None = None


class Step(Protocol):
    name: str

    async def frame(self, frame: VideoFrame, ctx: ReplayContext) -> None: ...

    async def utterance(
        self, utterance: LabelledUtterance, audio: np.ndarray, ctx: ReplayContext
    ) -> dict[str, Any]: ...

    def summary(self, ctx: ReplayContext, rows: list[dict[str, Any]]) -> dict[str, Any]: ...


def _round(value: Any) -> Any:
    """Floats to 3 decimals, recursively: what a report keeps."""
    if isinstance(value, float | np.floating):
        return round(float(value), 3)
    if isinstance(value, dict):
        return {str(k): _round(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_round(v) for v in value]
    if isinstance(value, np.integer):
        return int(value)
    return value


def fingerprint(report: dict[str, Any]) -> str:
    """SHA-256 of the report without its timings (which differ from run to run)."""
    stable = {k: v for k, v in report.items() if k not in ("timing", "fingerprint", "runs")}
    text = json.dumps(stable, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class MediaStep:
    """What the harness itself read: frame count and a digest, audio level per utterance.

    Proves the input is the same from run to run before any model sees it.
    """

    name = "media"

    def __init__(self) -> None:
        self._digest = hashlib.sha256()
        self._first: float | None = None

    async def frame(self, frame: VideoFrame, ctx: ReplayContext) -> None:
        if self._first is None:
            self._first = frame.t
        # A coarse thumbnail: decoders may differ in the last bit of a pixel.
        small = frame.rgb[:: max(1, frame.rgb.shape[0] // 36), :: max(1, frame.rgb.shape[1] // 64)]
        self._digest.update(f"{frame.t:.3f}".encode())
        self._digest.update((small // 16).astype(np.uint8).tobytes())

    async def utterance(
        self, utterance: LabelledUtterance, audio: np.ndarray, ctx: ReplayContext
    ) -> dict[str, Any]:
        rms = float(np.sqrt(np.mean(audio**2))) if audio.size else 0.0
        return {"audio_s": audio.size / SAMPLE_RATE, "rms": rms}

    def summary(self, ctx: ReplayContext, rows: list[dict[str, Any]]) -> dict[str, Any]:
        return {
            "frames": ctx.frames,
            "first_frame_t": self._first,
            "last_frame_t": ctx.last_t,
            "frames_digest": self._digest.hexdigest()[:16],
            "utterances": len(rows),
            "utterances_1s": sum(1 for r in rows if r["end"] - r["start"] >= 1.0),
        }


async def run_replay(
    labels: Labels,
    steps: Sequence[Step],
    *,
    fps: float = 10.0,
    video: Path | None = None,
    audio_path: Path | None = None,
    start: float | None = None,
    end: float | None = None,
) -> dict[str, Any]:
    """One run over the recording; returns the report (with its fingerprint)."""
    video = video or labels.video
    audio_path = audio_path or labels.audio or video
    start = labels.start if start is None else start
    end = labels.end if end is None else end
    ctx = ReplayContext(labels)
    timing: dict[str, float] = {"frames_s": 0.0, "utterances_s": 0.0}
    began = time.perf_counter()

    audio = np.zeros(0, np.float32)
    if audio_path is not None and labels.utterances:
        audio = await asyncio.to_thread(load_audio, audio_path, start, end)
    # Live, an utterance is handled when it ends.
    pending = sorted(
        (u for u in labels.utterances if u.start >= start and (end is None or u.end <= end)),
        key=lambda u: (u.end, u.start),
    )
    rows: list[dict[str, Any]] = []

    async def handle(utterance: LabelledUtterance) -> None:
        clip = audio_span(audio, start, utterance.start, utterance.end)
        row: dict[str, Any] = {
            "start": utterance.start,
            "end": utterance.end,
            "label": utterance.speaker,
            "addressed": utterance.addressed,
            "text": utterance.text,
        }
        t0 = time.perf_counter()
        for step in steps:
            row.update(await step.utterance(utterance, clip, ctx))
        timing["utterances_s"] += time.perf_counter() - t0
        rows.append(row)

    if video is not None:
        frames = video_frames(video, start, end, fps)
        while True:
            frame = await asyncio.to_thread(next, frames, None)
            if frame is None:
                break
            while pending and pending[0].end <= frame.t:
                await handle(pending.pop(0))
            t0 = time.perf_counter()
            for step in steps:
                await step.frame(frame, ctx)
            timing["frames_s"] += time.perf_counter() - t0
            ctx.frames += 1
            ctx.last_t = frame.t
    for utterance in pending:
        await handle(utterance)

    report: dict[str, Any] = {
        "recording": {
            "video": video.name if video else None,
            "audio": audio_path.name if audio_path else None,
            "start": start,
            "end": end,
            "fps": fps,
        },
        "steps": [s.name for s in steps],
        "utterances": rows,
        "summary": {s.name: s.summary(ctx, rows) for s in steps},
    }
    report = _round(report)
    timing["total_s"] = time.perf_counter() - began
    if ctx.frames:
        timing["ms_per_frame"] = 1000 * timing["frames_s"] / ctx.frames
    report["timing"] = _round(timing)
    report["fingerprint"] = fingerprint(report)
    return report


async def run_repeated(
    labels: Labels, make_steps: Any, repeat: int, **kwargs: Any
) -> dict[str, Any]:
    """Run ``repeat`` times with fresh steps each time; the last report plus agreement."""
    prints: list[str] = []
    report: dict[str, Any] = {}
    for _ in range(max(1, repeat)):
        report = await run_replay(labels, make_steps(), **kwargs)
        prints.append(report["fingerprint"])
    report["runs"] = {"count": len(prints), "fingerprints": prints, "same": len(set(prints)) == 1}
    return report


def add_arguments(parser: Any) -> None:
    """The ``companion replay`` command line."""
    parser.add_argument("labels", type=Path, help="the recording's labels file (JSON)")
    parser.add_argument("--video", type=Path, help="video file (default: from the labels)")
    parser.add_argument("--audio", type=Path, help="audio file (default: from the labels)")
    parser.add_argument("--start", type=float, help="seconds into the recording to start at")
    parser.add_argument("--end", type=float, help="seconds into the recording to stop at")
    parser.add_argument("--fps", type=float, default=10.0, help="frames per second to look at")
    parser.add_argument("--out", type=Path, help="write the report (JSON) here")
    parser.add_argument("--repeat", type=int, default=1, help="run N times and compare")


def run_cli(args: Any, config: Any) -> int:
    from companion.perception.recording import LabelsError, load_labels

    try:
        labels = load_labels(args.labels)
    except LabelsError as exc:
        print(f"error: {exc}")
        return 2
    if (args.video or labels.video) is None and not labels.utterances:
        print("error: nothing to replay: no video and no utterances")
        return 2

    def make_steps() -> list[Step]:
        return [MediaStep()]

    report = asyncio.run(
        run_repeated(
            labels,
            make_steps,
            args.repeat,
            fps=args.fps,
            video=args.video,
            audio_path=args.audio,
            start=args.start,
            end=args.end,
        )
    )
    text = json.dumps(report, ensure_ascii=False, indent=1)
    if args.out:
        args.out.write_text(text, encoding="utf-8")
    print(json.dumps({"summary": report["summary"], "timing": report["timing"]}, indent=1))
    print(f"fingerprint {report['fingerprint']}")
    if report["runs"]["count"] > 1:
        print("runs agree" if report["runs"]["same"] else "RUNS DIFFER")
        return 0 if report["runs"]["same"] else 1
    return 0
