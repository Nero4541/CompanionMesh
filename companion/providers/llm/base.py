from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Literal, Protocol, TypedDict


class ChatMessage(TypedDict):
    role: Literal["system", "user", "assistant"]
    content: str


class LLMProvider(Protocol):
    name: str

    def chat_stream(self, messages: list[ChatMessage]) -> AsyncIterator[str]:
        """Yield response text deltas."""
        ...

    async def chat(self, messages: list[ChatMessage]) -> str: ...

    async def aclose(self) -> None: ...
