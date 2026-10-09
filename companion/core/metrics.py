"""Latency instrumentation (v0.5): durations only, never audio, images or text.

Stage names (all in seconds)::

    mic_to_vad            audio captured on the device -> speech start detected
    vad_to_stt            end of speech -> final transcript
    stt_to_first_token    transcript (or text message) -> first agent token
    first_token_to_audio  first agent token -> first TTS audio sent
    speech_to_audio       end of speech -> first TTS audio sent (what the user waits)
    network_rtt           server -> device -> server round trip
    barge_in_to_stop      user cut in -> device confirmed playback stopped
    frame_to_observation  frame captured on the device -> visual observation
"""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any

STAGES = (
    "mic_to_vad",
    "vad_to_stt",
    "stt_to_first_token",
    "first_token_to_audio",
    "speech_to_audio",
    "network_rtt",
    "barge_in_to_stop",
    "frame_to_observation",
)


def _percentile(sorted_values: list[float], q: float) -> float:
    if not sorted_values:
        return 0.0
    k = (len(sorted_values) - 1) * q
    lo, hi = int(k), min(int(k) + 1, len(sorted_values) - 1)
    return sorted_values[lo] + (sorted_values[hi] - sorted_values[lo]) * (k - lo)


@dataclass
class Series:
    window: int
    values: deque[float] = field(init=False)
    count: int = 0

    def __post_init__(self) -> None:
        self.values = deque(maxlen=self.window)

    def add(self, value: float) -> None:
        self.values.append(value)
        self.count += 1

    def summary(self) -> dict[str, Any]:
        ordered = sorted(self.values)
        return {
            "count": self.count,
            "last": round(self.values[-1], 3) if self.values else None,
            "p50": round(_percentile(ordered, 0.5), 3) if ordered else None,
            "p95": round(_percentile(ordered, 0.95), 3) if ordered else None,
            "max": round(ordered[-1], 3) if ordered else None,
        }


class Metrics:
    """Rolling latency series per stage, plus simple counters."""

    def __init__(self, *, enabled: bool = True, window: int = 200) -> None:
        self.enabled = enabled
        self.window = window
        self._series: dict[str, Series] = {}
        self.counters: dict[str, int] = {}

    def record(self, stage: str, seconds: float | None) -> None:
        if not self.enabled or seconds is None or seconds < 0:
            return
        series = self._series.get(stage)
        if series is None:
            series = self._series[stage] = Series(self.window)
        series.add(seconds)

    def count(self, name: str, n: int = 1) -> None:
        if self.enabled:
            self.counters[name] = self.counters.get(name, 0) + n

    def summary(self) -> dict[str, Any]:
        stages = {name: s.summary() for name, s in sorted(self._series.items())}
        slowest = max(
            (n for n in stages if n in STAGES and n != "speech_to_audio" and stages[n]["p50"]),
            key=lambda n: stages[n]["p50"],
            default=None,
        )
        return {"stages": stages, "counters": dict(self.counters), "bottleneck": slowest}


@dataclass
class TurnTimings:
    """Monotonic timestamps of one turn's milestones (seconds, time.monotonic)."""

    speech_end: float | None = None
    transcript: float | None = None
    first_token: float | None = None
    first_audio: float | None = None

    def stages(self) -> dict[str, float]:
        out: dict[str, float] = {}

        def gap(name: str, a: float | None, b: float | None) -> None:
            if a is not None and b is not None and b >= a:
                out[name] = round(b - a, 3)

        gap("vad_to_stt", self.speech_end, self.transcript)
        gap("stt_to_first_token", self.transcript, self.first_token)
        gap("first_token_to_audio", self.first_token, self.first_audio)
        gap("speech_to_audio", self.speech_end, self.first_audio)
        return out

    @staticmethod
    def now() -> float:
        return time.monotonic()
