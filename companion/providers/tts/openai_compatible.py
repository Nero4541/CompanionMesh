"""TTS over the OpenAI ``POST /v1/audio/speech`` API.

Works with Irodori-TTS-Server, OpenAI, and other compatible servers.
Vendor-specific options go in ``tts.extra_body`` and are merged into the body.
"""

from __future__ import annotations

from typing import Any

import httpx

from companion.core.config import TTSConfig
from companion.core.errors import ProviderError
from companion.providers.http import describe_http_error, make_client, raise_for_status
from companion.providers.tts.base import SynthesizedAudio

_MIME = {
    "wav": "audio/wav",
    "mp3": "audio/mpeg",
    "opus": "audio/ogg",
    "flac": "audio/flac",
    "aac": "audio/aac",
    "pcm": "audio/pcm",
}


class OpenAICompatibleTTS:
    name = "openai_compatible_tts"

    def __init__(
        self, config: TTSConfig, *, transport: httpx.AsyncBaseTransport | None = None
    ) -> None:
        self._config = config
        self._client = make_client(
            config.base_url,
            api_key=config.api_key(),
            connect_timeout=config.connect_timeout,
            timeout=config.timeout,
            transport=transport,
        )

    async def synthesize(self, text: str, *, voice: str | None = None) -> SynthesizedAudio:
        cfg = self._config
        body: dict[str, Any] = {
            "model": cfg.model,
            "input": text,
            "response_format": cfg.response_format,
        }
        if voice or cfg.voice:
            body["voice"] = voice or cfg.voice
        if cfg.speed is not None:
            body["speed"] = cfg.speed
        body.update(cfg.extra_body)
        try:
            response = await self._client.post("audio/speech", json=body)
            await raise_for_status(response)
        except httpx.HTTPError as exc:
            raise describe_http_error(self.name, exc, cfg.base_url) from exc
        if not response.content:
            raise ProviderError(self.name, "empty audio response")
        mime = response.headers.get("content-type", "").split(";")[0].strip()
        if not mime.startswith("audio/"):
            mime = _MIME[cfg.response_format]
        return SynthesizedAudio(data=response.content, mime=mime)

    async def aclose(self) -> None:
        await self._client.aclose()
