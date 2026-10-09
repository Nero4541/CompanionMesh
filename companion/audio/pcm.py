from __future__ import annotations

import numpy as np

SAMPLE_RATE = 16_000


def pcm16_to_float32(data: bytes) -> np.ndarray:
    if len(data) % 2:
        data = data[:-1]
    return np.frombuffer(data, dtype="<i2").astype(np.float32) / 32768.0


def float32_to_pcm16(audio: np.ndarray) -> bytes:
    clipped = np.clip(audio, -1.0, 1.0)
    return (clipped * 32767.0).astype("<i2").tobytes()


def rms(audio: np.ndarray) -> float:
    if audio.size == 0:
        return 0.0
    return float(np.sqrt(np.mean(np.square(audio, dtype=np.float64))))


def resample(audio: np.ndarray, rate: int, target: int = SAMPLE_RATE) -> np.ndarray:
    """Simple resampler (box low-pass, then linear interpolation); good enough for
    echo references and level checks, not for listening."""
    if rate == target or audio.size == 0:
        return audio.astype(np.float32, copy=False)
    if rate > target:
        width = max(1, round(rate / target))
        if width > 1:
            kernel = np.ones(width, dtype=np.float32) / width
            audio = np.convolve(audio, kernel, mode="same")
    n = max(1, round(audio.size * target / rate))
    positions = np.linspace(0, audio.size - 1, n)
    return np.interp(positions, np.arange(audio.size), audio).astype(np.float32)


def wav_info(data: bytes) -> tuple[int, int, int, bytes] | None:
    """(rate, channels, sample width, PCM frames) of a WAV file, or None."""
    import io
    import wave

    try:
        with wave.open(io.BytesIO(data)) as w:
            return (
                w.getframerate(),
                w.getnchannels(),
                w.getsampwidth(),
                w.readframes(w.getnframes()),
            )
    except (wave.Error, EOFError):
        return None


def wav_to_mono16k(data: bytes) -> np.ndarray | None:
    """A 16-bit WAV clip as mono float32 at 16 kHz, or None if it is not one."""
    info = wav_info(data)
    if info is None or info[2] != 2:
        return None
    rate, channels, _, frames = info
    audio = pcm16_to_float32(frames)
    if channels > 1:
        audio = audio[: audio.size - audio.size % channels].reshape(-1, channels).mean(axis=1)
    return resample(audio, rate)


def audio_duration_s(data: bytes, mime: str) -> float | None:
    """Playing time of an encoded clip when it can be told cheaply (WAV); else None."""
    if "wav" not in mime and not data.startswith(b"RIFF"):
        return None
    info = wav_info(data)
    if info is None:
        return None
    rate, channels, width, frames = info
    return len(frames) / max(1, rate * channels * width)
