"""In-process event bus for observers (logging, metrics, future attention engine)."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable

from companion.protocol import Envelope

log = logging.getLogger(__name__)

Handler = Callable[[Envelope], Awaitable[None]]


class EventBus:
    def __init__(self) -> None:
        self._subs: list[tuple[str, Handler]] = []

    def subscribe(self, prefix: str, handler: Handler) -> Callable[[], None]:
        """Subscribe to events whose type starts with ``prefix`` ("" = all)."""
        entry = (prefix, handler)
        self._subs.append(entry)
        return lambda: self._subs.remove(entry) if entry in self._subs else None

    async def publish(self, event: Envelope) -> None:
        for prefix, handler in list(self._subs):
            if event.type.startswith(prefix):
                try:
                    await handler(event)
                except Exception:
                    log.exception("event bus handler failed")
