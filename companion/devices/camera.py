"""Camera device: feeds frames from an IP camera (or any FrameSource) into a session.

It joins the active session with the "camera" role, so a browser can keep the
mic and speaker while this process supplies the eyes:

    uv run companion camera http://192.168.1.20:8080/shot.jpg

In ``change`` mode (the default) it looks at the camera itself, cheaply, about
once a second and sends a frame only when something moved or the scene
changed, plus a routine frame every ``heartbeat_s``. There is no continuous
upstream video. ``periodic`` mode sends a frame every ``interval_s`` instead.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import uuid
from dataclasses import dataclass

import websockets
from websockets.asyncio.client import ClientConnection

from companion.core.clock import now_ms
from companion.core.errors import CompanionError
from companion.core.logging import kv
from companion.devices.sources import FrameSource, open_source
from companion.protocol import encode_binary_frame, make_event, parse_text_frame
from companion.protocol import events as ev
from companion.vision.image import to_jpeg
from companion.vision.motion import ChangeEvent, MotionDetector, tiny_gray

log = logging.getLogger(__name__)

MAX_SEND_BYTES = 1_500_000  # larger frames are downscaled before sending


@dataclass
class CameraOptions:
    server: str
    source_url: str
    session_id: str | None = None
    device_id: str = "ipcam"
    mode: str = "change"  # change: send frames when the view changes; periodic
    interval_s: float = 5.0  # periodic mode: seconds between frames (0 = only on request)
    analysis_interval_s: float = 1.0  # change mode: how often to look for changes
    heartbeat_s: float = 300.0  # change mode: a routine frame at least this often
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
        self.frames_analyzed = 0
        self.detector = MotionDetector()
        self._last_sent = 0.0

    async def _grab(self) -> tuple[bytes, float] | None:
        assert self.source is not None
        try:
            jpeg = await self.source.grab()
        except CompanionError as exc:
            log.warning("camera grab failed", extra=kv(error=str(exc)))
            return None
        return jpeg, now_ms()

    async def _send_frame(
        self,
        ws: ClientConnection,
        reason: str,
        frame_id: str,
        grabbed: tuple[bytes, float] | None = None,
        event: ChangeEvent | None = None,
    ) -> None:
        grabbed = grabbed or await self._grab()
        if grabbed is None:
            return
        jpeg, captured_at = grabbed
        jpeg = await asyncio.to_thread(_shrink, jpeg)
        payload: dict[str, object] = {
            "mime": "image/jpeg",
            "reason": reason,
            "frame_id": frame_id,
            "captured_at": captured_at,
        }
        if event is not None:
            payload["event"] = event.to_payload()
        header = make_event(ev.VISION_FRAME, payload, device_id=self.options.device_id)
        await ws.send(encode_binary_frame(header, jpeg))
        self.frames_sent += 1
        self._last_sent = asyncio.get_running_loop().time()

    async def _periodic(self, ws: ClientConnection) -> None:
        if self.options.mode == "change":
            await self._watch_changes(ws)
            return
        if self.options.interval_s <= 0:
            return
        while True:
            await asyncio.sleep(self.options.interval_s)
            if self.vision_enabled:
                await self._send_frame(ws, "periodic", uuid.uuid4().hex[:12])

    async def _watch_changes(self, ws: ClientConnection) -> None:
        """Look often, send rarely: only frames where something changed."""
        loop = asyncio.get_running_loop()
        self._last_sent = loop.time()
        while True:
            await asyncio.sleep(self.options.analysis_interval_s)
            if not self.vision_enabled:
                self.detector.reset()
                continue
            grabbed = await self._grab()
            if grabbed is None:
                continue
            try:
                gray = await asyncio.to_thread(tiny_gray, grabbed[0])
            except OSError as exc:  # not a decodable JPEG
                log.warning("camera frame unreadable", extra=kv(error=str(exc)))
                continue
            self.frames_analyzed += 1
            change = self.detector.feed(gray, loop.time())
            if change is not None:
                log.info("camera saw a change", extra=kv(**change.to_payload()))
                await self._send_frame(ws, "change", uuid.uuid4().hex[:12], grabbed, change)
            elif loop.time() - self._last_sent >= self.options.heartbeat_s:
                await self._send_frame(ws, "periodic", uuid.uuid4().hex[:12], grabbed)

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
                elif event.type == ev.SYSTEM_PING:
                    await ws.send(
                        make_event(
                            ev.SYSTEM_PONG,
                            {**payload, "t_device": now_ms()},
                            device_id=self.options.device_id,
                        ).to_json()
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
