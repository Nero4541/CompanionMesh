"""Error types surfaced to clients as ``system.error`` events."""

from __future__ import annotations


class CompanionError(Exception):
    """Base error with a stable machine-readable code."""

    code = "internal_error"
    recoverable = True

    def __init__(self, message: str, *, code: str | None = None) -> None:
        super().__init__(message)
        if code:
            self.code = code


class ProviderError(CompanionError):
    """An external provider (agent, LLM, STT, TTS) failed."""

    code = "provider_error"

    def __init__(self, provider: str, message: str, *, code: str | None = None) -> None:
        super().__init__(f"{provider}: {message}", code=code)
        self.provider = provider


class ProviderUnavailable(ProviderError):
    """The provider could not be reached at all (not running, wrong URL, firewall)."""

    code = "provider_unavailable"
