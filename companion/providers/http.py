"""Shared helpers for HTTP-based providers."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

import httpx

from companion.core.errors import ProviderError, ProviderUnavailable


def make_client(
    base_url: str,
    *,
    api_key: str | None,
    connect_timeout: float,
    timeout: float,
    transport: httpx.AsyncBaseTransport | None = None,
) -> httpx.AsyncClient:
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    return httpx.AsyncClient(
        base_url=base_url.rstrip("/") + "/",
        headers=headers,
        timeout=httpx.Timeout(timeout, connect=connect_timeout),
        transport=transport,
    )


def describe_http_error(provider: str, exc: Exception, base_url: str) -> ProviderError:
    if isinstance(exc, httpx.ConnectError):
        return ProviderUnavailable(
            provider,
            f"cannot connect to {base_url} ({exc}). Is the service running, is the URL "
            "correct, and is it allowed through the local firewall?",
        )
    if isinstance(exc, httpx.TimeoutException):
        return ProviderError(provider, f"request to {base_url} timed out", code="provider_timeout")
    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
        detail = _error_detail(exc.response)
        if status in (401, 403):
            return ProviderError(
                provider, f"authentication failed ({status}): {detail}", code="provider_auth"
            )
        return ProviderError(provider, f"HTTP {status}: {detail}")
    if isinstance(exc, (httpx.RemoteProtocolError, httpx.ReadError, httpx.WriteError)):
        return ProviderUnavailable(
            provider,
            f"connection to {base_url} was lost ({type(exc).__name__}: {exc or 'closed'})",
            code="provider_disconnected",
        )
    if isinstance(exc, httpx.HTTPError):
        return ProviderError(provider, f"HTTP error: {type(exc).__name__}: {exc}")
    return ProviderError(provider, str(exc))


def _error_detail(response: httpx.Response) -> str:
    try:
        body = response.json()
    except (json.JSONDecodeError, httpx.ResponseNotRead, UnicodeDecodeError):
        try:
            return response.text[:300]
        except httpx.ResponseNotRead:
            return response.reason_phrase
    if isinstance(body, dict):
        err = body.get("error", body.get("detail", body))
        if isinstance(err, dict):
            return str(err.get("message", err))[:300]
        return str(err)[:300]
    return str(body)[:300]


async def raise_for_status(response: httpx.Response) -> None:
    if response.is_error:
        await response.aread()
        response.raise_for_status()


@dataclass(slots=True)
class SSEEvent:
    event: str
    data: str


async def iter_sse(response: httpx.Response) -> AsyncIterator[SSEEvent]:
    """Minimal Server-Sent Events parser (event + data fields)."""
    event = "message"
    data: list[str] = []
    async for line in response.aiter_lines():
        if line == "":
            if data:
                yield SSEEvent(event, "\n".join(data))
            event, data = "message", []
            continue
        if line.startswith(":"):
            continue
        field, _, value = line.partition(":")
        value = value.removeprefix(" ")
        if field == "event":
            event = value
        elif field == "data":
            data.append(value)
    if data:
        yield SSEEvent(event, "\n".join(data))


def parse_json(data: str) -> Any:
    try:
        return json.loads(data)
    except json.JSONDecodeError:
        return None
