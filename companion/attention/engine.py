"""Deterministic attention policy: what to do with each event.

    event -> salience -> dedup -> session state -> quiet/cooldown/budget -> decision

The decision is one of IGNORE, REMEMBER, CONTEXT_ONLY or SPEAK, and comes
with the reasons that led to it. Rate limits, quiet hours and recursion guards
are plain rules here; a model is only consulted after SPEAK (the agent may
still decide to stay silent).
"""

from __future__ import annotations

from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, time, timedelta, tzinfo
from enum import StrEnum
from typing import Any
from zoneinfo import ZoneInfo

from companion.core.config import AttentionConfig, QuietHoursConfig

Clock = Callable[[], datetime]


def utc_now() -> datetime:
    return datetime.now(UTC)


class Decision(StrEnum):
    IGNORE = "ignore"
    REMEMBER = "remember"
    CONTEXT_ONLY = "context_only"
    SPEAK = "speak"


@dataclass(slots=True)
class AttentionEvent:
    kind: str  # e.g. "scene_change", "activity_resumed", or a client label
    description: str
    salience: float  # 0..1
    source: str  # "camera", "client", ...
    at: datetime
    data: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class DecisionRecord:
    event: AttentionEvent
    decision: Decision
    reasons: list[str]

    def to_payload(self) -> dict[str, Any]:
        return {
            "kind": self.event.kind,
            "description": self.event.description,
            "salience": round(self.event.salience, 2),
            "source": self.event.source,
            "decision": self.decision.value,
            "reasons": self.reasons,
        }


@dataclass
class AttentionState:
    last_user_turn_at: datetime | None = None
    last_agent_speech_at: datetime | None = None
    proactive_turns: deque[datetime] = field(default_factory=deque)
    quiet_mode: bool = False  # manual do-not-disturb
    last_event_at: dict[str, datetime] = field(default_factory=dict)


def resolve_zone(name: str | None) -> tzinfo:
    """The configured zone, or the host's local zone."""
    if name:
        return ZoneInfo(name)
    local = datetime.now().astimezone().tzinfo
    assert local is not None
    return local


def in_quiet_hours(cfg: QuietHoursConfig, now: datetime) -> bool:
    if not cfg.enabled:
        return False
    local = now.astimezone(resolve_zone(cfg.timezone)).time()
    start, end = time.fromisoformat(cfg.start), time.fromisoformat(cfg.end)
    if start == end:
        return False
    if start < end:
        return start <= local < end
    return local >= start or local < end  # wraps past midnight


class AttentionEngine:
    def __init__(self, config: AttentionConfig, clock: Clock = utc_now) -> None:
        self.config = config
        self.clock = clock
        self.state = AttentionState()

    # --- activity notes -----------------------------------------------------

    def note_user_turn(self) -> None:
        self.state.last_user_turn_at = self.clock()

    def note_agent_speech(self) -> None:
        self.state.last_agent_speech_at = self.clock()

    def set_quiet(self, quiet: bool) -> None:
        self.state.quiet_mode = quiet

    def record_proactive_turn(self) -> None:
        self.state.proactive_turns.append(self.clock())

    # --- policy --------------------------------------------------------------

    def _recent_proactive(self, now: datetime) -> int:
        turns = self.state.proactive_turns
        while turns and now - turns[0] > timedelta(hours=1):
            turns.popleft()
        return len(turns)

    def speech_blockers(self, now: datetime) -> list[str]:
        """Why proactive speech is not allowed right now (empty = allowed)."""
        cfg, st = self.config, self.state
        blockers: list[str] = []
        if not cfg.proactive:
            blockers.append("proactive speech is disabled")
        if st.quiet_mode:
            blockers.append("quiet mode is on")
        if in_quiet_hours(cfg.quiet_hours, now):
            blockers.append(f"quiet hours {cfg.quiet_hours.start}-{cfg.quiet_hours.end}")
        last_activity = max(
            (t for t in (st.last_user_turn_at, st.last_agent_speech_at) if t), default=None
        )
        if last_activity and (now - last_activity).total_seconds() < cfg.grace_after_activity_s:
            blockers.append("someone spoke recently")
        if st.proactive_turns:
            since = (now - st.proactive_turns[-1]).total_seconds()
            if since < cfg.min_interval_s:
                blockers.append(f"cooldown: {round(cfg.min_interval_s - since)} s left")
        if self._recent_proactive(now) >= cfg.max_per_hour:
            blockers.append(f"hourly budget of {cfg.max_per_hour} used")
        return blockers

    def evaluate(self, event: AttentionEvent, *, busy: bool = False) -> DecisionRecord:
        cfg, st = self.config, self.state
        now = event.at
        reasons = [f"salience {event.salience:.2f}"]

        if event.salience < cfg.remember_threshold:
            return DecisionRecord(event, Decision.IGNORE, [*reasons, "below remember threshold"])

        previous = st.last_event_at.get(event.kind)
        st.last_event_at[event.kind] = now
        if previous and (now - previous).total_seconds() < cfg.event_dedup_s:
            return DecisionRecord(event, Decision.IGNORE, [*reasons, "repeat of a recent event"])

        if event.salience < cfg.context_threshold:
            return DecisionRecord(event, Decision.REMEMBER, reasons)

        if event.salience < cfg.speak_threshold:
            return DecisionRecord(event, Decision.CONTEXT_ONLY, [*reasons, "below speak threshold"])

        blockers = self.speech_blockers(now)
        if busy:
            blockers.insert(0, "a conversation turn is in progress")
        if blockers:
            return DecisionRecord(event, Decision.CONTEXT_ONLY, [*reasons, *blockers])
        return DecisionRecord(event, Decision.SPEAK, [*reasons, "all guards passed"])


class SceneTracker:
    """Turn a stream of frame hashes into scene-change events (camera signal)."""

    def __init__(self, config: AttentionConfig) -> None:
        self.config = config
        self._baseline: int | None = None
        self._last_change_at: datetime | None = None

    def observe(self, frame_hash: int, at: datetime, device_id: str) -> AttentionEvent | None:
        if self._baseline is None:
            self._baseline = frame_hash
            self._last_change_at = at
            return None
        distance = (frame_hash ^ self._baseline).bit_count()
        if distance < self.config.scene_change_threshold:
            return None
        self._baseline = frame_hash
        quiet_for = (at - self._last_change_at).total_seconds() if self._last_change_at else 0
        self._last_change_at = at
        if quiet_for >= self.config.absence_s:
            minutes = round(quiet_for / 60)
            return AttentionEvent(
                kind="activity_resumed",
                description=(
                    f"The camera view changed a lot after staying still for about {minutes} "
                    "minutes; someone may have come back or started doing something."
                ),
                salience=0.85,
                source="camera",
                at=at,
                data={"distance": distance, "quiet_for_s": round(quiet_for), "device": device_id},
            )
        # Bigger changes are more salient: threshold -> 0.45, 2x threshold or more -> 0.65.
        scale = min(
            1.0,
            (distance - self.config.scene_change_threshold)
            / max(1, self.config.scene_change_threshold),
        )
        return AttentionEvent(
            kind="scene_change",
            description="The camera view changed noticeably.",
            salience=0.45 + 0.2 * scale,
            source="camera",
            at=at,
            data={"distance": distance, "device": device_id},
        )
