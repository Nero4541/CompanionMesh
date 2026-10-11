"""Admin API behind the dashboard (/v1/admin/*).

Guarded by :func:`companion.api.auth.require_admin`, never by the device
token. Secrets are reported only as set/unset, by environment-variable name.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel

from companion import __version__
from companion.api.auth import require_admin
from companion.core.config import CompanionConfig, needs_restart, secret_status
from companion.core.runtime import CompanionRuntime

router = APIRouter(prefix="/v1/admin", dependencies=[Depends(require_admin)])


def _runtime(request: Request) -> CompanionRuntime:
    return request.app.state.runtime


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


@router.get("/config")
async def config(request: Request) -> dict[str, Any]:
    effective = _runtime(request).config.public_view()
    return {
        "version": __version__,
        "effective": effective,
        "schema": CompanionConfig.model_json_schema(),
        "restart_required": {p: needs_restart(p) for p in setting_paths(CompanionConfig)},
        "secrets": secret_status(effective),
    }


@router.get("/status")
async def status(request: Request, probe: bool = False) -> dict[str, Any]:
    return await _runtime(request).status(probe=probe)


@router.get("/metrics")
async def metrics(request: Request) -> dict[str, Any]:
    runtime = _runtime(request)
    return {"enabled": runtime.metrics.enabled, **runtime.metrics.summary()}
