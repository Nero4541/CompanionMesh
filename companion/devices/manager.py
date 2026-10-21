"""Cameras the server watches itself (``cameras:`` in the config).

Each enabled camera runs a :class:`CameraDevice` as a task inside the server,
connected to this server's own realtime endpoint, exactly like
``companion camera`` would from another process. Changing the list starts,
stops or replaces only the cameras that changed.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from companion.core.config import CameraConfig, CompanionConfig
from companion.core.errors import CompanionError, ProviderError
from companion.core.logging import kv
from companion.devices.camera import CameraDevice, CameraOptions, server_url
from companion.devices.sources import add_credentials, open_source

log = logging.getLogger(__name__)

# Lets the server finish starting before the first camera connects to it.
START_DELAY_S = 1.0
PROBE_TIMEOUT_S = 15.0

DeviceFactory = Callable[[CameraConfig, str, str | None], CameraDevice]


def source_url(camera: CameraConfig) -> str:
    username, password = camera.credentials()
    return add_credentials(camera.url, username, password)


def build_device(camera: CameraConfig, server: str, token: str | None) -> CameraDevice:
    return CameraDevice(
        CameraOptions(
            server=server,
            source_url=source_url(camera),
            session_id=camera.session_id,
            device_id=camera.id,
            mode=camera.mode,
            interval_s=camera.interval_s,
            analysis_interval_s=camera.analysis_interval_s,
            heartbeat_s=camera.heartbeat_s,
            enable=camera.enable_vision,
            token=token,
        )
    )


async def probe(camera: CameraConfig) -> bytes:
    """Open the camera once and return one JPEG frame (for "test" in the dashboard)."""

    async def grab() -> bytes:
        source = await open_source(source_url(camera))
        try:
            return await source.grab()
        finally:
            await source.close()

    try:
        return await asyncio.wait_for(grab(), PROBE_TIMEOUT_S)
    except TimeoutError as exc:
        raise ProviderError("camera", f"no frame within {PROBE_TIMEOUT_S:.0f} s") from exc


@dataclass
class _Running:
    config: CameraConfig
    device: CameraDevice
    task: asyncio.Task[None]


class CameraManager:
    def __init__(self, factory: DeviceFactory | None = None) -> None:
        self._factory = factory or build_device
        self._running: dict[str, _Running] = {}
        self._configured: list[CameraConfig] = []

    def apply(self, config: CompanionConfig, *, delay: float = 0.0) -> None:
        """Run exactly the enabled cameras of ``config``. Call inside the event loop."""
        self._configured = list(config.cameras)
        server = server_url(config.server.host, config.server.port)
        token = config.server.auth_token()
        wanted = {c.id: c for c in config.cameras if c.enabled}
        for camera_id in list(self._running):
            running = self._running[camera_id]
            if wanted.get(camera_id) != running.config or running.task.done():
                running.task.cancel()
                del self._running[camera_id]
                log.info("camera stopped", extra=kv(camera=camera_id))
        for camera_id, camera in wanted.items():
            if camera_id in self._running:
                continue
            device = self._factory(camera, server, token)
            task = asyncio.create_task(
                self._run(camera_id, device, delay), name=f"camera {camera_id}"
            )
            self._running[camera_id] = _Running(camera, device, task)
            log.info("camera started", extra=kv(camera=camera_id, mode=camera.mode))

    async def _run(self, camera_id: str, device: CameraDevice, delay: float) -> None:
        if delay:
            await asyncio.sleep(delay)
        try:
            await device.run()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # keep the server up; report it on the status page
            device.state, device.error = "failed", str(exc)
            log.exception("camera failed", extra=kv(camera=camera_id))

    async def stop(self) -> None:
        tasks = [r.task for r in self._running.values()]
        self._running.clear()
        for task in tasks:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task

    async def snapshot(self, camera_id: str) -> bytes:
        """The camera's current view, from the running camera's own source."""
        running = self._running.get(camera_id)
        if running is None or running.device.source is None:
            raise CompanionError(f"camera {camera_id!r} is not running")
        return await asyncio.wait_for(running.device.source.grab(), PROBE_TIMEOUT_S)

    def running_ids(self) -> set[str]:
        return {cid for cid, r in self._running.items() if not r.task.done()}

    def source(self, camera_id: str) -> Any:
        """The open frame source of a running camera (None while it is still opening)."""
        running = self._running.get(camera_id)
        return running.device.source if running is not None else None

    def status(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for camera in self._configured:
            running = self._running.get(camera.id)
            entry: dict[str, Any] = {"id": camera.id, "enabled": camera.enabled, "running": False}
            if running is not None:
                device = running.device
                entry.update(
                    running=not running.task.done(),
                    state=device.state,
                    error=device.error,
                    session_id=device.session_id,
                    vision_enabled=device.vision_enabled,
                    frames_sent=device.frames_sent,
                    frames_analyzed=device.frames_analyzed,
                    last_frame_at=device.last_frame_at,
                    source=device.source.description if device.source else None,
                )
            out.append(entry)
        return out
