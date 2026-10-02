"""Per-connection conversation session: turn pipeline, half-duplex audio, vision."""

from __future__ import annotations

import asyncio
import base64
import contextlib
import logging
import time
import uuid
from enum import StrEnum
from typing import TYPE_CHECKING, Any, Protocol

import numpy as np

from companion.agent.base import AgentContext, SessionRef
from companion.audio.pcm import SAMPLE_RATE, pcm16_to_float32
from companion.audio.segmenter import SpeechStart, Utterance, UtteranceSegmenter
from companion.core.errors import CompanionError
from companion.core.logging import kv
from companion.core.text import SentenceSplitter
from companion.protocol import Envelope, make_event
from companion.protocol import events as ev
from companion.providers.llm.base import ChatMessage
from companion.vision.pipeline import VisionChannel

if TYPE_CHECKING:
    from companion.core.runtime import CompanionRuntime

log = logging.getLogger(__name__)

MIN_UTTERANCE_S = 0.3
PLAYBACK_ACK_TIMEOUT_S = 90.0


class State(StrEnum):
    IDLE = "idle"
    LISTENING = "listening"
    TRANSCRIBING = "transcribing"
    THINKING = "thinking"
    SPEAKING = "speaking"


class Outbox(Protocol):
    async def send_event(self, event: Envelope) -> None: ...

    async def send_binary(self, event: Envelope, data: bytes) -> None: ...


class Session:
    def __init__(
        self,
        runtime: CompanionRuntime,
        session_id: str,
        outbox: Outbox,
        *,
        device_id: str | None = None,
        speak: bool = True,
    ) -> None:
        self.runtime = runtime
        self.session_id = session_id
        self.device_id = device_id
        self.outbox = outbox
        self.speak = speak and runtime.tts is not None
        self.state = State.IDLE
        self._turn: asyncio.Task[None] | None = None
        self._audio_active = False
        self._segmenter: UtteranceSegmenter | None = None
        self._speaking_turn: str | None = None
        self._playback_timer: asyncio.TimerHandle | None = None
        self.vision = VisionChannel(self)

    # --- emit -----------------------------------------------------------

    async def emit(self, type_: str, payload: dict[str, Any] | None = None) -> None:
        event = make_event(type_, payload, session_id=self.session_id, device_id=self.device_id)
        await self.outbox.send_event(event)
        await self.runtime.bus.publish(event)

    async def emit_error(self, err: CompanionError | str, *, code: str | None = None) -> None:
        if isinstance(err, CompanionError):
            payload = {
                "code": code or err.code,
                "message": str(err),
                "recoverable": err.recoverable,
            }
        else:
            payload = {"code": code or "error", "message": err, "recoverable": True}
        log.warning("session error", extra=kv(session=self.session_id, **payload))
        await self.emit(ev.SYSTEM_ERROR, payload)

    async def set_state(self, state: State) -> None:
        if state != self.state:
            self.state = state
            await self.emit(ev.SYSTEM_STATE, {"state": state.value})

    def _rest_state(self) -> State:
        return State.LISTENING if self._audio_active else State.IDLE

    # --- text input -----------------------------------------------------

    async def handle_text(self, text: str) -> None:
        text = text.strip()
        if not text:
            return
        await self.cancel_turn()
        self._start_turn(self._text_turn(text))

    async def _text_turn(self, text: str) -> None:
        await self.run_turn(text)

    # --- audio input ----------------------------------------------------

    async def audio_start(self, payload: dict[str, Any]) -> None:
        rate = int(payload.get("sample_rate", SAMPLE_RATE))
        encoding = payload.get("encoding", "pcm_s16le")
        channels = int(payload.get("channels", 1))
        if rate != SAMPLE_RATE or encoding != "pcm_s16le" or channels != 1:
            await self.emit_error(
                f"unsupported audio format {encoding}/{rate}Hz/{channels}ch; "
                f"send pcm_s16le mono at {SAMPLE_RATE} Hz",
                code="audio_format",
            )
            return
        if self.runtime.stt is None:
            await self.emit_error("speech input is disabled (stt.provider: none)", code="stt_off")
            return
        self._segmenter = self.runtime.new_segmenter()
        self._audio_active = True
        if self.state == State.IDLE:
            await self.set_state(State.LISTENING)

    async def audio_stop(self) -> None:
        self._audio_active = False
        segmenter, self._segmenter = self._segmenter, None
        if segmenter is not None and self.state == State.LISTENING:
            utterance = segmenter.flush()
            if utterance is not None:
                await self._on_utterance(utterance)
                return
        if self.state == State.LISTENING:
            await self.set_state(State.IDLE)

    async def audio_chunk(self, data: bytes) -> None:
        if not self._audio_active or self._segmenter is None:
            return
        if self.state not in (State.IDLE, State.LISTENING):
            # Half-duplex: ignore the mic while thinking/speaking so the
            # companion never hears (and answers) its own voice.
            return
        for item in self._segmenter.feed(pcm16_to_float32(data)):
            if isinstance(item, SpeechStart):
                await self.emit(ev.AUDIO_VAD, {"state": "speech_start"})
            elif isinstance(item, Utterance):
                await self.emit(ev.AUDIO_VAD, {"state": "speech_end"})
                await self._on_utterance(item)
                break

    async def _on_utterance(self, utterance: Utterance) -> None:
        if self._segmenter is not None:
            self._segmenter.reset()
        if utterance.duration_s < MIN_UTTERANCE_S:
            return
        await self.cancel_turn()
        self._start_turn(self._voice_turn(utterance.audio))

    async def _voice_turn(self, audio: np.ndarray) -> None:
        stt = self.runtime.stt
        assert stt is not None
        await self.set_state(State.TRANSCRIBING)
        started = time.perf_counter()
        try:
            transcript = await stt.transcribe(audio)
        except CompanionError as exc:
            await self.emit_error(exc)
            await self.set_state(self._rest_state())
            return
        log.info(
            "transcribed",
            extra=kv(
                session=self.session_id,
                audio_s=round(audio.size / SAMPLE_RATE, 2),
                stt_s=round(time.perf_counter() - started, 2),
                text=transcript.text,
            ),
        )
        if not transcript.text:
            await self.set_state(self._rest_state())
            return
        await self.emit(ev.CONVERSATION_TRANSCRIPT, {"text": transcript.text, "final": True})
        await self.run_turn(transcript.text)

    # --- playback acknowledgement -----------------------------------------

    async def playback_finished(self, turn_id: str | None) -> None:
        if self._speaking_turn is None or (turn_id and turn_id != self._speaking_turn):
            return
        self._speaking_turn = None
        if self._playback_timer is not None:
            self._playback_timer.cancel()
            self._playback_timer = None
        if self._segmenter is not None:
            self._segmenter.reset()
        if self.state == State.SPEAKING:
            await self.set_state(self._rest_state())

    def _await_playback(self, turn_id: str) -> None:
        self._speaking_turn = turn_id
        loop = asyncio.get_running_loop()
        self._playback_timer = loop.call_later(
            PLAYBACK_ACK_TIMEOUT_S,
            lambda: loop.create_task(self.playback_finished(turn_id)),
        )

    # --- turns ----------------------------------------------------------

    def _start_turn(self, coro: Any) -> None:
        self._turn = asyncio.create_task(coro, name=f"turn-{self.session_id}")

    @property
    def busy(self) -> bool:
        return self._turn is not None and not self._turn.done()

    async def cancel_turn(self) -> None:
        turn, self._turn = self._turn, None
        if turn is not None and not turn.done():
            turn.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await turn
        if self._speaking_turn is not None:
            await self.playback_finished(self._speaking_turn)

    async def wait_turn(self) -> None:
        if self._turn is not None:
            with contextlib.suppress(asyncio.CancelledError):
                await self._turn

    async def run_turn(self, user_text: str) -> str:
        """Run one conversational turn; returns the assistant text."""
        rt = self.runtime
        turn_id = uuid.uuid4().hex[:12]
        await rt.store.add_message(self.session_id, "user", user_text, turn_id=turn_id)
        # Previous turns plus the user message just added.
        limit = rt.config.agent.history_turns * 2 + 1
        history = await rt.store.history(self.session_id, limit=limit)
        messages: list[ChatMessage] = [{"role": m.role, "content": m.content} for m in history]
        context = AgentContext(persona=rt.persona, turn_id=turn_id)
        session = SessionRef(self.session_id, self.device_id)

        await self.set_state(State.THINKING)
        await self.emit(ev.RESPONSE_START, {"turn_id": turn_id})

        if self.vision.enabled:
            await self.vision.wait_pending()
        seen = self.vision.context_for_turn()
        if seen.system_block:
            context.extra_system.append(seen.system_block)
        # Only the agent sees observations and images; the stored transcript
        # keeps the user's own words and never the image.
        text = f"{seen.user_prefix}\n\nUser: {user_text}" if seen.user_prefix else user_text
        if seen.image is not None:
            image_url = "data:image/jpeg;base64," + base64.b64encode(seen.image.jpeg).decode()
            note = f"(Camera image from {seen.image.device_id}, {round(seen.image.age_s)} s ago)"
            messages[-1] = {
                "role": "user",
                "content": [
                    {"type": "text", "text": f"{note}\n{text}"},
                    {"type": "image_url", "image_url": {"url": image_url}},
                ],
            }
            await self.emit(
                ev.VISION_FRAME_USED, {"frame_id": seen.image.frame_id, "turn_id": turn_id}
            )
        elif seen.user_prefix:
            messages[-1] = {"role": "user", "content": text}

        parts: list[str] = []
        audio_sent = False
        cancelled = False
        started = time.perf_counter()
        first_token_s: float | None = None
        queue: asyncio.Queue[str | None] = asyncio.Queue()
        tts_task: asyncio.Task[bool] | None = None
        if self.speak:
            tts_task = asyncio.create_task(self._tts_worker(turn_id, queue))
        splitter = SentenceSplitter()
        try:
            async for item in rt.agent.respond(session, messages, context):
                if item.kind == "tool":
                    await self.emit(
                        ev.AGENT_TOOL_PROGRESS,
                        {
                            "turn_id": turn_id,
                            "tool": item.tool,
                            "label": item.text,
                            "status": item.status,
                        },
                    )
                    continue
                if first_token_s is None:
                    first_token_s = time.perf_counter() - started
                parts.append(item.text)
                await self.emit(ev.RESPONSE_DELTA, {"turn_id": turn_id, "text": item.text})
                for sentence in splitter.feed(item.text):
                    queue.put_nowait(sentence)
            for sentence in splitter.flush():
                queue.put_nowait(sentence)
            queue.put_nowait(None)
            if tts_task is not None:
                audio_sent = await tts_task
        except asyncio.CancelledError:
            cancelled = True
            raise
        except CompanionError as exc:
            await self.emit_error(exc)
        except Exception as exc:
            log.exception("turn failed", extra=kv(session=self.session_id))
            await self.emit_error(f"internal error: {exc}", code="internal_error")
        finally:
            if tts_task is not None and not tts_task.done():
                tts_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    audio_sent = await tts_task or audio_sent
            text = "".join(parts).strip()
            await self._finish_turn(turn_id, text, cancelled=cancelled, audio_sent=audio_sent)
            log.info(
                "turn finished",
                extra=kv(
                    session=self.session_id,
                    turn=turn_id,
                    cancelled=cancelled,
                    first_token_s=round(first_token_s, 2) if first_token_s else None,
                    total_s=round(time.perf_counter() - started, 2),
                    chars=len(text),
                ),
            )
        return text

    async def _finish_turn(
        self, turn_id: str, text: str, *, cancelled: bool, audio_sent: bool
    ) -> None:
        with contextlib.suppress(Exception):
            if text:
                await self.runtime.store.add_message(
                    self.session_id, "assistant", text, turn_id=turn_id
                )
            await self.emit(
                ev.RESPONSE_DONE, {"turn_id": turn_id, "text": text, "cancelled": cancelled}
            )
            if audio_sent:
                await self.emit(ev.AUDIO_OUTPUT_DONE, {"turn_id": turn_id})
            if audio_sent and not cancelled:
                await self.set_state(State.SPEAKING)
                self._await_playback(turn_id)
            else:
                await self.set_state(self._rest_state())

    async def _tts_worker(self, turn_id: str, queue: asyncio.Queue[str | None]) -> bool:
        tts = self.runtime.tts
        assert tts is not None
        seq = 0
        failed = False
        while (sentence := await queue.get()) is not None:
            if failed:
                continue
            try:
                audio = await tts.synthesize(sentence, voice=self.runtime.persona.voice)
            except CompanionError as exc:
                failed = True  # continue text-only for the rest of this turn
                await self.emit_error(exc)
                continue
            header = make_event(
                ev.AUDIO_OUTPUT_CHUNK,
                {"turn_id": turn_id, "seq": seq, "mime": audio.mime, "text": sentence},
                session_id=self.session_id,
                device_id=self.device_id,
            )
            await self.outbox.send_binary(header, audio.data)
            if seq == 0:
                await self.set_state(State.SPEAKING)
            seq += 1
        return seq > 0

    async def close(self) -> None:
        self._audio_active = False
        await self.vision.close()
        await self.cancel_turn()
        if self._playback_timer is not None:
            self._playback_timer.cancel()
