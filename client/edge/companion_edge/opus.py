"""Opus encoder over the system libopus (``apt install libopus0``) via ctypes."""

from __future__ import annotations

import ctypes
import ctypes.util

OPUS_APPLICATION_VOIP = 2048
OPUS_SET_BITRATE_REQUEST = 4002
FRAME_SAMPLES = 320  # 20 ms at 16 kHz
SAMPLE_RATE = 16000
MAX_PACKET = 1500


class OpusUnavailable(RuntimeError):
    pass


def _load() -> ctypes.CDLL:
    name = ctypes.util.find_library("opus") or "libopus.so.0"
    try:
        lib = ctypes.CDLL(name)
    except OSError as exc:
        raise OpusUnavailable(f"libopus not found ({exc}); install libopus0") from exc
    lib.opus_encoder_create.restype = ctypes.c_void_p
    lib.opus_encoder_create.argtypes = [
        ctypes.c_int32,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.POINTER(ctypes.c_int),
    ]
    lib.opus_encode.restype = ctypes.c_int32
    lib.opus_encode.argtypes = [
        ctypes.c_void_p,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int32,
    ]
    lib.opus_encoder_destroy.argtypes = [ctypes.c_void_p]
    return lib


class OpusEncoder:
    """Encodes 20 ms frames of 16 kHz mono s16le PCM into raw Opus packets."""

    def __init__(self, bitrate: int = 24000) -> None:
        self._lib = _load()
        error = ctypes.c_int(0)
        self._state = self._lib.opus_encoder_create(
            SAMPLE_RATE, 1, OPUS_APPLICATION_VOIP, ctypes.byref(error)
        )
        if error.value != 0 or not self._state:
            raise OpusUnavailable(f"opus_encoder_create failed ({error.value})")
        self._lib.opus_encoder_ctl(
            ctypes.c_void_p(self._state), OPUS_SET_BITRATE_REQUEST, ctypes.c_int32(bitrate)
        )
        self._out = ctypes.create_string_buffer(MAX_PACKET)

    def encode(self, pcm20ms: bytes) -> bytes:
        if len(pcm20ms) != FRAME_SAMPLES * 2:
            raise ValueError("opus frames must be exactly 20 ms (640 bytes)")
        size = self._lib.opus_encode(self._state, pcm20ms, FRAME_SAMPLES, self._out, MAX_PACKET)
        if size < 0:
            raise RuntimeError(f"opus_encode failed ({size})")
        return self._out.raw[:size]

    def close(self) -> None:
        if self._state:
            self._lib.opus_encoder_destroy(self._state)
            self._state = None
