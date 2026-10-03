from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from companion.api.auth import require_token
from companion.core.runtime import CompanionRuntime
from companion.protocol import Envelope
from companion.protocol import events as ev

router = APIRouter()
protected = APIRouter(dependencies=[Depends(require_token)])


def _runtime(request: Request) -> CompanionRuntime:
    return request.app.state.runtime


@router.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@protected.get("/v1/status")
async def status(request: Request, probe: bool = False) -> dict[str, Any]:
    return await _runtime(request).status(probe=probe)


@protected.get("/v1/config")
async def config(request: Request) -> dict[str, Any]:
    return _runtime(request).config.public_view()


class ChatRequest(BaseModel):
    text: str = Field(min_length=1, max_length=8_000)
    session_id: str | None = Field(default=None, max_length=128)
    device_id: str | None = Field(default=None, max_length=128)


class ChatResponse(BaseModel):
    session_id: str
    text: str
    errors: list[dict[str, Any]] = []


class _CollectOutbox:
    def __init__(self) -> None:
        self.errors: list[dict[str, Any]] = []

    async def send_event(self, event: Envelope) -> None:
        if event.type == ev.SYSTEM_ERROR:
            self.errors.append(event.payload)

    async def send_binary(self, event: Envelope, data: bytes) -> None:
        pass

    async def close(self) -> None:
        pass


@protected.post("/v1/chat")
async def chat(request: Request, body: ChatRequest) -> ChatResponse:
    """Text-only, non-streaming turn. Use /v1/realtime for streaming and voice."""
    runtime = _runtime(request)
    outbox = _CollectOutbox()
    session, _ = await runtime.open_session(
        outbox,
        session_id=body.session_id,
        device_id=body.device_id or "http",
        speak=False,
    )
    try:
        text = await session.run_turn(body.text.strip())
    finally:
        await runtime.close_session(session)
    if not text and outbox.errors:
        raise HTTPException(status_code=502, detail=outbox.errors[0])
    return ChatResponse(session_id=session.session_id, text=text, errors=outbox.errors)
