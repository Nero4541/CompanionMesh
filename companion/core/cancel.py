"""Cancellation tokens: one per turn, shared by the agent stream and TTS.

Task cancellation stops the coroutine that owns a turn; the token carries
*why* (barge-in, user cancel, newer turn, device gone) to every component
working for that turn, including requests already in flight.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable
from typing import TypeVar

T = TypeVar("T")


class Cancelled(Exception):
    """The work was abandoned because its token was cancelled."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class CancelToken:
    def __init__(self) -> None:
        self._event = asyncio.Event()
        self.reason: str | None = None

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()

    def cancel(self, reason: str = "cancelled") -> bool:
        """Cancel once; returns False if it already was."""
        if self._event.is_set():
            return False
        self.reason = reason
        self._event.set()
        return True

    def raise_if_cancelled(self) -> None:
        if self.cancelled:
            raise Cancelled(self.reason or "cancelled")

    async def wait(self) -> str:
        await self._event.wait()
        return self.reason or "cancelled"

    async def run(self, awaitable: Awaitable[T]) -> T:
        """Await ``awaitable`` unless the token is cancelled first.

        On cancellation the awaitable is cancelled too (closing e.g. an HTTP
        request) and :class:`Cancelled` is raised.
        """
        self.raise_if_cancelled()
        work = asyncio.ensure_future(awaitable)
        waiter = asyncio.ensure_future(self._event.wait())
        try:
            await asyncio.wait({work, waiter}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            waiter.cancel()
        if work.done():
            return work.result()
        work.cancel()
        try:
            await work
        except (asyncio.CancelledError, Exception):
            pass
        raise Cancelled(self.reason or "cancelled")
