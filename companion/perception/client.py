"""Talks to the built-in perception engine (builtin/perception) over HTTP on 127.0.0.1."""

from __future__ import annotations

import base64
import io
import wave
from dataclasses import dataclass, field
from typing import Any, Protocol

import httpx
import numpy as np

from companion.core.errors import ProviderError
from companion.perception.tracker import FaceObs
from companion.providers.http import describe_http_error


def from_b64(text: str) -> np.ndarray:
    return np.frombuffer(base64.b64decode(text), dtype="<f4").copy()


def wav_bytes(audio: np.ndarray, rate: int = 16000) -> bytes:
    pcm = (np.clip(audio, -1.0, 1.0) * 32767).astype("<i2")
    out = io.BytesIO()
    with wave.open(out, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(rate)
        wav.writeframes(pcm.tobytes())
    return out.getvalue()


@dataclass
class FrameFaces:
    width: int
    height: int
    faces: list[FaceObs] = field(default_factory=list)
    ms: float = 0.0


class PerceptionBackend(Protocol):
    """What the server needs from the engine (tests and replays pass their own)."""

    async def faces(self, jpeg: bytes) -> FrameFaces: ...

    async def voice(self, audio: np.ndarray) -> np.ndarray | None: ...

    async def voice_windows(
        self, audio: np.ndarray, window_s: float, hop_s: float
    ) -> np.ndarray | None: ...

    async def aclose(self) -> None: ...


def parse_faces(body: dict[str, Any]) -> FrameFaces:
    faces = [
        FaceObs(
            box=tuple(float(v) for v in f["box"]),  # type: ignore[arg-type]
            score=float(f.get("score", 0.0)),
            embedding=from_b64(f["embedding"]),
            mouth=f.get("mouth"),
            yaw=f.get("yaw"),
        )
        for f in body.get("faces", [])
    ]
    return FrameFaces(
        int(body.get("width", 0)), int(body.get("height", 0)), faces, body.get("ms", 0)
    )


class EngineClient:
    """The running engine at ``url`` (http://127.0.0.1:<port>)."""

    def __init__(self, url: str, *, timeout: float = 30.0) -> None:
        self.url = url.rstrip("/")
        self.voice_available: bool | None = None
        self.voice_error: str | None = None
        self._client = httpx.AsyncClient(timeout=httpx.Timeout(timeout, connect=5.0))

    async def health(self) -> dict[str, Any]:
        response = await self._client.get(f"{self.url}/health")
        response.raise_for_status()
        body = response.json()
        self.voice_available = bool(body.get("voice"))
        self.voice_error = body.get("voice_error")
        return body

    async def faces(self, jpeg: bytes) -> FrameFaces:
        try:
            response = await self._client.post(
                f"{self.url}/v1/faces", content=jpeg, headers={"Content-Type": "image/jpeg"}
            )
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise describe_http_error("perception", exc, self.url) from exc
        return parse_faces(response.json())

    async def _embed(self, audio: np.ndarray, params: dict[str, float]) -> np.ndarray | None:
        if self.voice_available is False:
            return None
        try:
            response = await self._client.post(
                f"{self.url}/v1/voice/embed",
                content=wav_bytes(audio),
                params=params,
                headers={"Content-Type": "audio/wav"},
            )
        except httpx.HTTPError as exc:
            raise describe_http_error("perception", exc, self.url) from exc
        if response.status_code == 503:
            self.voice_available = False
            self.voice_error = response.json().get("detail")
            return None
        if response.status_code >= 400:
            raise ProviderError("perception", f"voice: {response.text[:200]}")
        rows = [from_b64(e) for e in response.json()["embeddings"]]
        return np.vstack(rows) if rows else None

    async def voice(self, audio: np.ndarray) -> np.ndarray | None:
        rows = await self._embed(audio, {})
        return None if rows is None else rows[0]

    async def voice_windows(
        self, audio: np.ndarray, window_s: float, hop_s: float
    ) -> np.ndarray | None:
        return await self._embed(audio, {"window_s": window_s, "hop_s": hop_s})

    async def aclose(self) -> None:
        await self._client.aclose()
