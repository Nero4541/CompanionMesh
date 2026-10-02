from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Literal, Protocol

from companion.core.persona import Persona
from companion.providers.llm.base import ChatMessage


@dataclass(slots=True, frozen=True)
class SessionRef:
    session_id: str
    device_id: str | None = None


@dataclass(slots=True)
class AgentContext:
    persona: Persona
    turn_id: str
    extra_system: list[str] = field(default_factory=list)

    def system_prompt(self) -> str:
        return "\n\n".join([self.persona.system_prompt.strip(), *self.extra_system])


@dataclass(slots=True)
class AgentEvent:
    kind: Literal["text", "tool"]
    text: str = ""
    tool: str | None = None
    status: str | None = None


class AgentBackend(Protocol):
    name: str

    def respond(
        self, session: SessionRef, messages: list[ChatMessage], context: AgentContext
    ) -> AsyncIterator[AgentEvent]:
        """Stream the reply to ``messages`` (conversation so far, ending with the user turn).

        Cancelling the consuming task must abort the underlying request.
        """
        ...

    async def health(self) -> str:
        """Return "ok" or a short reason the backend is unavailable."""
        ...

    async def aclose(self) -> None: ...
