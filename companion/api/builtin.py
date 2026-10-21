"""Built-in engines in the dashboard (/v1/admin/builtin).

GET reports each engine (installed, running, GPU memory, its model folders,
log), POST .../install installs one in the background, PUT .../{name} turns one
on or off or switches its model by saving its settings (``builtin.<name>.*``,
``perception.*`` for the perception engine); the runtime then starts, stops or
restarts it at once.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from companion.api.admin import _require_source, _runtime, _save
from companion.api.auth import require_admin
from companion.builtin import (
    ENGINE_SETTINGS,
    ENGINES,
    engine_enabled,
    engine_model,
    gpu_found,
    gpu_memory,
    resolve_libraries,
)
from companion.core.errors import CompanionError
from companion.core.logging import kv

log = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/admin/builtin", dependencies=[Depends(require_admin)])

_installs: set[asyncio.Task[None]] = set()


def _engine_name(name: str) -> str:
    if name not in ENGINES:
        raise HTTPException(status_code=404, detail=f"no built-in engine {name!r}")
    return name


@router.get("")
async def status(request: Request) -> dict[str, Any]:
    runtime = _runtime(request)
    gpu = await asyncio.to_thread(gpu_memory)
    engines = {}
    for name, engine in runtime.builtin.engines.items():
        model = engine_model(name, runtime.config)
        engines[name] = {
            **engine.report(engine_enabled(name, runtime.config), gpu),
            "in_use": runtime.uses_builtin(name),
            "model": model,
            "model_folder": model.split("/")[-1],  # models/<this> (downloaded if missing)
        }
    return {
        "engines": engines,
        "libraries": resolve_libraries(runtime.config),
        "gpu": gpu_found(),
        "gpu_memory": {"used_mb": gpu.used_mb, "total_mb": gpu.total_mb},
        "editable": getattr(request.app.state, "config_source", None) is not None,
    }


@router.post("/{name}/install", status_code=202)
async def install(request: Request, name: str) -> dict[str, Any]:
    """Install (or update) an engine in the background; GET shows its progress."""
    runtime = _runtime(request)
    engine = runtime.builtin.engines[_engine_name(name)]
    if engine.status.state == "installing":
        raise HTTPException(status_code=409, detail=f"{name} is already being installed")
    libraries = resolve_libraries(runtime.config)
    if engine.pid is not None:  # a running engine keeps its files open
        await engine.stop()

    async def run() -> None:
        try:
            await engine.install(libraries)
        except CompanionError as exc:
            log.error("builtin install failed", extra=kv(engine=name, error=str(exc)))
            return
        runtime.schedule_builtin(name, force=True)  # starts it if it is on

    task = asyncio.create_task(run())
    _installs.add(task)
    task.add_done_callback(_installs.discard)
    await asyncio.sleep(0)  # let it report "installing"
    return {"state": engine.status.state, "libraries": libraries}


class EngineBody(BaseModel):
    enabled: bool | None = None
    # A folder in builtin/<name>/models (or what builtin.<name>.model/checkpoint takes).
    model: str | None = Field(default=None, min_length=1)


@router.put("/{name}")
async def change(request: Request, name: str, body: EngineBody) -> dict[str, Any]:
    """Turn an engine on or off, or switch its model (saved, applied at once)."""
    runtime = _runtime(request)
    source = _require_source(request)
    desired = source.saved().model_dump(mode="json")
    path, model_key = ENGINE_SETTINGS[_engine_name(name)]
    section = desired
    for part in path.split("."):
        section = section[part]
    if body.enabled is not None:
        section["enabled"] = body.enabled
    if body.model is not None:
        section[model_key] = body.model
    result = await _save(runtime, source, desired)
    if body.enabled is False:  # wait for the process to end, so the memory shows as freed
        await runtime.builtin_settled(name)
    return result
