"""Audio and camera I/O through ALSA tools and ffmpeg (no Python media libraries).

Each device has a small interface so tests (and other boards) can swap them:

* ``Microphone.frames()`` yields 20 ms chunks of 16 kHz mono s16le PCM.
* ``Speaker.play(audio)`` plays one encoded clip (WAV), returning about when it ends.
* ``Camera.latest()`` returns the newest JPEG frame.
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import logging
import shutil
import wave
from collections.abc import AsyncIterator
from typing import Protocol

log = logging.getLogger(__name__)

FRAME_BYTES = 640  # 20 ms at 16 kHz s16le mono
SOI, EOI = b"\xff\xd8", b"\xff\xd9"


class Microphone(Protocol):
    def frames(self) -> AsyncIterator[bytes]: ...

    async def close(self) -> None: ...


class Speaker(Protocol):
    async def play(self, audio: bytes) -> None: ...

    async def stop(self) -> None: ...


class Camera(Protocol):
    async def start(self) -> None: ...

    async def latest(self) -> bytes | None: ...

    async def close(self) -> None: ...


def require(tool: str, package: str) -> str:
    path = shutil.which(tool)
    if path is None:
        raise RuntimeError(f"{tool} not found; install {package}")
    return path


class AlsaMicrophone:
    def __init__(self, device: str = "default") -> None:
        self.device = device
        self._proc: asyncio.subprocess.Process | None = None

    async def frames(self) -> AsyncIterator[bytes]:
        arecord = require("arecord", "alsa-utils")
        self._proc = await asyncio.create_subprocess_exec(
            arecord,
            "-q",
            "-D",
            self.device,
            "-f",
            "S16_LE",
            "-r",
            "16000",
            "-c",
            "1",
            "-t",
            "raw",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        assert self._proc.stdout is not None
        while True:
            try:
                chunk = await self._proc.stdout.readexactly(FRAME_BYTES)
            except asyncio.IncompleteReadError:
                raise RuntimeError(f"arecord stopped (device {self.device!r})") from None
            yield chunk

    async def close(self) -> None:
        if self._proc and self._proc.returncode is None:
            self._proc.kill()
            await self._proc.wait()


_ALSA_FORMATS = {1: "U8", 2: "S16_LE", 3: "S24_3LE", 4: "S32_LE"}
MARGIN_S = 0.15  # play() returns this long before its clip ends, so the next one follows
SILENCE_S = 0.1  # gap filler written while waiting for more speech


def parse_wav(audio: bytes) -> tuple[tuple[int, int, int], bytes] | None:
    """((rate, channels, sample width), PCM) of a WAV clip, or None if it is not one."""
    try:
        with wave.open(io.BytesIO(audio)) as w:
            fmt = (w.getframerate(), w.getnchannels(), w.getsampwidth())
            pcm = w.readframes(w.getnframes())
    except (wave.Error, EOFError):
        return None
    return (fmt, pcm) if fmt[2] in _ALSA_FORMATS else None


class AlsaSpeaker:
    """Plays clips through one long-lived ``aplay`` per run of speech.

    Opening an output stream is not free: a Bluetooth (A2DP) speaker has to
    start its stream and chops the first audio written after each open. So
    the sentences of a reply are written back to back as raw PCM into one
    ``aplay``, gaps between them are filled with silence, and the stream
    closes after ``hold_s`` without speech. ``lead_in_ms`` of silence opens
    each stream so the speaker is running before the first word.
    """

    def __init__(self, device: str = "default", *, lead_in_ms: int = 0,
                 hold_s: float = 2.0) -> None:
        self.device = device
        self.lead_in_s = lead_in_ms / 1000
        self.hold_s = hold_s
        self._proc: asyncio.subprocess.Process | None = None
        self._format: tuple[int, int, int] | None = None
        self._ends_at = 0.0  # loop time when everything written has played out
        self._speech_ends_at = 0.0
        self._keeper: asyncio.Task[None] | None = None
        self._closing = False  # the keeper is draining and closing the stream

    def _argv(self, rate: int, channels: int, width: int) -> list[str]:
        return [require("aplay", "alsa-utils"), "-q", "-D", self.device, "-t", "raw",
                "-f", _ALSA_FORMATS[width], "-r", str(rate), "-c", str(channels), "-"]

    async def play(self, audio: bytes) -> None:
        await self._cancel_keeper()
        clip = parse_wav(audio)
        if clip is None:  # not a WAV we can stream: let aplay read it whole
            await self.close()
            await self._play_whole(audio)
            return
        fmt, pcm = clip
        if self._format != fmt or self._proc is None or self._proc.returncode is not None:
            await self.close()
            await self._open(fmt)
        await self._write(pcm)
        self._speech_ends_at = self._ends_at
        loop = asyncio.get_running_loop()
        await asyncio.sleep(max(0.0, self._ends_at - loop.time() - MARGIN_S))
        self._keeper = asyncio.create_task(self._keep_open())

    async def _open(self, fmt: tuple[int, int, int]) -> None:
        self._proc = await asyncio.create_subprocess_exec(
            *self._argv(*fmt),
            stdin=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        self._format = fmt
        self._ends_at = 0.0
        if self.lead_in_s:
            await self._write(self._silence(self.lead_in_s))

    def _silence(self, seconds: float) -> bytes:
        assert self._format is not None
        rate, channels, width = self._format
        return (b"\x80" if width == 1 else b"\x00") * (int(rate * seconds) * channels * width)

    async def _write(self, pcm: bytes) -> None:
        assert self._proc is not None and self._proc.stdin is not None
        assert self._format is not None
        rate, channels, width = self._format
        self._proc.stdin.write(pcm)
        await self._proc.stdin.drain()
        now = asyncio.get_running_loop().time()
        self._ends_at = max(self._ends_at, now) + len(pcm) / (rate * channels * width)

    async def _keep_open(self) -> None:
        """Feed silence until more speech comes or ``hold_s`` passes, then close."""
        loop = asyncio.get_running_loop()
        try:
            while loop.time() < self._speech_ends_at + self.hold_s:
                if self._ends_at - loop.time() < MARGIN_S:
                    await self._write(self._silence(SILENCE_S))
                await asyncio.sleep(SILENCE_S / 2)
        except (BrokenPipeError, ConnectionResetError):
            pass
        self._closing = True
        try:
            await self.close()
        finally:
            self._closing = False

    async def _cancel_keeper(self) -> None:
        keeper, self._keeper = self._keeper, None
        if keeper is None or keeper is asyncio.current_task():
            return
        if self._closing:  # let it finish, so the device is free before a reopen
            await keeper
            return
        keeper.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await keeper

    async def _play_whole(self, audio: bytes) -> None:
        aplay = require("aplay", "alsa-utils")
        self._proc = await asyncio.create_subprocess_exec(
            aplay, "-q", "-D", self.device, "-",
            stdin=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        try:
            await self._proc.communicate(audio)
        finally:
            self._proc = None

    async def close(self) -> None:
        """Let what was written play out, then end the stream."""
        await self._cancel_keeper()
        proc, self._proc, self._format = self._proc, None, None
        if proc is None or proc.returncode is not None:
            return
        assert proc.stdin is not None
        with contextlib.suppress(BrokenPipeError, ConnectionResetError):
            proc.stdin.close()
        try:
            await asyncio.wait_for(proc.wait(), timeout=5)
        except TimeoutError:
            proc.kill()
            await proc.wait()

    async def stop(self) -> None:
        """Cut playback off now (barge-in, cancelled reply)."""
        await self._cancel_keeper()
        proc, self._proc, self._format = self._proc, None, None
        if proc and proc.returncode is None:
            proc.kill()
            await proc.wait()


class JpegStream:
    def __init__(self) -> None:
        self._buf = bytearray()

    def feed(self, chunk: bytes) -> list[bytes]:
        self._buf += chunk
        frames: list[bytes] = []
        while (start := self._buf.find(SOI)) >= 0:
            end = self._buf.find(EOI, start + 2)
            if end < 0:
                del self._buf[:start]
                if len(self._buf) > 4 * 1024 * 1024:
                    self._buf.clear()
                return frames
            frames.append(bytes(self._buf[start : end + 2]))
            del self._buf[: end + 2]
        del self._buf[:-1]
        return frames


class FfmpegCamera:
    """V4L2 camera decoded by ffmpeg at 1 fps; keeps only the newest JPEG."""

    def __init__(self, device: str, *, input_format: str, size: str, quality: int) -> None:
        self.device = device
        self.input_format = input_format
        self.size = size
        self.quality = quality
        self._frame: bytes | None = None
        self._proc: asyncio.subprocess.Process | None = None
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        ffmpeg = require("ffmpeg", "ffmpeg")
        args = [ffmpeg, "-hide_banner", "-loglevel", "error", "-f", "v4l2"]
        if self.input_format:
            args += ["-input_format", self.input_format]
        if self.size:
            args += ["-video_size", self.size]
        args += [
            "-i",
            self.device,
            "-vf",
            "fps=1",
            "-q:v",
            str(self.quality),
            "-f",
            "image2pipe",
            "-vcodec",
            "mjpeg",
            "-",
        ]
        self._proc = await asyncio.create_subprocess_exec(
            *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        self._task = asyncio.create_task(self._read())

    async def _read(self) -> None:
        assert self._proc and self._proc.stdout
        parser = JpegStream()
        while chunk := await self._proc.stdout.read(65536):
            for frame in parser.feed(chunk):
                self._frame = frame
        err = b""
        if self._proc.stderr:
            err = await self._proc.stderr.read()
        log.warning("camera stopped: %s", err.decode(errors="replace").strip()[-300:])

    async def latest(self) -> bytes | None:
        return self._frame

    async def close(self) -> None:
        if self._proc and self._proc.returncode is None:
            self._proc.kill()
            await self._proc.wait()
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task


class OggPacketReader:
    """Split an Ogg stream into packets (enough for Ogg Opus from ffmpeg)."""

    def __init__(self) -> None:
        self._buf = bytearray()
        self._partial = bytearray()

    def feed(self, chunk: bytes) -> list[bytes]:
        self._buf += chunk
        packets: list[bytes] = []
        while True:
            start = self._buf.find(b"OggS")
            if start < 0:
                del self._buf[:-3]
                return packets
            if start:
                del self._buf[:start]
            if len(self._buf) < 27:
                return packets
            nsegs = self._buf[26]
            header_len = 27 + nsegs
            if len(self._buf) < header_len:
                return packets
            lacing = self._buf[27:header_len]
            body_len = sum(lacing)
            if len(self._buf) < header_len + body_len:
                return packets
            if not self._buf[5] & 0x01:  # page does not continue a packet
                self._partial.clear()
            offset = header_len
            for size in lacing:
                self._partial += self._buf[offset : offset + size]
                offset += size
                if size < 255:  # the packet ends in this segment
                    packets.append(bytes(self._partial))
                    self._partial.clear()
            del self._buf[: header_len + body_len]


def ffmpeg_has_opus() -> bool:
    import subprocess

    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        return False
    try:
        out = subprocess.run(
            [ffmpeg, "-hide_banner", "-encoders"], capture_output=True, text=True, timeout=10
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return False
    return any(line.split()[1:2] in (["opus"], ["libopus"]) for line in out.splitlines())


class FfmpegOpusMicrophone:
    """ALSA capture encoded by ffmpeg's Opus encoder; yields raw Opus packets.

    For boards without libopus: ffmpeg reads the microphone and writes Ogg Opus
    with one page per 20 ms packet (low latency); we strip the Ogg framing.
    """

    encoded = True

    def __init__(self, device: str = "default", bitrate: int = 24000) -> None:
        self.device = device
        self.bitrate = bitrate
        self._proc: asyncio.subprocess.Process | None = None

    async def frames(self) -> AsyncIterator[bytes]:
        ffmpeg = require("ffmpeg", "ffmpeg")
        self._proc = await asyncio.create_subprocess_exec(
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "alsa",
            "-ar",
            "16000",
            "-ac",
            "1",
            "-i",
            self.device,
            "-c:a",
            "opus",
            "-strict",
            "-2",
            "-b:a",
            str(self.bitrate),
            "-page_duration",
            "20000",
            "-flush_packets",
            "1",
            "-f",
            "opus",
            "-",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        assert self._proc.stdout is not None
        reader = OggPacketReader()
        skipped = 0
        while chunk := await self._proc.stdout.read(4096):
            for packet in reader.feed(chunk):
                if skipped < 2:  # OpusHead and OpusTags
                    skipped += 1
                    continue
                yield packet
        err = await self._proc.stderr.read() if self._proc.stderr else b""
        raise RuntimeError(f"ffmpeg microphone stopped: {err.decode(errors='replace')[-300:]}")

    async def close(self) -> None:
        if self._proc and self._proc.returncode is None:
            self._proc.kill()
            await self._proc.wait()
