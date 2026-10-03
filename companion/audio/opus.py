"""Opus audio input (edge devices on metered links). Requires the ``opus`` extra (PyAV).

Wire format of an ``audio.input.chunk`` payload with ``encoding: "opus"``:
one or more raw Opus packets, each prefixed with its length as a big-endian
uint16. Batching a few 20 ms packets per frame keeps per-frame overhead small.
"""

from __future__ import annotations

import struct

import numpy as np

from companion.audio.pcm import SAMPLE_RATE
from companion.core.errors import CompanionError

_LEN = struct.Struct(">H")


class OpusUnavailable(CompanionError):
    code = "audio_codec_unavailable"


def split_packets(payload: bytes) -> list[bytes]:
    packets: list[bytes] = []
    offset = 0
    while offset + _LEN.size <= len(payload):
        (size,) = _LEN.unpack_from(payload, offset)
        offset += _LEN.size
        if size == 0 or offset + size > len(payload):
            break  # truncated or corrupt: drop the rest of this chunk
        packets.append(payload[offset : offset + size])
        offset += size
    return packets


def join_packets(packets: list[bytes]) -> bytes:
    return b"".join(_LEN.pack(len(p)) + p for p in packets)


class OpusDecoder:
    """Decode length-prefixed Opus packets to float32 mono PCM at 16 kHz."""

    def __init__(self) -> None:
        try:
            import av
        except ImportError as exc:
            raise OpusUnavailable(
                "Opus input needs the opus extra on the server: uv sync --extra opus"
            ) from exc
        self._av = av
        self._codec = av.CodecContext.create("opus", "r")
        self._codec.sample_rate = 48000
        self._codec.layout = "mono"
        self._resampler = av.AudioResampler(format="s16", layout="mono", rate=SAMPLE_RATE)

    def decode(self, payload: bytes) -> np.ndarray:
        out: list[np.ndarray] = []
        for packet in split_packets(payload):
            try:
                frames = self._codec.decode(self._av.Packet(packet))
            except self._av.error.FFmpegError:
                continue  # one bad packet must not end the stream
            for frame in frames:
                for resampled in self._resampler.resample(frame):
                    out.append(resampled.to_ndarray().reshape(-1))
        if not out:
            return np.zeros(0, dtype=np.float32)
        return np.concatenate(out).astype(np.float32) / 32768.0
