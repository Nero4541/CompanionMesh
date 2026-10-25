"""Conversation session: turn pipeline, full-duplex audio, vision.

A session can have several devices attached at once (e.g. a browser with the
mic and speaker plus a separate IP camera). Events go to every device; speech
audio goes only to devices with the "speaker" role. Several microphones may feed
one conversation (a board and the phone page): each has its own speech
detection, and the first sentence to end wins; the others' overlapping speech
is dropped, since they heard the same thing.

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
from companion.attention.decisions import ProactiveDecision, decision_prompt, parse_decision
from companion.attention.engine import IDLE_KIND, AttentionEngine, AttentionEvent, Decision
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
from companion.core.pipeline import VoiceTest
from companion.core.states import State, StateMachine
from companion.core.text import SentenceSplitter
from companion.protocol import Envelope, make_event
from companion.protocol import events as ev
from companion.providers.llm.base import ChatMessage
from companion.providers.stt.base import made_up
from companion.vision.pipeline import TurnImage, VisionChannel

if TYPE_CHECKING:
    from companion.core.config import CompanionConfig
    from companion.core.runtime import CompanionRuntime
    from companion.perception.routing import Route
    from companion.perception.speakers import Speaker

__all__ = ["DEFAULT_ROLES", "Device", "Outbox", "Session", "State", "parse_roles"]

log = logging.getLogger(__name__)

MIN_UTTERANCE_S = 0.3
PHONE_DEVICE = "phone"  # the phone page (/view/) as a device: push to talk, its speaker
# SPEAKING lasts at most this long when the length of the audio is unknown.
PLAYBACK_ACK_TIMEOUT_S = 90.0
CONTEXT_NOTE_MAX_AGE_S = 1800.0
WATCHDOG_INTERVAL_S = 0.5
IDLE_CHECK_S = 15.0  # how often the idle check-in timer is looked at
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


def image_parts(images: list[TurnImage]) -> list[dict[str, Any]]:
    """Chat-completions content parts for the camera images of one turn."""
    return [
        {
            "type": "image_url",
            "image_url": {"url": "data:image/jpeg;base64," + base64.b64encode(i.jpeg).decode()},
        }
        for i in images
    ]


def image_note(images: list[TurnImage]) -> str:
    first = images[0]
    if len(images) == 1:
        return f"(Camera image from {first.device_id}, {round(first.age_s)} s ago)"
    return (
        f"({len(images)} camera images from {first.device_id}, oldest first; "
        f"{round(first.age_s)} s to {round(images[-1].age_s)} s ago)"
    )


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
    # Remote settings (device.config); None if the device does not offer them.
    settings: dict[str, Any] | None = None
    # Stream mode (protocol 1.4): what the device offered in session.start
    # ({formats, max_fps}), or None.
    stream: dict[str, Any] | None = None
    # Requests waiting for the device's answer, by request_id
    # (device.config.set -> .result, device.diagnostics.request -> device.diagnostics).
    settings_requests: dict[str, asyncio.Future[dict[str, Any]]] = field(default_factory=dict)

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
            "configurable": self.settings is not None,
        }


@dataclass
class _Mic:
    """The open microphone stream of one device."""

    device: Device
    full_duplex: bool  # keeps sending while the companion speaks
    device_aec: bool  # the device cancels its own speaker's echo
    canceller: EchoCanceller
    segmenter: UtteranceSegmenter
    decoder: Any = None  # OpusDecoder when the mic sends opus
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
    devices: list[Device] = field(default_factory=list)  # the speakers it was sent to

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
        self._speak = speak
        self.attention = AttentionEngine(runtime.config.attention)
        self._context_notes: list[AttentionEvent] = []
        # Sentences not meant for the companion (routing), for the next turn: (when, line).
        self._overheard: list[tuple[float, str]] = []
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
        self._mics: list[_Mic] = []
        self._speech_end: float | None = None  # monotonic, end of the last utterance
        self._playback: _Playback | None = None
        self._barge_in_at: float | None = None
        self._stopping_turn: str | None = None  # audio.output.stop sent, not confirmed
        echo = self.realtime.echo
        self.echo_guard = EchoGuard(similarity=echo.guard_similarity, window_s=echo.guard_window_s)
        self.reference = ReferenceBuffer()
        self.vision = VisionChannel(self)
        # Lists that collect raw microphone audio of one device for a while (the dashboard's
        # mic test).
        self.mic_taps: list[tuple[Device, list[np.ndarray]]] = []
        # The dashboard's voice input test, by device id: the next sentence is only recognised.
        self.voice_tests: dict[str | None, VoiceTest] = {}
        self._test_acks: dict[str, asyncio.Future[str | None]] = {}  # speaker tests, by turn
        self._watchdog: asyncio.Task[None] | None = None
        self._tasks: set[asyncio.Task[Any]] = set()
        with contextlib.suppress(RuntimeError):  # no loop: built outside asyncio (tests)
            self._watchdog = asyncio.get_running_loop().create_task(self._watch())
            self._spawn(self._idle_loop())

    @property
    def state(self) -> State:
        return self.sm.state

    def reconfigure(self, config: CompanionConfig) -> None:
        """Follow live setting changes (realtime, attention, vision)."""
        self.realtime = config.realtime
        self.echo_guard.similarity = config.realtime.echo.guard_similarity
        self.echo_guard.window_s = config.realtime.echo.guard_window_s
        self.attention.config = config.attention
        self.vision.reconfigure(config)

    @property
    def _mic(self) -> _Mic | None:
        """The first open microphone (most conversations have only one)."""
        return self._mics[0] if self._mics else None

    def mic_of(self, device: Device | None) -> _Mic | None:
        if device is None:
            return self._mic
        return next((m for m in self._mics if m.device is device), None)

    def _reset_segmenters(self) -> None:
        for mic in self._mics:
            mic.segmenter.reset()

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
        if self.mic_of(device) is not None:
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

    def outputs_for(self, origin: Device | None) -> list[Device]:
        """The speakers that play the answer to what ``origin`` heard (phone.answer_on)."""
        speakers = self.with_role("speaker")
        phones = [d for d in speakers if d.device_id == PHONE_DEVICE]
        boards = [d for d in speakers if d.device_id != PHONE_DEVICE]
        if not phones or not boards:
            return speakers
        choice = self.runtime.config.phone.answer_on
        if choice == "both":
            return speakers
        if choice == "phone":
            return phones
        if choice == "board":
            return boards
        if origin is not None and origin.device_id == PHONE_DEVICE:
            return phones
        return [origin] if origin in boards else boards

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
        self._reset_segmenters()
        await self.rest("recovered")

    # --- text input -----------------------------------------------------

    async def handle_text(self, text: str) -> None:
        text = text.strip()
        if not text:
            return
        await self.interrupt("new_message")
        await self.vision.request_capture("text")
        self.runtime.feed.said(self.session_id, "user", text, typed=True)
        self._log_speech("you (typed)", "user", text, typed=True)
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
        if self.runtime.stt is None and not self.runtime.config.builtin.stt.enabled:
            await self.emit_error("speech input is disabled (stt.provider: none)", code="stt_off")
            return
        decoder = None
        if encoding == "opus":
            from companion.audio.opus import OpusDecoder

            try:
                decoder = OpusDecoder()
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
        owner = device or self.devices[0]
        mic = _Mic(
            device=owner,
            full_duplex=bool(payload.get("full_duplex")),
            device_aec=device_aec,
            canceller=canceller,
            segmenter=self.runtime.new_segmenter(),
            decoder=decoder,
        )
        self._mics = [m for m in self._mics if m.device is not owner] + [mic]
        log.info(
            "microphone open",
            extra=kv(
                session=self.session_id,
                device=owner.device_id,
                encoding=encoding,
                full_duplex=mic.full_duplex,
                aec="device" if device_aec else echo.canceller,
            ),
        )
        if self.state == State.IDLE:
            await self.set_state(State.LISTENING, "microphone open")

    async def audio_stop(self, device: Device | None = None) -> None:
        mic = self.mic_of(device)
        if mic is None:
            return  # only the device that opened a mic can close it
        self._mics.remove(mic)
        if self.state == State.LISTENING:
            utterance = mic.segmenter.flush()
            if utterance is not None:
                await self._on_utterance(utterance, mic)
                return
        if self.state == State.LISTENING and not self._mics:
            await self.set_state(State.IDLE, "microphone closed")

    def _playing(self) -> bool:
        """Reply audio may be coming out of a speaker right now."""
        return self._playback is not None or self._stopping_turn is not None

    def _echo_risk(self, mic: _Mic) -> bool:
        return not mic.device_aec and self._playing()

    async def audio_chunk(
        self, data: bytes, device: Device | None = None, meta: dict[str, Any] | None = None
    ) -> None:
        mic = self.mic_of(device)
        if mic is None:
            return
        samples = mic.decoder.decode(data) if mic.decoder else pcm16_to_float32(data)
        for owner, tap in self.mic_taps:
            if owner is mic.device:
                tap.append(samples)
        self.runtime.pipeline.audio(mic.device.device_id, samples)
        test = self.voice_tests.get(mic.device.device_id)
        if test is not None:
            test.heard(samples)
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
        segmenter = mic.segmenter
        segmenter.strict = (
            (self.realtime.barge_in_threshold, self.realtime.barge_in_min_speech_ms)
            if self._echo_risk(mic)
            else None
        )
        for item in segmenter.feed(samples):
            if isinstance(item, SpeechStart):
                self._record_mic_to_vad(mic)
                self.runtime.pipeline.speech(mic.device.device_id)
                if test is not None:
                    test.speech = True
                    continue  # a test sentence interrupts nothing
                if self._proactive or self.busy or self._playing():
                    await self.interrupt("barge_in")  # the user comes first
                await self.emit(ev.AUDIO_VAD, {"state": "speech_start"})
                await self.vision.request_capture("speech")
            elif isinstance(item, Utterance):
                if test is None:
                    await self.emit(ev.AUDIO_VAD, {"state": "speech_end"})
                await self._on_utterance(item, mic)
                break

    def _record_mic_to_vad(self, mic: _Mic) -> None:
        if mic.last_capture_ms is not None:
            age = mic.device.clock.age_ms(mic.last_capture_ms)
            if age is not None:
                self.metrics.record("mic_to_vad", age / 1000)

    async def _on_utterance(self, utterance: Utterance, mic: _Mic | None = None) -> None:
        test = self.voice_tests.get(mic.device.device_id) if mic is not None else None
        if test is not None:
            mic.segmenter.reset()  # type: ignore[union-attr]
            if utterance.duration_s >= MIN_UTTERANCE_S:
                self._spawn(self._run_voice_test(test, utterance.audio))
            return
        # The other microphones heard the same sentence: theirs is dropped.
        self._reset_segmenters()
        if utterance.duration_s < MIN_UTTERANCE_S:
            if self.state == State.INTERRUPTING:
                await self.rest("utterance too short")
            return
        await self.interrupt("new_utterance")
        self._speech_end = TurnTimings.now()
        self._timings = TurnTimings(speech_end=self._speech_end)
        # The speech ended when the silence that closed it began.
        ended = time.time() - self.runtime.config.vad.min_silence_ms / 1000
        origin = mic.device if mic is not None else None
        self._start_turn(self._voice_turn(utterance.audio, ended, origin=origin))

    async def _run_voice_test(self, test: VoiceTest, audio: np.ndarray) -> None:
        """The dashboard's voice input test: recognise the sentence, answer nothing."""
        test.utterance_s = audio.size / SAMPLE_RATE
        stt = self.runtime.stt
        if stt is None:
            test.finish(error="speech recognition is not running")
            return
        prompt = self.runtime.config.stt.prompt
        started = time.perf_counter()
        try:
            transcript = await stt.transcribe(audio, **({"prompt": prompt} if prompt else {}))
        except CompanionError as exc:
            self.runtime.pipeline.stt.note(False, str(exc))
            test.finish(error=str(exc))
            return
        test.stt_s = time.perf_counter() - started
        self.runtime.pipeline.stt.note(True, _heard(transcript.text, test.stt_s))
        test.finish(text=transcript.text)

    def _attribute(self, audio: np.ndarray, ended: float | None) -> asyncio.Task[Any] | None:
        """Who said it (perception.speakers), worked out while speech is recognised."""
        perception = self.runtime.perception
        if ended is None or not self.runtime.config.perception.speakers or not perception.ready:
            return None
        start = ended - audio.size / SAMPLE_RATE
        return asyncio.create_task(perception.attribute(audio, start, ended))

    async def _speaker(self, task: asyncio.Task[Any] | None) -> Speaker | None:
        if task is None:
            return None
        try:
            return await asyncio.wait_for(task, timeout=5.0)
        except (TimeoutError, CompanionError) as exc:
            log.warning("speaker unknown", extra=kv(session=self.session_id, error=str(exc)))
            return None

    async def push_to_talk(self, audio: np.ndarray) -> None:
        """A sentence from push to talk (a phone): the user, speaking to the companion."""
        self.runtime.pipeline.audio(PHONE_DEVICE, audio)
        self.runtime.pipeline.speech(PHONE_DEVICE, "push to talk")
        test = self.voice_tests.get(PHONE_DEVICE)
        if test is not None:
            test.heard(audio)
            test.speech = True
            await self._run_voice_test(test, audio)
            return
        await self.interrupt("new_utterance")
        self._speech_end = TurnTimings.now()
        self._timings = TurnTimings(speech_end=self._speech_end)
        phone = next((d for d in self.devices if d.device_id == PHONE_DEVICE), None)
        self._start_turn(self._voice_turn(audio, pushed=True, origin=phone))

    def _pusher(self) -> Speaker | None:
        """Who uses push to talk: the user (signed in with the admin token)."""
        from companion.perception.speakers import Speaker

        user = self.runtime.perception.registry.user
        if user is None:
            return None
        return Speaker(user.id, user.name, "user", "high", False, "push_to_talk", None, None)

    async def _voice_turn(
        self,
        audio: np.ndarray,
        ended: float | None = None,
        *,
        pushed: bool = False,
        origin: Device | None = None,
    ) -> None:
        stt = self.runtime.stt
        if stt is None:  # the built-in engine is still starting, or was turned off
            await self.emit_error(
                "speech recognition is not ready yet (see Speech in the dashboard)",
                code="stt_starting",
            )
            return
        await self.set_state(State.TRANSCRIBING, deadline_s=self.realtime.transcribe_timeout_s)
        started = time.perf_counter()
        prompt = self.runtime.config.stt.prompt
        attributing = None if pushed else self._attribute(audio, ended)
        try:
            transcript = await stt.transcribe(audio, **({"prompt": prompt} if prompt else {}))
        except CompanionError as exc:
            if attributing is not None:
                attributing.cancel()
            self.runtime.pipeline.stt.note(False, str(exc))
            await self.emit_error(exc)
            await self.rest("transcription failed")
            return
        if self._timings is not None:
            self._timings.transcript = TurnTimings.now()
        stt_s = time.perf_counter() - started
        invented = bool(transcript.text) and made_up(transcript.text)
        echo = bool(
            transcript.text
            and not invented
            and not pushed  # the phone is held to the mouth, not near the speaker
            and self.realtime.echo.guard
            and self.echo_guard.is_echo(transcript.text)
        )
        outcome = (
            "made_up" if invented else "echo" if echo else "turn" if transcript.text else "empty"
        )
        self.runtime.pipeline.stt.note(
            True,
            {
                "made_up": "dropped: made up from noise",
                "echo": "dropped: its own voice heard back",
            }.get(outcome, _heard(transcript.text, stt_s)),
        )
        heard_on = origin.device_id if origin is not None else PHONE_DEVICE if pushed else None
        recording_id = await self._keep_recording(
            audio, transcript.text, outcome, stt_s, device_id=heard_on
        )
        log.info(
            "transcribed",
            extra=kv(
                session=self.session_id,
                audio_s=round(audio.size / SAMPLE_RATE, 2),
                stt_s=round(stt_s, 2),
                text=transcript.text,
            ),
        )
        if (not transcript.text or echo or invented) and attributing is not None:
            attributing.cancel()
        if not transcript.text:
            await self.rest("nothing recognized")
            return
        if invented:  # Whisper's invention for noise, such as "thanks for watching"
            self.metrics.count("made_up_dropped")
            await self.rest("made up")
            return
        if echo:
            log.info(
                "dropped own voice heard back",
                extra=kv(session=self.session_id, text=transcript.text),
            )
            self.metrics.count("echo_dropped")
            await self.rest("echo")
            return
        if pushed:  # said to the companion by the user: no guessing who, or to whom
            speaker, route = self._pusher(), None
        else:
            speaker = await self._speaker(attributing)
            route = await self._route(speaker, transcript.text)
        heard: dict[str, Any] = {"text": transcript.text, "final": True}
        extra: dict[str, Any] = {}
        if speaker is not None:
            extra["speaker"] = speaker.to_payload()
        if route is not None:
            extra["route"] = route.route
        await self.emit(ev.CONVERSATION_TRANSCRIPT, {**heard, **extra})
        self.runtime.feed.said(
            self.session_id,
            "user",
            transcript.text,
            device_id=heard_on,
            recording_id=recording_id,
            **extra,
        )
        self._log_speech(
            speaker.describe() if speaker is not None else "the user",
            speaker.role if speaker is not None else "user",
            transcript.text,
            route=route.route if route is not None else "turn",
        )
        if route is not None and route.route != "turn":
            await self._not_for_me(route, speaker, transcript.text)
            return
        await self.run_turn(transcript.text, speaker=speaker, origin=origin)

    def _log_speech(
        self, who: str, role: str, text: str, *, route: str = "turn", typed: bool = False
    ) -> None:
        """Keep the line for the agent's recent_speech tool (only while the tools are on)."""
        cfg = self.runtime.config.mcp
        if cfg.enabled and text:
            from companion.agent.tools import SpokenLine

            line = SpokenLine(time.time(), who, role, text, route, typed)
            self.runtime.speech_log.add(line, cfg.speech_minutes)

    async def _route(self, speaker: Speaker | None, text: str) -> Route | None:
        """Whether a sentence is meant for the companion (routing.enabled); None when off."""
        from companion.perception.routing import FACING_YAW, decide

        cfg = self.runtime.config.routing
        if not cfg.enabled:
            return None
        persona = self.runtime.persona
        names = cfg.wake_names or [n for n in (persona.display_name, persona.name) if n]
        if not cfg.use_wake_names:
            names = []
        last = self.attention.state.last_agent_speech_at
        since = (self.attention.clock() - last).total_seconds() if last else None
        facing = False
        if speaker is not None and speaker.track_id and cfg.facing_cameras:
            track = self.runtime.perception.find_track(speaker.track_id)
            facing = (
                track is not None
                and track.camera in cfg.facing_cameras
                and track.yaw is not None
                and abs(track.yaw) < FACING_YAW
            )
        route = decide(
            speaker,
            text,
            wake_names=names,
            since_companion_spoke_s=since,
            followup_s=cfg.followup_s,
            people_around=len(self.runtime.perception.people_here()),
            facing=facing,
            alone_means_addressed=cfg.alone_means_addressed,
            answer_acquaintances=cfg.answer_acquaintances,
            answer_strangers=cfg.answer_strangers,
        )
        payload = {
            **route.to_payload(),
            "speaker": speaker.to_payload() if speaker is not None else None,
        }
        log.info(
            "routing decision",
            extra=kv(session=self.session_id, route=route.route, why="; ".join(route.reasons)),
        )
        await self.runtime.bus.publish(
            make_event(ev.ROUTING_DECISION, {**payload, "session_id": self.session_id})
        )
        if cfg.debug:
            await self.emit_to(self.devices, ev.ROUTING_DECISION, payload)
        return route

    async def _not_for_me(self, route: Route, speaker: Speaker | None, text: str) -> None:
        """A sentence not meant for the companion: kept as context, or only logged."""
        if route.route == "context" and speaker is not None:
            self.attention.note_user_turn()  # people are talking: no remarks right now
            self._overheard.append((time.time(), f"{speaker.describe()}: 「{text}」"))
            del self._overheard[:-10]
            if speaker.role == "stranger":
                await self.on_attention_event(
                    AttentionEvent(
                        kind="stranger_spoke",
                        description="Someone the companion does not know (a stranger) spoke.",
                        salience=0.5,
                        source="perception",
                        at=self.attention.clock(),
                        data={"text_length": len(text)},
                    )
                )
        await self.rest("not for the companion")

    async def _keep_recording(
        self, audio: np.ndarray, text: str, outcome: str, stt_s: float, *, device_id: str | None
    ) -> str | None:
        """Save the utterance for the dashboard (``stt.keep_audio``); its id, or None."""
        if not self.runtime.config.stt.keep_audio:
            return None
        try:
            rec = await asyncio.to_thread(
                self.runtime.recordings.save,
                audio,
                session_id=self.session_id,
                device_id=device_id,
                text=text,
                outcome=outcome,
                stt_s=stt_s,
            )
        except OSError as exc:
            log.warning("cannot keep the recording", extra=kv(error=str(exc)))
            return None
        return rec.id

    # --- playback ---------------------------------------------------------

    async def playback_finished(self, turn_id: str | None, device: Device | None = None) -> None:
        """The device played all of a turn's audio (audio.output.played)."""
        waiting = self._test_acks.get(turn_id or "")
        if waiting is not None:
            if not waiting.done():
                waiting.set_result(device.device_id if device is not None else None)
            return
        playback = self._playback
        if playback is None or (turn_id and turn_id != playback.turn_id):
            return
        if device is not None and playback.devices and device not in playback.devices:
            return  # it was not sent this answer (phone.answer_on)
        self.runtime.pipeline.played(
            device.device_id if device is not None else None, True, "played the answer"
        )
        await self._playback_done(playback.turn_id, "played")

    async def _playback_done(self, turn_id: str, reason: str) -> None:
        if self._playback is not None and self._playback.turn_id == turn_id:
            if reason != "played":
                for device in self._playback.devices:
                    self.runtime.pipeline.played(device.device_id, False, reason)
            self._playback = None
        for mic in self._mics:
            if not mic.segmenter.in_speech:
                mic.segmenter.reset()  # drop the tail of our own voice
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

    def _track_chunk(
        self, turn_id: str, seq: int, text: str, audio: bytes, mime: str, devices: list[Device]
    ) -> None:
        playback = self._playback
        if playback is None or playback.turn_id != turn_id:
            playback = self._playback = _Playback(turn_id, devices=list(devices))
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

    async def run_turn(
        self, user_text: str, *, speaker: Speaker | None = None, origin: Device | None = None
    ) -> str:
        """Run one conversational turn; returns the assistant text.

        With a ``speaker`` (perception.speakers), the agent sees who said it:
        「...」 (Yukari, the user); the stored transcript keeps the words alone.
        """
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
        said = speaker.label(user_text) if speaker is not None else user_text
        lead = "Said" if speaker is not None else "User"
        text = f"{prefix}\n\n{lead}: {said}" if prefix else said
        if seen.images:
            messages[-1] = {
                "role": "user",
                "content": [
                    {"type": "text", "text": f"{image_note(seen.images)}\n{text}"},
                    *image_parts(seen.images),
                ],
            }
            for image in seen.images:
                await self.emit(
                    ev.VISION_FRAME_USED, {"frame_id": image.frame_id, "turn_id": turn_id}
                )
        elif seen.user_prefix or speaker is not None:
            messages[-1] = {"role": "user", "content": text}

        parts: list[str] = []
        audio_sent = False
        cancelled = False
        started = time.perf_counter()
        queue: asyncio.Queue[str | None] = asyncio.Queue()
        tts_task: asyncio.Task[bool] | None = None
        outputs = self.outputs_for(origin)
        if self.speak and outputs:
            tts_task = asyncio.create_task(
                self._tts_worker(turn_id, queue, token, timings, outputs)
            )
        failure: str | None = None
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
            failure = str(exc)
            await self.emit_error(exc)
        except Exception as exc:
            failure = f"internal error: {exc}"
            log.exception("turn failed", extra=kv(session=self.session_id))
            await self.emit_error(f"internal error: {exc}", code="internal_error")
        finally:
            if tts_task is not None and not tts_task.done():
                tts_task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Cancelled):
                    audio_sent = await tts_task or audio_sent
            text = "".join(parts).strip()
            if failure is not None:
                rt.pipeline.agent.note(False, failure)
            elif not cancelled:
                took = time.perf_counter() - started
                rt.pipeline.agent.note(
                    bool(text), f"answered in {took:.1f} s" if text else "answered nothing"
                )
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
                self.runtime.feed.said(
                    self.session_id, "companion", stored, cancelled=cancelled, turn_id=turn_id
                )
                self._log_speech("the companion (you)", "companion", stored)
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
        outputs: list[Device] | None = None,
    ) -> bool:
        tts = self.runtime.tts
        assert tts is not None
        speakers = outputs if outputs is not None else self.with_role("speaker")
        seq = 0
        failed = False
        while (sentence := await queue.get()) is not None:
            if failed:
                continue
            started = time.perf_counter()
            try:
                audio = await token.run(tts.synthesize(sentence, voice=self.runtime.persona.voice))
            except Cancelled:
                break
            except CompanionError as exc:
                failed = True  # continue text-only for the rest of this turn
                self.runtime.pipeline.tts.note(False, str(exc))
                await self.emit_error(exc)
                continue
            self.runtime.pipeline.tts.note(
                True, f"{len(sentence)} characters in {time.perf_counter() - started:.1f} s"
            )
            if token.cancelled:
                break
            header = make_event(
                ev.AUDIO_OUTPUT_CHUNK,
                {"turn_id": turn_id, "seq": seq, "mime": audio.mime, "text": sentence},
                session_id=self.session_id,
                device_id=self.device_id,
            )
            self._track_chunk(turn_id, seq, sentence, audio.data, audio.mime, speakers)
            for device in speakers:
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

    @property
    def speak(self) -> bool:
        """Replies are spoken: asked for, and a TTS is running (built-in ones come and go)."""
        return self._speak and self.runtime.tts is not None

    @property
    def mic_device(self) -> Device | None:
        """The device whose microphone feeds this session, if any."""
        return self._mic.device if self._mic is not None else None

    async def say_test(
        self, device: Device, text: str, *, confirm_s: float | None = None
    ) -> dict[str, Any]:
        """Speak ``text`` on one device only, outside the conversation (speaker test).

        With ``confirm_s``, wait that long for the device to say it played it.
        """
        tts = self.runtime.tts
        if tts is None:
            raise CompanionError("no text-to-speech is configured (tts.provider)", code="no_tts")
        started = time.perf_counter()
        try:
            audio = await tts.synthesize(text, voice=self.runtime.persona.voice)
        except CompanionError as exc:
            self.runtime.pipeline.tts.note(False, str(exc))
            raise
        tts_s = time.perf_counter() - started
        self.runtime.pipeline.tts.note(True, f"{len(text)} characters in {tts_s:.1f} s")
        turn_id = f"test-{uuid.uuid4().hex[:8]}"
        self.echo_guard.spoke(text)  # the microphone may hear it
        header = make_event(
            ev.AUDIO_OUTPUT_CHUNK,
            {"turn_id": turn_id, "seq": 0, "mime": audio.mime, "text": text},
            session_id=self.session_id,
        )
        waiting: asyncio.Future[str | None] | None = None
        if confirm_s:
            waiting = asyncio.get_running_loop().create_future()
            self._test_acks[turn_id] = waiting
        try:
            await device.outbox.send_binary(header, audio.data)
            await self.emit_to([device], ev.AUDIO_OUTPUT_DONE, {"turn_id": turn_id})
            played: bool | None = None
            if waiting is not None:
                try:
                    await asyncio.wait_for(waiting, confirm_s)
                    played = True
                except TimeoutError:
                    played = False
                self.runtime.pipeline.played(
                    device.device_id, played, "played the test" if played else "test not confirmed"
                )
        finally:
            self._test_acks.pop(turn_id, None)
        return {
            "tts_s": round(tts_s, 2),
            "played": played,
            "seconds": audio_duration_s(audio.data, audio.mime),
        }

    # --- attention (v0.3) ------------------------------------------------------

    def _take_context_notes(self) -> str | None:
        now = self.attention.clock()
        notes = [
            n for n in self._context_notes if (now - n.at).total_seconds() <= CONTEXT_NOTE_MAX_AGE_S
        ]
        self._context_notes = []
        wall = time.time()
        overheard = [(t, line) for t, line in self._overheard if wall - t <= CONTEXT_NOTE_MAX_AGE_S]
        self._overheard = []
        parts = []
        if notes:
            lines = "\n".join(
                f"- {round((now - n.at).total_seconds())} s ago: {n.description}" for n in notes
            )
            parts.append(f"Noticed since the user's last message:\n{lines}")
        if overheard:
            lines = "\n".join(f"- {round(wall - t)} s ago, {line}" for t, line in overheard)
            parts.append(f"Overheard (not said to you):\n{lines}")
        return "\n\n".join(parts) or None

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

    async def _idle_loop(self) -> None:
        """Idle check-ins (attention.idle): a chance to speak after a long quiet spell."""
        while True:
            await asyncio.sleep(IDLE_CHECK_S)
            if not self.with_role("speaker"):
                continue
            busy = self.busy or self.state not in (State.IDLE, State.LISTENING)
            event = self.attention.idle_event(busy=busy)
            if event is not None:
                await self.on_attention_event(event)

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
            structured = rt.config.attention.decisions
            prompt = (
                decision_prompt(event.description, self._situation())
                if structured
                else PROACTIVE_PROMPT.format(description=event.description)
            )
            if structured and rt.config.attention.triage.enabled:
                verdict = await self._triage(prompt)
                if verdict is not None and verdict.action == "silent":
                    await self._decided(verdict, event, by="triage")
                    return
            if event.kind == IDLE_KIND and self.vision.enabled:
                # Nothing on camera prompted this one: let the agent see the room now.
                await self.vision.request_capture("idle")
                await self.vision.wait_for_capture()
                await self.vision.wait_pending()
            seen = self.vision.context_for_turn()
            if event.kind == IDLE_KIND and seen.user_prefix:  # vlm mode: what it saw
                prompt = f"{prompt}\n\n{seen.user_prefix}"
            content: Any = prompt
            if seen.images:
                content = [{"type": "text", "text": prompt}, *image_parts(seen.images)]
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
            if structured:
                decision = parse_decision(text)
                await self._decided(decision, event, by="agent")
                if decision.action != "reply":
                    return
                text = decision.text
            elif not text or SILENT in text:
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

    def _situation(self) -> list[str]:
        """Who is around and what has been going on, for a proactive decision."""
        from datetime import datetime

        from companion.attention.engine import resolve_zone

        lines: list[str] = []
        zone = resolve_zone(self.runtime.config.attention.quiet_hours.timezone)
        perception = self.runtime.perception
        if perception.ready:
            here = perception.people_here()
            if here:
                people = []
                for p in here:
                    since = datetime.fromtimestamp(p["since"], zone).strftime("%H:%M")
                    who = p["name"] or "a stranger"
                    role = {"user": "the user", "acquaintance": "an acquaintance"}.get(p["role"])
                    people.append(
                        f"{who} ({role}, since {since})" if role else f"{who} (since {since})"
                    )
                lines.append("People here now: " + "; ".join(people) + ".")
            else:
                lines.append("Nobody is in view of the cameras.")
        if self.runtime.config.timeline.enabled:
            lines.extend(perception.timeline.context_lines())
        doing = perception.state.doing if perception.ready else {}
        if doing:
            now = time.time()
            parts = [
                f"{d.name or 'a stranger'}: {d.activity.replace('_', ' ')}"
                f" for {max(1, round((now - d.since) / 60))} min"
                + (f", looking at {d.looking_at}" if d.looking_at else "")
                for d in doing.values()
            ]
            lines.append("Doing now: " + "; ".join(parts) + ".")
        lines.append("Time now: " + datetime.now(zone).strftime("%A %H:%M") + ".")
        return lines

    async def _triage(self, prompt: str) -> ProactiveDecision | None:
        """The triage model's decision (attention.triage); None if it did not answer."""
        from companion.core.config import LLMConfig
        from companion.providers.llm.openai_compatible import OpenAICompatibleLLM

        cfg = self.runtime.config.attention.triage
        llm = OpenAICompatibleLLM(
            LLMConfig(
                base_url=cfg.base_url,
                api_key_env=cfg.api_key_env,
                model=cfg.model,
                timeout=cfg.timeout,
                connect_timeout=cfg.connect_timeout,
                temperature=0.0,
                max_tokens=cfg.max_tokens,
                extra_body=cfg.extra_body,
            )
        )
        messages: list[ChatMessage] = [
            {
                "role": "system",
                "content": "You decide for a voice companion whether to react to an event. "
                "Answer with one JSON object only.",
            },
            {"role": "user", "content": prompt},
        ]
        try:
            return parse_decision(await llm.chat(messages))
        except CompanionError as exc:
            log.warning("triage model did not answer", extra=kv(error=str(exc)))
            return None
        finally:
            await llm.aclose()

    async def _decided(self, decision: ProactiveDecision, event: AttentionEvent, by: str) -> None:
        """Log a proactive decision, and keep a note when the decision is to remember."""
        payload = {
            "kind": event.kind,
            "action": decision.action,
            "reason": decision.reason,
            "parsed": decision.parsed,
            "by": by,
            "session_id": self.session_id,
        }
        log.info("proactive decision", extra=kv(**payload))
        if not decision.parsed:
            self.metrics.count("proactive_unparsed")
            log.warning("proactive_unparsed", extra=kv(kind=event.kind, by=by))
        await self.runtime.bus.publish(make_event(ev.ATTENTION_PROACTIVE, payload))
        if decision.action == "remember":
            from companion.vision.observation import VisualObservation

            with contextlib.suppress(Exception):
                await self.runtime.store.add_observation(
                    VisualObservation(
                        timestamp=event.at,
                        device_id=str(event.data.get("device") or event.source),
                        description=decision.note[:500],
                        confidence=None,
                        tags=["remember", event.kind],
                        source_event_id=event.kind,
                        source="event",
                        session_id=self.session_id,
                    )
                )

    async def _speak_proactively(
        self, turn_id: str, text: str, event: AttentionEvent, token: CancelToken
    ) -> None:
        await self.emit(
            ev.RESPONSE_START, {"turn_id": turn_id, "proactive": True, "reason": event.kind}
        )
        await self.emit(ev.RESPONSE_DELTA, {"turn_id": turn_id, "text": text})
        audio_sent = False
        outputs = self.outputs_for(None)  # by itself: on the board
        if self.speak and outputs:
            queue: asyncio.Queue[str | None] = asyncio.Queue()
            splitter = SentenceSplitter()
            for sentence in [*splitter.feed(text), *splitter.flush()]:
                queue.put_nowait(sentence)
            queue.put_nowait(None)
            audio_sent = await self._tts_worker(turn_id, queue, token, outputs=outputs)
        await self._finish_turn(turn_id, text, cancelled=token.cancelled, audio_sent=audio_sent)

    async def close(self) -> None:
        self._mics = []
        await self.vision.close()
        await self._cancel_turn("session closed")
        self._playback = None
        watchdog, self._watchdog = self._watchdog, None
        for task in [*self._tasks, *([watchdog] if watchdog else [])]:
            if task is not asyncio.current_task():
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task


def _heard(text: str, stt_s: float) -> str:
    if not text:
        return "recognized nothing"
    short = text if len(text) <= 40 else text[:39] + "…"
    return f"「{short}」 in {stt_s:.1f} s"
