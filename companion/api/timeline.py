"""What the companion sees going on, for the dashboard (/v1/admin/perception, /v1/admin/timeline).

GET /perception/state: who is doing what (from glances), the scene per camera,
the vision model in use. POST /perception/look: a fresh look at a camera (its
newest picture, described by the vision model). PUT /perception/settings
switches glances and the timeline (saved like any setting). GET /timeline:
episodes of the last hours, per person, with the rules and the watches the
agent added; DELETE /timeline/watches/{id} removes such a watch.
"""

from __future__ import annotations

import base64
import time
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from companion.api.admin import _require_source, _runtime, _save
from companion.api.auth import require_admin
from companion.core.errors import CompanionError

router = APIRouter(prefix="/v1/admin", dependencies=[Depends(require_admin)])


@router.get("/perception/state")
async def state(request: Request) -> dict[str, Any]:
    runtime = _runtime(request)
    perception = runtime.perception
    return {
        "perception": perception.report(),
        "state": perception.state.to_payload(),
        "here": perception.people_here(),
        "vlm": getattr(runtime.vlm, "name", None),
        "builtin_vlm": runtime.uses_builtin("vlm"),
        "timeline_enabled": runtime.config.timeline.enabled,
        "cameras": {c: round(time.time() - at, 1) for c, (_, at) in perception.frames.items()},
    }


@router.post("/perception/look")
async def look(request: Request, camera: str | None = None) -> dict[str, Any]:
    """The newest picture of a camera (as base64 JPEG) and, with a vision model, what it shows."""
    try:
        seen = await _runtime(request).perception.look(camera)
    except CompanionError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    jpeg = seen.pop("jpeg")
    return {**seen, "jpeg_base64": base64.b64encode(jpeg).decode()}


class PerceptionSettings(BaseModel):
    glance: bool | None = None
    timeline: bool | None = None


@router.put("/perception/settings")
async def settings(request: Request, body: PerceptionSettings) -> dict[str, Any]:
    """Switch glances (perception.glance) and the timeline (timeline.enabled); applied at once."""
    source = _require_source(request)
    desired = source.saved().model_dump(mode="json")
    if body.glance is not None:
        desired["perception"]["glance"] = body.glance
    if body.timeline is not None:
        desired["timeline"]["enabled"] = body.timeline
    return await _save(_runtime(request), source, desired)


@router.get("/timeline")
async def timeline(
    request: Request, hours: float = 24.0, person: str | None = None
) -> dict[str, Any]:
    runtime = _runtime(request)
    perception = runtime.perception
    line = perception.timeline
    minutes = max(1.0, min(float(hours), 24 * 7)) * 60
    return {
        "enabled": runtime.config.timeline.enabled,
        "glance": runtime.config.perception.glance,
        "now": time.time(),
        "episodes": [e.to_payload() for e in line.episodes(minutes, person)],
        "rules": [r.to_dict() for r in line.rules],
        "watches": [w.to_dict() for w in line.watches],
    }


@router.delete("/timeline/watches/{watch_id}")
async def remove_watch(request: Request, watch_id: str) -> dict[str, Any]:
    if not _runtime(request).perception.timeline.remove_watch(watch_id):
        raise HTTPException(status_code=404, detail=f"no watch {watch_id!r}")
    return {"removed": watch_id}
