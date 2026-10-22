"""Stream mode (protocol 1.4): the camera as a continuous stream, for the server's perception.

While the server's perception asks for it (``camera.stream`` with a lease of a
few seconds, renewed while it wants the stream), the device runs
``camera.stream_command``, which writes the camera to its standard output as
MJPEG (JPEG pictures one after another) or H.264 (Annex B), and sends that on:

- MJPEG: the newest picture at most ``stream_fps`` times a second, as
  ``vision.frame`` with reason ``stream`` (older ones are dropped, never queued);
- H.264: the bytes as they come, as ``camera.stream.chunk``; the server decodes.

The device only captures and encodes; finding faces, lips and everything else
happens on the server. The still camera has to let go of the sensor meanwhile
and is started again when the stream stops. A command that exits by itself is
not started again at once (``RETRY_S``): on Sipeed's LicheeRV image a crashing
encoder can leave the camera without pictures until the board is power-cycled.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import shlex
import time
from collections.abc import Awaitable, Callable

log = logging.getLogger(__name__)

SOI, EOI = b"\xff\xd8", b"\xff\xd9"
MAX_BUFFER = 4 * 1024 * 1024
RETRY_S = 300.0  # after the command exited by itself, wait this long before trying again
STOP_WAIT_S = 3.0
CHUNK = 32 * 1024


class JpegSplitter:
    """Split a byte stream into JPEG pictures."""

    def __init__(self) -> None:
        self._buf = bytearray()

    def feed(self, chunk: bytes) -> list[bytes]:
        self._buf += chunk
        frames: list[bytes] = []
        while True:
            start = self._buf.find(SOI)
            if start < 0:
                del self._buf[:-1]
                return frames
            end = self._buf.find(EOI, start + 2)
            if end < 0:
                del self._buf[:start]
                if len(self._buf) > MAX_BUFFER:
                    self._buf.clear()
                return frames
            frames.append(bytes(self._buf[start : end + 2]))
            del self._buf[: end + 2]


def command_line(template: str, *, fps: float, size: str, device: str) -> list[str]:
    """The stream command as arguments, with {fps}, {size}, {width}, {height}, {device} set."""
    width, _, height = size.partition("x")
    return [
        part.format(fps=f"{fps:g}", size=size, width=width, height=height, device=device)
        for part in shlex.split(template)
    ]


Sender = Callable[[bytes, float], Awaitable[None]]


class CameraStream:
    """One run of the stream command, sending what it writes."""

    def __init__(
        self,
        command: list[str],
        fmt: str,
        fps: float,
        *,
        send_frame: Sender,
        send_chunk: Callable[[bytes], Awaitable[None]],
    ) -> None:
        self.command = command
        self.format = fmt
        self.fps = max(0.5, fps)
        self._send_frame = send_frame
        self._send_chunk = send_chunk
        self.state = "starting"  # starting | running | stopped | failed
        self.error: str | None = None
        self.frames_sent = 0
        self.latest: bytes | None = None  # MJPEG: the newest picture (for stills)
        self.latest_at: float | None = None
        self._proc: asyncio.subprocess.Process | None = None
        self._tasks: list[asyncio.Task[None]] = []
        self._new = asyncio.Event()
        self._stopping = False
        self.exited = asyncio.Event()  # the command ended, asked to or not

    async def start(self) -> None:
        self._proc = await asyncio.create_subprocess_exec(
            *self.command,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        self.state = "running"
        self._tasks = [asyncio.create_task(self._read())]
        if self.format == "mjpeg":
            self._tasks.append(asyncio.create_task(self._pace()))
        log.info("camera stream started (%s, %g fps)", self.format, self.fps)

    async def _read(self) -> None:
        proc = self._proc
        assert proc is not None and proc.stdout is not None
        splitter = JpegSplitter()
        try:
            while chunk := await proc.stdout.read(CHUNK):
                if self.format == "h264":
                    await self._send_chunk(chunk)
                    self.frames_sent += 1
                    continue
                for frame in splitter.feed(chunk):
                    self.latest, self.latest_at = frame, time.time() * 1000
                    self._new.set()
            err = b""
            if proc.stderr is not None:
                err = await proc.stderr.read()
            code = await proc.wait()
            if not self._stopping:
                self.state = "failed"
                self.error = f"exited with {code}: {err.decode(errors='replace').strip()[-200:]}"
                log.warning("camera stream stopped by itself: %s", self.error)
        finally:
            self.exited.set()

    async def _pace(self) -> None:
        """Send the newest picture, at most fps a second; pictures in between are dropped."""
        interval = 1.0 / self.fps
        loop = asyncio.get_running_loop()
        while True:
            await self._new.wait()
            self._new.clear()
            started = loop.time()
            frame, at = self.latest, self.latest_at
            if frame is not None and at is not None:
                await self._send_frame(frame, at)
                self.frames_sent += 1
            await asyncio.sleep(max(0.0, interval - (loop.time() - started)))

    async def stop(self) -> None:
        self._stopping = True
        proc = self._proc
        if proc is not None and proc.returncode is None:
            for stop in (proc.terminate, proc.kill):
                with contextlib.suppress(ProcessLookupError):
                    stop()
                try:
                    await asyncio.wait_for(proc.wait(), STOP_WAIT_S)
                    break
                except TimeoutError:
                    continue
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        if self.state != "failed":
            self.state = "stopped"
        log.info("camera stream stopped (%d sent)", self.frames_sent)
