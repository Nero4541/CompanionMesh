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


def _jpeg(rgb: np.ndarray) -> bytes:
    import io

    from PIL import Image

    out = io.BytesIO()
    Image.fromarray(rgb).save(out, format="JPEG", quality=90)
    return out.getvalue()


class FaceStep:
    """Faces through the perception engine and the tracker, as the server does it.

    Scores the labelled faces: the track whose box covers a labelled face is
    named (after all its frames have voted) and compared with the label.
    """

    name = "faces"

    def __init__(
        self,
        backend: Any,
        registry: Any,
        *,
        face_threshold: float = 0.45,
        face_margin: float = 0.10,
        min_face_px: int = 40,
    ) -> None:
        from companion.perception.tracker import FaceTracker

        self.backend = backend
        self.registry = registry
        self.tracker = FaceTracker(
            "replay",
            lambda e: registry.match_face(e, face_threshold, face_margin),
            min_face_px=min_face_px,
            history_s=1e9,  # keep every track for the summary
        )
        self.ms: list[float] = []
        self.spots: list[tuple[str, str | None]] = []  # (label, track id or None)
        self._next_label = 0

    async def frame(self, frame: VideoFrame, ctx: ReplayContext) -> None:
        from companion.perception.tracker import iou, thumbnail

        jpeg = await asyncio.to_thread(_jpeg, frame.rgb)
        found = await self.backend.faces(jpeg)
        self.ms.append(found.ms)
        thumb = await asyncio.to_thread(thumbnail, frame.rgb)
        self.tracker.update(frame.t, found.faces, thumb)
        ctx.shared["tracker"] = self.tracker
        labels = ctx.labels.faces
        while self._next_label < len(labels) and labels[self._next_label].t <= frame.t + 1e-3:
            label = labels[self._next_label]
            self._next_label += 1
            if frame.t - label.t > 0.5:
                continue  # no frame close enough to this label
            best, best_iou = None, 0.3
            for track in self.tracker.tracks.values():
                if track.last_t == frame.t:
                    overlap = iou(label.box, track.box)
                    if overlap > best_iou:
                        best, best_iou = track.id, overlap
            self.spots.append((label.who, best))

    async def utterance(
        self, utterance: LabelledUtterance, audio: np.ndarray, ctx: ReplayContext
    ) -> dict[str, Any]:
        return {}

    def _got(self, track_id: str | None) -> str:
        track = self.tracker.tracks.get(track_id) if track_id else None
        if track is None:
            return "missed"
        if track.kind == "picture":
            return "picture"
        return track.name or "stranger"

    def summary(self, ctx: ReplayContext, rows: list[dict[str, Any]]) -> dict[str, Any]:
        from collections import Counter

        role = ctx.labels.role_of
        confusion: Counter[str] = Counter()
        by_track: dict[str, Counter[str]] = {}
        right = role_right = 0
        for who, track_id in self.spots:
            got = self._got(track_id)
            confusion[f"{who} -> {got}"] += 1
            right += got == who
            role_right += role(got) == role(who)
            if track_id is not None:
                by_track.setdefault(track_id, Counter())[who] += 1
        tracks_right = sum(
            self._got(tid) == votes.most_common(1)[0][0] for tid, votes in by_track.items()
        )
        tracks = self.tracker.tracks.values()
        return {
            "tracks": len(self.tracker.tracks),
            "tracks_named": sum(1 for t in tracks if t.person_id),
            "labelled_faces": len(self.spots),
            "faces_right": right,
            "faces_role_right": role_right,
            "faces_missed": sum(1 for _, t in self.spots if t is None),
            "labelled_tracks": len(by_track),
            "labelled_tracks_right": tracks_right,
            "track_accuracy": tracks_right / len(by_track) if by_track else None,
            "confusion": dict(sorted(confusion.items())),
        }

    def timing(self) -> dict[str, float]:
        return {"faces_engine_ms": float(np.mean(self.ms))} if self.ms else {}


class SpeakerStep:
    """Who said each utterance: its voice, the faces in view and their lips (FaceStep first).

    Scores utterances of a second or more: a labelled person must be named; a
    stranger or the TV must not be taken for anyone known. TV lines taken for
    the user are counted on their own (it must never happen).
    """

    name = "speakers"

    def __init__(
        self,
        backend: Any,
        registry: Any,
        *,
        voice_threshold: float = 0.5,
        voice_margin: float = 0.15,
        left_after_s: float = 60.0,
    ) -> None:
        self.backend = backend
        self.registry = registry
        self.voice_threshold = voice_threshold
        self.voice_margin = voice_margin
        self.left_after_s = left_after_s

    async def frame(self, frame: VideoFrame, ctx: ReplayContext) -> None:
        return None

    async def utterance(
        self, utterance: LabelledUtterance, audio: np.ndarray, ctx: ReplayContext
    ) -> dict[str, Any]:
        from companion.perception.speakers import attribute
        from companion.perception.tracker import talking_track

        voice = voice_to = None
        if audio.size >= 1600 and self.registry.voice_people():
            emb = await self.backend.voice(audio)
            if emb is not None:
                voice = self.registry.match_voice(emb, self.voice_threshold, 0.0)
                voice_to = self.registry.voice_similarities(emb)
        tracker = ctx.shared.get("tracker")
        visible, talking, around = [], None, {}
        if tracker is not None:
            visible = tracker.visible_between(utterance.start, utterance.end)
            talking, _ = talking_track(visible, utterance.start, utterance.end, tracker.fps)
            for track in tracker.tracks.values():
                recent = track.last_t >= utterance.start - self.left_after_s
                if track.person_id and track.name and recent and track.first_t <= utterance.end:
                    around[track.person_id] = (track.name, track.role)
        speaker = attribute(
            duration_s=utterance.duration_s,
            voice=voice,
            voice_to=voice_to,
            visible=visible,
            talking=talking,
            around=around,
            voice_margin=self.voice_margin,
        )
        ctx.shared.setdefault("speakers", []).append(speaker)
        return {
            "speaker": speaker.name or speaker.role,
            "speaker_role": speaker.role,
            "how": speaker.how,
            "confidence": speaker.confidence,
            "voice": None if voice is None else voice.closest,
            "voice_score": None if voice is None else voice.score,
            "voice_margin": None if voice is None else voice.margin,
        }

    def summary(self, ctx: ReplayContext, rows: list[dict[str, Any]]) -> dict[str, Any]:
        from collections import Counter

        def right(row: dict[str, Any]) -> bool:
            if row["label"] in ctx.labels.people:
                return bool(row["speaker"] == row["label"])
            return row["speaker_role"] in ("stranger", "media", "unknown")

        long = [r for r in rows if r["end"] - r["start"] >= 1.0]
        short = [r for r in rows if r["end"] - r["start"] < 1.0]
        confusion: Counter[str] = Counter(f"{r['label']} -> {r['speaker']}" for r in long)
        hows: Counter[str] = Counter(f"{r['how']}:{'right' if right(r) else 'wrong'}" for r in long)
        return {
            "utterances_1s": len(long),
            "right_1s": sum(right(r) for r in long),
            "accuracy_1s": sum(right(r) for r in long) / len(long) if long else None,
            "right_short": sum(right(r) for r in short),
            "short": len(short),
            "media_as_user": sum(
                1 for r in rows if r["label"] == "media" and r["speaker_role"] == "user"
            ),
            "confusion_1s": dict(sorted(confusion.items())),
            "how_1s": dict(sorted(hows.items())),
        }


class RoutingStep:
    """Whether each utterance would be answered (SpeakerStep first).

    No turn may come from a line not addressed to the companion (talk between
    people, strangers, the TV); addressed lines must still be answered.
    """

    name = "routing"

    def __init__(
        self,
        *,
        wake_names: list[str] | None = None,
        followup_s: float = 20.0,
        alone_means_addressed: bool = True,
        left_after_s: float = 60.0,
    ) -> None:
        self.wake_names = wake_names or []
        self.followup_s = followup_s
        self.alone_means_addressed = alone_means_addressed
        self.left_after_s = left_after_s

    async def frame(self, frame: VideoFrame, ctx: ReplayContext) -> None:
        return None

    def _around(self, ctx: ReplayContext, utterance: LabelledUtterance) -> int:
        tracker = ctx.shared.get("tracker")
        if tracker is None:
            return 0
        known, strangers = set(), 0
        for track in tracker.tracks.values():
            if track.kind != "person" or track.first_t > utterance.end:
                continue
            if track.person_id and track.last_t >= utterance.start - self.left_after_s:
                known.add(track.person_id)
            elif not track.person_id and track.last_t >= utterance.end - 1.0:
                strangers += 1
        return len(known) + strangers

    async def utterance(
        self, utterance: LabelledUtterance, audio: np.ndarray, ctx: ReplayContext
    ) -> dict[str, Any]:
        from companion.perception.routing import decide

        speakers = ctx.shared.get("speakers") or [None]
        route = decide(
            speakers[-1],
            utterance.text,
            wake_names=self.wake_names,
            since_companion_spoke_s=None,
            followup_s=self.followup_s,
            people_around=self._around(ctx, utterance),
            facing=False,
            alone_means_addressed=self.alone_means_addressed,
        )
        return {"route": route.route, "route_why": route.reasons[0] if route.reasons else ""}

    def summary(self, ctx: ReplayContext, rows: list[dict[str, Any]]) -> dict[str, Any]:
        from collections import Counter

        people = ctx.labels.people
        not_meant = [r for r in rows if not r["addressed"]]
        addressed = [r for r in rows if r["addressed"]]

        def kind(row: dict[str, Any]) -> str:
            if row["label"] in ("media", "stranger"):
                return str(row["label"])
            return "known" if row["label"] in people else "unknown"

        return {
            "utterances": len(rows),
            "not_addressed": len(not_meant),
            "turns_not_addressed": sum(1 for r in not_meant if r["route"] == "turn"),
            "addressed": len(addressed),
            "addressed_answered": sum(1 for r in addressed if r["route"] == "turn"),
            "routes": dict(sorted(Counter(f"{kind(r)}:{r['route']}" for r in rows).items())),
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
    for step in steps:
        extra = getattr(step, "timing", None)
        if extra is not None:
            timing.update(extra())
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
    parser.add_argument(
        "--people", type=Path, help="people folder (default: data/people of this config)"
    )
    parser.add_argument(
        "--engine",
        help="a running perception engine (http://127.0.0.1:<port>); default: start the "
        "built-in one with the perception settings",
    )
    parser.add_argument("--no-faces", action="store_true", help="do not look at faces")
    parser.add_argument(
        "--wake-name",
        action="append",
        default=[],
        help="a name of the companion that means it is spoken to (repeatable)",
    )


async def _backend(args: Any, config: Any) -> tuple[Any, Any]:
    """The perception engine to use: the one at --engine, or the built-in one started now."""
    from companion.perception.client import EngineClient

    if args.engine:
        client = EngineClient(args.engine)
        await client.health()
        return client, None
    from companion.builtin import Engine, worker_spec

    spec = worker_spec(
        "perception",
        config.model_copy(
            update={"perception": config.perception.model_copy(update={"enabled": True})}
        ),
    )
    engine = Engine("perception")
    assert spec is not None
    url = await engine.ensure(spec, config.builtin.start_timeout_s)
    if url is None:
        print(f"perception engine: {engine.status.state} {engine.status.error or ''}")
        return None, None
    client = EngineClient(url.removesuffix("/v1"))
    await client.health()
    return client, engine


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

    from companion.perception.registry import PeopleRegistry

    registry = PeopleRegistry(args.people or config.storage.data_dir / "people")
    people = config.people

    async def main() -> dict[str, Any]:
        backend, engine = None, None
        if not args.no_faces:
            backend, engine = await _backend(args, config)
            if backend is None:
                raise SystemExit("error: the perception engine is not available")

        def make_steps() -> list[Step]:
            steps: list[Step] = [MediaStep()]
            if backend is not None:
                steps.append(
                    FaceStep(
                        backend,
                        registry,
                        face_threshold=people.face_threshold,
                        face_margin=people.face_margin,
                        min_face_px=config.perception.min_face_px,
                    )
                )
                steps.append(
                    SpeakerStep(
                        backend,
                        registry,
                        voice_threshold=people.voice_threshold,
                        voice_margin=people.voice_margin,
                        left_after_s=config.perception.left_after_s,
                    )
                )
                steps.append(
                    RoutingStep(
                        wake_names=config.routing.wake_names or args.wake_name,
                        followup_s=config.routing.followup_s,
                        alone_means_addressed=config.routing.alone_means_addressed,
                        left_after_s=config.perception.left_after_s,
                    )
                )
            return steps

        try:
            return await run_repeated(
                labels,
                make_steps,
                args.repeat,
                fps=args.fps,
                video=args.video,
                audio_path=args.audio,
                start=args.start,
                end=args.end,
            )
        finally:
            if backend is not None:
                await backend.aclose()
            if engine is not None:
                await engine.stop()

    report = asyncio.run(main())
    text = json.dumps(report, ensure_ascii=False, indent=1)
    if args.out:
        args.out.write_text(text, encoding="utf-8")
    print(json.dumps({"summary": report["summary"], "timing": report["timing"]}, indent=1))
    print(f"fingerprint {report['fingerprint']}")
    if report["runs"]["count"] > 1:
        print("runs agree" if report["runs"]["same"] else "RUNS DIFFER")
        return 0 if report["runs"]["same"] else 1
    return 0
