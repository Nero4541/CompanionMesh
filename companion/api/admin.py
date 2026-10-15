"""Admin API behind the dashboard (/v1/admin/*).

Guarded by :func:`companion.api.auth.require_admin`, never by the device
token. Secrets are reported only as set/unset, by environment-variable name.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import re
import tomllib
from collections.abc import Callable
from pathlib import Path
from typing import Annotated, Any

from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
    Query,
    Request,
    Response,
    WebSocket,
    WebSocketDisconnect,
)
from pydantic import BaseModel, ValidationError

from companion import __version__
from companion.api.auth import admin_refusal, require_admin
from companion.core.config import (
    CameraConfig,
    CompanionConfig,
    ConfigError,
    ConfigSource,
    changed_settings,
    needs_restart,
    secret_status,
    validation_errors,
)
from companion.core.config_edit import ConfigEditError, save_settings
from companion.core.errors import CompanionError
from companion.core.logging import kv
from companion.core.persona import (
    Persona,
    delete_persona,
    is_builtin,
    list_personas,
    load_persona,
    persona_path,
    save_persona,
    valid_name,
)
from companion.core.runtime import CompanionRuntime
from companion.devices.manager import probe as probe_camera

log = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/admin", dependencies=[Depends(require_admin)])

# Time for the restart response to reach the browser before the server stops.
RESTART_DELAY_S = 0.5


def _runtime(request: Request) -> CompanionRuntime:
    return request.app.state.runtime


def _source(request: Request) -> ConfigSource | None:
    return getattr(request.app.state, "config_source", None)


def setting_paths(model: type[BaseModel], prefix: str = "") -> list[str]:
    """Dotted paths of every leaf setting, in declaration order."""
    paths: list[str] = []
    for name, field in model.model_fields.items():
        path = f"{prefix}{name}"
        annotation = field.annotation
        if isinstance(annotation, type) and issubclass(annotation, BaseModel):
            paths.extend(setting_paths(annotation, f"{path}."))
        else:
            paths.append(path)
    return paths


def _flatten(data: dict[str, Any], prefix: str = "") -> list[str]:
    paths: list[str] = []
    for key, value in data.items():
        if isinstance(value, dict) and value:
            paths.extend(_flatten(value, f"{prefix}{key}."))
        else:
            paths.append(f"{prefix}{key}")
    return paths


def _pending(runtime: CompanionRuntime, source: ConfigSource) -> list[str]:
    """Saved settings the running server does not use yet."""
    try:
        next_config = source.load()
    except ConfigError:
        return []
    return changed_settings(runtime.config.public_view(), next_config.public_view())


@router.get("/config")
async def config(request: Request) -> dict[str, Any]:
    runtime = _runtime(request)
    source = _source(request)
    out: dict[str, Any] = {
        "version": __version__,
        "running": runtime.config.public_view(),
        "schema": CompanionConfig.model_json_schema(),
        "restart_required": {p: needs_restart(p) for p in setting_paths(CompanionConfig)},
        "editable": False,
        "saved": None,
        "pending_restart": [],
        "command_line": [],
        "files": None,
        "error": None,
        "can_restart": getattr(request.app.state, "request_restart", None) is not None,
    }
    if source is not None:
        out["files"] = {
            "config": str(source.path),
            "local": str(source.local_path),
            "local_exists": source.local_path.is_file(),
        }
        out["command_line"] = _flatten(source.overrides)
        try:
            out["saved"] = source.saved().model_dump(mode="json")
            out["editable"] = source.path.is_file()
            out["pending_restart"] = _pending(runtime, source)
        except ConfigError as exc:
            out["error"] = str(exc)
    out["secrets"] = secret_status(out["saved"] or out["running"])
    return out


class SaveRequest(BaseModel):
    # Every setting, as GET /config returns under "saved".
    config: dict[str, Any]


def _require_source(request: Request) -> ConfigSource:
    source = _source(request)
    if source is None:
        raise HTTPException(
            status_code=501, detail="this server was not started from a config file"
        )
    return source


def _unprocessable(message: str, path: str = "") -> HTTPException:
    return HTTPException(status_code=422, detail={"errors": [{"path": path, "message": message}]})


async def _save(
    runtime: CompanionRuntime, source: ConfigSource, desired: dict[str, Any]
) -> dict[str, Any]:
    """Save settings, apply the live ones, report what waits for a restart."""
    try:
        written = save_settings(source, desired)
        next_config = source.load()
    except ConfigEditError as exc:
        raise HTTPException(status_code=422, detail={"errors": exc.errors}) from exc
    except ConfigError as exc:
        raise _unprocessable(str(exc)) from exc
    applied, _ = runtime.reconfigure(next_config)
    pending = [p for p in _pending(runtime, source) if needs_restart(p)]
    await runtime.notice("settings_saved", {"applied": applied, "pending_restart": pending})
    log.info(
        "settings saved", extra=kv(file=str(written), applied=len(applied), pending=len(pending))
    )
    return {"file": str(written), "applied": applied, "pending_restart": pending}


@router.put("/config")
async def save_config(request: Request, body: SaveRequest) -> dict[str, Any]:
    return await _save(_runtime(request), _require_source(request), body.config)


# --- personas ---------------------------------------------------------------


def _persona_summary(runtime: CompanionRuntime, name: str) -> dict[str, Any]:
    out: dict[str, Any] = {
        "name": name,
        "builtin": is_builtin(name),
        "active": name == runtime.config.agent.persona,
        "display_name": None,
        "language": None,
        "error": None,
    }
    try:
        persona = load_persona(runtime.config.personas_dir, name)
        out["display_name"] = persona.display_name
        out["language"] = persona.language
    except ConfigError as exc:
        out["error"] = str(exc)
    return out


@router.get("/personas")
async def personas(request: Request) -> dict[str, Any]:
    runtime = _runtime(request)
    names = list_personas(runtime.config.personas_dir)
    return {
        "directory": str(runtime.config.personas_dir),
        "active": runtime.config.agent.persona,
        "can_activate": _source(request) is not None,
        "personas": [_persona_summary(runtime, n) for n in names],
    }


@router.get("/personas/{name}")
async def persona(request: Request, name: str) -> dict[str, Any]:
    runtime = _runtime(request)
    try:
        loaded = load_persona(runtime.config.personas_dir, name)
    except ConfigError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {
        **_persona_summary(runtime, name),
        "persona": loaded.model_dump(exclude={"name"}),
    }


class PersonaBody(BaseModel):
    display_name: str | None = None
    language: str | None = None
    voice: str | None = None
    system_prompt: str


@router.put("/personas/{name}")
async def save_persona_route(
    request: Request, name: str, body: PersonaBody, create: bool = False
) -> dict[str, Any]:
    """Create (``?create=true``, refused if it exists) or replace a persona."""
    runtime = _runtime(request)
    directory = runtime.config.personas_dir
    if not valid_name(name):
        raise _unprocessable("use letters, digits, - and _ (up to 64)", "name")
    if is_builtin(name):
        raise HTTPException(
            status_code=409, detail=f"{name!r} ships with the project; save a copy instead"
        )
    exists = persona_path(directory, name).is_file()
    if create and (exists or name in list_personas(directory)):
        raise HTTPException(status_code=409, detail=f"a persona named {name!r} already exists")
    if not create and not exists:
        raise HTTPException(status_code=404, detail=f"persona not found: {name}")
    if not body.system_prompt.strip():
        raise _unprocessable("must not be empty", "system_prompt")
    saved = Persona(name=name, **body.model_dump())
    try:
        path = save_persona(directory, saved)
    except ConfigError as exc:
        raise _unprocessable(str(exc)) from exc
    applied = name == runtime.config.agent.persona
    if applied:
        runtime.persona = saved
    log.info("persona saved", extra=kv(persona=name, active=applied))
    return {"file": str(path), "applied": applied}


@router.delete("/personas/{name}")
async def delete_persona_route(request: Request, name: str) -> dict[str, Any]:
    runtime = _runtime(request)
    if name == runtime.config.agent.persona:
        raise HTTPException(
            status_code=409, detail="this persona is in use; switch to another first"
        )
    if is_builtin(name):
        raise HTTPException(status_code=409, detail=f"{name!r} ships with the project")
    source = _source(request)
    if source is not None:
        try:
            if source.saved().agent.persona == name:
                raise HTTPException(
                    status_code=409, detail="the saved settings use this persona after a restart"
                )
        except ConfigError:
            pass
    try:
        delete_persona(runtime.config.personas_dir, name)
    except ConfigError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    log.info("persona deleted", extra=kv(persona=name))
    return {"deleted": name}


@router.post("/personas/{name}/activate")
async def activate_persona(request: Request, name: str) -> dict[str, Any]:
    """Make ``name`` the persona in use: saved as agent.persona, applied at once."""
    runtime = _runtime(request)
    source = _require_source(request)
    try:
        desired = source.saved().model_dump(mode="json")
    except ConfigError as exc:
        raise _unprocessable(str(exc)) from exc
    desired["agent"]["persona"] = name
    return await _save(runtime, source, desired)


# --- cameras ----------------------------------------------------------------


_ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")


@router.get("/cameras")
async def cameras(
    request: Request, env: Annotated[list[str] | None, Query()] = None
) -> dict[str, Any]:
    """``?env=NAME`` (repeatable) also reports whether those variables are set,
    for names typed into a camera form before saving."""
    runtime = _runtime(request)
    source = _source(request)
    configured = [c.model_dump(mode="json") for c in runtime.config.cameras]
    editable = False
    if source is not None:
        with contextlib.suppress(ConfigError):
            configured = [c.model_dump(mode="json") for c in source.saved().cameras]
            editable = source.path.is_file()
    return {
        "cameras": configured,
        "status": runtime.cameras.status(),
        "editable": editable,
        "secrets": {
            **secret_status({"cameras": configured}),
            **{n: bool(os.environ.get(n)) for n in (env or [])[:8] if _ENV_NAME.match(n)},
        },
    }


class CamerasBody(BaseModel):
    cameras: list[dict[str, Any]]


@router.put("/cameras")
async def save_cameras(request: Request, body: CamerasBody) -> dict[str, Any]:
    """Replace the camera list; cameras start, stop or restart at once."""
    source = _require_source(request)
    try:
        desired = source.saved().model_dump(mode="json")
    except ConfigError as exc:
        raise _unprocessable(str(exc)) from exc
    desired["cameras"] = body.cameras
    return await _save(_runtime(request), source, desired)


def _jpeg(data: bytes) -> Response:
    return Response(content=data, media_type="image/jpeg", headers={"Cache-Control": "no-store"})


@router.post("/cameras/test")
async def test_camera(body: dict[str, Any]) -> Response:
    """Grab one frame with these camera settings (saved or not)."""
    try:
        camera = CameraConfig.model_validate({"id": "test", **body, "enabled": True})
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail={"errors": validation_errors(exc)}) from exc
    try:
        return _jpeg(await probe_camera(camera))
    except CompanionError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@router.get("/cameras/{camera_id}/snapshot")
async def camera_snapshot(request: Request, camera_id: str) -> Response:
    try:
        return _jpeg(await _runtime(request).cameras.snapshot(camera_id))
    except TimeoutError as exc:
        raise HTTPException(status_code=504, detail="the camera sent no frame in time") from exc
    except CompanionError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


# --- edge devices -------------------------------------------------------------

EDGE_PROFILES_DIR = Path(__file__).resolve().parents[2] / "client" / "edge" / "profiles"
_SETTING_ERROR = re.compile(r"^([a-z_]+(?:\.[a-z_]+)?): (.+)$")


@router.get("/devices")
async def devices(request: Request) -> dict[str, Any]:
    """Connected devices, with the settings of those that offer them (device.config)."""
    runtime = _runtime(request)
    return {
        "devices": [
            {"session_id": session.session_id, **device.report(), "settings": device.settings}
            for session in runtime.sessions.values()
            for device in session.devices
        ]
    }


class DeviceConfigBody(BaseModel):
    config: dict[str, Any]


@router.put("/devices/{device_id}/config")
async def configure_device(
    request: Request, device_id: str, body: DeviceConfigBody
) -> dict[str, Any]:
    """Send settings to a device. It checks and saves them, then restarts with them."""
    try:
        result = await _runtime(request).configure_device(device_id, body.config)
    except TimeoutError as exc:
        raise HTTPException(status_code=504, detail="the device did not answer in time") from exc
    except CompanionError as exc:
        status = 404 if exc.code == "not_connected" else 409
        raise HTTPException(status_code=status, detail=str(exc)) from exc
    if not result.get("ok"):
        message = str(result.get("error") or "the device refused the settings")
        match = _SETTING_ERROR.match(message)
        path, text = (match[1], match[2]) if match else ("", message)
        raise HTTPException(status_code=422, detail={"errors": [{"path": path, "message": text}]})
    return {"restarting": bool(result.get("restarting"))}


class EnrollBody(BaseModel):
    device_id: str


@router.get("/enrolled")
async def enrolled(request: Request) -> dict[str, Any]:
    """Devices with tokens of their own (set up by the installer or enrolled here)."""
    runtime = _runtime(request)
    return {
        "devices": [
            {**d.public(), "connected": runtime.find_device(d.device_id) is not None}
            for d in runtime.devices.list()
        ]
    }


@router.post("/enrolled")
async def enroll(request: Request, body: EnrollBody) -> dict[str, Any]:
    """Create a device token, or replace it (the old one stops working at once)."""
    runtime = _runtime(request)
    replaced = runtime.devices.get(body.device_id) is not None
    try:
        token = runtime.devices.enroll(body.device_id)
    except ValueError as exc:
        raise _unprocessable(str(exc), "device_id") from exc
    if replaced:
        await runtime.disconnect_device(body.device_id)
    log.info("device token issued", extra=kv(device=body.device_id, replaced=replaced))
    return {"device_id": body.device_id, "token": token, "replaced": replaced}


@router.delete("/enrolled/{device_id}")
async def revoke(request: Request, device_id: str) -> dict[str, Any]:
    """Forget a device: its token stops working and its connections close."""
    runtime = _runtime(request)
    if not runtime.devices.remove(device_id):
        raise HTTPException(status_code=404, detail=f"no enrolled device {device_id!r}")
    closed = await runtime.disconnect_device(device_id)
    log.info("device revoked", extra=kv(device=device_id, connections_closed=closed))
    return {"revoked": device_id, "connections_closed": closed}


@router.get("/enrolled/{device_id}/root-password")
async def root_password(request: Request, device_id: str) -> dict[str, Any]:
    """The root password the installer gave the board (for its console or SSH)."""
    device = _runtime(request).devices.get(device_id)
    if device is None or not device.root_password:
        raise HTTPException(status_code=404, detail="no password recorded for this device")
    return {"device_id": device_id, "root_password": device.root_password}


def _profile(path: Path) -> dict[str, Any]:
    """A profile's settings plus the comment block at its top, as notes."""
    text = path.read_text(encoding="utf-8")
    notes: list[str] = []
    for line in text.splitlines():
        if not line.startswith("#"):
            break
        notes.append(line[1:].removeprefix(" "))
    title = notes[0].removeprefix("Profile:").strip() if notes else path.stem
    # Paragraphs about copying the file by hand do not apply on the dashboard.
    paragraphs = "\n".join(notes[1:]).strip().split("\n\n")
    body = "\n\n".join(p for p in paragraphs if not p.startswith("Copy to edge.toml"))
    return {
        "name": path.stem,
        "title": title[:1].upper() + title[1:],
        "notes": body,
        "config": tomllib.loads(text),
    }


@router.get("/edge-profiles")
async def edge_profiles() -> dict[str, Any]:
    """Ready-made edge settings for common hardware (client/edge/profiles)."""
    if not EDGE_PROFILES_DIR.is_dir():
        return {"profiles": []}
    return {"profiles": [_profile(p) for p in sorted(EDGE_PROFILES_DIR.glob("*.toml"))]}


@router.post("/restart", status_code=202)
async def restart(request: Request) -> dict[str, Any]:
    request_restart: Callable[[], None] | None = getattr(request.app.state, "request_restart", None)
    if request_restart is None:
        raise HTTPException(
            status_code=501, detail="this server cannot restart itself; restart it where it runs"
        )
    source = _source(request)
    next_config = _runtime(request).config
    if source is not None:
        try:
            next_config = source.load()
        except ConfigError as exc:
            raise HTTPException(
                status_code=422, detail={"errors": [{"path": "", "message": str(exc)}]}
            ) from exc
    log.info("restart requested from the dashboard")
    asyncio.get_running_loop().call_later(RESTART_DELAY_S, request_restart)
    return {"host": next_config.server.host, "port": next_config.server.port}


# --- live events --------------------------------------------------------------

# Not under ``router``: a WebSocket cannot answer with an HTTP error, so it
# checks admin access itself and says why it closes.
events_router = APIRouter()


@events_router.websocket("/v1/admin/events")
async def admin_events(ws: WebSocket) -> None:
    """The dashboard's live events: first {"recent": [...]}, then {"event": {...}} each."""
    refusal = admin_refusal(ws)
    await ws.accept()
    if refusal is not None:
        status, reason = refusal
        await ws.send_json({"error": reason, "status": status})
        await ws.close(code=4000 + status)
        return
    feed = _runtime(ws).feed  # type: ignore[arg-type]
    queue = feed.watch()
    try:
        await ws.send_json({"recent": feed.recent()})
        while True:
            await ws.send_json({"event": await queue.get()})
    except (WebSocketDisconnect, RuntimeError):
        pass  # the page was closed
    finally:
        feed.unwatch(queue)


@router.get("/status")
async def status(request: Request, probe: bool = False) -> dict[str, Any]:
    return await _runtime(request).status(probe=probe)


@router.get("/metrics")
async def metrics(request: Request) -> dict[str, Any]:
    runtime = _runtime(request)
    return {"enabled": runtime.metrics.enabled, **runtime.metrics.summary()}
