"""The read-only viewer for phones on the tailnet (/v1/view/*, page at /view/).

With ``server.viewer.enabled`` on, devices on this server's Tailscale network
(and this machine) can see the conversation and the connected devices, with
what their cameras see when ``server.viewer.cameras`` is on. Nothing here
changes anything, except asking a camera for live pictures for a while, as the
dashboard's live view does.
"""

from __future__ import annotations

import time
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field

from companion.api.auth import from_tailnet, from_this_machine
from companion.core.errors import CompanionError
from companion.core.runtime import CompanionRuntime

LINE_FIELDS = (
    "id",
    "t",
    "session_id",
    "role",
    "text",
    "device_id",
    "typed",
    "cancelled",
    "speaker",
)


def _runtime(request: Request) -> CompanionRuntime:
    return request.app.state.runtime


async def require_viewer(request: Request) -> None:
    if not _runtime(request).config.server.viewer.enabled:
        raise HTTPException(
            status_code=404,
            detail="the viewer is off: turn on server.viewer.enabled under Configuration",
        )
    if not (from_tailnet(request) or from_this_machine(request)):
        raise HTTPException(
            status_code=403,
            detail="only devices on this server's tailnet may open the viewer, at the server's "
            "Tailscale address (100.x)",
        )


router = APIRouter(prefix="/v1/view", dependencies=[Depends(require_viewer)])


def _cameras(request: Request) -> None:
    if not _runtime(request).config.server.viewer.cameras:
        raise HTTPException(status_code=404, detail="cameras are not shown (server.viewer.cameras)")


@router.get("/state")
async def state(request: Request) -> dict[str, Any]:
    """The conversations going on and the devices in them, with their newest pictures."""
    runtime = _runtime(request)
    cameras = runtime.config.server.viewer.cameras
    now = time.time()
    frames = dict(runtime.frames.items()) if cameras else {}
    devices = []
    for session in runtime.sessions.values():
        for device in session.devices:
            report = device.report()
            frame = frames.pop(device.device_id, None) if device.device_id else None
            devices.append(
                {
                    "device_id": report.get("device_id"),
                    "roles": report.get("roles", []),
                    "session_id": session.session_id,
                    "connected_s": report.get("connected_s"),
                    "status": report.get("status") or {},
                    "frame_age_s": round(now - frame.received, 1) if frame else None,
                }
            )
    # Pictures from devices that are not connected now (the last they sent).
    for device_id, frame in frames.items():
        devices.append(
            {
                "device_id": device_id,
                "roles": ["camera"],
                "session_id": None,
                "connected_s": None,
                "status": {},
                "frame_age_s": round(now - frame.received, 1),
            }
        )
    return {
        "cameras": cameras,
        "conversation": runtime.feed.keep_said(),
        "sessions": [
            {"session_id": s.session_id, "state": s.state.value} for s in runtime.sessions.values()
        ],
        "devices": devices,
    }


@router.get("/conversation")
async def conversation(request: Request, after: int = 0) -> dict[str, Any]:
    lines = _runtime(request).feed.conversation(after=after)
    return {"lines": [{k: line.get(k) for k in LINE_FIELDS} for line in lines]}


@router.get("/devices/{device_id}/frame")
async def frame(request: Request, device_id: str) -> Response:
    _cameras(request)
    found = _runtime(request).frames.get(device_id)
    if found is None:
        raise HTTPException(status_code=404, detail="no picture from this device yet")
    return Response(
        found.data,
        media_type=found.mime,
        headers={"Cache-Control": "no-store", "X-Frame-Time": f"{found.received:.3f}"},
    )


class LiveBody(BaseModel):
    seconds: float = Field(default=15.0, ge=0, le=60)  # send again to keep it going


@router.post("/devices/{device_id}/live", status_code=202)
async def live(request: Request, device_id: str, body: LiveBody) -> dict[str, Any]:
    """Pictures in a row while someone watches, about one a second."""
    _cameras(request)
    try:
        await _runtime(request).live_view(device_id, body.seconds, 1.0)
    except CompanionError as exc:
        status = 404 if exc.code == "not_connected" else 409
        raise HTTPException(status_code=status, detail=str(exc)) from exc
    return {"live": device_id, "seconds": body.seconds}
