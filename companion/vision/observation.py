"""Normalized visual observations shared by frames, client events and memory."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal

Source = Literal["frame", "event"]


@dataclass(slots=True)
class VisualObservation:
    timestamp: datetime
    device_id: str
    description: str
    confidence: float | None
    tags: list[str]
    source_event_id: str
    source: Source = "frame"
    session_id: str | None = None
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])

    def to_payload(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "timestamp": self.timestamp.isoformat(timespec="milliseconds").replace("+00:00", "Z"),
            "device_id": self.device_id,
            "description": self.description,
            "confidence": self.confidence,
            "tags": self.tags,
            "source": self.source,
            "source_event_id": self.source_event_id,
        }

    def age_s(self, now: datetime | None = None) -> float:
        return ((now or datetime.now(UTC)) - self.timestamp).total_seconds()


def format_for_context(observations: list[VisualObservation], now: datetime | None = None) -> str:
    """Render observations as a compact block for the agent prompt."""
    now = now or datetime.now(UTC)
    lines = []
    for obs in observations:
        age = max(0, round(obs.age_s(now)))
        tags = f" [{', '.join(obs.tags)}]" if obs.tags else ""
        lines.append(f"- {age} s ago ({obs.device_id}): {obs.description}{tags}")
    return "Camera observations (most recent last):\n" + "\n".join(lines)
