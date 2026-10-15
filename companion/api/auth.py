"""Device token authentication.

When ``server.auth_token_env`` names a set environment variable (by default
``COMPANION_TOKEN`` in ``.env``), every API call and realtime connection must
present that token. ``/health`` and the static dev client stay open.

Clients send it as ``Authorization: Bearer <token>`` or, where headers are not
available (browser WebSockets), as a ``?token=`` query parameter.

Devices enrolled by the installer have tokens of their own
(:mod:`companion.devices.registry`), accepted wherever the shared token is and
bound to their device id.

The admin API has its own token (``server.admin_token_env``), so a device
token cannot change the server. Without it, only this machine is admitted.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass

from fastapi import HTTPException, Request, WebSocket

from companion.core.config import ServerConfig
from companion.devices.registry import DeviceRegistry

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


@dataclass(frozen=True)
class Identity:
    """Who connected: an enrolled device (bound to its id), or anyone with the
    shared token (``device_id`` None)."""

    device_id: str | None = None


def authenticate(
    config: ServerConfig, registry: DeviceRegistry, conn: Request | WebSocket
) -> Identity | None:
    token = presented_token(conn)
    enrolled = registry.identify(token)
    if enrolled is not None:
        return Identity(enrolled.device_id)
    if token_ok(config, token):
        return Identity()
    return None


async def require_token(request: Request) -> None:
    """FastAPI dependency for protected HTTP routes."""
    runtime = request.app.state.runtime
    if authenticate(runtime.config.server, runtime.devices, request) is None:
        raise HTTPException(
            status_code=401,
            detail="missing or wrong token",
            headers={"WWW-Authenticate": "Bearer"},
        )


def _hostname(host_header: str) -> str:
    if host_header.startswith("["):
        return host_header[1 : host_header.find("]")]
    return host_header.rsplit(":", 1)[0]


def from_this_machine(conn: Request | WebSocket) -> bool:
    """A loopback peer that also addressed us by a loopback name.

    Checking the Host header as well keeps a web page that rebinds its own
    domain name to 127.0.0.1 from reading the admin API through the browser.
    """
    peer = conn.client.host if conn.client else ""
    return peer in LOOPBACK_HOSTS and _hostname(conn.headers.get("host", "")) in LOOPBACK_HOSTS


def admin_refusal(conn: Request | WebSocket) -> tuple[int, str] | None:
    """Why ``conn`` may not use the admin API, as (HTTP status, reason); None if it may."""
    config: ServerConfig = conn.app.state.runtime.config.server
    expected = config.admin_token()
    if expected is None:
        if from_this_machine(conn):
            return None
        return (
            403,
            "the admin API only answers this machine until "
            f"{config.admin_token_env or 'COMPANION_ADMIN_TOKEN'} is set in .env",
        )
    provided = presented_token(conn)
    if provided is None or not secrets.compare_digest(provided, expected):
        return 401, "missing or wrong admin token"
    return None


async def require_admin(request: Request) -> None:
    """FastAPI dependency for the admin API (/v1/admin/*)."""
    refusal = admin_refusal(request)
    if refusal is not None:
        status, detail = refusal
        headers = {"WWW-Authenticate": "Bearer"} if status == 401 else None
        raise HTTPException(status_code=status, detail=detail, headers=headers)


def exposure_problem(config: ServerConfig) -> str | None:
    """Refuse to listen beyond this machine without a token."""
    if config.host in LOOPBACK_HOSTS or config.auth_token():
        return None
    return (
        f"server.host is {config.host!r} but no token is set: put "
        f"{config.auth_token_env or 'COMPANION_TOKEN'}=<a long random string> in .env "
        "so that only your own devices can connect"
    )
