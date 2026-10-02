from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import httpx

from companion.core.config import LLMConfig
from companion.providers.http import (
    describe_http_error,
    iter_sse,
    make_client,
    parse_json,
    raise_for_status,
)
from companion.providers.llm.base import ChatMessage


class OpenAICompatibleLLM:
    """Chat Completions client for any OpenAI-compatible server."""

    name = "openai_compatible"

    def __init__(
        self, config: LLMConfig, *, transport: httpx.AsyncBaseTransport | None = None
    ) -> None:
        self._config = config
        self._client = make_client(
            config.base_url,
            api_key=config.api_key(),
            connect_timeout=config.connect_timeout,
            timeout=config.timeout,
            transport=transport,
        )

    def _body(self, messages: list[ChatMessage], stream: bool) -> dict[str, Any]:
        body: dict[str, Any] = {
            **self._config.extra_body,
            "model": self._config.model,
            "messages": messages,
            "stream": stream,
        }
        if self._config.temperature is not None:
            body["temperature"] = self._config.temperature
        if self._config.max_tokens is not None:
            body["max_tokens"] = self._config.max_tokens
        return body

    async def chat(self, messages: list[ChatMessage]) -> str:
        try:
            response = await self._client.post("chat/completions", json=self._body(messages, False))
            await raise_for_status(response)
            data = response.json()
        except httpx.HTTPError as exc:
            raise describe_http_error(self.name, exc, self._config.base_url) from exc
        return data["choices"][0]["message"].get("content") or ""

    async def chat_stream(self, messages: list[ChatMessage]) -> AsyncIterator[str]:
        try:
            async with self._client.stream(
                "POST", "chat/completions", json=self._body(messages, True)
            ) as response:
                await raise_for_status(response)
                async for sse in iter_sse(response):
                    if sse.data.strip() == "[DONE]":
                        break
                    chunk = parse_json(sse.data)
                    if not isinstance(chunk, dict):
                        continue
                    for choice in chunk.get("choices") or []:
                        text = (choice.get("delta") or {}).get("content")
                        if text:
                            yield text
        except httpx.HTTPError as exc:
            raise describe_http_error(self.name, exc, self._config.base_url) from exc

    async def aclose(self) -> None:
        await self._client.aclose()
