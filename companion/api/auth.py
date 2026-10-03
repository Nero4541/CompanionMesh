"""Device token authentication.

When ``server.auth_token_env`` names a set environment variable (by default
``COMPANION_TOKEN`` in ``.env``), every API call and realtime connection must
present that token. ``/health`` and the static dev client stay open.

Clients send it as ``Authorization: Bearer <token>`` or, where headers are not
available (browser WebSockets), as a ``?token=`` query parameter.
"""

from __future__ import annotations

import secrets

from fastapi import HTTPException, Request, WebSocket

from companion.core.config import ServerConfig

LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}


def presented_token(conn: Request | WebSocket) -> str | None:
    header = conn.headers.get("authorization", "")
    if header.lower().startswith("bearer "):
        return header[7:].strip() or None
    return conn.query_params.get("token") or None


def token_ok(config: ServerConfig, provided: str | None) -> bool:
    expected = config.auth_token()
    if expected is None:
        return True
    return provided is not None and secrets.compare_digest(provided, expected)


async def require_token(request: Request) -> None:
    """FastAPI dependency for protected HTTP routes."""
    config: ServerConfig = request.app.state.runtime.config.server
    if not token_ok(config, presented_token(request)):
        raise HTTPException(
            status_code=401,
            detail="missing or wrong token",
            headers={"WWW-Authenticate": "Bearer"},
        )


def exposure_problem(config: ServerConfig) -> str | None:
    """Refuse to listen beyond this machine without a token."""
    if config.host in LOOPBACK_HOSTS or config.auth_token():
        return None
    return (
        f"server.host is {config.host!r} but no token is set: put "
        f"{config.auth_token_env or 'COMPANION_TOKEN'}=<a long random string> in .env "
        "so that only your own devices can connect"
    )
