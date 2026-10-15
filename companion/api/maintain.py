"""Admin API for looking after devices remotely (/v1/admin/devices/{id}/...).

Update the edge client, restart it or the whole board, change its Wi-Fi
networks, and test its speaker and microphone. All of it travels over the
device's own connection (protocol 1.2), so it works wherever the device is.
"""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from companion.api.auth import require_admin
from companion.core.errors import CompanionError
from companion.core.runtime import CompanionRuntime

router = APIRouter(prefix="/v1/admin/devices", dependencies=[Depends(require_admin)])

OLD_DEVICE = "the device did not answer (companion-edge 1.2 or newer does)"


def _runtime(request: Request) -> CompanionRuntime:
    return request.app.state.runtime


def _error(exc: Exception) -> HTTPException:
    if isinstance(exc, TimeoutError):
        return HTTPException(status_code=504, detail=OLD_DEVICE)
    if isinstance(exc, CompanionError):
        return HTTPException(
            status_code=404 if exc.code == "not_connected" else 409, detail=str(exc)
        )
    return HTTPException(status_code=422, detail=str(exc))


def _refused(result: dict[str, Any]) -> dict[str, Any]:
    if not result.get("ok"):
        raise HTTPException(status_code=422, detail=str(result.get("error") or "refused"))
    return result


@router.post("/{device_id}/update")
async def update(request: Request, device_id: str) -> dict[str, Any]:
    """Install this server's edge client on the device; it restarts with it."""
    try:
        result = await _runtime(request).update_device(device_id)
    except (TimeoutError, CompanionError) as exc:
        raise _error(exc) from exc
    return _refused(result)


class RestartBody(BaseModel):
    what: Literal["client", "board"] = "client"


@router.post("/{device_id}/restart")
async def restart(request: Request, device_id: str, body: RestartBody) -> dict[str, Any]:
    try:
        return _refused(await _runtime(request).restart_device(device_id, body.what))
    except (TimeoutError, CompanionError) as exc:
        raise _error(exc) from exc


class WifiNetworkBody(BaseModel):
    ssid: str = Field(min_length=1, max_length=32)
    password: str | None = None


class WifiBody(BaseModel):
    add: list[WifiNetworkBody] = Field(default_factory=list)
    remove: list[str] = Field(default_factory=list)


@router.get("/{device_id}/wifi")
async def wifi(request: Request, device_id: str) -> dict[str, Any]:
    try:
        return _refused(await _runtime(request).device_wifi(device_id, [], []))
    except (TimeoutError, CompanionError) as exc:
        raise _error(exc) from exc


@router.put("/{device_id}/wifi")
async def change_wifi(request: Request, device_id: str, body: WifiBody) -> dict[str, Any]:
    from companion.edge_install import InstallError

    try:
        result = await _runtime(request).device_wifi(
            device_id, [n.model_dump() for n in body.add], body.remove
        )
    except InstallError as exc:  # a password of the wrong length
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except (TimeoutError, CompanionError) as exc:
        raise _error(exc) from exc
    return _refused(result)


class SpeakerBody(BaseModel):
    text: str = Field(default="This is a speaker test.", min_length=1, max_length=300)


@router.post("/{device_id}/speaker-test")
async def speaker_test(request: Request, device_id: str, body: SpeakerBody) -> dict[str, Any]:
    try:
        await _runtime(request).speaker_test(device_id, body.text)
    except CompanionError as exc:
        raise _error(exc) from exc
    return {"played": device_id}


class MicBody(BaseModel):
    seconds: float = Field(default=5.0, gt=0, le=30)


@router.post("/{device_id}/mic-test")
async def mic_test(request: Request, device_id: str, body: MicBody) -> dict[str, Any]:
    try:
        recording = await _runtime(request).mic_test(device_id, body.seconds)
    except CompanionError as exc:
        raise _error(exc) from exc
    return recording.__dict__
