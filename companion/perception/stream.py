"""Pictures from a device's H.264 camera stream (stream mode, protocol 1.4).

A device in stream mode sends its encoder's output as ``camera.stream.chunk``
binary frames; this turns them back into pictures with PyAV, on the server,
so the device itself only captures and encodes. MJPEG streams need none of
this: they arrive as ``vision.frame`` pictures with reason ``stream``.
"""

from __future__ import annotations

import io
import logging
from typing import Any

import numpy as np

log = logging.getLogger(__name__)


class H264Frames:
    """Decodes one device's H.264 (Annex B) byte stream into RGB pictures."""

    def __init__(self) -> None:
        self._codec: Any = None
        self.frames = 0
        self.errors = 0

    def _context(self) -> Any:
        if self._codec is None:
            import av

            self._codec = av.CodecContext.create("h264", "r")
        return self._codec

    def decode(self, data: bytes) -> list[np.ndarray]:
        """The pictures completed by ``data`` (often none, sometimes several)."""
        import av

        codec = self._context()
        out: list[np.ndarray] = []
        try:
            for packet in codec.parse(data):
                for frame in codec.decode(packet):
                    out.append(frame.to_ndarray(format="rgb24"))
        except av.error.FFmpegError as exc:  # a broken piece: start over at the next key frame
            self.errors += 1
            log.debug("h264 stream: %s", exc)
            self._codec = None
        self.frames += len(out)
        return out


def to_jpeg(rgb: np.ndarray, quality: int = 85) -> bytes:
    from PIL import Image

    out = io.BytesIO()
    Image.fromarray(rgb).save(out, format="JPEG", quality=quality)
    return out.getvalue()
