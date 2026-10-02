"""Hermes Agent adapter over its OpenAI-compatible API server.

Hermes owns persistent memory. Each companion session maps to a Hermes
session via ``X-Hermes-Session-Id`` so Hermes reloads its own history (tool
calls included) after either side restarts; ``X-Hermes-Session-Key`` scopes
long-term memory across sessions. Both headers require ``API_SERVER_KEY``.
"""

from __future__ import annotations

import httpx

from companion.agent.base import AgentEvent, SessionRef
from companion.agent.gateway import ChatCompletionsAgent
from companion.core.config import HermesConfig
from companion.providers.http import SSEEvent, parse_json

TOOL_PROGRESS_EVENT = "hermes.tool.progress"


class HermesBackend(ChatCompletionsAgent):
    name = "hermes"
    empty_hint = (
        "check ~/.hermes/logs/errors.log (is an inference provider configured? run `hermes model`)"
    )

    def __init__(
        self, config: HermesConfig, *, transport: httpx.AsyncBaseTransport | None = None
    ) -> None:
        super().__init__(config, transport=transport)
        self._hermes = config

    @property
    def session_continuity(self) -> bool:
        # Hermes only honours session headers on authenticated requests.
        return bool(self._hermes.api_key())

    def request_headers(self, session: SessionRef) -> dict[str, str]:
        if not self.session_continuity:
            return {}
        headers = {"X-Hermes-Session-Id": self.agent_session_id(session)}
        if self._hermes.memory_scope:
            headers["X-Hermes-Session-Key"] = self._hermes.memory_scope
        return headers

    def custom_event(self, sse: SSEEvent) -> AgentEvent | None:
        if sse.event != TOOL_PROGRESS_EVENT:
            return None
        data = parse_json(sse.data)
        if not isinstance(data, dict):
            return None
        return AgentEvent(
            kind="tool",
            tool=str(data.get("tool") or ""),
            text=str(data.get("label") or ""),
            status=str(data.get("status") or "running"),
        )

    async def health(self) -> str:
        status = await super().health()
        if status == "ok" and not self.session_continuity:
            return f"ok (no API key in ${self._hermes.api_key_env}: session continuity disabled)"
        return status
