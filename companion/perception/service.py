"""Perception in the running server: faces from every camera, tracked, named, turned into events.

Frames come from two places:

- cameras the server watches itself (``cameras:``), pulled at their
  ``perception_fps`` (10 by default) straight from the camera's own stream;
- frames devices send to a conversation (boards, ``companion camera``), about
  one a second, offered here as they arrive.

A frame is analysed by the perception engine unless the previous frame of that
camera is still being analysed: frames are skipped, never queued. Tracks feed
the presence (who is here, since when), which turns into attention events:
``person_arrived``, ``person_left``, ``stranger_appeared``, ``user_left`` and
``user_returned``. An unknown face that stays gets one picture check (is it a
real person or a poster/screen?) by the vision model; pictures are remembered
and ignored from then on.
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import logging
import time
from collections import deque
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import numpy as np

from companion.attention.engine import AttentionEvent
from companion.core.errors import CompanionError
from companion.core.logging import kv
from companion.perception.client import FrameFaces, PerceptionBackend, wav_bytes
from companion.perception.glance import PerceptionState, marked_picture, marks_for, parse, question
from companion.perception.registry import ROLE_TEXT, Match, PeopleRegistry
from companion.perception.speakers import Speaker, attribute
from companion.perception.timeline import Fired, Timeline
from companion.perception.tracker import FaceTracker, Track, jpeg_thumbnail, talking_track
from companion.protocol import events as ev

if TYPE_CHECKING:
    from companion.core.config import CompanionConfig
    from companion.core.runtime import CompanionRuntime
    from companion.core.session import Session

log = logging.getLogger(__name__)

STRANGER_CHECK_S = 1.5  # an unknown face this long gets its one picture check
FRESH_FRAME_S = 5.0  # a glance looks only at a picture at most this old
SAME_PICTURE = 1.0  # mean difference of thumbnails below which a picture is "the same"
LOOK_QUESTION = (
    "People in this camera picture are marked: {legend}. Describe in two or three short "
    "sentences what is happening: who is there, what they are doing, anything notable. Use the "
    "names given; do not guess who unmarked people are."
)
CANDIDATE_S = 1.5  # utterances this long, with voice, face and lips agreeing, are voice samples
MAX_CANDIDATES = 20
HOUSEKEEPING_S = 5.0
STREAM_LEASE_S = 30.0  # stream mode: a device streams this long per lease ...
STREAM_RENEW_S = 20.0  # ... renewed this often while perception wants its pictures
SAVE_STRANGERS_S = 60.0
PICTURE_QUESTION = (
    "The first image is the whole scene; the second is a close-up of the face inside the red "
    "box. Is that face a real, live person in the room, or a picture (poster, illustration, "
    "drawing, photo, or screen)? Answer with one word: person or picture."
)


@dataclass
class Presence:
    """A known person seen recently."""

    person_id: str
    name: str
    role: str
    since: float
    last_seen: float
    camera: str


def describe_person(name: str | None, role: str) -> str:
    if name:
        return f"{name} ({ROLE_TEXT.get(role, role)})"
    return ROLE_TEXT.get(role, "someone")


def picture_images(jpeg: bytes, box: tuple[float, float, float, float]) -> list[bytes]:
    """The whole frame with the face boxed in red, and a 3x close-up of the face."""
    from PIL import Image, ImageDraw

    with Image.open(io.BytesIO(jpeg)) as opened:
        image = opened.convert("RGB")
    ImageDraw.Draw(image).rectangle(box, outline="red", width=4)
    cx, cy = (box[0] + box[2]) / 2, (box[1] + box[3]) / 2
    half = 1.5 * max(box[2] - box[0], box[3] - box[1])
    zoom = image.crop((int(cx - half), int(cy - half), int(cx + half), int(cy + half)))
    zoom = zoom.resize((448, 448))
    out = []
    for picture in (image, zoom):
        buffer = io.BytesIO()
        picture.save(buffer, format="JPEG", quality=88)
        out.append(buffer.getvalue())
    return out


def never_moves(track: Track, min_s: float) -> bool:
    """A face that stayed exactly in place for ``min_s`` (a poster, without a VLM to ask)."""
    if track.duration_s < min_s or len(track.boxes) < 5:
        return False
    boxes = np.array([b for _, b in track.boxes], np.float32)
    size = float(np.mean(boxes[:, 3] - boxes[:, 1])) + 1e-6
    centres = np.stack([(boxes[:, 0] + boxes[:, 2]) / 2, (boxes[:, 1] + boxes[:, 3]) / 2], 1)
    return float(centres.std(axis=0).max()) / size < 0.02


class PerceptionService:
    def __init__(self, runtime: CompanionRuntime) -> None:
        self.runtime = runtime
        config = runtime.config
        self.registry = PeopleRegistry(
            config.storage.data_dir / "people", max_face_samples=config.people.max_face_samples
        )
        self.backend: PerceptionBackend | None = None
        self.trackers: dict[str, FaceTracker] = {}
        self.present: dict[str, Presence] = {}
        self.frames: dict[str, tuple[bytes, float]] = {}  # camera -> newest frame, when
        self.stats: dict[str, Any] = {
            "analysed": 0,
            "skipped": 0,
            "errors": 0,
            "ms": deque(maxlen=50),
        }
        self.last_error: str | None = None
        self._busy: set[str] = set()
        self._loops: dict[str, tuple[float, asyncio.Task[None]]] = {}
        self._tasks: set[asyncio.Task[Any]] = set()
        self._user_left_at: float | None = None
        self._strangers_announced: dict[str, float] = {}
        self._housekeeping: asyncio.Task[None] | None = None
        self._last_saved = 0.0
        self.picture_asks = 0
        # Voice samples heard with voice, face and lips agreeing: person id -> [(emb, wav)].
        self.voice_candidates: dict[str, list[tuple[np.ndarray, bytes]]] = {}
        # Glances: who is doing what (perception.glance), and the timeline they feed.
        self.state = PerceptionState()
        self.timeline = Timeline(
            config.storage.data_dir / "timeline.db",
            min_switch_s=config.timeline.min_switch_s,
            retention_days=config.timeline.retention_days,
        )
        self._glance_task: asyncio.Task[None] | None = None
        self._glanced: dict[str, np.ndarray] = {}  # camera -> thumbnail of the last glance
        self._glance_turn = 0
        self._last_sample = 0.0
        # Stream mode: when each streaming device's lease was last renewed.
        self._leases: dict[int, float] = {}

    # -- state -------------------------------------------------------------------

    @property
    def config(self) -> CompanionConfig:
        return self.runtime.config

    @property
    def on(self) -> bool:
        return self.config.perception.enabled

    @property
    def ready(self) -> bool:
        return self.on and self.backend is not None

    def matcher(self, embedding: np.ndarray) -> Match:
        people = self.config.people
        return self.registry.match_face(embedding, people.face_threshold, people.face_margin)

    def tracker(self, camera: str) -> FaceTracker:
        tracker = self.trackers.get(camera)
        if tracker is None:
            tracker = FaceTracker(
                camera, self.matcher, min_face_px=self.config.perception.min_face_px
            )
            self.trackers[camera] = tracker
        return tracker

    def set_backend(self, backend: PerceptionBackend | None) -> None:
        old, self.backend = self.backend, backend
        if old is not None and old is not backend:
            self._spawn(old.aclose())
        if backend is not None and self._housekeeping is None:
            with contextlib.suppress(RuntimeError):
                self._housekeeping = asyncio.get_running_loop().create_task(self._housekeep())
        self.apply()

    def _spawn(self, coro: Any) -> None:
        try:
            task = asyncio.get_running_loop().create_task(coro)
        except RuntimeError:
            coro.close()
            return
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    # -- frames --------------------------------------------------------------------

    def has_loop(self, camera: str) -> bool:
        return camera in self._loops

    def busy(self, camera: str) -> bool:
        """A picture of this camera is being analysed now (the next would be skipped)."""
        return camera in self._busy

    def offer_frame(self, camera: str, jpeg: bytes, *, at: float | None = None) -> bool:
        """A frame a device sent; analysed unless that camera is busy. False if skipped."""
        if not self.ready or self.has_loop(camera):
            return False
        if camera in self._busy:
            self.stats["skipped"] += 1
            return False
        self._busy.add(camera)
        self._spawn(self._analyse_and_release(camera, jpeg, at or time.time()))
        return True

    async def _analyse_and_release(self, camera: str, jpeg: bytes, at: float) -> None:
        try:
            await self.analyse(camera, jpeg, at)
        finally:
            self._busy.discard(camera)

    async def analyse(self, camera: str, jpeg: bytes, at: float) -> FrameFaces | None:
        backend = self.backend
        if backend is None:
            return None
        self.frames[camera] = (jpeg, at)
        try:
            found = await backend.faces(jpeg)
            thumb = await asyncio.to_thread(jpeg_thumbnail, jpeg)
        except (CompanionError, OSError) as exc:
            self.stats["errors"] += 1
            if str(exc) != self.last_error:
                log.warning("perception failed", extra=kv(camera=camera, error=str(exc)))
            self.last_error = str(exc)
            return None
        self.last_error = None
        self.stats["analysed"] += 1
        self.stats["ms"].append(found.ms)
        tracker = self.tracker(camera)
        tracker.update(at, found.faces, thumb)
        await self._after_frame(camera, tracker, at, jpeg)
        return found

    async def _after_frame(self, camera: str, tracker: FaceTracker, at: float, jpeg: bytes) -> None:
        changed = False
        for track in tracker.active:
            if track.kind != "person":
                continue
            pid = track.person_id
            if pid is not None:
                changed |= await self._seen(track, at)
            elif track.duration_s >= STRANGER_CHECK_S and not track.picture_checked:
                track.picture_checked = True
                emb = track.embedding
                if emb is not None and self.registry.is_picture(emb):
                    track.kind = "picture"
                    changed = True
                    continue
                if self.config.perception.picture_check and self._vlm_asker() is not None:
                    self._spawn(self._check_picture(camera, track, jpeg))
                else:
                    await self._stranger(track, at)
                    changed = True
            elif (
                track.picture_checked
                and self._vlm_asker() is None
                and never_moves(track, self.config.perception.static_picture_s)
            ):
                track.kind = "picture"
                if track.embedding is not None:
                    self.registry.add_picture(track.embedding)
                changed = True
        if changed:
            await self._presence_changed(camera)

    async def _seen(self, track: Track, at: float) -> bool:
        pid, name, role = track.person_id, track.name, track.role
        assert pid is not None and name is not None
        entry = self.present.get(pid)
        if entry is not None:
            entry.last_seen = max(entry.last_seen, at)
            entry.camera = track.camera
            return False
        self.present[pid] = Presence(pid, name, role, at, at, track.camera)
        if role == "user":
            away = None if self._user_left_at is None else at - self._user_left_at
            self._user_left_at = None
            if away is not None and away >= self.config.attention.absence_s:
                minutes = max(1, round(away / 60))
                await self._event(
                    "user_returned",
                    f"{name} (the user) came back after about {minutes} minutes away.",
                    0.85,
                    track.camera,
                    person=name,
                    away_s=round(away),
                )
            return True
        await self._event(
            "person_arrived",
            f"{describe_person(name, role)} arrived.",
            0.7,
            track.camera,
            person=name,
        )
        return True

    async def _stranger(self, track: Track, at: float) -> None:
        emb = track.embedding
        if emb is None:
            return
        stranger = self.registry.note_stranger(emb, at, track.camera)
        track.stranger_id = stranger.id
        last = self._strangers_announced.get(stranger.id)
        self._strangers_announced[stranger.id] = at
        if last is None or at - last > self.config.perception.left_after_s:
            await self._event(
                "stranger_appeared",
                "Someone the companion does not know (a stranger) is in view.",
                0.6,
                track.camera,
                track_id=track.id,
            )

    def _vlm_asker(self) -> Any:
        vlm = self.runtime.vlm
        return getattr(vlm, "ask", None) if vlm is not None else None

    async def _check_picture(self, camera: str, track: Track, jpeg: bytes) -> None:
        ask = self._vlm_asker()
        if ask is None:
            return
        try:
            images = await asyncio.to_thread(picture_images, jpeg, track.box)
            self.picture_asks += 1
            answer = str(await ask(images, PICTURE_QUESTION, max_tokens=5))
        except (CompanionError, OSError) as exc:
            log.warning("picture check failed", extra=kv(track=track.id, error=str(exc)))
            await self._stranger(track, track.last_t)
            await self._presence_changed(camera)
            return
        picture = "picture" in answer.lower()
        log.info("picture check", extra=kv(track=track.id, answer=answer.strip()[:20]))
        if picture:
            track.kind = "picture"
            if track.embedding is not None:
                self.registry.add_picture(track.embedding)
        else:
            await self._stranger(track, track.last_t)
        await self._presence_changed(camera)

    # -- presence ------------------------------------------------------------------

    def people_here(self) -> list[dict[str, Any]]:
        """Who is here: known people (recently seen) and strangers in view now."""
        now = time.time()
        out = [
            {
                "person_id": p.person_id,
                "name": p.name,
                "role": p.role,
                "since": round(p.since, 3),
                "last_seen_s": round(now - p.last_seen, 1),
                "camera": p.camera,
            }
            for p in sorted(self.present.values(), key=lambda p: p.since)
        ]
        for tracker in self.trackers.values():
            for track in tracker.active:
                if track.kind == "person" and track.person_id is None and track.picture_checked:
                    out.append(
                        {
                            "person_id": None,
                            "name": None,
                            "role": "stranger",
                            "since": round(track.first_t, 3),
                            "last_seen_s": round(now - track.last_t, 1),
                            "camera": track.camera,
                            "track_id": track.id,
                        }
                    )
        return out

    def tracks(self) -> list[dict[str, Any]]:
        """Every face tracked right now, with its box (for the camera views)."""
        return [t.describe() for tr in self.trackers.values() for t in tr.active]

    def find_track(self, track_id: str) -> Track | None:
        for tracker in self.trackers.values():
            if track_id in tracker.tracks:
                return tracker.tracks[track_id]
        return None

    async def _presence_changed(self, camera: str) -> None:
        session = self._session_for(camera)
        if session is not None:
            await session.emit(
                ev.PRESENCE_UPDATE, {"people": self.people_here(), "tracks": self.tracks()}
            )

    async def _housekeep(self) -> None:
        while True:
            await asyncio.sleep(HOUSEKEEPING_S)
            with contextlib.suppress(Exception):
                await self.expire(time.time())
            with contextlib.suppress(Exception):
                await self.lease_streams(time.time())

    async def lease_streams(self, now: float) -> None:
        """Ask devices that can stream (stream mode) to, while perception wants their pictures.

        A lease lasts STREAM_LEASE_S and is renewed every STREAM_RENEW_S; once
        perception no longer wants it, the device is told to stop at once.
        """
        fps = self.config.perception.stream_fps
        seen: set[int] = set()
        for session in list(self.runtime.sessions.values()):
            for device in list(getattr(session, "devices", [])):
                offer = getattr(device, "stream", None)
                if not offer or "camera" not in device.roles:
                    continue
                key = id(device)
                seen.add(key)
                want = self.ready and fps > 0 and session.vision.from_devices
                last = self._leases.get(key)
                if want and (last is None or now - last >= STREAM_RENEW_S):
                    formats = offer.get("formats") or ["mjpeg"]
                    rate = min(fps, float(offer.get("max_fps") or fps))
                    await session.emit_to(
                        [device],
                        ev.CAMERA_STREAM,
                        {"fps": rate, "seconds": STREAM_LEASE_S, "format": formats[0]},
                    )
                    self._leases[key] = now
                elif not want and last is not None:
                    await session.emit_to([device], ev.CAMERA_STREAM, {"fps": 0, "seconds": 0})
                    del self._leases[key]
        for key in [k for k in self._leases if k not in seen]:
            del self._leases[key]

    async def expire(self, now: float) -> None:
        """People not seen for perception.left_after_s have left."""
        gone = [
            p
            for p in self.present.values()
            if now - p.last_seen > self.config.perception.left_after_s
        ]
        for person in gone:
            del self.present[person.person_id]
            if person.role == "user":
                self._user_left_at = person.last_seen
                await self._event(
                    "user_left", f"{person.name} (the user) left.", 0.35, person.camera
                )
            else:
                await self._event(
                    "person_left",
                    f"{describe_person(person.name, person.role)} left.",
                    0.3,
                    person.camera,
                    person=person.name,
                )
        if gone:
            await self._presence_changed(gone[0].camera)
        if (
            self.config.timeline.enabled
            and now - self._last_sample >= self.config.timeline.sample_s
        ):
            self._last_sample = now
            await self.sample_presence(now)
            self.timeline.flush(now)
        self.state.forget_older(now - self.config.perception.left_after_s)
        if now - self._last_saved >= SAVE_STRANGERS_S:
            self._last_saved = now
            await asyncio.to_thread(
                self.registry.save_strangers, self.config.people.stranger_retention_h, now
            )

    def _session_for(self, camera: str) -> Session | None:
        sessions = self.runtime.sessions
        for session in sessions.values():
            if any(d.device_id == camera for d in session.devices):
                return session
        return next(reversed(sessions.values()), None) if sessions else None

    async def _event(
        self, kind: str, description: str, salience: float, camera: str, **data: Any
    ) -> None:
        event = AttentionEvent(
            kind=kind,
            description=description,
            salience=salience,
            source="perception",
            at=datetime.now(UTC),
            data={"device": camera, **data},
        )
        log.info("perception event", extra=kv(kind=kind, camera=camera))
        session = self._session_for(camera)
        if session is not None:
            await session.on_attention_event(event)

    # -- cameras the server pulls itself --------------------------------------------------

    def apply(self) -> None:
        """Pull frames from exactly the running server cameras with a perception_fps; glance
        and keep the timeline as the settings say."""
        self._apply_timeline()
        wanted: dict[str, float] = {}
        if self.ready:
            running = self.runtime.cameras.running_ids()
            for camera in self.config.cameras:
                if camera.enabled and camera.perception_fps > 0 and camera.id in running:
                    wanted[camera.id] = camera.perception_fps
        for camera_id in list(self._loops):
            fps, task = self._loops[camera_id]
            if wanted.get(camera_id) != fps or task.done():
                task.cancel()
                del self._loops[camera_id]
        for camera_id, fps in wanted.items():
            if camera_id not in self._loops:
                with contextlib.suppress(RuntimeError):
                    task = asyncio.get_running_loop().create_task(
                        self._pull(camera_id, fps), name=f"perception {camera_id}"
                    )
                    self._loops[camera_id] = (fps, task)

    def _apply_timeline(self) -> None:
        from companion.attention.engine import resolve_zone
        from companion.perception.timeline import WatchRule

        config = self.config
        self.timeline.min_switch_s = config.timeline.min_switch_s
        self.timeline.retention_days = config.timeline.retention_days
        self.timeline.zone = resolve_zone(config.attention.quiet_hours.timezone)
        self.timeline.rules = [WatchRule.from_dict(r.model_dump()) for r in config.timeline.rules]
        if config.timeline.enabled:
            with contextlib.suppress(OSError, Exception):
                self.timeline.open()
        glance = self.ready and config.perception.glance
        running = self._glance_task is not None and not self._glance_task.done()
        if glance and not running:
            with contextlib.suppress(RuntimeError):
                self._glance_task = asyncio.get_running_loop().create_task(
                    self._glance_loop(), name="glances"
                )
        elif not glance and running:
            assert self._glance_task is not None
            self._glance_task.cancel()
            self._glance_task = None

    # -- glances --------------------------------------------------------------------------

    def glance_interval(self) -> float:
        configured = self.config.perception.glance_s
        if configured:
            return configured
        return 1.0 if getattr(self.runtime.vlm, "name", "") == "builtin_vlm" else 10.0

    async def _glance_loop(self) -> None:
        while True:
            began = time.monotonic()
            cameras = [c for c, (_, at) in self.frames.items() if time.time() - at <= FRESH_FRAME_S]
            if cameras and self._vlm_asker() is not None:
                self._glance_turn += 1
                camera = sorted(cameras)[self._glance_turn % len(cameras)]
                try:
                    await self.glance(camera)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # a vision model error: try again next time
                    log.warning("glance failed", extra=kv(camera=camera, error=str(exc)))
            elapsed = time.monotonic() - began
            await asyncio.sleep(max(0.05, self.glance_interval() - elapsed))

    async def glance(self, camera: str, *, force: bool = False) -> Any:
        """One glance at ``camera``'s newest picture; None when skipped (same picture)."""
        ask = self._vlm_asker()
        frame = self.frames.get(camera)
        if ask is None or frame is None:
            return None
        jpeg, at = frame
        thumb = await asyncio.to_thread(jpeg_thumbnail, jpeg)
        last = self._glanced.get(camera)
        if not force and last is not None and float(np.abs(thumb - last).mean()) < SAME_PICTURE:
            return None
        self._glanced[camera] = thumb
        tracker = self.trackers.get(camera)
        marks = marks_for(tracker.active if tracker else [])
        side = self.config.vlm.max_image_side
        picture = await asyncio.to_thread(marked_picture, jpeg, marks, side)
        began = time.perf_counter()
        answer = str(
            await ask([picture], question(marks), max_tokens=self.config.perception.glance_tokens)
        )
        result = parse(answer, marks, camera, at)
        result.seconds = time.perf_counter() - began
        self.state.take(result)
        if not result.parsed:
            log.info("glance not understood", extra=kv(camera=camera, answer=answer[:80]))
        if self.config.timeline.enabled:
            for g in result.people:
                if g.person_id and g.name:
                    for fired in self.timeline.add(
                        g.person_id, g.name, g.role, g.activity, at, posture=g.posture
                    ):
                        await self._fired(fired, camera)
        return result

    async def _fired(self, fired: Fired, camera: str) -> None:
        episode = fired.episode
        await self._event(
            "long_activity",
            fired.description(self.timeline.zone),
            fired.rule.salience,
            camera,
            rule=fired.rule.id,
            person=episode.name,
            activity=episode.activity,
            minutes=round(episode.minutes),
        )

    async def sample_presence(self, now: float) -> None:
        """Presence samples for the timeline: away when out of view, present without glances."""
        glancing = self.config.perception.glance and self._vlm_asker() is not None
        for person in self.registry.people():
            here = self.present.get(person.id)
            camera = here.camera if here else ""
            if here is None:
                if person.role == "user" or person.id in self.timeline.current:
                    activity = "away"
                else:
                    continue
            elif glancing:
                continue
            else:
                activity = "present"
            for fired in self.timeline.add(person.id, person.name, person.role, activity, now):
                await self._fired(fired, camera)

    async def look(self, camera: str | None = None) -> dict[str, Any]:
        """A fresh look at a camera: its newest picture and, with a vision model, what it shows."""
        cameras = sorted(self.frames, key=lambda c: -self.frames[c][1])
        if camera is None:
            camera = cameras[0] if cameras else None
        if camera is None or camera not in self.frames:
            raise CompanionError("no picture from any camera yet", code="no_frame")
        jpeg, at = self.frames[camera]
        tracker = self.trackers.get(camera)
        marks = marks_for(tracker.active if tracker else [])
        out: dict[str, Any] = {
            "camera": camera,
            "at": at,
            "jpeg": jpeg,
            "people": [{"mark": m.letter, "who": m.label} for m in marks],
            "description": None,
        }
        ask = self._vlm_asker()
        if ask is not None:
            picture = await asyncio.to_thread(marked_picture, jpeg, marks)
            legend = "; ".join(f"[{m.letter}] {m.label}" for m in marks) or "nobody"
            text = str(await ask([picture], LOOK_QUESTION.format(legend=legend), max_tokens=200))
            out["description"] = text.strip()
            self.state.description[camera] = (time.time(), out["description"])
        return out

    async def _pull(self, camera_id: str, fps: float) -> None:
        step = 1.0 / fps
        last: bytes | None = None
        while True:
            began = time.monotonic()
            source = self.runtime.cameras.source(camera_id)
            if source is None:
                await asyncio.sleep(1.0)
                continue
            try:
                jpeg = await source.grab()
            except (CompanionError, OSError, TimeoutError):
                await asyncio.sleep(1.0)
                continue
            if jpeg is not last:
                last = jpeg
                await self.analyse(camera_id, jpeg, time.time())
            await asyncio.sleep(max(0.0, step - (time.monotonic() - began)))

    # -- who spoke ---------------------------------------------------------------------

    async def attribute(self, audio: np.ndarray, start: float, end: float) -> Speaker | None:
        """Who said the utterance heard from ``start`` to ``end`` (None with perception off)."""
        backend = self.backend
        if not self.ready or backend is None:
            return None
        people = self.config.people
        voice = voice_to = emb = None
        if self.registry.voice_people():
            try:
                emb = await backend.voice(audio)
            except CompanionError as exc:
                log.warning("voice embedding failed", extra=kv(error=str(exc)))
                emb = None
            if emb is not None:
                voice = self.registry.match_voice(emb, people.voice_threshold, 0.0)
                voice_to = self.registry.voice_similarities(emb)
        visible: list[Track] = []
        talking: Track | None = None
        for tracker in self.trackers.values():
            seen = tracker.visible_between(start, end)
            visible.extend(seen)
            moving, _ = talking_track(seen, start, end, tracker.fps)
            if moving is not None and talking is None:
                talking = moving
        around = {p.person_id: (p.name, p.role) for p in self.present.values()}
        speaker = attribute(
            duration_s=end - start,
            voice=voice,
            voice_to=voice_to,
            visible=visible,
            talking=talking,
            around=around,
            voice_margin=people.voice_margin,
        )
        if (
            emb is not None
            and speaker.person_id
            and speaker.how == "voice+lips"
            and end - start >= CANDIDATE_S
        ):
            # Voice, face and lips agree: a good new voice sample, kept (in memory only)
            # until someone adds it on the People page.
            queue = self.voice_candidates.setdefault(speaker.person_id, [])
            queue.append((emb, wav_bytes(audio)))
            del queue[:-MAX_CANDIDATES]
        log.info(
            "speaker",
            extra=kv(
                who=speaker.name or speaker.role,
                how=speaker.how,
                confidence=speaker.confidence,
                voice=None if voice is None else round(voice.score, 3),
                seconds=round(end - start, 2),
            ),
        )
        return speaker

    # -- enrolling ---------------------------------------------------------------------

    async def collect_faces(self, camera: str, seconds: float) -> np.ndarray:
        """Face samples of the one person looking at ``camera`` for ``seconds``.

        Raises CompanionError when nobody, or more than one person, was in view.
        """
        if not self.ready:
            raise CompanionError("perception is off or its engine is not running", code="off")
        began = time.time()
        if not self.has_loop(camera):
            with contextlib.suppress(CompanionError):  # a board: pictures in a row for a while
                await self.runtime.live_view(camera, seconds, 2.0)
        await asyncio.sleep(seconds)
        tracker = self.trackers.get(camera)
        tracks = [
            t
            for t in (tracker.tracks.values() if tracker else [])
            if t.last_t >= began and t.kind == "person" and t.samples
        ]
        if not tracks:
            raise CompanionError(
                f"no clear face on {camera} in {seconds:g} s: look at the camera, closer, "
                "with light on your face",
                code="no_face",
            )
        tracks.sort(key=lambda t: -len(t.samples))
        if len(tracks) > 1 and len(tracks[1].samples) >= 0.5 * len(tracks[0].samples):
            raise CompanionError(
                "more than one face was in view: only the person to add should be there",
                code="several_faces",
            )
        return np.vstack(tracks[0].samples)

    async def identify(self, jpeg: bytes) -> list[dict[str, Any]]:
        """Who is in one picture (the dashboard's "who is this")."""
        backend = self.backend
        if backend is None:
            raise CompanionError("the perception engine is not running", code="off")
        found = await backend.faces(jpeg)
        out = []
        for face in found.faces:
            match = self.matcher(face.embedding)
            out.append(
                {
                    "box": [round(v, 1) for v in face.box],
                    "person_id": match.person_id,
                    "name": match.name,
                    "role": match.role,
                    "score": round(match.score, 3),
                    "closest": match.closest,
                    "picture": self.registry.is_picture(face.embedding),
                }
            )
        return out

    def report(self) -> dict[str, Any]:
        ms = list(self.stats["ms"])
        return {
            "enabled": self.on,
            "ready": self.ready,
            "error": self.last_error,
            "analysed": self.stats["analysed"],
            "skipped": self.stats["skipped"],
            "errors": self.stats["errors"],
            "ms_per_frame": round(sum(ms) / len(ms), 1) if ms else None,
            "cameras": {
                cid: {"fps": round(t.fps, 1), "tracks": len(t.active), "pulled": cid in self._loops}
                for cid, t in self.trackers.items()
            },
            "pictures": self.registry.pictures(),
            "strangers": len(self.registry.strangers()),
            "glance": {
                "enabled": self.config.perception.glance,
                "running": self._glance_task is not None and not self._glance_task.done(),
                "interval_s": self.glance_interval(),
                "glances": self.state.glances,
                "unparsed": self.state.unparsed,
                "last_seconds": self.state.last_seconds,
            },
        }

    async def close(self) -> None:
        if self._glance_task is not None:
            self._glance_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._glance_task
            self._glance_task = None
        with contextlib.suppress(Exception):
            self.timeline.close()
        for _, task in self._loops.values():
            task.cancel()
        self._loops.clear()
        if self._housekeeping is not None:
            self._housekeeping.cancel()
            self._housekeeping = None
        for task in list(self._tasks):
            task.cancel()
        for task in list(self._tasks):
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        if self.backend is not None:
            with contextlib.suppress(Exception):
                await self.backend.aclose()
            self.backend = None
        with contextlib.suppress(OSError):
            self.registry.save_strangers(self.config.people.stranger_retention_h)
