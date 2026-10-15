"""The public listener (``server.public``): what devices outside this network may reach.

Only ``/health`` and ``/v1/realtime``. Compared with the main listener it is
stricter, because anyone on the Internet can knock:

- only enrolled devices' own tokens (``companion edge install``), never the
  shared ``COMPANION_TOKEN``, and only in the ``Authorization`` header (a
  token in a URL ends up in logs);
- an address that fails to authenticate ``max_failed_auth`` times within
  ``ban_s`` is refused until that time has passed;
- at most ``max_connections_per_address`` connections per address.

It shares the runtime (sessions, devices) with the main listener.
"""

from __future__ import annotations

import logging
import time
from collections import defaultdict, deque

from fastapi import FastAPI, WebSocket
from fastapi.responses import JSONResponse

from companion import __version__
from companion.api import realtime
from companion.api.auth import Identity
from companion.core.config import PublicListenerConfig
from companion.core.logging import kv
from companion.core.runtime import CompanionRuntime

log = logging.getLogger(__name__)


class Guard:
    """Failed logins and open connections per client address."""

    def __init__(self, config: PublicListenerConfig) -> None:
        self.config = config
        self._failures: dict[str, deque[float]] = defaultdict(deque)
        self._banned: dict[str, float] = {}
        self.open: dict[str, int] = defaultdict(int)

    def _prune(self, address: str, now: float) -> deque[float]:
        failures = self._failures[address]
        while failures and failures[0] < now - self.config.ban_s:
            failures.popleft()
        return failures

    def banned(self, address: str) -> bool:
        until = self._banned.get(address)
        if until is None:
            return False
        if until > time.monotonic():
            return True
        del self._banned[address]
        self._failures.pop(address, None)
        return False

    def failed(self, address: str) -> None:
        now = time.monotonic()
        failures = self._prune(address, now)
        failures.append(now)
        if len(failures) >= self.config.max_failed_auth:
            self._banned[address] = now + self.config.ban_s
            log.warning(
                "address refused after failed logins",
                extra=kv(address=address, failures=len(failures), for_s=self.config.ban_s),
            )

    def too_many(self, address: str) -> bool:
        return self.open[address] >= self.config.max_connections_per_address


def client_address(ws: WebSocket, config: PublicListenerConfig) -> str:
    if config.trusted_proxy:
        forwarded = ws.headers.get("cf-connecting-ip") or ws.headers.get("x-forwarded-for", "")
        if forwarded:
            return forwarded.split(",")[0].strip()
    return ws.client.host if ws.client else "?"


def header_token(ws: WebSocket) -> str | None:
    header = ws.headers.get("authorization", "")
    if header.lower().startswith("bearer "):
        return header[7:].strip() or None
    return None


def create_public_app(runtime: CompanionRuntime) -> FastAPI:
    config = runtime.config.server.public
    app = FastAPI(
        title="Companion Server (devices)",
        version=__version__,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.runtime = runtime
    guard = Guard(config)
    app.state.guard = guard

    @app.get("/health")
    async def health() -> JSONResponse:
        return JSONResponse({"status": "ok"})

    @app.websocket("/v1/realtime")
    async def device_realtime(ws: WebSocket) -> None:
        address = client_address(ws, config)
        if guard.banned(address):
            await ws.close(code=1008, reason="too many failed logins; try again later")
            return
        enrolled = runtime.devices.identify(header_token(ws))
        if enrolled is None:
            guard.failed(address)
            log.info("public login refused", extra=kv(address=address))
            await ws.close(code=1008, reason="missing or wrong device token")
            return
        if guard.too_many(address):
            await ws.close(code=1013, reason="too many connections from this address")
            return
        guard.open[address] += 1
        try:
            await realtime.serve(ws, Identity(enrolled.device_id))
        finally:
            guard.open[address] -= 1
            if guard.open[address] <= 0:
                del guard.open[address]

    return app
