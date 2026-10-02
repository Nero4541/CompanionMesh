from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

import numpy as np

SAMPLE_RATE = 16_000


@dataclass(slots=True)
class Transcript:
    text: str
    language: str | None = None
    duration_s: float = 0.0


class STTProvider(Protocol):
    name: str

    async def transcribe(self, audio: np.ndarray, *, language: str | None = None) -> Transcript:
        """Transcribe mono float32 PCM at 16 kHz."""
        ...

    async def warmup(self) -> None: ...

    async def aclose(self) -> None: ...
