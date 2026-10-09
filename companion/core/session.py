"""Conversation session: turn pipeline, full-duplex audio, vision.

A session can have several devices attached at once (e.g. a browser with the
mic and speaker plus a separate IP camera). Events go to every device; speech
audio goes only to devices with the "speaker" role.

Audio is full duplex when the microphone's client says so (v0.5): it keeps
sending while the companion speaks, and speech detected meanwhile is the user
cutting in (barge-in), which stops the reply and its playback. Older clients
are half duplex: their audio is ignored while the companion thinks or speaks.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import logging
import re
import time
import uuid
from collections import deque
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol

import numpy as np

from companion.agent.base import AgentContext, SessionRef
from companion.attention.engine import AttentionEngine, AttentionEvent, Decision
from companion.audio.echo import (
    EchoCanceller,
    EchoGuard,
    NlmsEchoCanceller,
    NoEchoCanceller,
    ReferenceBuffer,
)
from companion.audio.pcm import SAMPLE_RATE, audio_duration_s, pcm16_to_float32, wav_to_mono16k
from companion.audio.segmenter import SpeechStart, Utterance, UtteranceSegmenter
from companion.core.cancel import Cancelled, CancelToken
from companion.core.clock import ClockSync
from companion.core.errors import CompanionError
from companion.core.logging import kv
from companion.core.metrics import TurnTimings
from companion.core.states import State, StateMachine
from companion.core.text import SentenceSplitter
from companion.protocol import Envelope, make_event
from companion.protocol import events as ev
from companion.providers.llm.base import ChatMessage
from companion.vision.pipeline import VisionChannel

if TYPE_CHECKING:
    from companion.core.runtime import CompanionRuntime

__all__ = ["DEFAULT_ROLES", "Device", "Outbox", "Session", "State", "parse_roles"]

log = logging.getLogger(__name__)

MIN_UTTERANCE_S = 0.3
# SPEAKING lasts at most this long when the length of the audio is unknown.
PLAYBACK_ACK_TIMEOUT_S = 90.0
CONTEXT_NOTE_MAX_AGE_S = 1800.0
WATCHDOG_INTERVAL_S = 0.5
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
    connected_at: float = field(default_factory=time.time)
    status: dict[str, Any] = field(default_factory=dict)  # last device.status
    status_at: float | None = None
    clock: ClockSync = field(default_factory=ClockSync)

    def describe(self) -> dict[str, Any]:
        return {"device_id": self.device_id, "roles": sorted(self.roles)}

    def report(self) -> dict[str, Any]:
        best = self.clock.best
        return {
            **self.describe(),
            "connected_s": round(time.time() - self.connected_at),
            "status": self.status,
            "status_age_s": round(time.time() - self.status_at) if self.status_at else None,
            "rtt_ms": round(best.rtt_ms, 1) if best else None,
            "clock_offset_ms": round(best.offset_ms, 1) if best else None,
        }


@dataclass
class _Mic:
    """The open microphone stream of one device."""

    device: Device
    full_duplex: bool  # keeps sending while the companion speaks
    device_aec: bool  # the device cancels its own speaker's echo
    canceller: EchoCanceller
    pos: int = 0  # next microphone sample index
    last_capture_ms: float | None = None  # device clock, end of the newest chunk


@dataclass
class _Chunk:
    seq: int
    text: str
    duration_s: float | None
    reference: np.ndarray | None  # 16 kHz copy for the echo canceller
    started: bool = False


@dataclass
class _Playback:
    """Reply audio sent to speaker devices for one turn."""

    turn_id: str
    chunks: dict[int, _Chunk] = field(default_factory=dict)
    expected_end: float = 0.0  # monotonic; when the audio sent should have played
    unknown_length: bool = False
    progress_reported: bool = False  # the device reports per-clip progress

    def heard_text(self) -> str | None:
        """Text of the clips the device started playing; None when it does not say."""
        if not self.progress_reported:
            return None
        return "".join(c.text for _, c in sorted(self.chunks.items()) if c.started)


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
        self.realtime = runtime.config.realtime
        self.metrics = runtime.metrics
        self.speak = speak and runtime.tts is not None
        self.attention = AttentionEngine(runtime.config.attention)
        self._context_notes: list[AttentionEvent] = []
        # Events emitted while every conversation device is away (session
        # linger); replayed to the device that comes back. Bounded.
        self._backlog: deque[Envelope] = deque(maxlen=200)
        self.linger: asyncio.TimerHandle | None = None
        self._proactive = False  # the current turn was started by the companion
        self.sm = StateMachine()
        self._turn: asyncio.Task[None] | None = None
        self._turn_id: str | None = None
        self._token: CancelToken | None = None
        self._timings: TurnTimings | None = None
        self._mic: _Mic | None = None
        self._decoder: Any = None  # OpusDecoder when the mic sends opus
        self._segmenter: UtteranceSegmenter | None = None
        self._speech_end: float | None = None  # monotonic, end of the last utterance
        self._playback: _Playback | None = None
        self._barge_in_at: float | None = None
        self._stopping_turn: str | None = None  # audio.output.stop sent, not confirmed
        echo = self.realtime.echo
        self.echo_guard = EchoGuard(similarity=echo.guard_similarity, window_s=echo.guard_window_s)
        self.reference = ReferenceBuffer()
        self.vision = VisionChannel(self)
        self._watchdog: asyncio.Task[None] | None = None
        self._tasks: set[asyncio.Task[Any]] = set()
        with contextlib.suppress(RuntimeError):  # no loop: built outside asyncio (tests)
            self._watchdog = asyncio.get_running_loop().create_task(self._watch())

    @property
    def state(self) -> State:
        return self.sm.state

    def _spawn(self, coro: Any) -> None:
        """Run ``coro`` detached from the caller (kept referenced until done)."""
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

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
        if self._mic is not None and device is self._mic.device:
            await self.audio_stop(device)
        if "speaker" in device.roles and not self.with_role("speaker"):
            # Nobody left to play (or confirm) the reply audio. Recover in a task
            # of our own: this runs in the leaving connection's handler, which
            # may be cancelled as it closes.
            if self._playback is not None or self.state in (State.SPEAKING, State.INTERRUPTING):
                self._spawn(self.recover("speaker device left"))
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

    async def replay_backlog(self, device: Device) -> int:
        """Deliver what happened while the conversation devices were away."""
        events, self._backlog = list(self._backlog), deque(maxlen=self._backlog.maxlen)
        for event in events:
            await device.outbox.send_event(event)
        return len(events)

    async def announce_devices(self) -> None:
        await self.emit(ev.SESSION_DEVICES, {"devices": [d.describe() for d in self.devices]})

    # --- emit -----------------------------------------------------------

    async def emit(self, type_: str, payload: dict[str, Any] | None = None) -> None:
        event = make_event(type_, payload, session_id=self.session_id, device_id=self.device_id)
        if not self.has_conversation_device and type_ != ev.SESSION_DEVICES:
            self._backlog.append(event)
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

    # --- state ------------------------------------------------------------

    async def set_state(
        self, state: State, reason: str = "", *, deadline_s: float | None = None
    ) -> None:
        previous = self.sm.state
        if self.sm.transition(state, reason, deadline_s=deadline_s):
            payload: dict[str, Any] = {"state": state.value, "previous": previous.value}
            if reason:
                payload["reason"] = reason
            if self._turn_id and state not in (State.IDLE, State.LISTENING):
                payload["turn_id"] = self._turn_id
            await self.emit(ev.SYSTEM_STATE, payload)

    def _rest_state(self) -> State:
        return State.LISTENING if self._mic is not None else State.IDLE

    async def rest(self, reason: str = "") -> None:
        await self.set_state(self._rest_state(), reason)

    def _thinking_deadline_s(self) -> float:
        agent = self.runtime.config.agent
        provider = getattr(agent, agent.provider, None)
        timeout = getattr(provider, "timeout", None) or self.runtime.config.llm.timeout
        return float(timeout) + 30.0

    async def _watch(self) -> None:
        """Watchdog: no state may outlive its deadline (lost acks, vanished devices)."""
        while True:
            await asyncio.sleep(WATCHDOG_INTERVAL_S)
            if not self.sm.overdue():
                continue
            state = self.state
            if state == State.INTERRUPTING:
                # The device never confirmed it stopped; carry on regardless.
                self._stopping_turn = None
                self._barge_in_at = None
                await self.rest("stop not confirmed")
            elif state == State.SPEAKING and self._playback is not None:
                await self._playback_done(self._playback.turn_id, "playback not confirmed")
            else:
                await self.recover(f"{state} took too long")

    async def recover(self, reason: str) -> None:
        """Get back to a clean resting state after something went missing."""
        log.warning("recovering", extra=kv(session=self.session_id, reason=reason))
        self.metrics.count("recoveries")
        await self.set_state(State.RECOVERING, reason)
        playback = self._playback
        await self._cancel_turn(reason)
        if playback is not None and self.with_role("speaker"):
            await self.emit_to(
                self.with_role("speaker"),
                ev.AUDIO_OUTPUT_STOP,
                {"turn_id": playback.turn_id, "reason": "recovering"},
            )
        self._playback = None
        self._stopping_turn = None
        self._barge_in_at = None
        if self._segmenter is not None:
            self._segmenter.reset()
        await self.rest("recovered")

    # --- text input -----------------------------------------------------

    async def handle_text(self, text: str) -> None:
        text = text.strip()
        if not text:
            return
        await self.interrupt("new_message")
        await self.vision.request_capture("text")
        self._timings = TurnTimings(transcript=TurnTimings.now())
        self._start_turn(self._text_turn(text))

    async def _text_turn(self, text: str) -> None:
        await self.run_turn(text)

    # --- audio input ----------------------------------------------------

    async def audio_start(self, payload: dict[str, Any], device: Device | None = None) -> None:
        rate = int(payload.get("sample_rate", SAMPLE_RATE))
        encoding = payload.get("encoding", "pcm_s16le")
        channels = int(payload.get("channels", 1))
        pcm_ok = encoding == "pcm_s16le" and rate == SAMPLE_RATE
        if not (pcm_ok or encoding == "opus") or channels != 1:
            await self.emit_error(
                f"unsupported audio format {encoding}/{rate}Hz/{channels}ch; "
                f"send mono pcm_s16le at {SAMPLE_RATE} Hz, or opus",
                code="audio_format",
            )
            return
        if self.runtime.stt is None:
            await self.emit_error("speech input is disabled (stt.provider: none)", code="stt_off")
            return
        self._decoder = None
        if encoding == "opus":
            from companion.audio.opus import OpusDecoder

            try:
                self._decoder = OpusDecoder()
            except CompanionError as exc:
                await self.emit_error(exc)
                return
        device_aec = str(payload.get("aec") or "none") == "device"
        canceller: EchoCanceller = NoEchoCanceller()
        echo = self.realtime.echo
        if not device_aec and echo.canceller == "nlms":
            canceller = NlmsEchoCanceller(
                self.reference, filter_ms=echo.filter_ms, max_delay_ms=echo.max_delay_ms
            )
        self._mic = _Mic(
            device=device or self.devices[0],
            full_duplex=bool(payload.get("full_duplex")),
            device_aec=device_aec,
            canceller=canceller,
        )
        self._segmenter = self.runtime.new_segmenter()
        log.info(
            "microphone open",
            extra=kv(
                session=self.session_id,
                encoding=encoding,
                full_duplex=self._mic.full_duplex,
                aec="device" if device_aec else echo.canceller,
            ),
        )
        if self.state == State.IDLE:
            await self.set_state(State.LISTENING, "microphone open")

    async def audio_stop(self, device: Device | None = None) -> None:
        mic = self._mic
        if mic is None or (device is not None and device is not mic.device):
            return  # only the device that opened the mic can close it
        self._mic = None
        segmenter, self._segmenter = self._segmenter, None
        if segmenter is not None and self.state == State.LISTENING:
            utterance = segmenter.flush()
            if utterance is not None:
                await self._on_utterance(utterance)
                return
        if self.state == State.LISTENING:
            await self.set_state(State.IDLE, "microphone closed")

    def _playing(self) -> bool:
        """Reply audio may be coming out of a speaker right now."""
        return self._playback is not None or self._stopping_turn is not None

    def _echo_risk(self) -> bool:
        mic = self._mic
        return mic is not None and not mic.device_aec and self._playing()

    async def audio_chunk(
        self, data: bytes, device: Device | None = None, meta: dict[str, Any] | None = None
    ) -> None:
        mic = self._mic
        if mic is None or self._segmenter is None:
            return
        if device is not None and device is not mic.device:
            return  # one microphone per session
        samples = self._decoder.decode(data) if self._decoder else pcm16_to_float32(data)
        meta = meta or {}
        pos = meta.get("pos")
        if isinstance(pos, int) and pos >= 0:
            mic.pos = pos
        start = mic.pos
        mic.pos += samples.size
        t = meta.get("t")
        if isinstance(t, (int, float)):
            mic.last_capture_ms = float(t) + samples.size * 1000 / SAMPLE_RATE
        samples = mic.canceller.process(samples, start)

        at_rest = self.state in (State.IDLE, State.LISTENING)
        listening_through = self._proactive and self.state == State.THINKING
        full_duplex = mic.full_duplex and self.realtime.barge_in
        if not (at_rest or listening_through or full_duplex):
            # Half-duplex: ignore the mic while thinking/speaking so the
            # companion never hears (and answers) its own voice. A proactive
            # turn that has not started speaking yet keeps listening, so the
            # user can simply talk over it.
            return
        if self.state == State.TRANSCRIBING:
            return  # the utterance before this one is being transcribed
        segmenter = self._segmenter
        segmenter.strict = (
            (self.realtime.barge_in_threshold, self.realtime.barge_in_min_speech_ms)
            if self._echo_risk()
            else None
        )
        for item in segmenter.feed(samples):
            if isinstance(item, SpeechStart):
                self._record_mic_to_vad(mic)
                if self._proactive or self.busy or self._playing():
                    await self.interrupt("barge_in")  # the user comes first
                await self.emit(ev.AUDIO_VAD, {"state": "speech_start"})
                await self.vision.request_capture("speech")
            elif isinstance(item, Utterance):
                await self.emit(ev.AUDIO_VAD, {"state": "speech_end"})
                await self._on_utterance(item)
                break

    def _record_mic_to_vad(self, mic: _Mic) -> None:
        if mic.last_capture_ms is not None:
            age = mic.device.clock.age_ms(mic.last_capture_ms)
            if age is not None:
                self.metrics.record("mic_to_vad", age / 1000)

    async def _on_utterance(self, utterance: Utterance) -> None:
        if self._segmenter is not None:
            self._segmenter.reset()
        if utterance.duration_s < MIN_UTTERANCE_S:
            if self.state == State.INTERRUPTING:
                await self.rest("utterance too short")
            return
        await self.interrupt("new_utterance")
        self._speech_end = TurnTimings.now()
        self._timings = TurnTimings(speech_end=self._speech_end)
        self._start_turn(self._voice_turn(utterance.audio))

    async def _voice_turn(self, audio: np.ndarray) -> None:
        stt = self.runtime.stt
        assert stt is not None
        await self.set_state(State.TRANSCRIBING, deadline_s=self.realtime.transcribe_timeout_s)
        started = time.perf_counter()
        try:
            transcript = await stt.transcribe(audio)
        except CompanionError as exc:
            await self.emit_error(exc)
            await self.rest("transcription failed")
            return
        if self._timings is not None:
            self._timings.transcript = TurnTimings.now()
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
            await self.rest("nothing recognized")
            return
        if self.realtime.echo.guard and self.echo_guard.is_echo(transcript.text):
            log.info(
                "dropped own voice heard back",
                extra=kv(session=self.session_id, text=transcript.text),
            )
            self.metrics.count("echo_dropped")
            await self.rest("echo")
            return
        await self.emit(ev.CONVERSATION_TRANSCRIPT, {"text": transcript.text, "final": True})
        await self.run_turn(transcript.text)

    # --- playback ---------------------------------------------------------

    async def playback_finished(self, turn_id: str | None) -> None:
        """The device played all of a turn's audio (audio.output.played)."""
        playback = self._playback
        if playback is None or (turn_id and turn_id != playback.turn_id):
            return
        await self._playback_done(playback.turn_id, "played")

    async def _playback_done(self, turn_id: str, reason: str) -> None:
        if self._playback is not None and self._playback.turn_id == turn_id:
            self._playback = None
        segmenter = self._segmenter
        if segmenter is not None and not segmenter.in_speech:
            segmenter.reset()  # drop the tail of our own voice
        if self.state == State.SPEAKING:
            await self.rest(reason)

    async def playback_progress(self, payload: dict[str, Any]) -> None:
        """audio.output.progress: a clip started (or finished) playing on a device."""
        playback = self._playback
        turn_id = payload.get("turn_id")
        if playback is None or turn_id != playback.turn_id:
            return
        playback.progress_reported = True
        chunk = playback.chunks.get(int(payload.get("seq", -1)))
        if chunk is None or payload.get("state") != "started" or chunk.started:
            return
        chunk.started = True
        self.echo_guard.spoke(chunk.text)
        if chunk.duration_s:
            self.echo_guard.extend(chunk.duration_s)
        pos = payload.get("pos")
        if chunk.reference is not None and isinstance(pos, int):
            self.reference.add(chunk.reference, pos)

    async def playback_stopped(self, payload: dict[str, Any]) -> None:
        """audio.output.stopped: the device cut playback off as asked."""
        turn_id = payload.get("turn_id")
        if self._stopping_turn is None or turn_id != self._stopping_turn:
            return
        self._stopping_turn = None
        if self._barge_in_at is not None:
            self.metrics.record("barge_in_to_stop", time.monotonic() - self._barge_in_at)
            self._barge_in_at = None
        if self.state == State.INTERRUPTING:
            await self.rest("playback stopped")

    def _track_chunk(self, turn_id: str, seq: int, text: str, audio: bytes, mime: str) -> None:
        playback = self._playback
        if playback is None or playback.turn_id != turn_id:
            playback = self._playback = _Playback(turn_id)
        duration = audio_duration_s(audio, mime)
        reference = None
        if self._mic is not None and isinstance(self._mic.canceller, NlmsEchoCanceller):
            reference = wav_to_mono16k(audio)
        playback.chunks[seq] = _Chunk(seq, text, duration, reference)
        now = time.monotonic()
        if duration is None:
            playback.unknown_length = True
        playback.expected_end = max(playback.expected_end, now) + (duration or 0.0)
        if not playback.progress_reported:
            # Devices that do not report progress: assume playback starts now.
            self.echo_guard.spoke(text)
            if duration:
                self.echo_guard.extend(playback.expected_end - now)

    def _speaking_deadline_s(self) -> float:
        playback = self._playback
        if playback is None or playback.unknown_length:
            return PLAYBACK_ACK_TIMEOUT_S
        return max(0.0, playback.expected_end - time.monotonic()) + self.realtime.playback_slack_s

    # --- turns ----------------------------------------------------------

    def _start_turn(self, coro: Any) -> None:
        self._token = CancelToken()
        self._turn = asyncio.create_task(coro, name=f"turn-{self.session_id}")

    @property
    def busy(self) -> bool:
        return self._turn is not None and not self._turn.done()

    async def interrupt(self, reason: str) -> None:
        """Stop the current turn and any playback: barge-in, cancel or a newer turn."""
        playback = self._playback
        if not self.busy and playback is None:
            return
        if reason == "barge_in":
            self.metrics.count("barge_ins")
            if playback is not None:
                self._barge_in_at = time.monotonic()
        await self.set_state(
            State.INTERRUPTING, reason, deadline_s=self.realtime.interrupt_timeout_s
        )
        if self._token is not None:
            self._token.cancel(reason)  # no more audio for this turn from here on
        speakers = self.with_role("speaker")
        if playback is not None and speakers:
            # Silence first: that is what the user notices.
            self._stopping_turn = playback.turn_id
            await self.emit_to(
                speakers, ev.AUDIO_OUTPUT_STOP, {"turn_id": playback.turn_id, "reason": reason}
            )
        await self._cancel_turn(reason)
        self._playback = None
        if self._stopping_turn is None:
            self._barge_in_at = None
            await self.rest("nothing playing")

    async def cancel_turn(self, reason: str = "cancelled") -> None:
        """Cancel the turn in progress (conversation.cancel); stops its playback."""
        await self.interrupt(reason)

    async def _cancel_turn(self, reason: str) -> None:
        token = self._token
        if token is not None:
            token.cancel(reason)
        turn, self._turn = self._turn, None
        if turn is not None and not turn.done() and turn is not asyncio.current_task():
            turn.cancel()
            with contextlib.suppress(asyncio.CancelledError, Cancelled):
                await turn

    async def wait_turn(self) -> None:
        if self._turn is not None:
            with contextlib.suppress(asyncio.CancelledError):
                await self._turn

    async def run_turn(self, user_text: str) -> str:
        """Run one conversational turn; returns the assistant text."""
        rt = self.runtime
        turn_id = uuid.uuid4().hex[:12]
        self._turn_id = turn_id
        token = self._token or CancelToken()
        timings = self._timings or TurnTimings(transcript=TurnTimings.now())
        self._timings = None
        self.attention.note_user_turn()
        await rt.store.add_message(self.session_id, "user", user_text, turn_id=turn_id)
        # Previous turns plus the user message just added.
        limit = rt.config.agent.history_turns * 2 + 1
        history = await rt.store.history(self.session_id, limit=limit)
        messages: list[ChatMessage] = [{"role": m.role, "content": m.content} for m in history]
        context = AgentContext(persona=rt.persona, turn_id=turn_id)
        session = SessionRef(self.session_id, self.device_id)

        await self.set_state(State.THINKING, deadline_s=self._thinking_deadline_s())
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
        queue: asyncio.Queue[str | None] = asyncio.Queue()
        tts_task: asyncio.Task[bool] | None = None
        if self.speak and self.with_role("speaker"):
            tts_task = asyncio.create_task(self._tts_worker(turn_id, queue, token, timings))
        splitter = SentenceSplitter()
        try:
            async for item in rt.agent.respond(session, messages, context):
                token.raise_if_cancelled()
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
                if timings.first_token is None:
                    timings.first_token = TurnTimings.now()
                parts.append(item.text)
                await self.emit(ev.RESPONSE_DELTA, {"turn_id": turn_id, "text": item.text})
                for sentence in splitter.feed(item.text):
                    queue.put_nowait(sentence)
            for sentence in splitter.flush():
                queue.put_nowait(sentence)
            queue.put_nowait(None)
            if tts_task is not None:
                audio_sent = await tts_task
        except Cancelled:
            cancelled = True
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
                with contextlib.suppress(asyncio.CancelledError, Cancelled):
                    audio_sent = await tts_task or audio_sent
            text = "".join(parts).strip()
            await self._finish_turn(
                turn_id, text, cancelled=cancelled, audio_sent=audio_sent, reason=token.reason
            )
            self._record_timings(turn_id, timings)
            log.info(
                "turn finished",
                extra=kv(
                    session=self.session_id,
                    turn=turn_id,
                    cancelled=cancelled,
                    reason=token.reason if cancelled else None,
                    total_s=round(time.perf_counter() - started, 2),
                    chars=len(text),
                    **timings.stages(),
                ),
            )
        return text

    def _record_timings(self, turn_id: str, timings: TurnTimings) -> None:
        stages = timings.stages()
        for name, seconds in stages.items():
            self.metrics.record(name, seconds)
        if self.runtime.config.metrics.emit_turn and stages:
            self._spawn(self.emit(ev.METRICS_TURN, {"turn_id": turn_id, **stages}))

    async def _finish_turn(
        self,
        turn_id: str,
        text: str,
        *,
        cancelled: bool,
        audio_sent: bool,
        reason: str | None = None,
    ) -> None:
        playback = self._playback if self._playback and self._playback.turn_id == turn_id else None
        with contextlib.suppress(Exception):
            stored = text
            heard = playback.heard_text() if (cancelled and playback) else None
            if cancelled and heard is not None:
                # Keep what the user actually heard before cutting in.
                stored = f"{heard}…" if heard else ""
            if stored:
                self.attention.note_agent_speech()
                await self.runtime.store.add_message(
                    self.session_id, "assistant", stored, turn_id=turn_id
                )
            done: dict[str, Any] = {"turn_id": turn_id, "text": text, "cancelled": cancelled}
            if cancelled and reason:
                done["reason"] = reason
            if heard is not None:
                done["heard_text"] = heard
            await self.emit(ev.RESPONSE_DONE, done)
            if audio_sent:
                await self.emit(ev.AUDIO_OUTPUT_DONE, {"turn_id": turn_id})
            if self._turn_id == turn_id:
                self._turn_id = None
            if self.state in (State.INTERRUPTING, State.RECOVERING):
                return  # whoever cancelled the turn decides what comes next
            if audio_sent and not cancelled and playback is not None:
                await self.set_state(
                    State.SPEAKING, "awaiting playback", deadline_s=self._speaking_deadline_s()
                )
            else:
                await self.rest("turn finished")

    async def _tts_worker(
        self,
        turn_id: str,
        queue: asyncio.Queue[str | None],
        token: CancelToken,
        timings: TurnTimings | None = None,
    ) -> bool:
        tts = self.runtime.tts
        assert tts is not None
        seq = 0
        failed = False
        while (sentence := await queue.get()) is not None:
            if failed:
                continue
            try:
                audio = await token.run(tts.synthesize(sentence, voice=self.runtime.persona.voice))
            except Cancelled:
                break
            except CompanionError as exc:
                failed = True  # continue text-only for the rest of this turn
                await self.emit_error(exc)
                continue
            if token.cancelled:
                break
            header = make_event(
                ev.AUDIO_OUTPUT_CHUNK,
                {"turn_id": turn_id, "seq": seq, "mime": audio.mime, "text": sentence},
                session_id=self.session_id,
                device_id=self.device_id,
            )
            self._track_chunk(turn_id, seq, sentence, audio.data, audio.mime)
            for device in self.with_role("speaker"):
                await device.outbox.send_binary(header, audio.data)
            if seq == 0:
                if timings is not None:
                    timings.first_audio = TurnTimings.now()
                await self.set_state(State.SPEAKING, deadline_s=self._speaking_deadline_s())
            elif self.state == State.SPEAKING:
                # More audio queued: push the deadline out accordingly.
                await self.set_state(State.SPEAKING, deadline_s=self._speaking_deadline_s())
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
        self._turn_id = turn_id
        token = self._token or CancelToken()
        self._proactive = True
        try:
            await self.set_state(
                State.THINKING, f"proactive: {event.kind}", deadline_s=self._thinking_deadline_s()
            )
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
                    token.raise_if_cancelled()
                    if item.kind == "text":
                        parts.append(item.text)
            except CompanionError as exc:
                log.warning("proactive turn failed", extra=kv(error=str(exc)))
                return
            text = "".join(parts).strip()
            if not text or SILENT in text:
                log.info("proactive turn: agent chose silence", extra=kv(kind=event.kind))
                return
            # We asked for one short remark; some models draft several
            # alternatives separated by blank lines. Say only the first.
            first, *rest = re.split(r"\n\s*\n", text, maxsplit=1)
            if rest:
                log.info(
                    "proactive reply trimmed to its first paragraph", extra=kv(kind=event.kind)
                )
                text = first.strip()
            self._proactive = False  # speaking now: half-duplex applies again
            await self._speak_proactively(turn_id, text, event, token)
        except Cancelled:
            pass
        finally:
            self._proactive = False
            if self._turn_id == turn_id:
                self._turn_id = None
            if self.state == State.THINKING:
                await self.rest("proactive turn ended")

    async def _speak_proactively(
        self, turn_id: str, text: str, event: AttentionEvent, token: CancelToken
    ) -> None:
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
            audio_sent = await self._tts_worker(turn_id, queue, token)
        await self._finish_turn(turn_id, text, cancelled=token.cancelled, audio_sent=audio_sent)

    async def close(self) -> None:
        self._mic = None
        await self.vision.close()
        await self._cancel_turn("session closed")
        self._playback = None
        watchdog, self._watchdog = self._watchdog, None
        for task in [*self._tasks, *([watchdog] if watchdog else [])]:
            if task is not asyncio.current_task():
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task
