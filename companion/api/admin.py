"""Admin API behind the dashboard (/v1/admin/*).

Guarded by :func:`companion.api.auth.require_admin`, never by the device
token. Secrets are reported only as set/unset, by environment-variable name.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from companion import __version__
from companion.api.auth import require_admin
from companion.core.config import (
    CompanionConfig,
    ConfigError,
    ConfigSource,
    changed_settings,
    needs_restart,
    secret_status,
)
from companion.core.config_edit import ConfigEditError, save_settings
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


def _save(
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
    log.info(
        "settings saved", extra=kv(file=str(written), applied=len(applied), pending=len(pending))
    )
    return {"file": str(written), "applied": applied, "pending_restart": pending}


@router.put("/config")
async def save_config(request: Request, body: SaveRequest) -> dict[str, Any]:
    return _save(_runtime(request), _require_source(request), body.config)


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
    return _save(runtime, source, desired)


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


@router.get("/status")
async def status(request: Request, probe: bool = False) -> dict[str, Any]:
    return await _runtime(request).status(probe=probe)


@router.get("/metrics")
async def metrics(request: Request) -> dict[str, Any]:
    runtime = _runtime(request)
    return {"enabled": runtime.metrics.enabled, **runtime.metrics.summary()}
