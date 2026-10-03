"""Audio and camera I/O through ALSA tools and ffmpeg (no Python media libraries).

Each device has a small interface so tests (and other boards) can swap them:

* ``Microphone.frames()`` yields 20 ms chunks of 16 kHz mono s16le PCM.
* ``Speaker.play(audio)`` plays one encoded clip (WAV) to completion.
* ``Camera.latest()`` returns the newest JPEG frame.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import shutil
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


class AlsaSpeaker:
    def __init__(self, device: str = "default") -> None:
        self.device = device
        self._proc: asyncio.subprocess.Process | None = None

    async def play(self, audio: bytes) -> None:
        aplay = require("aplay", "alsa-utils")
        self._proc = await asyncio.create_subprocess_exec(
            aplay,
            "-q",
            "-D",
            self.device,
            "-",
            stdin=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        try:
            await self._proc.communicate(audio)
        finally:
            self._proc = None

    async def stop(self) -> None:
        if self._proc and self._proc.returncode is None:
            self._proc.kill()


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
