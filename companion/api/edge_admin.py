"""Admin API for setting up boards from the dashboard (/v1/admin/edge/*).

An installation takes half a minute or more, so it runs as a job: POST starts
it, GET reports its steps as they happen. Wi-Fi and root passwords arrive in
the request and go to the board; they are never logged or kept, except the
new root password the installer sets (kept in the device registry).
"""

from __future__ import annotations

import asyncio
import contextlib
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from companion import edge_install
from companion.api.auth import require_admin
from companion.core.runtime import CompanionRuntime
from companion.devices.registry import DEVICE_ID

router = APIRouter(prefix="/v1/admin/edge", dependencies=[Depends(require_admin)])

KEEP_JOBS = 20


@dataclass
class Job:
    id: str
    device_id: str
    host: str
    state: str = "running"  # running | done | failed
    steps: list[dict[str, str]] = field(default_factory=list)
    error: str | None = None
    result: dict[str, Any] | None = None
    started: float = field(default_factory=time.time)
    task: asyncio.Task[None] | None = None

    def progress(self, step: str, status: str, detail: str) -> None:
        entry = {"step": step, "status": status, "detail": detail}
        if self.steps and self.steps[-1]["step"] == step:
            self.steps[-1] = entry  # "run" becomes its outcome
        else:
            self.steps.append(entry)

    def report(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "device_id": self.device_id,
            "host": self.host,
            "state": self.state,
            "steps": self.steps,
            "error": self.error,
            "result": self.result,
            "started": self.started,
        }


def _jobs(request: Request) -> dict[str, Job]:
    jobs = getattr(request.app.state, "edge_jobs", None)
    if jobs is None:
        jobs = request.app.state.edge_jobs = {}
    return jobs


def _runtime(request: Request) -> CompanionRuntime:
    return request.app.state.runtime


@router.get("/version")
async def version() -> dict[str, Any]:
    """The edge client version this server installs and offers for updates."""
    return {"version": edge_install.edge_version()}


@router.get("/setup")
async def setup_info(request: Request, scan: bool = True) -> dict[str, Any]:
    """What the Add device form offers: boards on USB, server URLs, profiles."""
    runtime = _runtime(request)
    port = runtime.config.server.port
    boards = await edge_install.discover() if scan else []
    addresses = await asyncio.to_thread(edge_install.lan_addresses)
    return {
        "boards": boards,
        "server_urls": [f"ws://{a}:{port}/v1/realtime" for a in addresses],
        "profiles": edge_install.profiles(),
        "edge_version": edge_install.edge_version(),
        "listening": runtime.config.server.host,
        "public_url": edge_install.public_reach(runtime.config)[0],
        "public_enabled": runtime.config.server.public.enabled,
        "public": _public_state(request),
    }


def _public_state(request: Request) -> dict[str, Any]:
    """The public listener as running, and as saved (they differ until a restart)."""
    from companion.api.admin import _source

    runtime = _runtime(request)
    running = runtime.config.server.public
    source = _source(request)
    saved = source.saved().server.public if source is not None else running
    cert = None
    if running.enabled and not running.cert_file:
        from companion.core.tls import ensure_certificate

        cert = ensure_certificate(runtime.config.storage.data_dir / "tls").sha256
    return {
        "enabled": running.enabled,
        "port": running.port,
        "url": running.url,
        "listening": getattr(request.app.state, "public_listening", None),
        "certificate_sha256": cert,
        "saved": {"enabled": saved.enabled, "port": saved.port, "url": saved.url},
        "editable": source is not None,
        "can_restart": getattr(request.app.state, "request_restart", None) is not None,
        "server_host": runtime.config.server.host,
    }


class PublicBody(BaseModel):
    enabled: bool
    address: str = ""  # a DNS name or public IP, or a whole wss:// URL
    port: int = Field(default=8766, ge=1, le=65535)


@router.put("/public")
async def save_public(request: Request, body: PublicBody) -> dict[str, Any]:
    """Turn the public listener on or off (takes effect at the next restart)."""
    from companion.api.admin import _require_source, _save

    source = _require_source(request)
    settings = source.saved().model_dump(mode="json")
    public = settings["server"]["public"]
    public["enabled"] = body.enabled
    public["port"] = body.port
    address = body.address.strip()
    if address and "://" not in address:
        host = f"[{address}]" if ":" in address and not address.startswith("[") else address
        address = f"wss://{host}:{body.port}/v1/realtime"
    if body.enabled and not address:
        raise HTTPException(status_code=422, detail="give the address boards reach from outside")
    public["url"] = address or public.get("url")
    if body.enabled and settings["server"]["host"] in ("127.0.0.1", "localhost", "::1"):
        settings["server"]["host"] = "0.0.0.0"  # boards at home need it too
    result = await _save(_runtime(request), source, settings)
    return {**result, "url": public["url"]}


class WifiBody(BaseModel):
    ssid: str = Field(min_length=1, max_length=32)
    password: str | None = None


class InstallBody(BaseModel):
    device_id: str
    host: str = Field(min_length=1)
    server_url: str = Field(pattern=r"^wss?://")
    profile: str = "licheerv-nano"
    camera: bool | None = None
    rotate: int | None = None
    wifi: list[WifiBody] = Field(default_factory=list)
    password: str | None = None
    keep_token: bool = False  # update a board set up before (software, settings, Wi-Fi)


@router.post("/install", status_code=202)
async def start_install(request: Request, body: InstallBody) -> dict[str, Any]:
    runtime = _runtime(request)
    jobs = _jobs(request)
    if any(j.state == "running" and j.host == body.host for j in jobs.values()):
        raise HTTPException(status_code=409, detail=f"{body.host} is being set up already")
    if body.profile not in edge_install.profiles():
        raise HTTPException(status_code=422, detail=f"no profile {body.profile!r}")
    if not DEVICE_ID.match(body.device_id):
        raise HTTPException(status_code=422, detail="device id: letters, digits, '.', '_', '-'")
    if body.keep_token and runtime.devices.get(body.device_id) is None:
        raise HTTPException(
            status_code=422, detail=f"this server has not set up {body.device_id!r} yet"
        )
    for network in body.wifi:
        if network.password:
            try:
                edge_install.wpa_psk(network.ssid, network.password)
            except edge_install.InstallError as exc:
                raise HTTPException(status_code=422, detail=str(exc)) from exc

    public_url, cert_pem = edge_install.public_reach(runtime.config)
    plan = edge_install.InstallPlan(
        public_url=public_url,
        server_cert_pem=cert_pem,
        device_id=body.device_id,
        host=body.host,
        server_url=body.server_url,
        profile=body.profile,
        camera=body.camera,
        rotate=body.rotate,
        wifi=[edge_install.WifiNetwork(n.ssid, n.password or None) for n in body.wifi],
        password=body.password or None,
        keep_token=body.keep_token,
    )
    job = Job(id=uuid.uuid4().hex[:12], device_id=body.device_id, host=body.host)

    async def run() -> None:
        try:
            result = await edge_install.install(
                plan,
                registry=runtime.devices,
                data_dir=runtime.config.storage.data_dir,
                progress=job.progress,
                connected=lambda device_id: runtime.find_device(device_id) is not None,
            )
            job.result = {
                "version": result.version,
                "log_tail": result.log_tail,
                "has_root_password": bool(result.root_password),
            }
            job.state = "done"
        except edge_install.InstallError as exc:
            job.error, job.state = str(exc), "failed"
        except Exception as exc:  # report anything, the page is waiting for it
            job.error, job.state = f"{type(exc).__name__}: {exc}", "failed"
        if job.steps and job.steps[-1]["status"] == "run":
            job.steps[-1]["status"] = "fail"
        await runtime.notice(
            "device_installed" if job.state == "done" else "device_install_failed",
            {"device_id": job.device_id, "host": job.host, "error": job.error},
        )

    job.task = asyncio.create_task(run())
    jobs[job.id] = job
    for old in sorted(jobs.values(), key=lambda j: j.started)[:-KEEP_JOBS]:
        if old.state != "running":
            del jobs[old.id]
    return {"job": job.id}


@router.get("/install/{job_id}")
async def install_status(request: Request, job_id: str) -> dict[str, Any]:
    job = _jobs(request).get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="no such installation")
    return job.report()


async def cancel_jobs(app: Any) -> None:
    """On shutdown: stop installations still running."""
    for job in getattr(app.state, "edge_jobs", {}).values():
        if job.task is not None and not job.task.done():
            job.task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await job.task
