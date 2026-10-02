from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(slots=True)
class SynthesizedAudio:
    data: bytes
    mime: str


class TTSProvider(Protocol):
    name: str

    async def synthesize(self, text: str, *, voice: str | None = None) -> SynthesizedAudio:
        """Synthesize one utterance (typically a sentence)."""
        ...

    async def aclose(self) -> None: ...
