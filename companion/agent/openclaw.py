"""OpenClaw adapter over the Gateway's OpenAI-compatible endpoint.

Requires ``gateway.http.endpoints.chatCompletions.enabled: true`` in the
OpenClaw config. The agent is chosen with ``model: "openclaw:<agentId>"``.
The OpenAI ``user`` field gives each companion session a stable OpenClaw
session, so OpenClaw keeps the transcript and memory on its own host.
"""

from __future__ import annotations

from typing import Any

import httpx

from companion.agent.base import SessionRef
from companion.agent.gateway import ChatCompletionsAgent
from companion.core.config import OpenClawConfig


class OpenClawBackend(ChatCompletionsAgent):
    name = "openclaw"
    empty_hint = "check the OpenClaw gateway logs (`openclaw logs`) and its model credentials"

    def __init__(
        self, config: OpenClawConfig, *, transport: httpx.AsyncBaseTransport | None = None
    ) -> None:
        super().__init__(config, transport=transport)
        self._openclaw = config

    def request_body(self, session: SessionRef) -> dict[str, Any]:
        model = self._openclaw.model
        if self._openclaw.agent_id and ":" not in model and "/" not in model:
            model = f"{model}:{self._openclaw.agent_id}"
        return {"model": model, "user": self.agent_session_id(session)}

    async def health(self) -> str:
        # The gateway serves its control UI at the root; any HTTP answer means
        # it is reachable. Authentication is verified on the first request.
        return await self.probe(f"{self.root_url()}/", reachable_only=True)
