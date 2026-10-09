"""Turn a continuous 16 kHz PCM stream into utterances using a frame-level VAD."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field

import numpy as np

from companion.audio.pcm import SAMPLE_RATE
from companion.audio.vad import FRAME_SAMPLES, VAD
from companion.core.config import VADConfig

_FRAME_MS = FRAME_SAMPLES * 1000 // SAMPLE_RATE


@dataclass(slots=True)
class SpeechStart:
    pass


@dataclass(slots=True)
class Utterance:
    audio: np.ndarray
    forced: bool = False

    @property
    def duration_s(self) -> float:
        return self.audio.size / SAMPLE_RATE


SegmenterEvent = SpeechStart | Utterance


@dataclass
class UtteranceSegmenter:
    vad: VAD
    config: VADConfig
    _pending: np.ndarray = field(default_factory=lambda: np.zeros(0, dtype=np.float32))
    _preroll: deque[np.ndarray] = field(init=False)
    _speech: list[np.ndarray] = field(default_factory=list)
    _candidate_frames: int = 0
    _silence_frames: int = 0
    _triggered: bool = False
    # (threshold, min_speech_ms) used instead of the config while set: stricter
    # speech detection while the companion's own voice may reach the microphone.
    strict: tuple[float, int] | None = None

    def __post_init__(self) -> None:
        self._preroll = deque(maxlen=max(1, self.config.speech_pad_ms // _FRAME_MS))

    @property
    def in_speech(self) -> bool:
        return self._triggered

    def reset(self) -> None:
        self.vad.reset()
        self._pending = np.zeros(0, dtype=np.float32)
        self._preroll.clear()
        self._speech = []
        self._candidate_frames = 0
        self._silence_frames = 0
        self._triggered = False

    def feed(self, audio: np.ndarray) -> list[SegmenterEvent]:
        events: list[SegmenterEvent] = []
        buf = np.concatenate([self._pending, audio.astype(np.float32, copy=False)])
        n_frames = buf.size // FRAME_SAMPLES
        for i in range(n_frames):
            frame = buf[i * FRAME_SAMPLES : (i + 1) * FRAME_SAMPLES]
            event = self._process_frame(frame)
            if event is not None:
                events.append(event)
        self._pending = buf[n_frames * FRAME_SAMPLES :].copy()
        return events

    def flush(self) -> Utterance | None:
        """End of stream: return any in-progress utterance."""
        utterance = self._finish(forced=True) if self._triggered else None
        self.reset()
        return utterance

    def _process_frame(self, frame: np.ndarray) -> SegmenterEvent | None:
        cfg = self.config
        threshold, min_speech_ms = self.strict or (cfg.threshold, cfg.min_speech_ms)
        is_speech = self.vad.speech_prob(frame) >= threshold

        if not self._triggered:
            if is_speech:
                self._speech.append(frame)
                self._candidate_frames += 1
                if self._candidate_frames * _FRAME_MS >= min_speech_ms:
                    self._triggered = True
                    self._silence_frames = 0
                    self._speech = [*self._preroll, *self._speech]
                    self._preroll.clear()
                    return SpeechStart()
            else:
                # Candidate speech was too short: treat it as noise.
                for f in self._speech:
                    self._preroll.append(f)
                self._speech = []
                self._candidate_frames = 0
                self._preroll.append(frame)
            return None

        self._speech.append(frame)
        self._silence_frames = 0 if is_speech else self._silence_frames + 1
        if self._silence_frames * _FRAME_MS >= cfg.min_silence_ms:
            return self._finish(forced=False)
        if len(self._speech) * _FRAME_MS >= cfg.max_utterance_s * 1000:
            return self._finish(forced=True)
        return None

    def _finish(self, *, forced: bool) -> Utterance:
        pad_frames = self.config.speech_pad_ms // _FRAME_MS
        trailing = max(0, self._silence_frames - pad_frames)
        frames = self._speech[: len(self._speech) - trailing] if trailing else self._speech
        audio = np.concatenate(frames) if frames else np.zeros(0, dtype=np.float32)
        self._speech = []
        self._candidate_frames = 0
        self._silence_frames = 0
        self._triggered = False
        self.vad.reset()
        return Utterance(audio=audio, forced=forced)
