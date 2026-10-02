from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any, Literal, Protocol, TypedDict

# OpenAI content parts, e.g. {"type": "text", ...} or {"type": "image_url", ...}.
ContentPart = dict[str, Any]


class ChatMessage(TypedDict):
    role: Literal["system", "user", "assistant"]
    content: str | list[ContentPart]


def message_text(content: str | list[ContentPart]) -> str:
    """The text of a message, ignoring non-text parts such as images."""
    if isinstance(content, str):
        return content
    return "\n".join(str(p.get("text", "")) for p in content if p.get("type") == "text")


class LLMProvider(Protocol):
    name: str

    def chat_stream(self, messages: list[ChatMessage]) -> AsyncIterator[str]:
        """Yield response text deltas."""
        ...

    async def chat(self, messages: list[ChatMessage]) -> str: ...

    async def aclose(self) -> None: ...
