"""Admin API for looking after devices remotely (/v1/admin/devices/{id}/...).

Update the edge client, restart it or the whole board, change its Wi-Fi
networks, pair a Bluetooth speaker, put it on Tailscale, and test its speaker
and microphone. All of it travels over the device's own connection (protocol
1.2, Bluetooth and Tailscale 1.3), so it works wherever the device is.
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
NEWER_DEVICE = "the device did not answer: update it first (companion-edge 1.3 or newer does)"


def _runtime(request: Request) -> CompanionRuntime:
    return request.app.state.runtime


def _error(exc: Exception, old: str = OLD_DEVICE) -> HTTPException:
    if isinstance(exc, TimeoutError):
        return HTTPException(status_code=504, detail=old)
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


Address = Field(pattern=r"^[0-9A-Fa-f]{2}([:-][0-9A-Fa-f]{2}){5}$")


class ScanBody(BaseModel):
    seconds: float = Field(default=10.0, ge=3, le=30)


class BluetoothDeviceBody(BaseModel):
    address: str = Address


@router.get("/{device_id}/bluetooth")
async def bluetooth(request: Request, device_id: str) -> dict[str, Any]:
    try:
        return _refused(await _runtime(request).device_bluetooth(device_id, "state"))
    except (TimeoutError, CompanionError) as exc:
        raise _error(exc, NEWER_DEVICE) from exc


@router.post("/{device_id}/bluetooth/scan")
async def bluetooth_scan(request: Request, device_id: str, body: ScanBody) -> dict[str, Any]:
    """What is in reach: a speaker shows only while it is in pairing mode."""
    try:
        return _refused(
            await _runtime(request).device_bluetooth(device_id, "scan", seconds=body.seconds)
        )
    except (TimeoutError, CompanionError) as exc:
        raise _error(exc, NEWER_DEVICE) from exc


@router.post("/{device_id}/bluetooth/pair")
async def bluetooth_pair(
    request: Request, device_id: str, body: BluetoothDeviceBody
) -> dict[str, Any]:
    """Pair, trust and connect a speaker; the device plays through it from now on."""
    try:
        return _refused(
            await _runtime(request).device_bluetooth(device_id, "pair", address=body.address)
        )
    except (TimeoutError, CompanionError) as exc:
        raise _error(exc, NEWER_DEVICE) from exc


@router.post("/{device_id}/bluetooth/remove")
async def bluetooth_remove(
    request: Request, device_id: str, body: BluetoothDeviceBody
) -> dict[str, Any]:
    """Forget a device; for the speaker, the device goes back to its output from before."""
    try:
        return _refused(
            await _runtime(request).device_bluetooth(device_id, "remove", address=body.address)
        )
    except (TimeoutError, CompanionError) as exc:
        raise _error(exc, NEWER_DEVICE) from exc


class TailscaleBody(BaseModel):
    # From the Tailscale admin console (Settings, Keys); empty: a login link instead.
    auth_key: str | None = Field(default=None, pattern=r"^tskey-[A-Za-z0-9_-]+$")


@router.get("/{device_id}/tailscale")
async def tailscale_state(request: Request, device_id: str) -> dict[str, Any]:
    try:
        return _refused(await _runtime(request).device_tailscale(device_id, "state"))
    except (TimeoutError, CompanionError) as exc:
        raise _error(exc, NEWER_DEVICE) from exc


@router.post("/{device_id}/tailscale")
async def tailscale_up(request: Request, device_id: str, body: TailscaleBody) -> dict[str, Any]:
    """Put the device on Tailscale (installing it first if it has none)."""
    from companion.tailscale import PackageError

    try:
        result = await _runtime(request).device_tailscale(
            device_id, "up", auth_key=body.auth_key or None
        )
    except PackageError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except (TimeoutError, CompanionError) as exc:
        raise _error(exc, NEWER_DEVICE) from exc
    return _refused(result)


@router.post("/{device_id}/tailscale/logout")
async def tailscale_logout(request: Request, device_id: str) -> dict[str, Any]:
    try:
        return _refused(await _runtime(request).device_tailscale(device_id, "logout"))
    except (TimeoutError, CompanionError) as exc:
        raise _error(exc, NEWER_DEVICE) from exc


class SpeakerBody(BaseModel):
    text: str = Field(default="This is a speaker test.", min_length=1, max_length=300)
    confirm_s: float | None = Field(default=None, gt=0, le=60)
    """Wait this long for the device to say it played the test (the pipeline self-test)."""


@router.post("/{device_id}/speaker-test")
async def speaker_test(request: Request, device_id: str, body: SpeakerBody) -> dict[str, Any]:
    try:
        result = await _runtime(request).speaker_test(
            device_id, body.text, confirm_s=body.confirm_s
        )
    except CompanionError as exc:
        raise _error(exc) from exc
    return {"played": device_id, **result}


class VoiceBody(BaseModel):
    seconds: float = Field(default=15.0, gt=0, le=60)


@router.post("/{device_id}/voice-test")
async def voice_test(request: Request, device_id: str, body: VoiceBody) -> dict[str, Any]:
    """The pipeline self-test of voice input: the device's next sentence, recognised but not
    answered, with what the microphone, speech detection and recognition each saw."""
    try:
        return await _runtime(request).voice_test(device_id, body.seconds)
    except CompanionError as exc:
        raise _error(exc) from exc


class MicBody(BaseModel):
    seconds: float = Field(default=5.0, gt=0, le=30)


@router.post("/{device_id}/mic-test")
async def mic_test(request: Request, device_id: str, body: MicBody) -> dict[str, Any]:
    try:
        recording = await _runtime(request).mic_test(device_id, body.seconds)
    except CompanionError as exc:
        raise _error(exc) from exc
    return recording.__dict__
