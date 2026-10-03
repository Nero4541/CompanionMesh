"""Conversation session: turn pipeline, half-duplex audio, vision.

A session can have several devices attached at once (e.g. a browser with the
mic and speaker plus a separate IP camera). Events go to every device; speech
audio goes only to devices with the "speaker" role.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import logging
import time
import uuid
from collections.abc import Iterable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Any, Protocol

import numpy as np

from companion.agent.base import AgentContext, SessionRef
from companion.attention.engine import AttentionEngine, AttentionEvent, Decision
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
CONTEXT_NOTE_MAX_AGE_S = 1800.0
SILENT = "[SILENT]"

PROACTIVE_PROMPT = (
    "[Proactive check: this is not a message from the user.] {description} "
    "You may say one short, natural remark to the user about it, in the language you "
    "normally use with them. If it is not worth mentioning, or you are unsure, reply with "
    "exactly " + SILENT + " and nothing else."
)

ROLES = frozenset({"mic", "speaker", "camera"})
# Clients that do not declare roles (v0.1/v0.2) can still send frames, but are
# not asked for captures, which they would not answer.
DEFAULT_ROLES = frozenset({"mic", "speaker"})


class State(StrEnum):
    IDLE = "idle"
    LISTENING = "listening"
    TRANSCRIBING = "transcribing"
    THINKING = "thinking"
    SPEAKING = "speaking"


class Outbox(Protocol):
    async def send_event(self, event: Envelope) -> None: ...

    async def send_binary(self, event: Envelope, data: bytes) -> None: ...

    async def close(self) -> None: ...


def parse_roles(value: object) -> frozenset[str]:
    """Roles a device declares in session.start; absent means mic + speaker."""
    if not isinstance(value, list):
        return DEFAULT_ROLES
    return frozenset(str(r) for r in value) & ROLES


@dataclass(eq=False)
class Device:
    outbox: Outbox
    device_id: str | None
    roles: frozenset[str]
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:8])

    def describe(self) -> dict[str, Any]:
        return {"device_id": self.device_id, "roles": sorted(self.roles)}


class Session:
    def __init__(
        self,
        runtime: CompanionRuntime,
        session_id: str,
        outbox: Outbox,
        *,
        device_id: str | None = None,
        roles: frozenset[str] = DEFAULT_ROLES,
        speak: bool = True,
    ) -> None:
        self.runtime = runtime
        self.session_id = session_id
        self.device_id = device_id
        self.devices: list[Device] = [Device(outbox, device_id, roles)]
        self._mic_device: Device | None = None
        self.speak = speak and runtime.tts is not None
        self.attention = AttentionEngine(runtime.config.attention)
        self._context_notes: list[AttentionEvent] = []
        self._proactive = False  # the current turn was started by the companion
        self.state = State.IDLE
        self._turn: asyncio.Task[None] | None = None
        self._audio_active = False
        self._segmenter: UtteranceSegmenter | None = None
        self._speaking_turn: str | None = None
        self._playback_timer: asyncio.TimerHandle | None = None
        self.vision = VisionChannel(self)

    # --- devices ----------------------------------------------------------

    def attach(self, outbox: Outbox, *, device_id: str | None, roles: frozenset[str]) -> Device:
        device = Device(outbox, device_id, roles)
        self.devices.append(device)
        return device

    @property
    def has_conversation_device(self) -> bool:
        return any(d.roles & {"mic", "speaker"} for d in self.devices)

    async def detach(self, device: Device) -> bool:
        """Remove a device; returns True when the conversation is over.

        That is when no device with a mic or speaker remains: a camera on its
        own does not keep a session alive.
        """
        if device in self.devices:
            self.devices.remove(device)
        if device is self._mic_device:
            await self.audio_stop(device)
        if not self.has_conversation_device:
            return True
        await self.announce_devices()
        return False

    async def end(self, reason: str) -> None:
        """Tell the remaining devices the session is over and disconnect them."""
        remaining, self.devices = list(self.devices), []
        await self.emit_to(remaining, ev.SESSION_ENDED, {"reason": reason})
        for device in remaining:
            with contextlib.suppress(Exception):
                await device.outbox.close()

    def with_role(self, role: str) -> list[Device]:
        return [d for d in self.devices if role in d.roles]

    async def announce_devices(self) -> None:
        await self.emit(ev.SESSION_DEVICES, {"devices": [d.describe() for d in self.devices]})

    # --- emit -----------------------------------------------------------

    async def emit(self, type_: str, payload: dict[str, Any] | None = None) -> None:
        event = make_event(type_, payload, session_id=self.session_id, device_id=self.device_id)
        for device in list(self.devices):
            await device.outbox.send_event(event)
        await self.runtime.bus.publish(event)

    async def emit_to(
        self, devices: Iterable[Device], type_: str, payload: dict[str, Any] | None = None
    ) -> None:
        event = make_event(type_, payload, session_id=self.session_id, device_id=self.device_id)
        for device in devices:
            await device.outbox.send_event(event)

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
        await self.vision.request_capture("text")
        self._start_turn(self._text_turn(text))

    async def _text_turn(self, text: str) -> None:
        await self.run_turn(text)

    # --- audio input ----------------------------------------------------

    async def audio_start(self, payload: dict[str, Any], device: Device | None = None) -> None:
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
        self._mic_device = device or self.devices[0]
        self._audio_active = True
        if self.state == State.IDLE:
            await self.set_state(State.LISTENING)

    async def audio_stop(self, device: Device | None = None) -> None:
        if device is not None and self._mic_device is not None and device is not self._mic_device:
            return  # only the device that opened the mic can close it
        self._mic_device = None
        self._audio_active = False
        segmenter, self._segmenter = self._segmenter, None
        if segmenter is not None and self.state == State.LISTENING:
            utterance = segmenter.flush()
            if utterance is not None:
                await self._on_utterance(utterance)
                return
        if self.state == State.LISTENING:
            await self.set_state(State.IDLE)

    async def audio_chunk(self, data: bytes, device: Device | None = None) -> None:
        if not self._audio_active or self._segmenter is None:
            return
        if device is not None and device is not self._mic_device:
            return  # one microphone per session
        listening_through = self._proactive and self.state == State.THINKING
        if self.state not in (State.IDLE, State.LISTENING) and not listening_through:
            # Half-duplex: ignore the mic while thinking/speaking so the
            # companion never hears (and answers) its own voice. A proactive
            # turn that has not started speaking yet keeps listening, so the
            # user can simply talk over it.
            return
        for item in self._segmenter.feed(pcm16_to_float32(data)):
            if isinstance(item, SpeechStart):
                if self._proactive:
                    await self.cancel_turn()  # the user comes first
                await self.emit(ev.AUDIO_VAD, {"state": "speech_start"})
                await self.vision.request_capture("speech")
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
        self.attention.note_user_turn()
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
            await self.vision.wait_for_capture()
            await self.vision.wait_pending()
        seen = self.vision.context_for_turn()
        if seen.system_block:
            context.extra_system.append(seen.system_block)
        # Only the agent sees observations, noticed events and images; the
        # stored transcript keeps the user's own words and never the image.
        prefix = "\n\n".join(p for p in (self._take_context_notes(), seen.user_prefix) if p)
        seen.user_prefix = prefix or None
        text = f"{prefix}\n\nUser: {user_text}" if prefix else user_text
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
        if self.speak and self.with_role("speaker"):
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
                self.attention.note_agent_speech()
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
            for device in self.with_role("speaker"):
                await device.outbox.send_binary(header, audio.data)
            if seq == 0:
                await self.set_state(State.SPEAKING)
            seq += 1
        return seq > 0

    # --- attention (v0.3) ------------------------------------------------------

    def _take_context_notes(self) -> str | None:
        now = self.attention.clock()
        notes = [
            n for n in self._context_notes if (now - n.at).total_seconds() <= CONTEXT_NOTE_MAX_AGE_S
        ]
        self._context_notes = []
        if not notes:
            return None
        lines = "\n".join(
            f"- {round((now - n.at).total_seconds())} s ago: {n.description}" for n in notes
        )
        return f"Noticed since the user's last message:\n{lines}"

    async def set_quiet(self, quiet: bool) -> None:
        self.attention.set_quiet(quiet)
        await self.emit_attention_state()

    async def emit_attention_state(self) -> None:
        engine = self.attention
        await self.emit(
            ev.ATTENTION_STATE,
            {
                "proactive": engine.config.proactive,
                "quiet": engine.state.quiet_mode,
                "speech_blockers": engine.speech_blockers(engine.clock()),
            },
        )

    async def on_attention_event(self, event: AttentionEvent) -> Decision:
        """Decide what to do with a noticed event and do it."""
        busy = self.busy or self.state not in (State.IDLE, State.LISTENING)
        record = self.attention.evaluate(event, busy=busy)
        payload = {**record.to_payload(), "session_id": self.session_id}
        log.info("attention decision", extra=kv(**payload))
        decision_event = make_event(ev.ATTENTION_DECISION, payload, session_id=self.session_id)
        await self.runtime.bus.publish(decision_event)
        if self.runtime.config.attention.debug:
            await self.emit(ev.ATTENTION_DECISION, record.to_payload())

        match record.decision:
            case Decision.REMEMBER:
                from companion.vision.observation import VisualObservation

                with contextlib.suppress(Exception):
                    await self.runtime.store.add_observation(
                        VisualObservation(
                            timestamp=event.at,
                            device_id=str(event.data.get("device") or event.source),
                            description=event.description,
                            confidence=None,
                            tags=[event.kind],
                            source_event_id=event.kind,
                            source="event",
                            session_id=self.session_id,
                        )
                    )
            case Decision.CONTEXT_ONLY:
                self._context_notes.append(event)
                del self._context_notes[:-5]
            case Decision.SPEAK:
                self.attention.record_proactive_turn()
                self._start_turn(self._proactive_turn(event))
            case Decision.IGNORE:
                pass
        return record.decision

    async def _proactive_turn(self, event: AttentionEvent) -> None:
        """Offer the agent a chance to remark on an event; it may stay silent."""
        rt = self.runtime
        turn_id = uuid.uuid4().hex[:12]
        self._proactive = True
        try:
            await self.set_state(State.THINKING)
            limit = rt.config.agent.history_turns * 2
            history = await rt.store.history(self.session_id, limit=limit) if limit else []
            prompt = PROACTIVE_PROMPT.format(description=event.description)
            seen = self.vision.context_for_turn()
            content: Any = prompt
            if seen.image is not None:
                image_url = "data:image/jpeg;base64," + base64.b64encode(seen.image.jpeg).decode()
                content = [
                    {"type": "text", "text": prompt},
                    {"type": "image_url", "image_url": {"url": image_url}},
                ]
            messages: list[ChatMessage] = [
                *({"role": m.role, "content": m.content} for m in history),
                {"role": "user", "content": content},
            ]
            context = AgentContext(persona=rt.persona, turn_id=turn_id)
            session = SessionRef(self.session_id, self.device_id)
            parts: list[str] = []
            try:
                async for item in rt.agent.respond(session, messages, context):
                    if item.kind == "text":
                        parts.append(item.text)
            except CompanionError as exc:
                log.warning("proactive turn failed", extra=kv(error=str(exc)))
                return
            text = "".join(parts).strip()
            if not text or SILENT in text:
                log.info("proactive turn: agent chose silence", extra=kv(kind=event.kind))
                return
            self._proactive = False  # speaking now: half-duplex applies again
            await self._speak_proactively(turn_id, text, event)
        finally:
            self._proactive = False
            if self.state == State.THINKING:
                await self.set_state(self._rest_state())

    async def _speak_proactively(self, turn_id: str, text: str, event: AttentionEvent) -> None:
        await self.emit(
            ev.RESPONSE_START, {"turn_id": turn_id, "proactive": True, "reason": event.kind}
        )
        await self.emit(ev.RESPONSE_DELTA, {"turn_id": turn_id, "text": text})
        audio_sent = False
        if self.speak and self.with_role("speaker"):
            queue: asyncio.Queue[str | None] = asyncio.Queue()
            splitter = SentenceSplitter()
            for sentence in [*splitter.feed(text), *splitter.flush()]:
                queue.put_nowait(sentence)
            queue.put_nowait(None)
            audio_sent = await self._tts_worker(turn_id, queue)
        await self._finish_turn(turn_id, text, cancelled=False, audio_sent=audio_sent)

    async def close(self) -> None:
        self._audio_active = False
        await self.vision.close()
        await self.cancel_turn()
        if self._playback_timer is not None:
            self._playback_timer.cancel()
