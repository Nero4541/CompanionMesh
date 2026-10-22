"""The agent's tools over MCP (POST /mcp) and as REST twins (POST /v1/tools/<name>).

MCP is the Model Context Protocol's streamable HTTP transport, answered with
plain JSON (no server-sent events, no server-initiated messages): ``initialize``,
``ping``, ``tools/list`` and ``tools/call``; notifications are accepted with 202.
Hermes (``mcp_servers`` in its config.yaml) and OpenClaw (``mcp.servers`` in
openclaw.json) connect to it directly; anything that cannot speak MCP calls
the same tools with ``POST /v1/tools/<name>`` and a JSON object of arguments.

Both need ``mcp.enabled`` and the token in ``mcp.token_env`` (Authorization:
Bearer); while that is not set, only this machine is let in.
"""

from __future__ import annotations

import json
import logging
import os
import secrets
import uuid
from typing import Any

from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import JSONResponse

from companion import __version__
from companion.agent.tools import ToolError, ToolResult, image_b64
from companion.api.auth import from_this_machine, presented_token
from companion.core.logging import kv

log = logging.getLogger(__name__)

router = APIRouter()

PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")
INSTRUCTIONS = (
    "Tools of the companion server: who is around (by name, user or acquaintance or stranger), "
    "what they have been doing, what was said and by whom, a look through a camera, remembering "
    "people (only after the user agreed) and watches on long activities. Never tell strangers "
    "about the user's private life."
)


def _check(request: Request) -> None:
    runtime = request.app.state.runtime
    cfg = runtime.config.mcp
    if not cfg.enabled:
        raise HTTPException(status_code=404, detail="the agent tools are off (mcp.enabled)")
    expected = os.environ.get(cfg.token_env) if cfg.token_env else None
    if not expected:
        if from_this_machine(request):
            return
        raise HTTPException(
            status_code=403,
            detail=f"set {cfg.token_env or 'COMPANION_AGENT_TOKEN'} in .env to use the tools "
            "from another machine",
        )
    provided = presented_token(request)
    if provided is None or not secrets.compare_digest(provided, expected):
        raise HTTPException(
            status_code=401, detail="missing or wrong token", headers={"WWW-Authenticate": "Bearer"}
        )


def _content(result: ToolResult) -> list[dict[str, Any]]:
    content: list[dict[str, Any]] = [
        {"type": "text", "text": json.dumps(result.data, ensure_ascii=False)}
    ]
    if result.image is not None:
        content.append({"type": "image", "data": image_b64(result), "mimeType": "image/jpeg"})
    return content


async def _handle(request: Request, message: Any) -> dict[str, Any] | None:
    """One JSON-RPC message; None for notifications (no answer)."""
    if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
        return _error(None, -32600, "not a JSON-RPC 2.0 message")
    method = message.get("method")
    ident = message.get("id")
    params = message.get("params") or {}
    if ident is None:  # a notification (initialized, cancelled, ...)
        return None
    tools = request.app.state.runtime.tools
    if method == "initialize":
        asked = str(params.get("protocolVersion") or "")
        version = asked if asked in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0]
        return _result(
            ident,
            {
                "protocolVersion": version,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": "companion", "version": __version__},
                "instructions": INSTRUCTIONS,
            },
        )
    if method == "ping":
        return _result(ident, {})
    if method == "tools/list":
        return _result(ident, {"tools": tools.describe()})
    if method == "tools/call":
        name = str(params.get("name") or "")
        try:
            result = await tools.call(name, params.get("arguments"))
        except ToolError as exc:
            log.info("agent tool refused", extra=kv(tool=name, error=str(exc)))
            return _result(
                ident, {"content": [{"type": "text", "text": str(exc)}], "isError": True}
            )
        log.info("agent tool", extra=kv(tool=name))
        return _result(
            ident, {"content": _content(result), "structuredContent": result.data, "isError": False}
        )
    return _error(ident, -32601, f"method not found: {method}")


def _result(ident: Any, result: dict[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": ident, "result": result}


def _error(ident: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": ident, "error": {"code": code, "message": message}}


@router.post("/mcp")
async def mcp(request: Request) -> Response:
    _check(request)
    try:
        body = json.loads(await request.body() or b"null")
    except json.JSONDecodeError:
        return JSONResponse(_error(None, -32700, "parse error"), status_code=400)
    headers = {}
    if isinstance(body, dict) and body.get("method") == "initialize":
        headers["Mcp-Session-Id"] = uuid.uuid4().hex
    if isinstance(body, list):
        answers = [a for a in [await _handle(request, m) for m in body] if a is not None]
        if not answers:
            return Response(status_code=202)
        return JSONResponse(answers, headers=headers)
    answer = await _handle(request, body)
    if answer is None:
        return Response(status_code=202)
    return JSONResponse(answer, headers=headers)


@router.get("/mcp")
async def mcp_stream(request: Request) -> Response:
    """No server-initiated messages: there is no stream to open."""
    _check(request)
    return Response(status_code=405, headers={"Allow": "POST"})


@router.delete("/mcp")
async def mcp_end(request: Request) -> Response:
    _check(request)
    return Response(status_code=204)


@router.get("/v1/tools")
async def list_tools(request: Request) -> dict[str, Any]:
    _check(request)
    return {"tools": request.app.state.runtime.tools.describe()}


@router.post("/v1/tools/{name}")
async def call_tool(request: Request, name: str) -> dict[str, Any]:
    """A tool by its name, with its arguments as a JSON object: the REST twin of tools/call."""
    _check(request)
    raw = await request.body()
    try:
        args = json.loads(raw) if raw.strip() else {}
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail="send the arguments as a JSON object") from exc
    if not isinstance(args, dict):
        raise HTTPException(status_code=400, detail="send the arguments as a JSON object")
    tools = request.app.state.runtime.tools
    if name not in tools.tools:
        raise HTTPException(status_code=404, detail=f"no tool {name!r}")
    try:
        result = await tools.call(name, args)
    except ToolError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    out: dict[str, Any] = {"result": result.data}
    if result.image is not None:
        out["image_base64"] = image_b64(result)
    return out
