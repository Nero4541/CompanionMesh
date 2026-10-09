"""Cross-device clock offset from ping/pong round trips (NTP-style).

The server sends ``system.ping {ping_id, t_server}``; the device answers
``system.pong {ping_id, t_server, t_device}`` with its own wall clock (ms since
the Unix epoch) at the moment it handled the ping. Assuming the trip is
symmetric, ``offset = t_device - (t_server + rtt / 2)``. The sample with the
smallest round trip among recent ones is the most trustworthy.
"""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass


def now_ms() -> float:
    return time.time() * 1000.0


@dataclass(frozen=True, slots=True)
class ClockSample:
    rtt_ms: float
    offset_ms: float  # device clock minus server clock


class ClockSync:
    def __init__(self, keep: int = 8) -> None:
        self._samples: deque[ClockSample] = deque(maxlen=keep)

    def add(self, t_server_ms: float, t_device_ms: float, t_received_ms: float) -> ClockSample:
        rtt = max(0.0, t_received_ms - t_server_ms)
        sample = ClockSample(rtt, t_device_ms - (t_server_ms + rtt / 2))
        self._samples.append(sample)
        return sample

    @property
    def synced(self) -> bool:
        return bool(self._samples)

    @property
    def best(self) -> ClockSample | None:
        return min(self._samples, key=lambda s: s.rtt_ms, default=None)

    @property
    def offset_ms(self) -> float:
        best = self.best
        return best.offset_ms if best else 0.0

    def to_server_ms(self, t_device_ms: float) -> float:
        """A device timestamp expressed on the server's clock."""
        return t_device_ms - self.offset_ms

    def age_ms(self, t_device_ms: float) -> float | None:
        """How long ago (server clock) a device timestamp was; None if unsynced."""
        if not self.synced:
            return None
        return now_ms() - self.to_server_ms(t_device_ms)
