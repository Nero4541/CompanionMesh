"""STT over the OpenAI ``POST /v1/audio/transcriptions`` API.

For speech recognition on another host: speaches (faster-whisper-server),
whisper.cpp's server, OpenAI, Groq and other compatible servers.
"""

from __future__ import annotations

import io
import wave
from typing import Any

import httpx
import numpy as np

from companion.audio.pcm import float32_to_pcm16
from companion.core.config import STTConfig
from companion.core.errors import ProviderError
from companion.providers.http import describe_http_error, make_client, raise_for_status
from companion.providers.stt.base import SAMPLE_RATE, Transcript


def to_wav(audio: np.ndarray) -> bytes:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(SAMPLE_RATE)
        out.writeframes(float32_to_pcm16(audio))
    return buffer.getvalue()


class OpenAICompatibleSTT:
    name = "openai_compatible_stt"

    def __init__(
        self, config: STTConfig, *, transport: httpx.AsyncBaseTransport | None = None
    ) -> None:
        self._config = config
        self._client = make_client(
            config.base_url,
            api_key=config.api_key(),
            connect_timeout=config.connect_timeout,
            timeout=config.timeout,
            transport=transport,
        )
        self._warm = False

    @property
    def device(self) -> str:
        return self._config.base_url

    async def warmup(self) -> None:
        # One tiny request: fails early when the server is down, and loads its model.
        if not self._warm:
            await self.transcribe(np.zeros(SAMPLE_RATE // 2, dtype=np.float32))
            self._warm = True

    async def transcribe(self, audio: np.ndarray, *, language: str | None = None) -> Transcript:
        cfg = self._config
        data: dict[str, Any] = {"model": cfg.model, "response_format": "json"}
        lang = language or cfg.language
        if lang:
            data["language"] = lang
        files = {"file": ("speech.wav", to_wav(audio), "audio/wav")}
        try:
            response = await self._client.post("audio/transcriptions", data=data, files=files)
            await raise_for_status(response)
        except httpx.HTTPError as exc:
            raise describe_http_error(self.name, exc, cfg.base_url) from exc
        try:
            body = response.json()
        except ValueError as exc:
            raise ProviderError(self.name, "transcription response is not JSON") from exc
        if not isinstance(body, dict) or not isinstance(body.get("text"), str):
            raise ProviderError(self.name, "transcription response has no text")
        duration = body.get("duration")
        return Transcript(
            text=body["text"].strip(),
            language=body.get("language") or lang,
            duration_s=(
                float(duration) if isinstance(duration, (int, float)) else len(audio) / SAMPLE_RATE
            ),
        )

    async def aclose(self) -> None:
        await self._client.aclose()
