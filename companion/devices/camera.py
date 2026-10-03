"""Camera device: feeds frames from an IP camera (or any FrameSource) into a session.

It joins the active session with the "camera" role, so a browser can keep the
mic and speaker while this process supplies the eyes:

    uv run companion camera http://192.168.1.20:8080/shot.jpg
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import uuid
from dataclasses import dataclass

import websockets
from websockets.asyncio.client import ClientConnection

from companion.core.errors import CompanionError
from companion.core.logging import kv
from companion.devices.sources import FrameSource, open_source
from companion.protocol import encode_binary_frame, make_event, parse_text_frame
from companion.protocol import events as ev
from companion.vision.image import to_jpeg

log = logging.getLogger(__name__)

MAX_SEND_BYTES = 1_500_000  # larger frames are downscaled before sending


@dataclass
class CameraOptions:
    server: str
    source_url: str
    session_id: str | None = None
    device_id: str = "ipcam"
    interval_s: float = 5.0  # 0 = only when the server asks
    enable: bool = True
    token: str | None = None  # the server's COMPANION_TOKEN, if it requires one


def _shrink(jpeg: bytes) -> bytes:
    if len(jpeg) <= MAX_SEND_BYTES:
        return jpeg
    import io

    from PIL import Image

    with Image.open(io.BytesIO(jpeg)) as image:
        return to_jpeg(image.convert("RGB"), max_side=1280)


class CameraDevice:
    def __init__(self, options: CameraOptions, *, source: FrameSource | None = None) -> None:
        self.options = options
        self.source: FrameSource | None = source
        self.vision_enabled = False
        self.user_disabled = False  # someone turned vision off; do not re-enable
        self.frames_sent = 0

    async def _send_frame(self, ws: ClientConnection, reason: str, frame_id: str) -> None:
        assert self.source is not None
        try:
            jpeg = await self.source.grab()
        except CompanionError as exc:
            log.warning("camera grab failed", extra=kv(error=str(exc)))
            return
        jpeg = await asyncio.to_thread(_shrink, jpeg)
        header = make_event(
            ev.VISION_FRAME,
            {"mime": "image/jpeg", "reason": reason, "frame_id": frame_id},
            device_id=self.options.device_id,
        )
        await ws.send(encode_binary_frame(header, jpeg))
        self.frames_sent += 1

    async def _periodic(self, ws: ClientConnection) -> None:
        if self.options.interval_s <= 0:
            return
        while True:
            await asyncio.sleep(self.options.interval_s)
            if self.vision_enabled:
                await self._send_frame(ws, "periodic", uuid.uuid4().hex[:12])

    async def _session(self, ws: ClientConnection) -> str:
        """Serve one session; returns why it ended ("no_session", "ended", "closed")."""
        start: dict[str, object] = {"roles": ["camera"]}
        if self.options.session_id:
            start["session_id"] = self.options.session_id
        else:
            start["join"] = True
        await ws.send(
            make_event(ev.SESSION_START, start, device_id=self.options.device_id).to_json()
        )
        periodic: asyncio.Task[None] | None = None
        try:
            async for message in ws:
                if isinstance(message, bytes):
                    continue  # audio etc. is not for us
                event = parse_text_frame(message)
                payload = event.payload
                if event.type == ev.SESSION_STARTED:
                    self.vision_enabled = bool(payload.get("vision_enabled"))
                    log.info(
                        "camera joined session",
                        extra=kv(
                            session=payload.get("session_id"),
                            source=self.source.description if self.source else None,
                        ),
                    )
                    if self.options.enable and not self.vision_enabled and not self.user_disabled:
                        await ws.send(make_event(ev.VISION_ENABLE).to_json())
                    periodic = asyncio.create_task(self._periodic(ws))
                elif event.type == ev.VISION_STATE:
                    was = self.vision_enabled
                    self.vision_enabled = bool(payload.get("enabled"))
                    if was and not self.vision_enabled:
                        self.user_disabled = True
                        log.info("vision turned off in the session; camera paused")
                    elif self.vision_enabled:
                        self.user_disabled = False
                elif event.type == ev.VISION_CAPTURE_REQUEST and self.vision_enabled:
                    await self._send_frame(
                        ws, "manual", str(payload.get("request_id") or uuid.uuid4().hex[:12])
                    )
                elif event.type == ev.SESSION_ENDED:
                    log.info("session ended; waiting for the next conversation")
                    self.user_disabled = False  # a new conversation is a fresh start
                    return "ended"
                elif event.type == ev.SYSTEM_ERROR:
                    if payload.get("code") == "no_session":
                        return "no_session"
                    log.warning("server error", extra=kv(**payload))
        finally:
            if periodic is not None:
                periodic.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await periodic
        return "closed"

    async def run(self) -> None:
        warned = False
        while self.source is None:
            try:
                self.source = await open_source(self.options.source_url)
            except CompanionError as exc:
                # A phone app may simply not be running yet; keep trying.
                if not warned:
                    log.warning("camera unavailable; retrying every 5 s", extra=kv(error=str(exc)))
                    warned = True
                await asyncio.sleep(5.0)
        log.info("camera source ready", extra=kv(source=self.source.description))
        backoff = 1.0
        waiting_logged = False
        try:
            while True:
                outcome = "error"
                try:
                    headers = (
                        {"Authorization": f"Bearer {self.options.token}"}
                        if self.options.token
                        else None
                    )
                    async with websockets.connect(
                        self.options.server, max_size=None, additional_headers=headers
                    ) as ws:
                        backoff = 1.0
                        outcome = await self._session(ws)
                except (OSError, websockets.WebSocketException) as exc:
                    log.warning("server connection lost", extra=kv(error=str(exc)))
                if outcome in ("no_session", "ended"):
                    # Server is fine; wait for someone to start a conversation.
                    if not waiting_logged:
                        log.info("no active session yet; retrying every 2 s")
                        waiting_logged = True
                    await asyncio.sleep(2.0)
                    continue
                waiting_logged = False
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30.0)
        finally:
            await self.source.close()


def server_url(host: str, port: int) -> str:
    host = "127.0.0.1" if host in ("0.0.0.0", "::") else host
    return f"ws://{host}:{port}/v1/realtime"
