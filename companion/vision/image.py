"""Image validation, downscaling and perceptual hashing.

Frames are checked against their declared MIME type by magic bytes and fully
decoded before anything else touches them; image bytes are never logged.
"""

from __future__ import annotations

import io
from dataclasses import dataclass

from PIL import Image, UnidentifiedImageError

from companion.core.errors import CompanionError

SUPPORTED_MIME = {"image/jpeg": "JPEG", "image/png": "PNG", "image/webp": "WEBP"}


class ImageRejected(CompanionError):
    code = "vision_frame_rejected"


@dataclass(slots=True)
class DecodedImage:
    image: Image.Image
    mime: str
    size_bytes: int

    @property
    def width(self) -> int:
        return self.image.width

    @property
    def height(self) -> int:
        return self.image.height


def sniff_mime(data: bytes) -> str | None:
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def decode_image(data: bytes, mime: str, *, max_bytes: int, max_pixels: int) -> DecodedImage:
    """Validate and decode one frame, or raise :class:`ImageRejected`."""
    if mime not in SUPPORTED_MIME:
        raise ImageRejected(f"unsupported image type {mime!r}; use JPEG, PNG or WebP")
    if not data:
        raise ImageRejected("empty frame")
    if len(data) > max_bytes:
        raise ImageRejected(f"frame is {len(data)} bytes; the limit is {max_bytes}")
    actual = sniff_mime(data)
    if actual != mime:
        raise ImageRejected(f"frame declared {mime} but contains {actual or 'unknown data'}")
    try:
        with Image.open(io.BytesIO(data)) as probe:
            if probe.width * probe.height > max_pixels:
                raise ImageRejected(
                    f"frame is {probe.width}x{probe.height}; the limit is {max_pixels} pixels"
                )
            probe.load()
            image = probe.convert("RGB")
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError) as exc:
        raise ImageRejected(f"frame could not be decoded: {exc}") from exc
    return DecodedImage(image=image, mime=mime, size_bytes=len(data))


def to_jpeg(image: Image.Image, *, max_side: int, quality: int = 85) -> bytes:
    """Downscale so the longer side is at most ``max_side`` and encode as JPEG."""
    scaled = image.copy()
    scaled.thumbnail((max_side, max_side), Image.Resampling.LANCZOS)
    out = io.BytesIO()
    scaled.save(out, format="JPEG", quality=quality)
    return out.getvalue()


def dhash(image: Image.Image, size: int = 8) -> int:
    """64-bit difference hash: robust to compression noise, sensitive to change."""
    small = image.convert("L").resize((size + 1, size), Image.Resampling.BILINEAR)
    pixels = small.tobytes()  # one byte per pixel in mode "L"
    bits = 0
    for row in range(size):
        for col in range(size):
            left = pixels[row * (size + 1) + col]
            right = pixels[row * (size + 1) + col + 1]
            bits = (bits << 1) | (left > right)
    return bits


def hamming(a: int, b: int) -> int:
    return (a ^ b).bit_count()


def test_card_jpeg(size: int = 256) -> bytes:
    """A synthetic picture (colored shapes) used by `companion check` to probe the VLM."""
    from PIL import ImageDraw

    card = Image.new("RGB", (size, size), "white")
    draw = ImageDraw.Draw(card)
    draw.rectangle((24, 24, size // 2, size // 2), fill="red")
    draw.ellipse((size // 2, size // 2, size - 24, size - 24), fill="blue")
    out = io.BytesIO()
    card.save(out, format="JPEG", quality=90)
    return out.getvalue()
