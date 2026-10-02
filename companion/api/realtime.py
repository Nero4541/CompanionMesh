"""WebSocket ``/v1/realtime`` transport."""

from __future__ import annotations

import asyncio
import contextlib
import logging

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from starlette.websockets import WebSocketState

from companion.core.logging import kv
from companion.core.runtime import CompanionRuntime
from companion.core.session import Session
from companion.protocol import (
    Envelope,
    ProtocolError,
    decode_binary_frame,
    encode_binary_frame,
    make_event,
    parse_text_frame,
)
from companion.protocol import events as ev

log = logging.getLogger(__name__)
router = APIRouter()

MAX_TEXT_CHARS = 8_000


class WebSocketOutbox:
    def __init__(self, ws: WebSocket) -> None:
        self._ws = ws
        self._lock = asyncio.Lock()

    @property
    def open(self) -> bool:
        return self._ws.application_state == WebSocketState.CONNECTED

    async def send_event(self, event: Envelope) -> None:
        if not self.open:
            return
        async with self._lock:
            with contextlib.suppress(WebSocketDisconnect, RuntimeError):
                await self._ws.send_text(event.to_json())

    async def send_binary(self, event: Envelope, data: bytes) -> None:
        if not self.open:
            return
        async with self._lock:
            with contextlib.suppress(WebSocketDisconnect, RuntimeError):
                await self._ws.send_bytes(encode_binary_frame(event, data))


async def _send_error(outbox: WebSocketOutbox, code: str, message: str) -> None:
    await outbox.send_event(
        make_event(ev.SYSTEM_ERROR, {"code": code, "message": message, "recoverable": True})
    )


async def _dispatch(session: Session, event: Envelope) -> None:
    payload = event.payload
    match event.type:
        case ev.CONVERSATION_TEXT:
            text = str(payload.get("text", ""))
            if len(text) > MAX_TEXT_CHARS:
                await session.emit_error(f"text exceeds {MAX_TEXT_CHARS} chars", code="too_long")
                return
            await session.handle_text(text)
        case ev.CONVERSATION_CANCEL:
            await session.cancel_turn()
        case ev.AUDIO_INPUT_START:
            await session.audio_start(payload)
        case ev.AUDIO_INPUT_STOP:
            await session.audio_stop()
        case ev.AUDIO_OUTPUT_PLAYED:
            await session.playback_finished(payload.get("turn_id"))
        case ev.VISION_ENABLE:
            await session.vision.enable()
        case ev.VISION_DISABLE:
            await session.vision.disable()
        case ev.VISION_EVENT:
            await session.vision.submit_event(event)
        case _:
            # Forward compatibility: unknown event types are ignored.
            log.debug("ignored event", extra=kv(type=event.type))


@router.websocket("/v1/realtime")
async def realtime(ws: WebSocket) -> None:
    runtime: CompanionRuntime = ws.app.state.runtime
    await ws.accept()
    outbox = WebSocketOutbox(ws)
    session: Session | None = None
    try:
        # First frame must be session.start (resume by passing session_id).
        start = parse_text_frame(await ws.receive_text())
        if start.type != ev.SESSION_START:
            await _send_error(outbox, "protocol", "first event must be session.start")
            await ws.close(code=1002)
            return
        requested = start.payload.get("session_id") or start.session_id
        session, resumed = await runtime.open_session(
            outbox,
            session_id=str(requested) if requested else None,
            device_id=start.device_id,
        )
        history = await runtime.store.history(session.session_id, limit=50)
        await session.emit(
            ev.SESSION_STARTED,
            {
                "session_id": session.session_id,
                "resumed": resumed,
                "protocol": ev.PROTOCOL_VERSION,
                "persona": runtime.persona.display_name or runtime.persona.name,
                "speech_input": runtime.stt is not None,
                "speech_output": session.speak,
                "vision": session.vision.available,
                "history": [{"role": m.role, "content": m.content} for m in history],
            },
        )
        await session.emit(ev.SYSTEM_STATE, {"state": session.state.value})
        log.info(
            "realtime connected",
            extra=kv(session=session.session_id, device=start.device_id, resumed=resumed),
        )

        while True:
            message = await ws.receive()
            if message["type"] == "websocket.disconnect":
                break
            try:
                if message.get("bytes") is not None:
                    event, data = decode_binary_frame(message["bytes"])
                    if event.type == ev.AUDIO_INPUT_CHUNK:
                        await session.audio_chunk(data)
                    elif event.type == ev.VISION_FRAME:
                        await session.vision.submit_frame(event, data)
                    continue
                if message.get("text") is not None:
                    await _dispatch(session, parse_text_frame(message["text"]))
            except ProtocolError as exc:
                await _send_error(outbox, "protocol", str(exc))
    except ProtocolError as exc:
        await _send_error(outbox, "protocol", str(exc))
        with contextlib.suppress(Exception):
            await ws.close(code=1002)
    except WebSocketDisconnect:
        pass
    finally:
        if session is not None:
            await runtime.close_session(session)
            log.info("realtime disconnected", extra=kv(session=session.session_id))
