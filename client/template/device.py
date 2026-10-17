"""A minimal Companion device: the starting point for a client of your own.

One file, one dependency (``websockets``), the whole realtime protocol 1.0 a
device needs: joining a session, sending microphone audio, playing the
companion's speech, sending camera frames, answering pings, and reconnecting.
Everything hardware-specific sits in the four methods marked ``PORT:``;
replace them for your microphone, speaker and camera, or translate the file
to another language (the protocol is plain JSON over a WebSocket, plus one
small binary framing described in ``binary_frame``).

Try it against a running server (it plays "speech" by saving it to files):

    pip install "websockets>=14"      # or: uv run --with websockets python device.py ...
    export COMPANION_TOKEN=...        # if the server has one
    python device.py --text "こんにちは"
    python device.py --wav question.wav                  # 16 kHz mono 16-bit WAV as the mic
    python device.py --roles camera --jpeg room.jpg       # join a conversation as a camera

See "Realtime protocol" in docs/api.md of the repository for every event.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
import struct
import sys
import time
import uuid
import wave
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import websockets

SAMPLE_RATE = 16_000  # the server expects 16 kHz mono 16-bit PCM (or Opus)
CHUNK_MS = 100  # microphone audio per message
EXTENSIONS = {"audio/wav": "wav", "audio/mpeg": "mp3", "audio/ogg": "ogg", "audio/opus": "opus"}


def now_ms() -> float:
    return time.time() * 1000


# --- the protocol's two frame types ------------------------------------------


def text_frame(type_: str, payload: dict[str, Any] | None = None, **fields: Any) -> str:
    """A JSON envelope. Only ``type`` and ``payload`` are required."""
    return json.dumps({"type": type_, **fields, "payload": payload or {}}, ensure_ascii=False)


def binary_frame(type_: str, payload: dict[str, Any], data: bytes, **fields: Any) -> bytes:
    """``[uint32 big-endian length of the JSON envelope][envelope][raw bytes]``."""
    header = text_frame(type_, payload, **fields).encode("utf-8")
    return struct.pack(">I", len(header)) + header + data


def parse_binary(frame: bytes) -> tuple[dict[str, Any], bytes]:
    (length,) = struct.unpack_from(">I", frame)
    return json.loads(frame[4 : 4 + length]), frame[4 + length :]


# --- the device ----------------------------------------------------------------


class Device:
    def __init__(
        self,
        server: str,
        *,
        token: str | None = None,
        device_id: str = "template-device",
        roles: tuple[str, ...] = ("mic", "speaker"),
        text: str | None = None,
        wav: Path | None = None,
        jpeg: Path | None = None,
        out_dir: Path = Path("replies"),
        once: bool = False,
    ) -> None:
        self.server, self.token, self.device_id, self.roles = server, token, device_id, roles
        self.text, self.wav, self.jpeg, self.out_dir, self.once = text, wav, jpeg, out_dir, once
        self.session_id: str | None = None  # kept across reconnects to resume the session
        self.ws: Any = None
        self.replies: list[str] = []  # finished reply texts
        self.clips: list[Path] = []  # speech the "speaker" played
        self._playback: asyncio.Queue[tuple[str, int, str, bytes] | tuple[str, None, None, None]]
        self._playback = asyncio.Queue()
        self._stopped: set[str] = set()  # turns the server told us to stop
        self._done = asyncio.Event()
        self._started = time.monotonic()
        self._audio_turns: set[str] = set()  # turns that brought speech
        self._tasks: set[asyncio.Task[Any]] = set()

    # --- PORT: hardware ----------------------------------------------------------

    async def microphone(self) -> AsyncIterator[bytes]:
        """PORT: yield 16 kHz mono s16le PCM, ``CHUNK_MS`` at a time, in real time.

        Here: the --wav file, then a little silence so the server hears the end
        of the sentence (it waits for ~1 s of quiet, ``vad.min_silence_ms``).
        """
        pcm = b""
        if self.wav is not None:
            with wave.open(str(self.wav), "rb") as w:
                if (w.getframerate(), w.getnchannels(), w.getsampwidth()) != (SAMPLE_RATE, 1, 2):
                    raise SystemExit("--wav must be 16 kHz, mono, 16-bit")
                pcm = w.readframes(w.getnframes())
        pcm += bytes(SAMPLE_RATE * 2 * 3 // 2)  # 1.5 s of silence
        step = SAMPLE_RATE * 2 * CHUNK_MS // 1000
        for offset in range(0, len(pcm), step):
            yield pcm[offset : offset + step]
            await asyncio.sleep(CHUNK_MS / 1000)

    async def play(self, turn_id: str, seq: int, mime: str, audio: bytes) -> None:
        """PORT: play one encoded sentence (WAV by default, see ``tts.response_format``)
        and return when it has finished. Here: save it to a file."""
        self.out_dir.mkdir(parents=True, exist_ok=True)
        path = self.out_dir / f"{turn_id}-{seq:03d}.{EXTENSIONS.get(mime, 'bin')}"
        path.write_bytes(audio)
        self.clips.append(path)

    async def stop_playback(self) -> None:
        """PORT: silence the speaker at once (the user cut in)."""

    async def capture(self) -> bytes | None:
        """PORT: return one JPEG of what the camera sees now. Here: the --jpeg file."""
        return self.jpeg.read_bytes() if self.jpeg else None

    # --- sending -------------------------------------------------------------------

    async def send(self, type_: str, payload: dict[str, Any] | None = None) -> None:
        await self.ws.send(text_frame(type_, payload, device_id=self.device_id))

    async def send_frame(self, reason: str, frame_id: str | None = None) -> None:
        jpeg = await self.capture()
        if jpeg is None:
            return
        info = {
            "mime": "image/jpeg",
            "reason": reason,  # periodic | change | manual (manual = asked for)
            "frame_id": frame_id or uuid.uuid4().hex[:12],
            "captured_at": now_ms(),
        }
        await self.ws.send(binary_frame("vision.frame", info, jpeg, device_id=self.device_id))

    async def stream_microphone(self) -> None:
        # full_duplex: False = we stop listening while the companion talks;
        # True needs echo cancellation (aec: "device") or the companion hears itself.
        start = {
            "sample_rate": SAMPLE_RATE,
            "encoding": "pcm_s16le",
            "channels": 1,
            "full_duplex": False,
            "aec": "none",
        }
        await self.send("audio.input.start", start)
        pos = 0  # index of the first sample of each chunk in this mic stream
        async for pcm in self.microphone():
            info = {"pos": pos, "t": now_ms()}
            await self.ws.send(
                binary_frame("audio.input.chunk", info, pcm, device_id=self.device_id)
            )
            pos += len(pcm) // 2
        await self.send("audio.input.stop")

    async def heartbeat(self) -> None:
        """Free-form health, shown on the server's dashboard."""
        while True:
            await self.send("device.status", {"uptime_s": round(time.monotonic() - self._started)})
            await asyncio.sleep(30)

    # --- the speaker: play sentences in order, report when a turn has played ------

    async def speaker_loop(self) -> None:
        while True:
            turn_id, seq, mime, audio = await self._playback.get()
            if seq is None:  # audio.output.done: everything of this turn is queued
                if turn_id not in self._stopped:
                    # The server stays in SPEAKING until it hears this, then listens again.
                    await self.send("audio.output.played", {"turn_id": turn_id})
                    if self.once:
                        self._done.set()
                continue
            if turn_id in self._stopped:
                continue
            await self.send(
                "audio.output.progress", {"turn_id": turn_id, "seq": seq, "state": "started"}
            )
            await self.play(turn_id, seq, mime, audio)

    async def _done_without_audio(self, turn_id: str) -> None:
        await asyncio.sleep(3.0)
        if turn_id not in self._audio_turns:
            self._done.set()

    async def stop_turn(self, turn_id: str) -> None:
        self._stopped.add(turn_id)
        await self.stop_playback()
        await self.send("audio.output.stopped", {"turn_id": turn_id, "reason": "stopped"})

    # --- receiving -------------------------------------------------------------------

    async def handle(self, event: dict[str, Any]) -> str | None:
        """React to one server event; return a reason to drop the connection, if any."""
        kind, p = event.get("type"), event.get("payload") or {}
        if kind == "session.started":
            self.session_id = p["session_id"]
            print(f"joined session {self.session_id} (resumed={p.get('resumed')})")
            if "camera" in self.roles:
                await self.send("vision.enable")
                await self.send_frame("periodic")
            if self.text:
                await self.send("conversation.text", {"text": self.text})
            if "mic" in self.roles and self.wav:
                self._tasks.add(asyncio.create_task(self.stream_microphone()))
        elif kind == "system.ping":  # clock sync: echo it with our own clock
            await self.send("system.pong", {**p, "t_device": now_ms()})
        elif kind == "conversation.transcript" and p.get("final"):
            print(f"you: {p.get('text')}")
        elif kind == "conversation.response.delta":
            print(p.get("text", ""), end="", flush=True)
        elif kind == "conversation.response.done":
            print()
            self.replies.append(p.get("text", ""))
            if self.once:
                # With a speaker, finish once its audio has played, unless the
                # server sends no audio at all (no TTS configured).
                turn_id = str(p.get("turn_id"))
                if "speaker" not in self.roles:
                    self._done.set()
                else:
                    self._tasks.add(asyncio.create_task(self._done_without_audio(turn_id)))
        elif kind == "audio.output.done":
            await self._playback.put((str(p.get("turn_id")), None, None, None))
        elif kind == "audio.output.stop":
            await self.stop_turn(str(p.get("turn_id")))
        elif kind == "vision.capture.request":  # the server wants a fresh look
            await self.send_frame("manual", str(p.get("request_id") or "") or None)
        elif kind == "session.ended":  # cameras: the conversation is over; wait for the next
            self.session_id = None
            return "ended"
        elif kind == "system.error":
            print(f"server error: {p.get('code')}: {p.get('message')}", file=sys.stderr)
            if p.get("code") == "no_session":
                return "no_session"
        # Anything else (system.state, audio.vad, vision.*, attention.*, metrics.*,
        # and event types added later) can be ignored.
        return None

    async def connection(self) -> str:
        start: dict[str, Any] = {"roles": list(self.roles)}
        if self.session_id:
            start["session_id"] = self.session_id  # resume after a dropped connection
        elif self.roles == ("camera",):
            start["join"] = True  # a camera joins the conversation someone else started
        await self.send("session.start", start)
        tasks = [asyncio.create_task(self.heartbeat())]
        if "speaker" in self.roles:
            tasks.append(asyncio.create_task(self.speaker_loop()))
        try:
            async for message in self.ws:
                if isinstance(message, bytes):
                    header, data = parse_binary(message)
                    p = header.get("payload") or {}
                    if header.get("type") == "audio.output.chunk" and "speaker" in self.roles:
                        self._audio_turns.add(str(p.get("turn_id")))
                        await self._playback.put(
                            (str(p.get("turn_id")), int(p.get("seq", 0)), str(p.get("mime")), data)
                        )
                    continue
                reason = await self.handle(json.loads(message))
                if reason:
                    return reason
        finally:
            for task in [*tasks, *self._tasks]:
                task.cancel()
            self._tasks.clear()
        return "closed"

    async def run(self) -> None:
        # Headers where the WebSocket library allows them; otherwise append
        # ?token=... to the URL (servers mask it in their logs).
        headers = {"Authorization": f"Bearer {self.token}"} if self.token else None
        backoff = 1.0
        while True:
            reason = "error"
            try:
                async with websockets.connect(self.server, additional_headers=headers) as ws:
                    self.ws, backoff = ws, 1.0
                    runner = asyncio.create_task(self.connection())
                    finished = asyncio.create_task(self._done.wait())
                    done, _ = await asyncio.wait(
                        {runner, finished}, return_when=asyncio.FIRST_COMPLETED
                    )
                    if finished in done:
                        runner.cancel()
                        with contextlib.suppress(asyncio.CancelledError):
                            await runner
                        return
                    finished.cancel()
                    reason = runner.result()
            except (OSError, websockets.WebSocketException) as exc:
                print(f"connection lost: {exc}", file=sys.stderr)
            delay = 2.0 if reason in ("no_session", "ended") else backoff
            backoff = min(backoff * 2, 30.0)
            await asyncio.sleep(delay)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--server", default="ws://127.0.0.1:8765/v1/realtime")
    parser.add_argument("--device-id", default="template-device")
    parser.add_argument("--roles", default="mic,speaker", help="mic, speaker, camera")
    parser.add_argument("--text", help="say this (typed) once connected")
    parser.add_argument("--wav", type=Path, help="16 kHz mono WAV to send as the microphone")
    parser.add_argument("--jpeg", type=Path, help="JPEG to send as the camera's view")
    parser.add_argument("--out", type=Path, default=Path("replies"), help="where speech is saved")
    parser.add_argument("--keep-running", action="store_true", help="do not exit after a reply")
    args = parser.parse_args()
    device = Device(
        args.server,
        token=os.environ.get("COMPANION_TOKEN") or None,
        device_id=args.device_id,
        roles=tuple(r.strip() for r in args.roles.split(",") if r.strip()),
        text=args.text,
        wav=args.wav,
        jpeg=args.jpeg,
        out_dir=args.out,
        once=not args.keep_running and bool(args.text or args.wav),
    )
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(device.run())


if __name__ == "__main__":
    main()
