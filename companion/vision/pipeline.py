"""Per-session vision: opt-in state, frame filtering, and hand-off to the agent.

Flow for a frame::

    vision.frame -> enabled? -> rate limit -> decode/validate -> dedup -> ...

    mode "agent": keep the newest frame; attach it as an image to the user's
                  next message (once), so a multimodal agent sees it directly.
    mode "vlm":   latest-wins slot -> VLM -> VisualObservation
                  -> vision.observation event, episodic store, turn context

Disabling vision cancels any analysis in flight and discards held frames.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Literal

from companion.core.errors import CompanionError
from companion.core.logging import kv
from companion.protocol import Envelope
from companion.protocol import events as ev
from companion.vision.image import ImageRejected, decode_image, dhash, hamming, to_jpeg
from companion.vision.observation import VisualObservation, format_for_context

if TYPE_CHECKING:
    from companion.core.session import Session

log = logging.getLogger(__name__)

FrameStatus = Literal["accepted", "duplicate", "rate_limited", "rejected", "disabled"]
MANUAL_REASONS = {"manual", "question"}


@dataclass(slots=True)
class _Pending:
    jpeg: bytes
    device_id: str
    frame_id: str
    received_at: datetime


@dataclass(slots=True)
class TurnImage:
    jpeg: bytes
    frame_id: str
    device_id: str
    age_s: float


@dataclass(slots=True)
class TurnVision:
    """What vision contributes to one agent turn."""

    user_prefix: str | None = None
    system_block: str | None = None
    image: TurnImage | None = None


class VisionChannel:
    def __init__(self, session: Session) -> None:
        self.session = session
        self.runtime = session.runtime
        self.config = session.runtime.config.vision
        self.mode = self.config.mode
        self.enabled = False
        self._latest: _Pending | None = None  # agent mode: newest unseen frame
        self._last_accept = 0.0
        self._last_hash: int | None = None
        self._pending: _Pending | None = None
        self._wake = asyncio.Event()
        self._idle = asyncio.Event()
        self._idle.set()
        self._worker: asyncio.Task[None] | None = None
        self._recent: list[VisualObservation] = []
        self._injected: set[str] = set()

    # --- state ----------------------------------------------------------

    @property
    def available(self) -> bool:
        if self.mode == "agent":
            return self.config.enabled
        return self.config.enabled and self.runtime.vlm is not None

    async def _emit_state(self) -> None:
        await self.session.emit(
            ev.VISION_STATE, {"enabled": self.enabled, "available": self.available}
        )

    async def enable(self) -> None:
        if not self.config.enabled:
            await self.session.emit_error("vision is disabled on this server", code="vision_off")
        elif self.mode == "vlm" and self.runtime.vlm is None:
            await self.session.emit_error(
                "no vision model is configured (vlm.provider: none)", code="vision_unavailable"
            )
        else:
            self.enabled = True
            log.info("vision enabled", extra=kv(session=self.session.session_id))
        await self._emit_state()

    async def disable(self) -> None:
        """Stop all visual processing immediately."""
        was_enabled, self.enabled = self.enabled, False
        self._pending = None
        self._latest = None
        await self._stop_worker()
        self._last_hash = None
        if was_enabled:
            log.info("vision disabled", extra=kv(session=self.session.session_id))
        await self._emit_state()

    # --- frames ---------------------------------------------------------

    async def _status(self, frame_id: str, status: FrameStatus, detail: str = "") -> FrameStatus:
        payload: dict[str, Any] = {"frame_id": frame_id, "status": status}
        if detail:
            payload["detail"] = detail
        await self.session.emit(ev.VISION_FRAME_STATUS, payload)
        return status

    async def submit_frame(self, header: Envelope, data: bytes) -> FrameStatus:
        payload = header.payload
        frame_id = str(payload.get("frame_id") or header.id)
        if not self.enabled:
            return await self._status(frame_id, "disabled")
        reason = str(payload.get("reason") or "periodic")
        manual = reason in MANUAL_REASONS
        now = time.monotonic()
        if not manual and now - self._last_accept < self.config.min_interval_s:
            return await self._status(frame_id, "rate_limited")
        mime = str(payload.get("mime") or "")
        try:
            decoded = await asyncio.to_thread(
                decode_image,
                data,
                mime,
                max_bytes=self.config.max_frame_bytes,
                max_pixels=self.config.max_pixels,
            )
        except ImageRejected as exc:
            log.info(
                "vision frame rejected",
                extra=kv(session=self.session.session_id, bytes=len(data), reason=str(exc)),
            )
            return await self._status(frame_id, "rejected", str(exc))
        frame_hash = await asyncio.to_thread(dhash, decoded.image)
        if (
            self.config.dedup
            and not manual
            and self._last_hash is not None
            and hamming(frame_hash, self._last_hash) <= self.config.dedup_threshold
        ):
            return await self._status(frame_id, "duplicate")
        jpeg = await asyncio.to_thread(
            to_jpeg, decoded.image, max_side=self.runtime.config.vlm.max_image_side
        )
        if not self.enabled:  # disabled while decoding
            return await self._status(frame_id, "disabled")
        self._last_accept = now
        self._last_hash = frame_hash
        frame = _Pending(
            jpeg=jpeg,
            device_id=header.device_id or self.session.device_id or "camera",
            frame_id=frame_id,
            received_at=datetime.now(UTC),
        )
        if self.mode == "agent":
            self._latest = frame
        else:
            self._pending = frame
            self._idle.clear()
            self._wake.set()
            if self._worker is None or self._worker.done():
                self._worker = asyncio.create_task(
                    self._run_worker(), name=f"vision-{self.session.session_id}"
                )
        log.info(
            "vision frame accepted",
            extra=kv(
                session=self.session.session_id,
                frame=frame_id,
                reason=reason,
                size=f"{decoded.width}x{decoded.height}",
                bytes=decoded.size_bytes,
            ),
        )
        return await self._status(frame_id, "accepted")

    async def _run_worker(self) -> None:
        vlm = self.runtime.vlm
        assert vlm is not None
        try:
            while self.enabled:
                if self._pending is None:
                    self._idle.set()
                    self._wake.clear()
                    await self._wake.wait()
                    continue
                item, self._pending = self._pending, None
                started = time.perf_counter()
                try:
                    result = await vlm.describe(item.jpeg, language=self.runtime.persona.language)
                except CompanionError as exc:
                    await self.session.emit_error(exc)
                    continue
                if not self.enabled:
                    break
                obs = VisualObservation(
                    timestamp=item.received_at,
                    device_id=item.device_id,
                    description=result.description,
                    confidence=result.confidence,
                    tags=result.tags,
                    source_event_id=item.frame_id,
                    source="frame",
                    session_id=self.session.session_id,
                )
                log.info(
                    "visual observation",
                    extra=kv(
                        session=self.session.session_id,
                        vlm_s=round(time.perf_counter() - started, 2),
                        description=obs.description,
                    ),
                )
                await self._record(obs, item.jpeg)
        finally:
            self._idle.set()

    # --- client-reported events -------------------------------------------

    async def submit_event(self, envelope: Envelope) -> None:
        """Normalize a ``vision.event`` (e.g. from an edge detector) without a VLM."""
        if not self.enabled:
            await self._status(envelope.id, "disabled")
            return
        payload = envelope.payload
        description = str(payload.get("description") or payload.get("label") or "").strip()
        if not description:
            await self.session.emit_error(
                "vision.event needs a description or label", code="vision_event_invalid"
            )
            return
        tags = payload.get("tags")
        confidence = payload.get("confidence")
        obs = VisualObservation(
            timestamp=datetime.now(UTC),
            device_id=envelope.device_id or self.session.device_id or "camera",
            description=description[:500],
            confidence=float(confidence) if isinstance(confidence, (int, float)) else None,
            tags=[str(t) for t in tags][:8] if isinstance(tags, list) else [],
            source_event_id=envelope.id,
            source="event",
            session_id=self.session.session_id,
        )
        await self._record(obs, None)

    # --- observations -----------------------------------------------------

    async def _record(self, obs: VisualObservation, jpeg: bytes | None) -> None:
        self._recent.append(obs)
        del self._recent[:-50]
        with contextlib.suppress(Exception):
            await self.runtime.store.add_observation(obs)
        if jpeg is not None and self.runtime.image_archive is not None:
            await asyncio.to_thread(self.runtime.image_archive.save, obs, jpeg)
        await self.session.emit(ev.VISION_OBSERVATION, obs.to_payload())

    async def wait_pending(self) -> None:
        """Let a turn wait briefly for a frame that is still being analyzed."""
        if self._idle.is_set() or self.config.wait_for_pending_s <= 0:
            return
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self._idle.wait(), timeout=self.config.wait_for_pending_s)

    def context_for_turn(self) -> TurnVision:
        """Observations and/or the newest frame to give the agent on this turn."""
        cfg = self.config
        now = datetime.now(UTC)
        turn = TurnVision()
        frame, self._latest = self._latest, None  # each frame is shown once
        if frame is not None and self.enabled:
            age = (now - frame.received_at).total_seconds()
            if age <= cfg.observation_max_age_s:
                turn.image = TurnImage(frame.jpeg, frame.frame_id, frame.device_id, age)
        if cfg.max_context_observations == 0:
            return turn
        fresh = [o for o in self._recent if o.age_s(now) <= cfg.observation_max_age_s]
        if cfg.inject == "system":
            recent = fresh[-cfg.max_context_observations :]
            turn.system_block = format_for_context(recent, now) if recent else None
            return turn
        new = [o for o in fresh if o.id not in self._injected][-cfg.max_context_observations :]
        if new:
            self._injected.update(o.id for o in new)
            turn.user_prefix = format_for_context(new, now)
        return turn

    # --- lifecycle ---------------------------------------------------------

    async def _stop_worker(self) -> None:
        worker, self._worker = self._worker, None
        if worker is not None and not worker.done():
            worker.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await worker
        self._idle.set()

    async def close(self) -> None:
        self.enabled = False
        self._pending = None
        self._latest = None
        await self._stop_worker()
