"""Admin API for looking at devices and conversations (/v1/admin/...).

- device logs (``device.log``) and diagnostics (``device.diagnostics``);
- the newest frame of each camera, and asking it for a fresh one;
- the conversation and the recordings of what was heard, when the settings
  ``server.dashboard_conversation`` / ``stt.keep_audio`` allow it;
- images kept by ``vision.store_images``.
"""

from __future__ import annotations

import re
import time
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import FileResponse

from companion.api.auth import require_admin
from companion.core.errors import CompanionError
from companion.core.runtime import CompanionRuntime

router = APIRouter(prefix="/v1/admin", dependencies=[Depends(require_admin)])

_IMAGE = re.compile(r"^\d{4}-\d{2}-\d{2}/[A-Za-z0-9_-]+\.jpg$")


def _runtime(request: Request) -> CompanionRuntime:
    return request.app.state.runtime


def _not_connected(exc: CompanionError) -> HTTPException:
    return HTTPException(status_code=404 if exc.code == "not_connected" else 409, detail=str(exc))


# --- device logs and diagnostics ------------------------------------------------


@router.get("/device-logs")
async def device_logs_index(request: Request) -> dict[str, Any]:
    """Devices with logs on this server (connected now or before)."""
    return {"devices": _runtime(request).device_logs.known()}


@router.get("/devices/{device_id}/logs")
async def device_log(
    request: Request, device_id: str, after: int = 0, limit: int = 500
) -> dict[str, Any]:
    runtime = _runtime(request)
    lines = runtime.device_logs.recent(device_id, after=after, limit=min(limit, 1000))
    return {
        "device_id": device_id,
        "connected": runtime.find_device(device_id) is not None,
        "lines": lines,
        "file": runtime.device_logs.file(device_id) is not None,
    }


@router.get("/devices/{device_id}/logs/file")
async def device_log_file(request: Request, device_id: str) -> FileResponse:
    path = _runtime(request).device_logs.file(device_id)
    if path is None:
        raise HTTPException(status_code=404, detail="no log kept for this device")
    return FileResponse(path, media_type="text/plain; charset=utf-8", filename=f"{device_id}.log")


@router.post("/devices/{device_id}/diagnostics")
async def device_diagnostics(request: Request, device_id: str) -> dict[str, Any]:
    try:
        sections = await _runtime(request).device_diagnostics(device_id)
    except TimeoutError as exc:
        raise HTTPException(
            status_code=504,
            detail="the device did not answer (companion-edge 1.2 or newer answers)",
        ) from exc
    except CompanionError as exc:
        raise _not_connected(exc) from exc
    return {"device_id": device_id, "sections": sections, "at": time.time()}


# --- cameras ----------------------------------------------------------------------


@router.get("/devices/{device_id}/frame")
async def device_frame(request: Request, device_id: str) -> Response:
    frame = _runtime(request).frames.get(device_id)
    if frame is None:
        raise HTTPException(status_code=404, detail="no frame from this device yet")
    return Response(
        frame.data,
        media_type=frame.mime,
        headers={
            "Cache-Control": "no-store",
            "X-Frame-Reason": frame.reason,
            "X-Frame-Age": f"{time.time() - frame.received:.1f}",
        },
    )


@router.get("/frames")
async def frames(request: Request) -> dict[str, Any]:
    """Devices that sent camera frames, newest first."""
    now = time.time()
    items = sorted(_runtime(request).frames.items(), key=lambda kv: -kv[1].received)
    return {
        "frames": [
            {"device_id": d, "age_s": round(now - f.received, 1), "reason": f.reason}
            for d, f in items
        ]
    }


@router.post("/devices/{device_id}/capture", status_code=202)
async def capture(request: Request, device_id: str) -> dict[str, Any]:
    try:
        await _runtime(request).capture(device_id)
    except CompanionError as exc:
        raise _not_connected(exc) from exc
    return {"requested": device_id}


# --- conversation and recordings ----------------------------------------------------


@router.get("/conversation")
async def conversation(request: Request, after: int = 0) -> dict[str, Any]:
    runtime = _runtime(request)
    return {
        "enabled": runtime.config.server.dashboard_conversation,
        "lines": runtime.feed.conversation(after=after),
    }


@router.get("/recordings")
async def recordings(request: Request, limit: int = 100) -> dict[str, Any]:
    runtime = _runtime(request)
    return {
        "enabled": runtime.config.stt.keep_audio,
        "days": runtime.config.stt.keep_audio_days,
        "recordings": [r.__dict__ for r in runtime.recordings.list(limit=min(limit, 500))],
    }


@router.get("/recordings/{recording_id}/audio")
async def recording_audio(request: Request, recording_id: str) -> FileResponse:
    path = _runtime(request).recordings.audio_path(recording_id)
    if path is None:
        raise HTTPException(status_code=404, detail="no such recording")
    return FileResponse(path, media_type="audio/wav")


# --- stored camera images -----------------------------------------------------------


@router.get("/vision/images")
async def stored_images(request: Request, limit: int = 60) -> dict[str, Any]:
    archive = _runtime(request).image_archive
    if archive is None or not archive.root.is_dir():
        return {"enabled": archive is not None, "images": []}
    paths = sorted(archive.root.glob("*/*.jpg"), key=lambda p: p.stat().st_mtime, reverse=True)
    return {
        "enabled": True,
        "images": [
            {"path": f"{p.parent.name}/{p.name}", "t": p.stat().st_mtime}
            for p in paths[: min(limit, 500)]
        ],
    }


@router.get("/vision/images/{day}/{name}")
async def stored_image(request: Request, day: str, name: str) -> FileResponse:
    archive = _runtime(request).image_archive
    rel = f"{day}/{name}"
    if archive is None or not _IMAGE.match(rel) or not (archive.root / rel).is_file():
        raise HTTPException(status_code=404, detail="no such image")
    return FileResponse(archive.root / rel, media_type="image/jpeg")
