"""Shared client for agent frameworks that expose OpenAI Chat Completions.

Subclasses map a companion session onto the framework's own session so the
framework keeps the transcript and long-term memory on its host, which may
be a different machine than the companion server.
"""

from __future__ import annotations

import contextlib
import logging
from collections.abc import AsyncIterator
from typing import Any

import httpx

from companion.agent.base import AgentContext, AgentEvent, SessionRef
from companion.core.config import GatewayAgentConfig
from companion.core.errors import ProviderError
from companion.core.logging import kv
from companion.providers.http import (
    SSEEvent,
    describe_http_error,
    iter_sse,
    make_client,
    parse_json,
    raise_for_status,
)
from companion.providers.llm.base import ChatMessage

log = logging.getLogger(__name__)

# How an agent answers when its session no longer fits its model (OpenClaw: "Context overflow:
# prompt too large for the model. Try /reset ..."). The conversation then goes on in a new
# agent session; the old one stays with the agent.
CONTEXT_FULL = ("Context overflow:",)


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
        # Companion session -> how many times its agent session was started anew.
        self._renewed: dict[str, int] = {}

    # --- hooks ----------------------------------------------------------

    @property
    def session_continuity(self) -> bool:
        """Whether the agent keeps the session transcript itself."""
        return True

    def agent_session_id(self, session: SessionRef) -> str:
        renewed = self._renewed.get(session.session_id, 0)
        suffix = f"-{renewed}" if renewed else ""
        return f"{self._config.session_prefix}{session.session_id}{suffix}"

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
        full = False
        async with contextlib.aclosing(self._respond(session, messages, context)) as events:
            async for event in events:
                if event.kind == "context_full":
                    full = True
                    break
                yield event
        if full:  # once: a new session holds nothing but this turn
            self._renewed[session.session_id] = self._renewed.get(session.session_id, 0) + 1
            log.warning(
                "agent session full, going on in a new one",
                extra=kv(agent=self.name, session=self.agent_session_id(session)),
            )
            async for event in self._respond(session, messages, context, retry=True):
                yield event

    async def _respond(
        self,
        session: SessionRef,
        messages: list[ChatMessage],
        context: AgentContext,
        *,
        retry: bool = False,
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
        visible = False  # anything but blanks
        # The first words are held back until they cannot be the agent saying it is full.
        held = "" if not retry else None
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
                        if not text:
                            continue
                        produced = True
                        visible = visible or bool(text.strip())
                        if held is None:
                            yield AgentEvent(kind="text", text=text)
                            continue
                        held += text
                        if any(p.startswith(held) or held.startswith(p) for p in CONTEXT_FULL):
                            if any(held.startswith(p) for p in CONTEXT_FULL):
                                yield AgentEvent(kind="context_full", text=held)
                                return
                            continue  # still maybe the start of it
                        yield AgentEvent(kind="text", text=held)
                        held = None
        except httpx.HTTPError as exc:
            raise describe_http_error(self.name, exc, self._config.base_url) from exc
        if held:  # a short answer that looked like the start of it
            yield AgentEvent(kind="text", text=held)
        if produced and not visible:
            # Seen from OpenClaw when its session is busy with the turn before or too full.
            log.warning(
                "the agent answered only blanks",
                extra=kv(agent=self.name, session=self.agent_session_id(session)),
            )
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
