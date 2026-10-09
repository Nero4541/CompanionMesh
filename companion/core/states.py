"""The realtime conversation state machine (v0.5).

Every state change goes through :meth:`StateMachine.transition`, which only
allows the moves listed in ``TRANSITIONS``. Each state may also have a
deadline; the session's watchdog moves a state that outlives it to
RECOVERING, so a lost packet or a vanished device never leaves the
conversation stuck in SPEAKING or TRANSCRIBING.

    IDLE / LISTENING   waiting for the user (LISTENING: a microphone is open)
    TRANSCRIBING       speech-to-text of a finished utterance
    THINKING           the agent is producing a reply
    SPEAKING           reply audio is being played on a speaker device
    INTERRUPTING       the user cut in (barge-in) or a turn was cancelled;
                       playback is being stopped
    RECOVERING         a deadline passed or a device vanished; cleaning up
"""

from __future__ import annotations

import logging
import time
from collections import deque
from dataclasses import dataclass
from enum import StrEnum

log = logging.getLogger(__name__)


class State(StrEnum):
    IDLE = "idle"
    LISTENING = "listening"
    TRANSCRIBING = "transcribing"
    THINKING = "thinking"
    SPEAKING = "speaking"
    INTERRUPTING = "interrupting"
    RECOVERING = "recovering"


_REST = frozenset({State.IDLE, State.LISTENING})

TRANSITIONS: dict[State, frozenset[State]] = {
    State.IDLE: frozenset({State.LISTENING, State.THINKING, State.RECOVERING}),
    State.LISTENING: frozenset(
        {State.IDLE, State.TRANSCRIBING, State.THINKING, State.INTERRUPTING, State.RECOVERING}
    ),
    State.TRANSCRIBING: _REST | {State.THINKING, State.INTERRUPTING, State.RECOVERING},
    State.THINKING: _REST | {State.SPEAKING, State.INTERRUPTING, State.RECOVERING},
    State.SPEAKING: _REST | {State.INTERRUPTING, State.RECOVERING},
    State.INTERRUPTING: _REST | {State.TRANSCRIBING, State.THINKING, State.RECOVERING},
    State.RECOVERING: _REST,
}


class InvalidTransition(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class Change:
    previous: State
    state: State
    reason: str
    at: float  # time.monotonic()


class StateMachine:
    """Holds the current state, validates moves and remembers recent ones."""

    def __init__(self, *, strict: bool = False) -> None:
        self.state = State.IDLE
        self.entered_at = time.monotonic()
        self.deadline: float | None = None  # monotonic time the state must end by
        self.history: deque[Change] = deque(maxlen=50)
        # strict: raise on an invalid move (tests); otherwise log and allow it,
        # so a bug degrades into a logged warning instead of a dead session.
        self.strict = strict

    @staticmethod
    def allowed(previous: State, state: State) -> bool:
        return state in TRANSITIONS[previous]

    def transition(
        self, state: State, reason: str = "", *, deadline_s: float | None = None
    ) -> bool:
        """Move to ``state``; returns False when already there."""
        previous = self.state
        if state == previous:
            if deadline_s is not None:
                self.deadline = time.monotonic() + deadline_s
            return False
        if not self.allowed(previous, state):
            message = f"invalid state transition {previous} -> {state} ({reason or 'no reason'})"
            if self.strict:
                raise InvalidTransition(message)
            log.warning(message)
        now = time.monotonic()
        self.state = state
        self.entered_at = now
        self.deadline = now + deadline_s if deadline_s is not None else None
        self.history.append(Change(previous, state, reason, now))
        return True

    def overdue(self, now: float | None = None) -> bool:
        return self.deadline is not None and (now or time.monotonic()) > self.deadline

    @property
    def at_rest(self) -> bool:
        return self.state in _REST
