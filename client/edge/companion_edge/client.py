"""The edge device: one realtime connection carrying mic, speaker and camera.

Network loss is expected (Wi-Fi, phone hotspot): the client reconnects with
exponential backoff, resumes the same session id (stored on disk), and keeps
at most ``audio.buffer_s`` seconds of microphone audio while offline.

Audio is full duplex when the server supports barge-in: the microphone keeps
streaming while a reply plays, every chunk carries its position in the
microphone stream and its capture time, and the device reports when each
reply clip starts playing (as a microphone position, so the server can line
up echo references) and stops playback the moment the server says so.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
import uuid
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import websockets

from companion_edge import diagnostics, logship, protocol, settings, status
from companion_edge.config import EdgeConfig
from companion_edge.cvi import CviCamera
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
from companion_edge.motion import MotionDetector, MotionSettings
from companion_edge.opus import OpusEncoder, OpusUnavailable

log = logging.getLogger("companion_edge")

PACKET_S = 0.02
SAMPLE_RATE = 16000
PACKET_SAMPLES = int(PACKET_S * SAMPLE_RATE)
JOIN_RETRY_S = 2.0  # camera-only devices: how often to look for a conversation to join
LOG_SHIP_S = 2.0  # how often new log lines go to the server


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
        config_path: Path | None = None,
        log_file: Path | None = None,
        logs: logship.LogBuffer | None = None,
    ) -> None:
        self.config = config
        # The TOML file the settings came from; the server may change them there.
        self.config_path = config_path
        self.log_file = log_file
        # This device's log lines, sent to the server (device.log) while connected.
        self.logs = logs or logship.BUFFER
        self._logs_sent = -1  # seq of the newest line the server has
        self.restart_requested = False
        a, c = config.audio, config.camera
        self.encoder = encoder
        self.codec = a.codec
        self.mic = microphone or (
            self._pick_microphone() if a.enabled and a.input_enabled else None
        )
        self.speaker = speaker or (
            AlsaSpeaker(a.output_device, lead_in_ms=a.output_lead_in_ms, hold_s=a.output_hold_s)
            if a.enabled
            else None
        )
        self.camera = camera or (self._make_camera() if c.enabled else None)
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
        # Mic packets waiting to be sent (also the offline buffer), each with its
        # position in the microphone stream and capture time (ms since epoch).
        self._pending: deque[tuple[bytes, int, float]] = deque(
            maxlen=max(1, int(a.buffer_s / PACKET_S))
        )
        self._mic_pos = 0  # samples captured so far
        self._playback: asyncio.Queue[tuple[str, int, bytes]] = asyncio.Queue()
        self._playing = False
        self._playing_turn: str | None = None
        self._turns_done: set[str] = set()
        self._stopped_turns: deque[str] = deque(maxlen=32)  # drop their late audio
        self.server_barge_in = False
        self.vision_enabled = False
        self.vision_user_disabled = False
        self.frame_interval = c.min_interval_s
        self.detector = MotionDetector(
            MotionSettings(
                motion_threshold=c.motion_threshold,
                scene_threshold=c.scene_threshold,
                cooldown_s=c.cooldown_s,
            )
        )
        self._last_frame_sent = 0.0
        self._tasks: set[asyncio.Task[None]] = set()

    def _spawn(self, coro: Any) -> None:
        task = asyncio.get_running_loop().create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def _make_camera(self) -> Camera:
        c = self.config.camera
        analysis_fps = c.analysis_fps if c.mode == "change" else 0.0
        if c.driver == "cvi":
            return CviCamera(
                size=c.size, quality=c.quality, analysis_fps=analysis_fps, rotate=c.rotate
            )
        return FfmpegCamera(
            c.device,
            input_format=c.input_format,
            size=c.size,
            quality=c.quality,
            analysis_fps=analysis_fps,
            rotate=c.rotate,
        )

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

    @property
    def camera_only(self) -> bool:
        return self.mic is None and self.speaker is None and self.camera is not None

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

    @property
    def full_duplex(self) -> bool:
        """Keep sending while a reply plays (the server can tell the user cut in)."""
        return self.config.audio.full_duplex and self.server_barge_in

    async def _mic_loop(self) -> None:
        assert self.mic is not None
        async for frame in self.mic.frames():
            pos = self._mic_pos
            self._mic_pos += PACKET_SAMPLES
            captured = time.time() * 1000 - PACKET_S * 1000  # start of this 20 ms
            if self._playing and not self.full_duplex:
                continue  # half-duplex: do not send our own voice back
            if getattr(self.mic, "encoded", False) or self.codec != "opus":
                packet = frame  # already Opus (ffmpeg) or plain PCM
            else:
                packet = self.encoder.encode(frame)
            if len(self._pending) == self._pending.maxlen and not self.streaming:
                self.stats.dropped_audio_s += PACKET_S
            self._pending.append((packet, pos, captured))
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
            chunks = [p for p, _, _ in group]
            data = protocol.join_packets(chunks) if self.codec == "opus" else b"".join(chunks)
            _, pos, captured = group[0]
            frame = protocol.compact_binary(
                "audio.input.chunk", data, {"pos": pos, "t": round(captured)}
            )
            try:
                await ws.send(frame)
            except websockets.ConnectionClosed:
                # Keep what did not go out for the next connection.
                for packet in packets[i:]:
                    self._pending.append(packet)
                return
            self.stats.audio_bytes_sent += len(data)

    # --- speaker -----------------------------------------------------------------

    async def _playback_loop(self) -> None:
        assert self.speaker is not None
        loop = asyncio.get_running_loop()
        while True:
            turn_id, seq, audio = await self._playback.get()
            if turn_id in self._stopped_turns:
                continue
            self._playing = True
            self._playing_turn = turn_id

            def started(at: float, turn_id: str = turn_id, seq: int = seq) -> None:
                # Where in the microphone stream this clip begins to sound.
                pos = self._mic_pos + int((at - loop.time()) * SAMPLE_RATE)
                task = loop.create_task(self._send_progress(turn_id, seq, pos))
                self._tasks.add(task)
                task.add_done_callback(self._tasks.discard)

            try:
                if getattr(self.speaker, "reports_start", False):
                    await self.speaker.play(audio, on_start=started)  # type: ignore[call-arg]
                else:
                    started(loop.time())
                    await self.speaker.play(audio)
            except Exception as exc:  # a broken clip must not stop playback
                log.warning("playback failed: %s", exc)
            if self._playback.empty():
                self._playing = False
                self._playing_turn = None
                await self._report_played(turn_id)

    async def _send_progress(self, turn_id: str, seq: int, pos: int) -> None:
        if self.ws is None or turn_id in self._stopped_turns:
            return
        payload = {"turn_id": turn_id, "seq": seq, "state": "started"}
        if self.mic is not None:
            payload["pos"] = max(0, pos)
        with contextlib.suppress(websockets.ConnectionClosed):
            await self.ws.send(protocol.text("audio.output.progress", payload))

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
        self._playing_turn = None

    async def _stop_turn(self, turn_id: str, reason: str) -> None:
        """audio.output.stop: silence this turn now and drop the rest of its audio."""
        self._stopped_turns.append(turn_id)
        self._turns_done.discard(turn_id)
        keep = []
        while not self._playback.empty():
            item = self._playback.get_nowait()
            if item[0] != turn_id:
                keep.append(item)
        for item in keep:
            self._playback.put_nowait(item)
        if self._playing_turn == turn_id and self.speaker is not None:
            await self.speaker.stop()
            self._playing = bool(keep)
            self._playing_turn = None
        log.info("playback stopped (%s)", reason)
        if self.ws is not None:
            with contextlib.suppress(websockets.ConnectionClosed):
                await self.ws.send(
                    protocol.text("audio.output.stopped", {"turn_id": turn_id, "reason": reason})
                )

    # --- camera ------------------------------------------------------------------

    async def _send_frame(
        self, reason: str, frame_id: str | None = None, event: dict[str, Any] | None = None
    ) -> None:
        if self.camera is None or self.ws is None or not self.vision_enabled:
            return
        jpeg = await self.camera.latest()
        if jpeg is None:
            return
        payload: dict[str, Any] = {
            "mime": "image/jpeg",
            "reason": reason,
            "frame_id": frame_id or uuid.uuid4().hex[:12],
        }
        captured = getattr(self.camera, "latest_at", None)
        if captured is not None:
            payload["captured_at"] = round(captured)
        if event is not None:
            payload["event"] = event
        header = protocol.envelope("vision.frame", payload, device_id=self.config.device_id)
        with contextlib.suppress(websockets.ConnectionClosed):
            await self.ws.send(protocol.binary(header, jpeg))
            self.stats.frames_sent += 1
            self._last_frame_sent = asyncio.get_running_loop().time()

    async def _camera_loop(self) -> None:
        if self.config.camera.mode == "change" and getattr(self.camera, "analysis", False):
            await self._watch_changes()
            return
        while True:
            await asyncio.sleep(self.frame_interval)
            await self._send_frame("periodic")

    async def _watch_changes(self) -> None:
        """Change mode: look at every small frame, send a picture only when it changed."""
        assert self.camera is not None
        loop = asyncio.get_running_loop()
        self._last_frame_sent = loop.time()
        heartbeat = self.config.camera.heartbeat_s
        async for gray, at in self.camera.gray_frames():  # type: ignore[attr-defined]
            if not self.vision_enabled or self.ws is None:
                self.detector.reset()
                continue
            change = self.detector.feed(gray, at)
            if change is not None:
                log.info("camera: %s (score %.2f)", change.kind, change.score)
                await self._send_frame("change", event=change.to_payload())
            elif loop.time() - self._last_frame_sent >= heartbeat:
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
            self._spawn(self._ship_logs(self.ws))
            log.info(
                "joined session %s (resumed=%s, roles=%s)",
                self.session_id,
                p.get("resumed"),
                ",".join(p.get("roles", [])),
            )
            self.server_barge_in = bool(p.get("barge_in"))
            # What can be changed from the server's dashboard (protocol 1.1).
            await ws.send(
                protocol.text("device.config", settings.describe(self.config, self.config_path))
            )
            if self.mic is not None:
                encoding = "opus" if self.codec == "opus" else "pcm_s16le"
                await ws.send(
                    protocol.text(
                        "audio.input.start",
                        {
                            "sample_rate": SAMPLE_RATE,
                            "encoding": encoding,
                            "channels": 1,
                            "full_duplex": self.full_duplex,
                            "aec": self.config.audio.aec,
                        },
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
        elif kind == "audio.output.stop":
            await self._stop_turn(str(p.get("turn_id")), str(p.get("reason") or "stop"))
        elif kind == "conversation.response.done" and p.get("cancelled"):
            turn_id = str(p.get("turn_id"))
            if turn_id not in self._stopped_turns:  # older servers send no audio.output.stop
                await self._stop_playback()
        elif kind == "system.ping":
            await ws.send(protocol.text("system.pong", {**p, "t_device": time.time() * 1000}))
        elif kind == "device.diagnostics.request":
            self._spawn(self._send_diagnostics(self.ws, str(p.get("request_id") or "")))
        elif kind == "device.config.set":
            await self._apply_settings(p)
            if self.restart_requested:
                return "restart"
        elif kind == "session.ended":
            # Sent to cameras when the conversation is over; the next is a new one.
            self.session_id = None
            self.vision_enabled = False
            self.vision_user_disabled = False
            return "ended"
        elif kind == "system.error":
            if p.get("code") == "no_session":
                return "no_session"
            log.warning("server: %s: %s", p.get("code"), p.get("message"))
        return None

    async def _ship_logs(self, ws: Any) -> None:
        """Send new log lines every couple of seconds while this connection lasts."""
        while self.ws is ws:
            lines = self.logs.after(self._logs_sent)
            if lines:
                try:
                    await ws.send(
                        protocol.text("device.log", {"boot": self.logs.boot, "records": lines})
                    )
                except websockets.WebSocketException:
                    return
                self._logs_sent = lines[-1]["seq"]
                if len(lines) == logship.BATCH:
                    continue  # more waiting (e.g. after a long time offline)
            await asyncio.sleep(LOG_SHIP_S)

    async def _send_diagnostics(self, ws: Any, request_id: str) -> None:
        sections = await diagnostics.collect(self.config, self.config_path, self.log_file)
        with contextlib.suppress(websockets.WebSocketException):
            await ws.send(
                protocol.text(
                    "device.diagnostics", {"request_id": request_id, "sections": sections}
                )
            )

    async def _apply_settings(self, p: dict[str, Any]) -> None:
        """Save settings sent from the dashboard; restart with them if anything changed."""
        assert self.ws is not None
        result: dict[str, Any] = {
            "request_id": p.get("request_id"),
            "ok": True,
            "restarting": False,
        }
        try:
            if self.config_path is None:
                raise ValueError("started without --config, so there is no file to save to")
            update = p.get("config")
            if not isinstance(update, dict):
                raise ValueError("config must be a table of settings")
            new = settings.apply_update(self.config, update)
            if settings.to_dict(new) != settings.to_dict(self.config):
                await asyncio.to_thread(settings.save, self.config_path, self.config, new)
                result["restarting"] = True
                self.restart_requested = True
                log.info("settings changed from the server; restarting with them")
        except (ValueError, OSError) as exc:
            result.update(ok=False, error=str(exc))
            log.warning("settings from the server refused: %s", exc)
        await self.ws.send(protocol.text("device.config.result", result))

    async def _connection(self, ws: Any) -> str:
        self.ws = ws
        self.streaming = False
        start: dict[str, Any] = {"roles": self.roles()}
        if self.camera_only:
            # A camera alone does not start conversations: it joins the one
            # started most recently, and the next one after that ends.
            start["join"] = True
        elif self.session_id:
            start["session_id"] = self.session_id
        await ws.send(protocol.text("session.start", start, device_id=self.config.device_id))
        try:
            async for message in ws:
                if isinstance(message, bytes):
                    header, data = protocol.parse_binary(message)
                    if header.get("type") == "audio.output.chunk" and self.speaker is not None:
                        info = header.get("payload", {})
                        turn_id = str(info.get("turn_id"))
                        if turn_id not in self._stopped_turns:
                            await self._playback.put((turn_id, int(info.get("seq", 0)), data))
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

    async def run(self) -> str:
        """Serve until cancelled, or until new settings need a restart ("restart")."""
        tasks: list[asyncio.Task[None]] = [asyncio.create_task(self._heartbeat_loop())]
        if self.mic is not None:
            tasks.append(asyncio.create_task(self._mic_loop()))
        if self.speaker is not None:
            tasks.append(asyncio.create_task(self._playback_loop()))
        if self.camera is not None:
            await self.camera.start()
            tasks.append(asyncio.create_task(self._camera_loop()))
        backoff = 1.0
        waiting_logged = False
        try:
            while True:
                outcome = "error"
                try:
                    async with websockets.connect(self._url(), max_size=None) as ws:
                        backoff = 1.0
                        outcome = await self._connection(ws)
                except (OSError, websockets.WebSocketException) as exc:
                    log.warning("server unreachable: %s (retrying in %.0f s)", exc, backoff)
                if self.restart_requested:
                    return "restart"
                if outcome in ("no_session", "ended"):
                    # The server is fine; a camera waits for someone to start talking.
                    if not waiting_logged:
                        log.info("no conversation to join yet; checking every 2 s")
                        waiting_logged = True
                    await asyncio.sleep(JOIN_RETRY_S)
                    continue
                if outcome != "error":
                    log.info("connection ended (%s)", outcome)
                waiting_logged = False
                self.stats.reconnects += 1
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30.0)
        finally:
            tasks += list(self._tasks)
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
