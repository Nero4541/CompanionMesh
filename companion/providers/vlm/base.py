from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol


@dataclass(slots=True)
class ImageDescription:
    description: str
    tags: list[str] = field(default_factory=list)
    confidence: float | None = None


class VLMProvider(Protocol):
    name: str

    async def describe(self, jpeg: bytes, *, language: str | None = None) -> ImageDescription:
        """Describe one JPEG image."""
        ...

    async def aclose(self) -> None: ...
