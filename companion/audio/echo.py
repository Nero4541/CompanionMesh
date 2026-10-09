"""Echo handling: keep the companion's own voice out of its ears (v0.5).

Three layers, from cheapest to most involved:

* **Gating** (in the session): while reply audio plays on a device without
  echo cancellation, speech must be longer and more certain to count as the
  user cutting in.
* :class:`EchoGuard`: a transcript that mostly repeats what the companion said
  moments ago is its own voice coming back through the microphone; drop it.
* :class:`NlmsEchoCanceller`: a reference acoustic echo canceller. The server
  knows exactly which audio it sent to the speaker; devices report when each
  clip started playing, as a position in their microphone stream, so the
  reference can be lined up with the microphone signal. A delay estimator
  finds the remaining speaker/room latency and a frequency-domain NLMS filter
  subtracts the echo. Devices with their own AEC (browsers, phones, many USB
  speakerphones) say so in ``audio.input.start`` and skip all of this.
"""

from __future__ import annotations

import re
import time
import unicodedata
from collections import deque
from difflib import SequenceMatcher
from typing import Protocol

import numpy as np

from companion.audio.pcm import SAMPLE_RATE

_DROP = re.compile(r"[\W_]+", re.UNICODE)


def normalize_text(text: str) -> str:
    """Comparable form of a sentence: width-folded, lower case, no punctuation/spaces."""
    return _DROP.sub("", unicodedata.normalize("NFKC", text).lower())


class EchoGuard:
    """Recognize transcripts that repeat what the companion just said."""

    def __init__(self, *, similarity: float = 0.6, window_s: float = 20.0) -> None:
        self.similarity = similarity
        self.window_s = window_s
        self._spoken: deque[tuple[float, str]] = deque(maxlen=64)

    def spoke(self, text: str, *, at: float | None = None) -> None:
        normalized = normalize_text(text)
        if normalized:
            self._spoken.append((time.monotonic() if at is None else at, normalized))

    def extend(self, seconds: float) -> None:
        """Speech still playing: keep everything said so far fresh a while longer."""
        now = time.monotonic()
        self._spoken = deque(
            ((max(t, now + seconds), s) for t, s in self._spoken), maxlen=self._spoken.maxlen
        )

    def overlap(self, transcript: str, *, now: float | None = None) -> float:
        """Share (0..1) of the transcript found in recent companion speech."""
        heard = normalize_text(transcript)
        if len(heard) < 2:
            return 0.0
        now = time.monotonic() if now is None else now
        recent = "".join(s for t, s in self._spoken if now - t <= self.window_s)
        if not recent:
            return 0.0
        matcher = SequenceMatcher(None, heard, recent, autojunk=False)
        matched = sum(block.size for block in matcher.get_matching_blocks() if block.size >= 2)
        return matched / len(heard)

    def is_echo(self, transcript: str, *, now: float | None = None) -> bool:
        return self.overlap(transcript, now=now) >= self.similarity


# --- reference signal ----------------------------------------------------------


class ReferenceBuffer:
    """Far-end (speaker) audio placed on the microphone's sample timeline."""

    def __init__(self, keep_s: float = 60.0) -> None:
        self.keep = int(keep_s * SAMPLE_RATE)
        self._clips: deque[tuple[int, np.ndarray]] = deque()

    def add(self, audio: np.ndarray, start: int) -> None:
        """``audio`` (16 kHz mono) started playing at microphone sample ``start``."""
        self._clips.append((start, audio.astype(np.float32, copy=False)))
        newest_end = max(s + a.size for s, a in self._clips)
        while self._clips and self._clips[0][0] + self._clips[0][1].size < newest_end - self.keep:
            self._clips.popleft()

    def clear(self) -> None:
        self._clips.clear()

    def active(self, start: int, n: int) -> bool:
        return any(s < start + n and s + a.size > start for s, a in self._clips)

    def get(self, start: int, n: int) -> np.ndarray:
        """Reference samples ``[start, start + n)`` (zeros where nothing played)."""
        out = np.zeros(n, dtype=np.float32)
        for s, a in self._clips:
            lo, hi = max(start, s), min(start + n, s + a.size)
            if lo < hi:
                out[lo - start : hi - start] += a[lo - s : hi - s]
        return out


# --- delay estimation ------------------------------------------------------------


def estimate_delay(mic: np.ndarray, ref: np.ndarray, max_delay: int) -> tuple[int, float]:
    """Lag (samples, mic behind ref) maximizing GCC-PHAT, and its peak sharpness.

    ``ref`` must cover ``max_delay`` samples before the start of ``mic``:
    ``ref[i + max_delay]`` lines up with ``mic[i]`` at zero lag.
    """
    n = 1 << int(np.ceil(np.log2(mic.size + ref.size)))
    spec = np.fft.rfft(mic, n) * np.conj(np.fft.rfft(ref, n))
    spec /= np.abs(spec) + 1e-12
    corr = np.fft.irfft(spec, n)
    # mic[i] ~ ref[i + max_delay - lag]  =>  correlation peak at index -(max_delay - lag)
    lags = np.arange(0, max_delay + 1)
    values = corr[(lags - max_delay) % n]
    best = int(np.argmax(values))
    sharpness = float(values[best] / (np.mean(np.abs(values)) + 1e-12))
    return int(lags[best]), sharpness


# --- adaptive filter -----------------------------------------------------------------


class EchoCanceller(Protocol):
    def process(self, mic: np.ndarray, pos: int) -> np.ndarray:
        """Echo-reduced copy of ``mic``, whose first sample is microphone sample ``pos``."""
        ...

    def reset(self) -> None: ...


class NoEchoCanceller:
    def process(self, mic: np.ndarray, pos: int) -> np.ndarray:
        return mic

    def reset(self) -> None:
        pass


class NlmsEchoCanceller:
    """Frequency-domain block NLMS (overlap-save, constrained) with delay search.

    Works on blocks of ``filter_ms``; output lags input by at most one block.
    """

    def __init__(
        self,
        reference: ReferenceBuffer,
        *,
        filter_ms: int = 64,
        max_delay_ms: int = 600,
        step: float = 0.5,
    ) -> None:
        self.reference = reference
        self.block = int(SAMPLE_RATE * filter_ms / 1000)
        self.max_delay = int(SAMPLE_RATE * max_delay_ms / 1000)
        self.step = step
        self.delay: int | None = None
        self._search_mic: list[np.ndarray] = []
        self._search_start = 0
        self.reset()

    def reset(self) -> None:
        b = self.block
        self._weights = np.zeros(b + 1, dtype=np.complex128)
        self._power = np.full(b + 1, 1e-3)
        self._pending = np.zeros(0, dtype=np.float32)
        self._pending_pos = 0
        self._search_mic = []
        self.delay = None
        self.erle_db = 0.0

    def process(self, mic: np.ndarray, pos: int) -> np.ndarray:
        if self._pending.size == 0:
            self._pending_pos = pos
        self._pending = np.concatenate([self._pending, mic.astype(np.float32, copy=False)])
        out: list[np.ndarray] = []
        b = self.block
        while self._pending.size >= b:
            block, self._pending = self._pending[:b], self._pending[b:]
            out.append(self._block(block, self._pending_pos))
            self._pending_pos += b
        return np.concatenate(out) if out else np.zeros(0, dtype=np.float32)

    def _find_delay(self, block: np.ndarray, pos: int) -> None:
        if not self._search_mic:
            self._search_start = pos
        self._search_mic.append(block)
        mic = np.concatenate(self._search_mic)
        if mic.size < SAMPLE_RATE:  # one second of echo to correlate
            return
        ref = self.reference.get(self._search_start - self.max_delay, mic.size + self.max_delay)
        if np.sqrt(np.mean(ref**2)) > 1e-4 and np.sqrt(np.mean(mic**2)) > 1e-4:
            lag, sharpness = estimate_delay(mic, ref, self.max_delay)
            if sharpness > 8.0:
                self.delay = lag
        self._search_mic = []

    def _block(self, mic: np.ndarray, pos: int) -> np.ndarray:
        b = self.block
        if not self.reference.active(pos - self.max_delay - b, self.max_delay + 2 * b):
            return mic  # nothing playing: leave the microphone untouched
        if self.delay is None:
            self._find_delay(mic, pos)
            if self.delay is None:
                return mic
        x = self.reference.get(pos - self.delay - b, 2 * b).astype(np.float64)
        spectrum = np.fft.rfft(x)
        y = np.fft.irfft(spectrum * self._weights, 2 * b)[b:]
        error = mic.astype(np.float64) - y
        mic_energy = float(np.dot(mic, mic)) + 1e-9
        error_energy = float(np.dot(error, error)) + 1e-9
        if error_energy > 2.0 * mic_energy:
            # Diverged (e.g. the echo path changed): start the filter over.
            self._weights[:] = 0
            return mic
        self._power = 0.9 * self._power + 0.1 * np.abs(spectrum) ** 2
        grad_spec = np.fft.rfft(np.concatenate([np.zeros(b), error]))
        update = np.conj(spectrum) * grad_spec / (self._power + 1e-6)
        constrained = np.fft.irfft(update, 2 * b)
        constrained[b:] = 0
        self._weights += self.step * np.fft.rfft(constrained)
        self.erle_db = 0.9 * self.erle_db + 0.1 * 10 * np.log10(mic_energy / error_energy)
        return error.astype(np.float32)
