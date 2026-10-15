"""What the server is doing, for the dashboard's live events page.

``AdminFeed`` listens to the event bus (every event a session sends, attention
decisions even without ``attention.debug``, and the runtime's own notices such
as devices connecting) and keeps the last few hundred for new viewers.

What people say is never part of it: transcripts, replies and conversation
history are reduced to their length, and no audio or images pass through the
bus. Descriptions of what a camera saw stay, since the attention decisions
built on them are what the page is for.
"""

from __future__ import annotations

import asyncio
import contextlib
import itertools
from collections import deque
from typing import Any

from companion.core.bus import EventBus
from companion.protocol import Envelope
from companion.protocol import events as ev

KEEP = 300  # events kept for viewers that open the page later
QUEUE = 500  # events buffered per viewer before the oldest are dropped

# Streamed reply text: one entry per token would drown everything else.
SKIP = frozenset({ev.RESPONSE_DELTA, ev.SYSTEM_PING})
# Fields that carry what people said; replaced by their length.
PRIVATE = ("text", "heard_text", "history")


def _private_free(payload: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in payload.items():
        if key in PRIVATE:
            if isinstance(value, str | list):
                out[f"{key}_length"] = len(value)
        else:
            out[key] = value
    return out


class AdminFeed:
    def __init__(self, bus: EventBus) -> None:
        self._recent: deque[dict[str, Any]] = deque(maxlen=KEEP)
        self._viewers: set[asyncio.Queue[dict[str, Any]]] = set()
        self._ids = itertools.count(1)
        bus.subscribe("", self._on_event)

    async def _on_event(self, event: Envelope) -> None:
        if event.type in SKIP:
            return
        # Decisions are published once by the attention engine (with the session
        # id) and again when attention.debug sends them to clients; keep one.
        if event.type == ev.ATTENTION_DECISION and "session_id" not in event.payload:
            return
        entry = {
            "id": next(self._ids),
            "timestamp": event.timestamp,
            "type": event.type,
            "session_id": event.session_id or event.payload.get("session_id"),
            "payload": _private_free(event.payload),
        }
        self._recent.append(entry)
        for queue in list(self._viewers):
            if queue.full():
                with contextlib.suppress(asyncio.QueueEmpty):
                    queue.get_nowait()
            queue.put_nowait(entry)

    def recent(self) -> list[dict[str, Any]]:
        return list(self._recent)

    def watch(self) -> asyncio.Queue[dict[str, Any]]:
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=QUEUE)
        self._viewers.add(queue)
        return queue

    def unwatch(self, queue: asyncio.Queue[dict[str, Any]]) -> None:
        self._viewers.discard(queue)
