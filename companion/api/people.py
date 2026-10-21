"""People in the dashboard and on the phone page (/v1/admin/people), behind the admin token.

GET lists the people (with how many face and voice samples each has), who is
here now and the state of the perception engine. Adding someone needs their
agreement (``consent``). Faces come from a camera ("look at the camera for
ten seconds") or from a face tracked right now; voices from recordings
uploaded in any common audio format (WAV, MP3, M4A, WebM, Ogg), split into
clips and checked to hold a single voice. Deleting a person deletes their
samples at once.
"""

from __future__ import annotations

import asyncio
import io
import logging
from typing import Any

import numpy as np
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from companion.api.auth import require_admin
from companion.core.errors import CompanionError
from companion.core.logging import kv
from companion.core.runtime import CompanionRuntime
from companion.perception.client import wav_bytes
from companion.perception.registry import Person, RegistryError

log = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/admin/people", dependencies=[Depends(require_admin)])

SAMPLE_RATE = 16000
CLIP_S = 6.0  # a recording is cut into clips of about this length
MIN_CLIP_S = 1.5
MAX_UPLOAD_BYTES = 30 * 1024 * 1024
SAME_VOICE = 0.45  # each clip at least this alike to the others' mean, or it is left out


def _runtime(request: Request) -> CompanionRuntime:
    return request.app.state.runtime


def _person(runtime: CompanionRuntime, person: Person) -> dict[str, Any]:
    here = runtime.perception.present.get(person.id)
    return {
        **person.public(),
        **runtime.perception.registry.counts(person.id),
        "here": here is not None,
        "camera": here.camera if here else None,
    }


def _found(runtime: CompanionRuntime, person_id: str) -> Person:
    person = runtime.perception.registry.get(person_id)
    if person is None:
        raise HTTPException(status_code=404, detail=f"no person {person_id!r}")
    return person


def _refused(exc: Exception) -> HTTPException:
    return HTTPException(status_code=422, detail=str(exc))


@router.get("")
async def people(request: Request) -> dict[str, Any]:
    runtime = _runtime(request)
    perception = runtime.perception
    engine = runtime.builtin.engines["perception"]
    backend = perception.backend
    return {
        "people": [_person(runtime, p) for p in perception.registry.people()],
        "here": perception.people_here(),
        "tracks": perception.tracks(),
        "perception": perception.report(),
        "engine": {
            "state": engine.status.state,
            "error": engine.status.error,
            "installed": engine.installed() is not None,
            "voice": getattr(backend, "voice_available", None),
            "voice_error": getattr(backend, "voice_error", None),
        },
        "cameras": sorted(
            {*runtime.cameras.running_ids(), *runtime.frames.keys(), *perception.trackers}
        ),
        "vlm": runtime.vlm is not None,
    }


class NewPerson(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    role: str = "acquaintance"
    notes: str = ""
    consent: bool = False  # the person agreed to be recognised


@router.post("", status_code=201)
async def add(request: Request, body: NewPerson) -> dict[str, Any]:
    if not body.consent:
        raise HTTPException(
            status_code=422, detail="ask the person first: they must agree to be recognised"
        )
    runtime = _runtime(request)
    try:
        person = runtime.perception.registry.add(
            body.name, body.role, notes=body.notes, consent=True
        )
    except RegistryError as exc:
        raise _refused(exc) from exc
    log.info("person added", extra=kv(person=person.id, role=person.role))
    return _person(runtime, person)


class Change(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=64)
    role: str | None = None
    notes: str | None = None


@router.patch("/{person_id}")
async def change(request: Request, person_id: str, body: Change) -> dict[str, Any]:
    runtime = _runtime(request)
    _found(runtime, person_id)
    try:
        person = runtime.perception.registry.update(
            person_id, name=body.name, role=body.role, notes=body.notes
        )
    except RegistryError as exc:
        raise _refused(exc) from exc
    return _person(runtime, person)


@router.delete("/{person_id}")
async def delete(request: Request, person_id: str) -> dict[str, Any]:
    runtime = _runtime(request)
    _found(runtime, person_id)
    runtime.perception.registry.delete(person_id)
    runtime.perception.present.pop(person_id, None)
    log.info("person deleted", extra=kv(person=person_id))
    return {"deleted": person_id}


class FaceCapture(BaseModel):
    camera: str | None = None  # the camera to look at (default: the only one)
    track_id: str | None = None  # or a face tracked right now
    seconds: float | None = Field(default=None, gt=0, le=60)


@router.post("/{person_id}/face")
async def add_face(request: Request, person_id: str, body: FaceCapture) -> dict[str, Any]:
    """Face samples from a camera ("look at it for ten seconds") or from a current track."""
    runtime = _runtime(request)
    perception = runtime.perception
    _found(runtime, person_id)
    if body.track_id:
        track = perception.find_track(body.track_id)
        if track is None or not track.samples:
            raise HTTPException(status_code=404, detail="that face is not in view any more")
        samples = np.vstack(track.samples)
    else:
        camera = body.camera
        if camera is None:
            cameras = sorted({*runtime.cameras.running_ids(), *runtime.frames.keys()})
            if len(cameras) != 1:
                raise HTTPException(status_code=422, detail="say which camera to use")
            camera = cameras[0]
        seconds = body.seconds or runtime.config.perception.enrol_seconds
        try:
            samples = await perception.collect_faces(camera, seconds)
        except CompanionError as exc:
            status = 409 if exc.code == "off" else 422
            raise HTTPException(status_code=status, detail=str(exc)) from exc
    total = perception.registry.add_faces(person_id, samples)
    log.info("face samples added", extra=kv(person=person_id, found=len(samples), kept=total))
    return {"found": len(samples), "face_samples": total}


@router.delete("/{person_id}/face")
async def clear_face(request: Request, person_id: str) -> dict[str, Any]:
    runtime = _runtime(request)
    _found(runtime, person_id)
    runtime.perception.registry.clear(person_id, "face")
    return {"face_samples": 0}


def decode_audio(data: bytes) -> np.ndarray:
    """Any audio file PyAV reads, as 16 kHz mono float32."""
    import av

    chunks: list[np.ndarray] = []
    with av.open(io.BytesIO(data)) as container:
        stream = container.streams.audio[0]
        resampler = av.AudioResampler(format="flt", layout="mono", rate=SAMPLE_RATE)
        for frame in container.decode(stream):
            chunks.extend(out.to_ndarray().reshape(-1) for out in resampler.resample(frame))
        chunks.extend(out.to_ndarray().reshape(-1) for out in resampler.resample(None))
    return np.concatenate(chunks).astype(np.float32) if chunks else np.zeros(0, np.float32)


def speech_clips(audio: np.ndarray) -> list[np.ndarray]:
    """Cut a recording into clips of about CLIP_S, leaving out silent stretches."""
    frame = SAMPLE_RATE // 50  # 20 ms
    n = len(audio) // frame
    if n == 0:
        return []
    energy = np.sqrt((audio[: n * frame].reshape(n, frame) ** 2).mean(axis=1))
    loud = energy > max(0.01, 0.1 * float(np.percentile(energy, 95)))
    voiced = np.concatenate([audio[i * frame : (i + 1) * frame] for i in range(n) if loud[i]])
    step = int(CLIP_S * SAMPLE_RATE)
    clips = [voiced[i : i + step] for i in range(0, len(voiced), step)]
    return [c for c in clips if len(c) >= MIN_CLIP_S * SAMPLE_RATE]


@router.post("/{person_id}/voice")
async def add_voice(request: Request, person_id: str) -> dict[str, Any]:
    """A recording of the person speaking alone (30 to 60 s is good), any common format."""
    runtime = _runtime(request)
    _found(runtime, person_id)
    backend = runtime.perception.backend
    if backend is None:
        raise HTTPException(status_code=409, detail="the perception engine is not running")
    data = await request.body()
    if not data or len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=422, detail="send one audio file of up to 30 MB")
    try:
        audio = await asyncio.to_thread(decode_audio, data)
    except Exception as exc:
        raise HTTPException(status_code=422, detail=f"cannot read that audio: {exc}") from exc
    clips = speech_clips(audio)
    if not clips:
        raise HTTPException(
            status_code=422, detail="no speech found: record at least a few seconds"
        )
    embeddings = []
    try:
        for clip in clips:
            emb = await backend.voice(clip)
            if emb is None:
                raise HTTPException(
                    status_code=409,
                    detail="the voice model is not available: "
                    + (getattr(backend, "voice_error", None) or "see the People page"),
                )
            embeddings.append(emb)
    except CompanionError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    rows = np.vstack(embeddings)
    mean = rows.mean(axis=0)
    mean /= np.linalg.norm(mean) + 1e-12
    alike = rows @ mean
    keep = [i for i, s in enumerate(alike) if len(clips) < 3 or s >= SAME_VOICE]
    registry = runtime.perception.registry
    total = 0
    for i in keep:
        total = registry.add_voice(person_id, rows[i], wav_bytes(clips[i]))
    log.info("voice samples added", extra=kv(person=person_id, clips=len(clips), kept=len(keep)))
    return {
        "seconds": round(len(audio) / SAMPLE_RATE, 1),
        "clips": len(clips),
        "kept": len(keep),
        "left_out": len(clips) - len(keep),
        "voice_samples": total or registry.counts(person_id)["voice_samples"],
    }


@router.delete("/{person_id}/voice")
async def clear_voice(request: Request, person_id: str) -> dict[str, Any]:
    runtime = _runtime(request)
    _found(runtime, person_id)
    runtime.perception.registry.clear(person_id, "voice")
    return {"voice_samples": 0}


class Promote(BaseModel):
    track_id: str
    name: str = Field(min_length=1, max_length=64)
    role: str = "acquaintance"
    consent: bool = False


@router.post("/promote", status_code=201)
async def promote(request: Request, body: Promote) -> dict[str, Any]:
    """Add the stranger in view (a face tracked now) as a person."""
    if not body.consent:
        raise HTTPException(
            status_code=422, detail="ask the person first: they must agree to be recognised"
        )
    runtime = _runtime(request)
    track = runtime.perception.find_track(body.track_id)
    if track is None or not track.samples:
        raise HTTPException(status_code=404, detail="that face is not in view any more")
    registry = runtime.perception.registry
    try:
        person = registry.add(body.name, body.role, consent=True)
    except RegistryError as exc:
        raise _refused(exc) from exc
    registry.add_faces(person.id, np.vstack(track.samples))
    log.info("stranger added as a person", extra=kv(person=person.id, track=track.id))
    return _person(runtime, person)


@router.post("/identify")
async def identify(request: Request, camera: str | None = None) -> dict[str, Any]:
    """Who is in a picture: the body (a JPEG), or the newest picture of ``camera``."""
    runtime = _runtime(request)
    data = await request.body()
    if not data:
        if camera is None:
            raise HTTPException(status_code=422, detail="send a picture or name a camera")
        frame = runtime.frames.get(camera)
        jpeg = runtime.perception.frames.get(camera, (None, 0))[0] or (
            frame.data if frame else None
        )
        if jpeg is None:
            raise HTTPException(status_code=404, detail=f"no picture from {camera} yet")
        data = jpeg
    try:
        faces = await runtime.perception.identify(data)
    except CompanionError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"faces": faces}
