"""Companion runtime: owns providers, storage and live sessions."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from dataclasses import dataclass, field
from typing import Any

from companion import __version__
from companion.agent.base import AgentBackend
from companion.audio.segmenter import UtteranceSegmenter
from companion.audio.vad import VAD, create_vad
from companion.core.bus import EventBus
from companion.core.config import CompanionConfig
from companion.core.logging import kv
from companion.core.persona import Persona, load_persona
from companion.core.session import Outbox, Session
from companion.memory.store import EphemeralStore, SQLiteStore, TranscriptStore
from companion.providers.stt.base import STTProvider
from companion.providers.tts.base import TTSProvider
from companion.providers.vlm.base import VLMProvider
from companion.vision.archive import ImageArchive

log = logging.getLogger(__name__)


@dataclass
class Providers:
    """Explicit provider instances; any left as ``None`` are built from config."""

    agent: AgentBackend | None = None
    stt: STTProvider | None = None
    tts: TTSProvider | None = None
    vlm: VLMProvider | None = None
    vad_factory: Any = None  # Callable[[], VAD]
    disabled: set[str] = field(default_factory=set)


def build_store(config: CompanionConfig) -> TranscriptStore:
    """External agents own the transcript and memory; only ``direct`` persists locally."""
    if config.agent.provider == "direct":
        return SQLiteStore(config.storage.data_dir / "companion.db")
    return EphemeralStore()


def build_agent(config: CompanionConfig, store: TranscriptStore) -> AgentBackend:
    if config.agent.provider == "hermes":
        from companion.agent.hermes import HermesBackend

        return HermesBackend(config.agent.hermes)
    if config.agent.provider == "openclaw":
        from companion.agent.openclaw import OpenClawBackend

        return OpenClawBackend(config.agent.openclaw)
    from companion.agent.direct import DirectLLMBackend
    from companion.providers.llm.openai_compatible import OpenAICompatibleLLM

    assert isinstance(store, SQLiteStore)
    return DirectLLMBackend(OpenAICompatibleLLM(config.llm), store)


def build_stt(config: CompanionConfig) -> STTProvider | None:
    if config.stt.provider == "none":
        return None
    from companion.providers.stt.faster_whisper import FasterWhisperSTT

    return FasterWhisperSTT(config.stt)


def build_tts(config: CompanionConfig) -> TTSProvider | None:
    if config.tts.provider == "none":
        return None
    from companion.providers.tts.openai_compatible import OpenAICompatibleTTS

    return OpenAICompatibleTTS(config.tts)


def build_vlm(config: CompanionConfig) -> VLMProvider | None:
    if config.vlm.provider == "none":
        return None
    from companion.providers.vlm.openai_compatible import OpenAICompatibleVLM

    return OpenAICompatibleVLM(config.vlm)


class CompanionRuntime:
    def __init__(self, config: CompanionConfig, providers: Providers | None = None) -> None:
        providers = providers or Providers()
        self.config = config
        self.bus = EventBus()
        self.store: TranscriptStore = build_store(config)
        self.persona: Persona = load_persona(config.personas_dir, config.agent.persona)
        self.agent: AgentBackend = providers.agent or build_agent(config, self.store)
        self.stt: STTProvider | None = (
            None if "stt" in providers.disabled else providers.stt or build_stt(config)
        )
        self.tts: TTSProvider | None = (
            None if "tts" in providers.disabled else providers.tts or build_tts(config)
        )
        self.vlm: VLMProvider | None = (
            None if "vlm" in providers.disabled else providers.vlm or build_vlm(config)
        )
        self.image_archive: ImageArchive | None = (
            ImageArchive(config.storage.data_dir / "vision", config.vision.retention_days)
            if config.vision.store_images
            else None
        )
        self._vad_factory = providers.vad_factory or (lambda: create_vad(config.vad))
        self.sessions: dict[str, Session] = {}
        self.started_at = time.time()

    async def start(self) -> None:
        self.store.open()
        await self.store.purge_observations(self.config.vision.retention_days)
        if self.image_archive is not None:
            await asyncio.to_thread(self.image_archive.purge)
        log.info(
            "runtime started",
            extra=kv(
                agent=self.agent.name,
                stt=getattr(self.stt, "name", None),
                tts=getattr(self.tts, "name", None),
                vlm=getattr(self.vlm, "name", None),
                images="stored" if self.image_archive else "not stored",
                transcripts=self.storage_description(),
            ),
        )
        if self.stt is not None and self.config.stt.preload:
            # Load in the background so the HTTP server comes up immediately.
            self._warmup = asyncio.create_task(self._warm_stt())

    async def _warm_stt(self) -> None:
        assert self.stt is not None
        try:
            await self.stt.warmup()
        except Exception as exc:
            log.error("stt warmup failed", extra=kv(error=str(exc)))

    async def stop(self) -> None:
        for session in list(self.sessions.values()):
            with contextlib.suppress(Exception):
                await session.close()
        self.sessions.clear()
        warmup = getattr(self, "_warmup", None)
        if warmup is not None:
            warmup.cancel()
        for provider in (self.agent, self.stt, self.tts, self.vlm):
            if provider is not None:
                with contextlib.suppress(Exception):
                    await provider.aclose()
        self.store.close()
        log.info("runtime stopped")

    def new_vad(self) -> VAD:
        return self._vad_factory()

    def new_segmenter(self) -> UtteranceSegmenter:
        return UtteranceSegmenter(self.new_vad(), self.config.vad)

    async def open_session(
        self,
        outbox: Outbox,
        *,
        session_id: str | None = None,
        device_id: str | None = None,
        speak: bool = True,
        register: bool = True,
    ) -> tuple[Session, bool]:
        sid, resumed = await self.store.ensure_session(session_id, device_id)
        previous = self.sessions.get(sid)
        if previous is not None and register:
            # Same session reconnecting (e.g. page reload): retire the old connection.
            await previous.close()
        session = Session(self, sid, outbox, device_id=device_id, speak=speak)
        if register:
            self.sessions[sid] = session
        return session, resumed

    async def close_session(self, session: Session) -> None:
        await session.close()
        current = self.sessions.get(session.session_id)
        if current is session:
            del self.sessions[session.session_id]
        if current is None or current is session:
            # Not persistent: the transcript lives only as long as its connection.
            await self.store.forget(session.session_id)

    def storage_description(self) -> str:
        if isinstance(self.store, SQLiteStore):
            return str(self.store.path)
        return f"in-memory only (owned by {self.config.agent.provider})"

    async def status(self, *, probe: bool = False) -> dict[str, Any]:
        out: dict[str, Any] = {
            "version": __version__,
            "uptime_s": round(time.time() - self.started_at, 1),
            "active_sessions": len(self.sessions),
            "stored_sessions": await self.store.session_count(),
            "transcripts": self.storage_description(),
            "persona": self.persona.name,
            "providers": {
                "agent": self.agent.name,
                "stt": getattr(self.stt, "name", None),
                "stt_device": getattr(self.stt, "device", None),
                "tts": getattr(self.tts, "name", None),
                "vlm": getattr(self.vlm, "name", None),
            },
            "vision": {
                "enabled": self.config.vision.enabled and self.vlm is not None,
                "store_images": self.image_archive is not None,
                "active_sessions": sum(s.vision.enabled for s in self.sessions.values()),
            },
        }
        if probe:
            out["agent_health"] = await self.agent.health()
        return out
