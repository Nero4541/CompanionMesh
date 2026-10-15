"""MIPI CSI cameras on CVITEK/SOPHGO boards (e.g. the LicheeRV Nano's GC4653).

Sipeed's Buildroot image does not expose these cameras through V4L2, so
ffmpeg cannot open them. The image ships ``sensor_test``, which starts the
sensor and ISP from ``/mnt/data/sensor_cfg.ini`` and, when asked on its
menu, dumps the current frame as an NV21 file. ``CviCamera`` keeps one
``sensor_test`` running and drives that menu:

* change detection reads a few rows of each dump's luma plane (no decoding,
  about 120 kB of a 5.5 MB frame) for the tiny greyscale frames
  ``MotionDetector`` wants;
* a JPEG is made only when a picture is to be sent, by ffmpeg, from the
  newest dump.

Avoid Sipeed's ``sample_venc``/``sample_vio`` for the camera on this image:
when they crash (they do with several option sets) the camera delivers no
frames again until the board restarts.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import re
import shutil
import signal
import tempfile
import time
from collections.abc import AsyncIterator
from pathlib import Path

from companion_edge.devices import rotate_filter
from companion_edge.motion import HEIGHT as GRAY_HEIGHT
from companion_edge.motion import WIDTH as GRAY_WIDTH

log = logging.getLogger(__name__)

SENSOR_TEST = "/mnt/system/usr/bin/sensor_test"
SENSOR_CONFIG = "/mnt/data/sensor_cfg.ini"
DUMP_NAME = "sample_0.yuv"  # written by sensor_test into its working directory
# Menu answers: 0 = linear (not WDR) mode at start; per frame "2" (dump a YUV
# frame), dev 0, one frame; 255 quits.
START = b"0\n"
DUMP = b"2\n0\n1\n"
QUIT = b"255\n"
WARMUP_S = 6.0  # the first frames after start are empty buffers
QUIT_WAIT_S = 10.0  # for each step of stopping it: quit, then SIGTERM, then SIGKILL
DUMP_TIMEOUT_S = 3.0
STALLED_AFTER = 5  # failed dumps in a row before warning that the camera is stuck
_SIZE = re.compile(rb"u16Width (\d+) stSnsrMode\.u16Height (\d+)")


def available(tool: str = SENSOR_TEST) -> bool:
    return os.path.exists(tool) and os.path.exists(SENSOR_CONFIG)


def stray_tools(name: str) -> list[int]:
    """Pids of running ``name`` processes whose parent has gone (adopted by init)."""
    found = []
    for entry in os.listdir("/proc") if os.path.isdir("/proc") else []:
        if not entry.isdigit():
            continue
        try:
            cmd = Path(f"/proc/{entry}/cmdline").read_bytes().split(bytes(1))[0]
            stat = Path(f"/proc/{entry}/stat").read_text()
        except OSError:
            continue
        parent = int(stat.rsplit(")", 1)[1].split()[1])
        if os.path.basename(cmd.decode(errors="replace")) == name and parent == 1:
            found.append(int(entry))
    return found


def stop_pids(pids: list[int]) -> None:
    for pid in pids:
        with contextlib.suppress(OSError):
            os.kill(pid, signal.SIGTERM)
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline and any(Path(f"/proc/{p}").exists() for p in pids):
        time.sleep(0.1)


def sample_gray(luma: bytes, width: int, height: int) -> bytes:
    """A GRAY_WIDTH x GRAY_HEIGHT greyscale frame by sampling the luma plane."""
    step_x, step_y = max(1, width // GRAY_WIDTH), max(1, height // GRAY_HEIGHT)
    rows = []
    for r in range(GRAY_HEIGHT):
        start = r * step_y * width
        rows.append(luma[start : start + step_x * GRAY_WIDTH : step_x])
    return b"".join(rows)


def read_gray(path: Path, width: int, height: int) -> bytes:
    """``sample_gray`` of an NV21 file, reading only the rows it samples.

    The board has little memory to spare; a whole luma plane is 3.7 MB.
    """
    step_x, step_y = max(1, width // GRAY_WIDTH), max(1, height // GRAY_HEIGHT)
    rows = []
    with path.open("rb") as fh:
        for r in range(GRAY_HEIGHT):
            fh.seek(r * step_y * width)
            rows.append(fh.read(step_x * GRAY_WIDTH)[::step_x])
    return b"".join(rows)


class CviCamera:
    """``Camera`` for CVITEK MIPI sensors through Sipeed's ``sensor_test``."""

    def __init__(
        self,
        *,
        size: str = "640x360",
        quality: int = 5,
        analysis_fps: float = 1.0,
        tool: str | list[str] = SENSOR_TEST,
        workdir: str | None = None,
        rotate: int = 0,
        warmup_s: float = WARMUP_S,
    ) -> None:
        self.size = size
        self.quality = quality
        self.analysis_fps = analysis_fps
        # The program, or a whole command (tests run a stand-in through Python).
        self.command = [tool] if isinstance(tool, str) else list(tool)
        self.tool_found = os.path.exists(self.command[0])
        self.warmup_s = warmup_s
        self.rotate = rotate
        # A fixed place, so a device that was killed leaves no stray 5.5 MB
        # frames in RAM (/tmp is tmpfs) when it starts again.
        self._workdir = Path(workdir or Path(tempfile.gettempdir()) / "companion-cvi")
        self.width, self.height = 2560, 1440  # replaced by what the sensor reports
        self.latest_at: float | None = None  # wall clock ms of the newest dump
        self._proc: asyncio.subprocess.Process | None = None
        self._lock = asyncio.Lock()  # one dump (and its conversion) at a time
        self._ready = asyncio.Event()
        self._failures = 0
        self._tasks: list[asyncio.Task[None]] = []

    @property
    def analysis(self) -> bool:
        return self.analysis_fps > 0

    @property
    def dump_path(self) -> Path:
        return self._workdir / DUMP_NAME

    async def start(self) -> None:
        if not self.tool_found:
            raise RuntimeError(f"{self.command[0]} not found; this needs Sipeed's Buildroot image")
        self._workdir.mkdir(parents=True, exist_ok=True)
        stray = await asyncio.to_thread(stray_tools, os.path.basename(self.command[-1]))
        if stray:
            # Left by a device that died without closing the camera: it would keep
            # the sensor, and sensor_test does not stop when its input closes.
            log.warning("stopping a camera tool left running (pid %s)", ", ".join(map(str, stray)))
            await asyncio.to_thread(stop_pids, stray)
        await self._spawn()

    async def _spawn(self) -> None:
        self._ready.clear()
        self._proc = await asyncio.create_subprocess_exec(
            *self.command,
            cwd=str(self._workdir),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        assert self._proc.stdin is not None
        self._proc.stdin.write(START)
        await self._proc.stdin.drain()
        self._tasks = [asyncio.create_task(self._read_output(self._proc))]
        # Dumps wait until the sensor has delivered real frames; the client
        # meanwhile connects as usual.
        asyncio.get_running_loop().call_later(self.warmup_s, self._ready.set)

    async def _read_output(self, proc: asyncio.subprocess.Process) -> None:
        assert proc.stdout is not None
        tail: list[bytes] = []
        while line := await proc.stdout.readline():
            if match := _SIZE.search(line):
                self.width, self.height = int(match[1]), int(match[2])
            if b"Init OK" in line:
                log.info("camera: %s", line.decode(errors="replace").strip(" =\r\n"))
            elif b" NG" in line or b"fail" in line.lower():
                log.warning("camera: %s", line.decode(errors="replace").strip()[-200:])
            tail = [*tail[-4:], line]
        code = await proc.wait()
        if self._proc is proc:
            log.warning(
                "camera tool exited (%s): %s", code, b"".join(tail).decode(errors="replace")[-300:]
            )

    async def _restart(self) -> None:
        log.warning("restarting the camera tool")
        await self._stop_proc()
        await self._spawn()

    async def _dump(self) -> bytes | None:
        """Ask for one frame; return it as a tiny greyscale frame, or None if none came."""
        if self._proc is None or self._proc.returncode is not None:
            await self._restart()
        await self._ready.wait()
        assert self._proc is not None and self._proc.stdin is not None
        path = self.dump_path
        size = self.width * self.height * 3 // 2
        with contextlib.suppress(FileNotFoundError):
            path.unlink()
        self._proc.stdin.write(DUMP)
        await self._proc.stdin.drain()
        deadline = time.monotonic() + DUMP_TIMEOUT_S
        while time.monotonic() < deadline:
            with contextlib.suppress(FileNotFoundError):
                if path.stat().st_size >= size:
                    await asyncio.sleep(0.03)  # let the writer finish and close
                    gray = await asyncio.to_thread(read_gray, path, self.width, self.height)
                    if not any(gray):
                        break  # an empty buffer (all zero, no sensor noise): not a picture yet
                    self._failures = 0
                    self.latest_at = time.time() * 1000
                    return gray
            await asyncio.sleep(0.03)
        self._failures += 1
        if self._failures == STALLED_AFTER:
            log.warning(
                "camera gives no frames; if this persists, restart the board "
                "(the sensor can get stuck after another camera program crashed)"
            )
        if self._failures % STALLED_AFTER == 0:
            await self._restart()
        return None

    async def gray_frames(self) -> AsyncIterator[tuple[bytes, float]]:
        """Tiny greyscale frames with their loop time, ``analysis_fps`` per second."""
        loop = asyncio.get_running_loop()
        interval = 1.0 / self.analysis_fps if self.analysis_fps > 0 else 1.0
        while True:
            started = loop.time()
            async with self._lock:
                gray = await self._dump()
            if gray is not None:
                yield gray, loop.time()
            await asyncio.sleep(max(0.0, interval - (loop.time() - started)))

    async def latest(self) -> bytes | None:
        """A JPEG of the newest frame (a fresh one if the last is over a second old)."""
        async with self._lock:
            fresh = self.latest_at is not None and time.time() * 1000 - self.latest_at < 1000
            if not fresh and await self._dump() is None:
                return None
            return await self._jpeg()

    async def _jpeg(self) -> bytes | None:
        ffmpeg = shutil.which("ffmpeg")
        if ffmpeg is None:
            raise RuntimeError("ffmpeg not found; it turns camera frames into JPEG")
        proc = await asyncio.create_subprocess_exec(
            ffmpeg, "-hide_banner", "-loglevel", "error",
            "-f", "rawvideo", "-pix_fmt", "nv21", "-s", f"{self.width}x{self.height}",
            "-i", str(self.dump_path),
            "-vf", f"scale={self.size.replace('x', ':')}{rotate_filter(self.rotate)}",
            "-q:v", str(self.quality), "-f", "image2pipe", "-vcodec", "mjpeg", "-",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )  # fmt: skip
        out, err = await proc.communicate()
        if proc.returncode != 0 or not out.startswith(b"\xff\xd8"):
            log.warning("camera: JPEG conversion failed: %s", err.decode(errors="replace")[-200:])
            return None
        return out

    async def _stop_proc(self) -> None:
        proc, self._proc = self._proc, None
        for task in self._tasks:
            task.cancel()
        if proc is None or proc.returncode is not None:
            return
        with contextlib.suppress(Exception):
            assert proc.stdin is not None
            proc.stdin.write(QUIT)
            await proc.stdin.drain()
        # The menu's quit releases the camera properly (it takes well under a
        # second); a killed sensor_test can leave the sensor giving no frames
        # until the board restarts, so signals are only the last resort.
        for stop in (None, proc.terminate, proc.kill):
            if stop is not None:
                with contextlib.suppress(ProcessLookupError):
                    stop()
            try:
                await asyncio.wait_for(proc.wait(), QUIT_WAIT_S)
                return
            except TimeoutError:
                log.warning(
                    "camera tool did not stop; %s", "terminating" if stop is None else "killing"
                )

    async def close(self) -> None:
        await self._stop_proc()
        with contextlib.suppress(OSError):
            self.dump_path.unlink()
            self._workdir.rmdir()
