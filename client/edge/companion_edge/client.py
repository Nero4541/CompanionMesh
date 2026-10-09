"""The edge device: one realtime connection carrying mic, speaker and camera.

Network loss is expected (Wi-Fi, phone hotspot): the client reconnects with
exponential backoff, resumes the same session id (stored on disk), and keeps
at most ``audio.buffer_s`` seconds of microphone audio while offline.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import uuid
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import websockets

from companion_edge import protocol, status
from companion_edge.config import EdgeConfig
from companion_edge.devices import (
    AlsaMicrophone,
    AlsaSpeaker,
    Camera,
    FfmpegCamera,
    FfmpegOpusMicrophone,
    Microphone,
    Speaker,
    ffmpeg_has_opus,
)
from companion_edge.opus import OpusEncoder, OpusUnavailable

log = logging.getLogger("companion_edge")

PACKET_S = 0.02


@dataclass
class Stats:
    reconnects: int = 0
    audio_bytes_sent: int = 0
    frames_sent: int = 0
    dropped_audio_s: float = 0.0


class EdgeClient:
    def __init__(
        self,
        config: EdgeConfig,
        *,
        microphone: Microphone | None = None,
        speaker: Speaker | None = None,
        camera: Camera | None = None,
        encoder: Any = None,
    ) -> None:
        self.config = config
        a, c = config.audio, config.camera
        self.encoder = encoder
        self.codec = a.codec
        self.mic = microphone or (self._pick_microphone() if a.enabled else None)
        self.speaker = speaker or (
            AlsaSpeaker(a.output_device, lead_in_ms=a.output_lead_in_ms, hold_s=a.output_hold_s)
            if a.enabled
            else None
        )
        self.camera = camera or (
            FfmpegCamera(c.device, input_format=c.input_format, size=c.size, quality=c.quality)
            if c.enabled
            else None
        )
        if self.mic is not None and microphone is not None and self.codec == "opus":
            if self.encoder is None and not getattr(self.mic, "encoded", False):
                try:
                    self.encoder = OpusEncoder(a.opus_bitrate)
                except OpusUnavailable as exc:
                    log.warning("%s; sending uncompressed PCM", exc)
                    self.codec = "pcm"
        self.stats = Stats()
        self.ws: Any = None  # websockets connection (legacy or new client API)
        self.session_id: str | None = self._load_state().get("session_id")
        self.streaming = False  # audio.input.start sent on this connection
        # Mic packets waiting to be sent (also the offline buffer).
        self._pending: deque[bytes] = deque(maxlen=max(1, int(a.buffer_s / PACKET_S)))
        self._playback: asyncio.Queue[tuple[str, bytes]] = asyncio.Queue()
        self._playing = False
        self._turns_done: set[str] = set()
        self.vision_enabled = False
        self.vision_user_disabled = False
        self.frame_interval = c.min_interval_s

    def _pick_microphone(self) -> Microphone:
        """libopus if present, else ffmpeg's Opus encoder, else uncompressed PCM."""
        a = self.config.audio
        if self.codec == "opus" and a.opus_encoder in ("auto", "libopus"):
            try:
                if self.encoder is None:
                    self.encoder = OpusEncoder(a.opus_bitrate)
                log.info("microphone: arecord + libopus")
                return AlsaMicrophone(a.input_device)
            except OpusUnavailable as exc:
                if a.opus_encoder == "libopus":
                    log.warning("%s; sending uncompressed PCM", exc)
                    self.codec = "pcm"
                    return AlsaMicrophone(a.input_device)
        if self.codec == "opus" and a.opus_encoder in ("auto", "ffmpeg") and ffmpeg_has_opus():
            log.info("microphone: ffmpeg (ALSA capture + Opus encoder)")
            return FfmpegOpusMicrophone(a.input_device, a.opus_bitrate)
        if self.codec == "opus":
            log.warning("no Opus encoder (libopus or ffmpeg); sending uncompressed PCM")
        self.codec = "pcm"
        return AlsaMicrophone(a.input_device)

    # --- state on disk -----------------------------------------------------

    @property
    def state_path(self) -> Path:
        return self.config.state_file.expanduser()

    def _load_state(self) -> dict[str, Any]:
        try:
            return json.loads(self.state_path.read_text())
        except (OSError, ValueError):
            return {}

    def _save_state(self) -> None:
        path = self.state_path
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"session_id": self.session_id}))
        tmp.replace(path)

    # --- roles -----------------------------------------------------------------

    def roles(self) -> list[str]:
        roles = []
        if self.mic is not None:
            roles.append("mic")
        if self.speaker is not None:
            roles.append("speaker")
        if self.camera is not None:
            roles.append("camera")
        return roles

    # --- microphone ------------------------------------------------------------

    async def _mic_loop(self) -> None:
        assert self.mic is not None
        batch: list[bytes] = []
        async for frame in self.mic.frames():
            if self._playing:
                batch.clear()  # half-duplex: do not send our own voice back
                continue
            if getattr(self.mic, "encoded", False) or self.codec != "opus":
                packet = frame  # already Opus (ffmpeg) or plain PCM
            else:
                packet = self.encoder.encode(frame)
            if len(self._pending) == self._pending.maxlen and not self.streaming:
                self.stats.dropped_audio_s += PACKET_S
            self._pending.append(packet)
            if self.streaming and len(self._pending) >= self.config.audio.packets_per_frame:
                await self._flush_audio()

    async def _flush_audio(self) -> None:
        ws = self.ws
        if ws is None or not self._pending:
            return
        packets = list(self._pending)
        self._pending.clear()
        n = max(1, self.config.audio.packets_per_frame)
        for i in range(0, len(packets), n):
            group = packets[i : i + n]
            data = protocol.join_packets(group) if self.codec == "opus" else b"".join(group)
            try:
                await ws.send(protocol.compact_binary("audio.input.chunk", data))
            except websockets.ConnectionClosed:
                # Keep what did not go out for the next connection.
                for packet in packets[i:]:
                    self._pending.append(packet)
                return
            self.stats.audio_bytes_sent += len(data)

    # --- speaker -----------------------------------------------------------------

    async def _playback_loop(self) -> None:
        assert self.speaker is not None
        while True:
            turn_id, audio = await self._playback.get()
            self._playing = True
            try:
                await self.speaker.play(audio)
            except Exception as exc:  # a broken clip must not stop playback
                log.warning("playback failed: %s", exc)
            if self._playback.empty():
                self._playing = False
                await self._report_played(turn_id)

    async def _report_played(self, turn_id: str) -> None:
        if turn_id in self._turns_done and self._playback.empty() and self.ws is not None:
            self._turns_done.discard(turn_id)
            with contextlib.suppress(websockets.ConnectionClosed):
                await self.ws.send(protocol.text("audio.output.played", {"turn_id": turn_id}))

    async def _stop_playback(self) -> None:
        while not self._playback.empty():
            self._playback.get_nowait()
        if self.speaker is not None:
            await self.speaker.stop()
        self._playing = False

    # --- camera ------------------------------------------------------------------

    async def _send_frame(self, reason: str, frame_id: str | None = None) -> None:
        if self.camera is None or self.ws is None or not self.vision_enabled:
            return
        jpeg = await self.camera.latest()
        if jpeg is None:
            return
        header = protocol.envelope(
            "vision.frame",
            {"mime": "image/jpeg", "reason": reason, "frame_id": frame_id or uuid.uuid4().hex[:12]},
            device_id=self.config.device_id,
        )
        with contextlib.suppress(websockets.ConnectionClosed):
            await self.ws.send(protocol.binary(header, jpeg))
            self.stats.frames_sent += 1

    async def _camera_loop(self) -> None:
        while True:
            await asyncio.sleep(self.frame_interval)
            await self._send_frame("periodic")

    def _adapt_interval(self, frame_status: str) -> None:
        """Adaptive sampling: slow down while the view is unchanged."""
        c = self.config.camera
        if frame_status == "duplicate":
            self.frame_interval = min(self.frame_interval * 2, c.max_interval_s)
        elif frame_status == "accepted":
            self.frame_interval = c.min_interval_s

    # --- heartbeat -----------------------------------------------------------------

    async def _heartbeat_loop(self) -> None:
        while True:
            if self.ws is not None:
                payload = status.snapshot(
                    codec=self.codec,
                    audio_buffer_ms=round(len(self._pending) * PACKET_S * 1000),
                    reconnects=self.stats.reconnects,
                    frames_sent=self.stats.frames_sent,
                    frame_interval_s=self.frame_interval,
                )
                with contextlib.suppress(websockets.ConnectionClosed):
                    await self.ws.send(protocol.text("device.status", payload))
            await asyncio.sleep(self.config.heartbeat_s)

    # --- one connection --------------------------------------------------------------

    async def _handle_text(self, event: dict[str, Any]) -> str | None:
        kind, p = event.get("type"), event.get("payload", {})
        ws = self.ws
        assert ws is not None
        if kind == "session.started":
            self.session_id = p["session_id"]
            self._save_state()
            log.info(
                "joined session %s (resumed=%s, roles=%s)",
                self.session_id,
                p.get("resumed"),
                ",".join(p.get("roles", [])),
            )
            if self.mic is not None:
                encoding = "opus" if self.codec == "opus" else "pcm_s16le"
                await ws.send(
                    protocol.text(
                        "audio.input.start",
                        {"sample_rate": 16000, "encoding": encoding, "channels": 1},
                    )
                )
                self.streaming = True
                await self._flush_audio()  # what we heard while offline
            self.vision_enabled = bool(p.get("vision_enabled"))
            if (
                self.camera is not None
                and not self.vision_enabled
                and not self.vision_user_disabled
            ):
                await ws.send(protocol.text("vision.enable"))
        elif kind == "vision.state":
            was = self.vision_enabled
            self.vision_enabled = bool(p.get("enabled"))
            if was and not self.vision_enabled:
                self.vision_user_disabled = True
            elif self.vision_enabled:
                self.vision_user_disabled = False
        elif kind == "vision.capture.request":
            await self._send_frame("manual", str(p.get("request_id") or ""))
        elif kind == "vision.frame.status":
            self._adapt_interval(str(p.get("status")))
        elif kind == "audio.output.done":
            self._turns_done.add(str(p.get("turn_id")))
            if not self._playing:
                await self._report_played(str(p.get("turn_id")))
        elif kind == "conversation.response.done" and p.get("cancelled"):
            await self._stop_playback()
        elif kind == "session.ended":
            return "ended"
        elif kind == "system.error":
            log.warning("server: %s: %s", p.get("code"), p.get("message"))
        return None

    async def _connection(self, ws: Any) -> str:
        self.ws = ws
        self.streaming = False
        start: dict[str, Any] = {"roles": self.roles()}
        if self.session_id:
            start["session_id"] = self.session_id
        await ws.send(protocol.text("session.start", start, device_id=self.config.device_id))
        try:
            async for message in ws:
                if isinstance(message, bytes):
                    header, data = protocol.parse_binary(message)
                    if header.get("type") == "audio.output.chunk" and self.speaker is not None:
                        await self._playback.put((header["payload"]["turn_id"], data))
                    continue
                outcome = await self._handle_text(json.loads(message))
                if outcome:
                    return outcome
        finally:
            self.ws = None
            self.streaming = False
        return "closed"

    def _url(self) -> str:
        token = self.config.token()
        if not token:
            return self.config.server
        sep = "&" if "?" in self.config.server else "?"
        return f"{self.config.server}{sep}token={token}"

    async def run(self) -> None:
        tasks: list[asyncio.Task[None]] = [asyncio.create_task(self._heartbeat_loop())]
        if self.mic is not None:
            tasks.append(asyncio.create_task(self._mic_loop()))
        if self.speaker is not None:
            tasks.append(asyncio.create_task(self._playback_loop()))
        if self.camera is not None:
            await self.camera.start()
            tasks.append(asyncio.create_task(self._camera_loop()))
        backoff = 1.0
        try:
            while True:
                try:
                    async with websockets.connect(self._url(), max_size=None) as ws:
                        backoff = 1.0
                        outcome = await self._connection(ws)
                        log.info("connection ended (%s)", outcome)
                except (OSError, websockets.WebSocketException) as exc:
                    log.warning("server unreachable: %s (retrying in %.0f s)", exc, backoff)
                self.stats.reconnects += 1
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30.0)
        finally:
            for task in tasks:
                task.cancel()
            for task in tasks:
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task
            for device in (self.mic, self.camera):
                if device is not None:
                    with contextlib.suppress(Exception):
                        await device.close()
            if self.encoder is not None and hasattr(self.encoder, "close"):
                self.encoder.close()
