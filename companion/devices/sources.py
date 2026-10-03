"""Frame sources for camera devices: HTTP snapshots, MJPEG streams, RTSP.

Phone "IP camera" apps usually offer all three, e.g. ``/shot.jpg`` (snapshot),
``/video`` (MJPEG) and ``rtsp://…`` (needs ffmpeg).

Credentials come from the URL (``http://user:pass@host/…``) or from the
``CAMERA_USERNAME`` / ``CAMERA_PASSWORD`` environment variables (``.env``).
Basic and Digest authentication are both handled.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import shutil
from collections.abc import Generator
from typing import Protocol
from urllib.parse import quote, unquote, urlsplit, urlunsplit

import httpx

from companion.core.errors import ProviderError, ProviderUnavailable
from companion.core.logging import kv
from companion.providers.http import describe_http_error

log = logging.getLogger(__name__)

SOI, EOI = b"\xff\xd8", b"\xff\xd9"
MAX_BUFFER = 8 * 1024 * 1024


class AutoAuth(httpx.Auth):
    """Answer whichever challenge the camera sends: Basic or Digest."""

    def __init__(self, username: str, password: str) -> None:
        self._basic = httpx.BasicAuth(username, password)
        self._digest = httpx.DigestAuth(username, password)
        self._scheme: str | None = None

    def auth_flow(self, request: httpx.Request) -> Generator[httpx.Request, httpx.Response]:
        if self._scheme == "digest":
            yield from self._digest.auth_flow(request)
            return
        if self._scheme == "basic":
            yield from self._basic.auth_flow(request)
            return
        response = yield request
        if response.status_code != 401:
            return
        challenge = response.headers.get("www-authenticate", "").lower()
        self._scheme = "digest" if challenge.startswith("digest") else "basic"
        yield from self.auth_flow(request)


def split_credentials(url: str) -> tuple[str, httpx.Auth | None]:
    """Remove ``user:pass@`` from a URL and turn it (or CAMERA_* env vars) into auth."""
    parts = urlsplit(url)
    username = unquote(parts.username) if parts.username else os.environ.get("CAMERA_USERNAME")
    password = unquote(parts.password) if parts.password else os.environ.get("CAMERA_PASSWORD")
    host = parts.hostname or ""
    if parts.port:
        host = f"{host}:{parts.port}"
    clean = urlunsplit((parts.scheme, host, parts.path, parts.query, parts.fragment))
    if not username:
        return clean, None
    return clean, AutoAuth(username, password or "")


def with_credentials(url: str) -> str:
    """For ffmpeg: put CAMERA_* credentials into the URL if it has none."""
    parts = urlsplit(url)
    user = os.environ.get("CAMERA_USERNAME")
    if parts.username or not user:
        return url
    secret = quote(os.environ.get("CAMERA_PASSWORD", ""), safe="")
    netloc = f"{quote(user, safe='')}:{secret}@{parts.netloc}"
    return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))


class FrameSource(Protocol):
    description: str

    async def start(self) -> None: ...

    async def grab(self) -> bytes:
        """Return the newest JPEG frame."""
        ...

    async def close(self) -> None: ...


class JpegStreamParser:
    """Split a byte stream (MJPEG over HTTP or ffmpeg's image2pipe) into JPEGs."""

    def __init__(self) -> None:
        self._buf = bytearray()

    def feed(self, chunk: bytes) -> list[bytes]:
        self._buf += chunk
        frames: list[bytes] = []
        while True:
            start = self._buf.find(SOI)
            if start < 0:
                del self._buf[:-1]  # keep a possible half marker
                break
            end = self._buf.find(EOI, start + 2)
            if end < 0:
                del self._buf[:start]
                if len(self._buf) > MAX_BUFFER:
                    self._buf.clear()  # runaway frame; resynchronize
                break
            frames.append(bytes(self._buf[start : end + 2]))
            del self._buf[: end + 2]
        return frames


class SnapshotSource:
    """Fetch a single JPEG per grab (e.g. ``http://phone:8080/shot.jpg``)."""

    def __init__(
        self,
        url: str,
        *,
        timeout: float = 10.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.url, auth = split_credentials(url)
        self.description = f"snapshot {self.url}"
        self._client = httpx.AsyncClient(
            timeout=timeout, follow_redirects=True, transport=transport, auth=auth
        )

    async def start(self) -> None:
        await self.grab()

    async def grab(self) -> bytes:
        try:
            response = await self._client.get(self.url)
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise describe_http_error("camera", exc, self.url) from exc
        if not response.content.startswith(SOI):
            raise ProviderError("camera", f"{self.url} did not return a JPEG image")
        return response.content

    async def close(self) -> None:
        await self._client.aclose()


class _LatestFrame:
    """Shared by stream sources: keeps only the newest decoded frame."""

    def __init__(self) -> None:
        self.frame: bytes | None = None
        self.error: Exception | None = None
        self.updated = asyncio.Event()

    def put(self, frame: bytes) -> None:
        self.frame = frame
        self.updated.set()

    async def get(self, wait_s: float) -> bytes:
        if self.frame is None:
            with contextlib.suppress(TimeoutError):
                async with asyncio.timeout(wait_s):
                    await self.updated.wait()
        if self.frame is None:
            raise self.error or ProviderUnavailable("camera", "no frame received yet")
        return self.frame


class MjpegSource:
    """Read a ``multipart/x-mixed-replace`` MJPEG stream (e.g. ``/video``)."""

    def __init__(
        self,
        url: str,
        *,
        timeout: float = 10.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.url, auth = split_credentials(url)
        self.description = f"MJPEG {self.url}"
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(timeout, read=None),
            follow_redirects=True,
            transport=transport,
            auth=auth,
        )
        self._latest = _LatestFrame()
        self._task: asyncio.Task[None] | None = None
        self._timeout = timeout

    async def _read(self) -> None:
        parser = JpegStreamParser()
        while True:
            try:
                async with self._client.stream("GET", self.url) as response:
                    response.raise_for_status()
                    async for chunk in response.aiter_bytes():
                        for frame in parser.feed(chunk):
                            self._latest.put(frame)
            except httpx.HTTPError as exc:
                self._latest.error = describe_http_error("camera", exc, self.url)
                log.warning("camera stream interrupted", extra=kv(url=self.url, error=str(exc)))
            await asyncio.sleep(2.0)

    async def start(self) -> None:
        self._task = asyncio.create_task(self._read(), name="mjpeg-reader")
        await self._latest.get(self._timeout)

    async def grab(self) -> bytes:
        return await self._latest.get(self._timeout)

    async def close(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
        await self._client.aclose()


class FfmpegSource:
    """Decode RTSP (or anything ffmpeg reads) to JPEG frames at a low rate."""

    def __init__(self, url: str, *, fps: float = 2.0, timeout: float = 15.0) -> None:
        self.url = with_credentials(url)
        self.description = f"ffmpeg {split_credentials(url)[0]}"
        self._fps = fps
        self._timeout = timeout
        self._latest = _LatestFrame()
        self._proc: asyncio.subprocess.Process | None = None
        self._task: asyncio.Task[None] | None = None

    async def _read(self) -> None:
        assert self._proc is not None and self._proc.stdout is not None
        parser = JpegStreamParser()
        while chunk := await self._proc.stdout.read(65536):
            for frame in parser.feed(chunk):
                self._latest.put(frame)
        self._latest.error = ProviderUnavailable(
            "camera", f"ffmpeg stopped reading {self.description}"
        )

    async def start(self) -> None:
        ffmpeg = shutil.which("ffmpeg")
        if ffmpeg is None:
            raise ProviderUnavailable(
                "camera", "RTSP needs ffmpeg on PATH; or use the camera's HTTP snapshot/MJPEG URL"
            )
        args = [ffmpeg, "-hide_banner", "-loglevel", "error"]
        if self.url.startswith("rtsp"):
            args += ["-rtsp_transport", "tcp"]
        args += [
            "-i",
            self.url,
            "-vf",
            f"fps={self._fps}",
            "-q:v",
            "4",
            "-f",
            "image2pipe",
            "-vcodec",
            "mjpeg",
            "-",
        ]
        self._proc = await asyncio.create_subprocess_exec(
            *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL
        )
        self._task = asyncio.create_task(self._read(), name="ffmpeg-reader")
        await self._latest.get(self._timeout)

    async def grab(self) -> bytes:
        return await self._latest.get(self._timeout)

    async def close(self) -> None:
        if self._proc is not None and self._proc.returncode is None:
            self._proc.kill()
            await self._proc.wait()
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task


async def open_source(
    url: str, *, transport: httpx.AsyncBaseTransport | None = None
) -> FrameSource:
    """Pick a source from the URL (and, for HTTP, the response content type)."""
    if not url.startswith(("http://", "https://")):
        source: FrameSource = FfmpegSource(url)
    else:
        probe_url, auth = split_credentials(url)
        async with httpx.AsyncClient(
            timeout=10.0, follow_redirects=True, transport=transport, auth=auth
        ) as client:
            try:
                async with client.stream("GET", probe_url) as response:
                    response.raise_for_status()
                    content_type = response.headers.get("content-type", "").lower()
            except httpx.HTTPError as exc:
                raise describe_http_error("camera", exc, probe_url) from exc
        source = (
            MjpegSource(url, transport=transport)
            if "multipart" in content_type
            else SnapshotSource(url, transport=transport)
        )
    await source.start()
    return source
