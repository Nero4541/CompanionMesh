"""Built-in speech engines in the dashboard (/v1/admin/builtin).

GET reports each engine (installed, running, GPU memory, log), POST .../install
installs one in the background, PUT .../{name} turns one on or off by saving
``builtin.<name>.enabled``; the runtime then starts or stops it at once.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from companion.api.admin import _require_source, _runtime, _save
from companion.api.auth import require_admin
from companion.builtin import ENGINES, gpu_found, gpu_memory, resolve_libraries
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
    engines = {
        name: {
            **engine.report(getattr(runtime.config.builtin, name).enabled, gpu),
            "in_use": runtime.uses_builtin(name),
        }
        for name, engine in runtime.builtin.engines.items()
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


class EnableBody(BaseModel):
    enabled: bool


@router.put("/{name}")
async def enable(request: Request, name: str, body: EnableBody) -> dict[str, Any]:
    """Turn an engine on or off (saved in the settings, applied at once)."""
    runtime = _runtime(request)
    source = _require_source(request)
    desired = source.saved().model_dump(mode="json")
    desired["builtin"][_engine_name(name)]["enabled"] = body.enabled
    result = await _save(runtime, source, desired)
    if not body.enabled:  # wait for the process to end, so the memory shows as freed
        await runtime.builtin_settled(name)
    return result
