"""Shared client for agent frameworks that expose OpenAI Chat Completions.

Subclasses map a companion session onto the framework's own session so the
framework keeps the transcript and long-term memory on its host, which may
be a different machine than the companion server.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import httpx

from companion.agent.base import AgentContext, AgentEvent, SessionRef
from companion.core.config import GatewayAgentConfig
from companion.core.errors import ProviderError
from companion.providers.http import (
    SSEEvent,
    describe_http_error,
    iter_sse,
    make_client,
    parse_json,
    raise_for_status,
)
from companion.providers.llm.base import ChatMessage


class ChatCompletionsAgent:
    name = "gateway"
    # Shown when the agent finished without producing any text.
    empty_hint = "check the agent's logs (is a model provider configured?)"

    def __init__(
        self, config: GatewayAgentConfig, *, transport: httpx.AsyncBaseTransport | None = None
    ) -> None:
        self._config = config
        self._client = make_client(
            config.base_url,
            api_key=config.api_key(),
            connect_timeout=config.connect_timeout,
            timeout=config.timeout,
            transport=transport,
        )

    # --- hooks ----------------------------------------------------------

    @property
    def session_continuity(self) -> bool:
        """Whether the agent keeps the session transcript itself."""
        return True

    def agent_session_id(self, session: SessionRef) -> str:
        return f"{self._config.session_prefix}{session.session_id}"

    def request_headers(self, session: SessionRef) -> dict[str, str]:
        return {}

    def request_body(self, session: SessionRef) -> dict[str, Any]:
        return {"model": self._config.model}

    def custom_event(self, sse: SSEEvent) -> AgentEvent | None:
        """Translate a non-standard SSE event; ``None`` ignores it."""
        return None

    # --- AgentBackend ---------------------------------------------------

    def _messages(self, messages: list[ChatMessage], context: AgentContext) -> list[ChatMessage]:
        system: ChatMessage = {"role": "system", "content": context.system_prompt()}
        if self.session_continuity:
            # The agent loads the session history itself; send only the new turn.
            return [system, messages[-1]]
        return [system, *messages]

    async def respond(
        self, session: SessionRef, messages: list[ChatMessage], context: AgentContext
    ) -> AsyncIterator[AgentEvent]:
        if not messages or messages[-1]["role"] != "user":
            raise ValueError("messages must end with a user message")
        body = {
            **self._config.extra_body,
            **self.request_body(session),
            "stream": True,
            "messages": self._messages(messages, context),
        }
        produced = False
        try:
            async with self._client.stream(
                "POST", "chat/completions", json=body, headers=self.request_headers(session)
            ) as response:
                await raise_for_status(response)
                async for sse in iter_sse(response):
                    if sse.event != "message":
                        event = self.custom_event(sse)
                        if event is not None:
                            yield event
                        continue  # unknown events are ignored (forward compatibility)
                    if sse.data.strip() == "[DONE]":
                        break
                    chunk = parse_json(sse.data)
                    if not isinstance(chunk, dict):
                        continue
                    if "error" in chunk:
                        raise ProviderError(self.name, str(chunk["error"]))
                    for choice in chunk.get("choices") or []:
                        text = (choice.get("delta") or {}).get("content")
                        if text:
                            produced = True
                            yield AgentEvent(kind="text", text=text)
        except httpx.HTTPError as exc:
            raise describe_http_error(self.name, exc, self._config.base_url) from exc
        if not produced:
            raise ProviderError(
                self.name,
                f"the agent returned an empty response; {self.empty_hint}",
                code="agent_empty_response",
            )

    async def probe(self, url: str, *, reachable_only: bool = False) -> str:
        """GET ``url``; with ``reachable_only`` any HTTP response counts as ok."""
        try:
            response = await self._client.get(url, timeout=3.0)
            if not reachable_only:
                await raise_for_status(response)
        except httpx.HTTPError as exc:
            return str(describe_http_error(self.name, exc, url))
        return "ok"

    def root_url(self) -> str:
        return self._config.base_url.rstrip("/").removesuffix("/v1")

    async def health(self) -> str:
        return await self.probe(f"{self.root_url()}/health")

    async def aclose(self) -> None:
        await self._client.aclose()
