"""Agent backend that talks straight to an LLM provider (no agent framework).

Useful without Hermes and for development. Long-term recall is a small
full-text lookup over earlier conversations.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

from companion.agent.base import AgentContext, AgentEvent, SessionRef
from companion.memory.store import MemoryStore
from companion.providers.llm.base import ChatMessage, LLMProvider


class DirectLLMBackend:
    name = "direct"

    def __init__(self, llm: LLMProvider, memory: MemoryStore | None = None) -> None:
        self._llm = llm
        self._memory = memory

    async def _recall_block(self, session: SessionRef, query: str) -> str | None:
        if self._memory is None:
            return None
        hits = [
            m
            for m in await self._memory.recall(query, limit=6)
            if m.session_id != session.session_id
        ]
        if not hits:
            return None
        lines = "\n".join(f"- ({m.role}) {m.content}" for m in hits)
        return f"Possibly relevant excerpts from earlier conversations:\n{lines}"

    async def respond(
        self, session: SessionRef, messages: list[ChatMessage], context: AgentContext
    ) -> AsyncIterator[AgentEvent]:
        system = context.system_prompt()
        recall = await self._recall_block(session, messages[-1]["content"])
        if recall:
            system = f"{system}\n\n{recall}"
        prompt: list[ChatMessage] = [{"role": "system", "content": system}, *messages]
        async for delta in self._llm.chat_stream(prompt):
            yield AgentEvent(kind="text", text=delta)

    async def health(self) -> str:
        return "ok"

    async def aclose(self) -> None:
        await self._llm.aclose()
