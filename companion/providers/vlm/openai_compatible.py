"""VLM over OpenAI-compatible chat completions with an inline image.

Works with llama.cpp (with a multimodal projector), vLLM, LM Studio, Ollama
and cloud APIs that accept ``image_url`` content parts.
"""

from __future__ import annotations

import base64
import json
import re
from typing import Any

import httpx

from companion.core.config import VLMConfig
from companion.core.errors import ProviderError
from companion.providers.http import describe_http_error, make_client, raise_for_status
from companion.providers.vlm.base import ImageDescription

DEFAULT_PROMPT = (
    "You are the eyes of a voice companion. Describe what the camera sees in one or two "
    "short sentences, focusing on people's activity, notable objects and changes a friend "
    "would comment on. Do not identify people by name or guess their identity. "
    "Write the description in {language}.\n"
    'Reply with JSON only: {{"description": "...", "tags": ["..."], "confidence": 0.0-1.0}}'
)

_LANGUAGES = {"ja": "Japanese", "en": "English", "zh": "Chinese", "yue": "Cantonese"}
_JSON_BLOCK = re.compile(r"\{.*\}", re.S)


def parse_description(text: str) -> ImageDescription:
    """Parse the model's JSON reply; fall back to the raw text."""
    text = text.strip()
    match = _JSON_BLOCK.search(text)
    if match:
        try:
            data = json.loads(match.group(0))
        except json.JSONDecodeError:
            data = None
        if isinstance(data, dict) and isinstance(data.get("description"), str):
            tags = data.get("tags")
            confidence = data.get("confidence")
            return ImageDescription(
                description=data["description"].strip(),
                tags=[str(t) for t in tags][:8] if isinstance(tags, list) else [],
                confidence=(
                    max(0.0, min(1.0, float(confidence)))
                    if isinstance(confidence, (int, float))
                    else None
                ),
            )
    cleaned = re.sub(r"^```\w*|```$", "", text).strip()
    return ImageDescription(description=cleaned)


class OpenAICompatibleVLM:
    name = "openai_compatible_vlm"

    def __init__(
        self, config: VLMConfig, *, transport: httpx.AsyncBaseTransport | None = None
    ) -> None:
        self._config = config
        self._client = make_client(
            config.base_url,
            api_key=config.api_key(),
            connect_timeout=config.connect_timeout,
            timeout=config.timeout,
            transport=transport,
        )

    def _prompt(self, language: str | None) -> str:
        lang = _LANGUAGES.get(language or "", language or "English")
        return (self._config.prompt or DEFAULT_PROMPT).format(language=lang)

    async def describe(self, jpeg: bytes, *, language: str | None = None) -> ImageDescription:
        data_uri = "data:image/jpeg;base64," + base64.b64encode(jpeg).decode("ascii")
        body: dict[str, Any] = {
            **self._config.extra_body,
            "model": self._config.model,
            "stream": False,
            "max_tokens": self._config.max_tokens,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": self._prompt(language)},
                        {"type": "image_url", "image_url": {"url": data_uri}},
                    ],
                }
            ],
        }
        try:
            response = await self._client.post("chat/completions", json=body)
            await raise_for_status(response)
            data = response.json()
        except httpx.HTTPError as exc:
            raise describe_http_error(self.name, exc, self._config.base_url) from exc
        try:
            content = data["choices"][0]["message"].get("content") or ""
        except (KeyError, IndexError, TypeError) as exc:
            raise ProviderError(self.name, "unexpected response shape") from exc
        result = parse_description(content)
        if not result.description:
            raise ProviderError(self.name, "empty description", code="vlm_empty_response")
        return result

    async def aclose(self) -> None:
        await self._client.aclose()
