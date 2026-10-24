"""Who said an utterance: voice, the faces in view and their lips, fused by plain rules.

    voice and lips agree                      -> that person          (high)
    voice confident (>= 1 s, clear margin)    -> the voice's person   (high; on_screen if seen)
    lips clearly moving on one face           -> that face's person   (medium; stranger if unknown)
    only one known person around, voice not
      against them                            -> that person          (medium)
    a stranger's voice, nobody visibly talking,
      no unknown face in view                 -> media (TV, radio)    (low; never a turn)
    otherwise                                 -> unknown              (low)

Voices shorter than a second are not trusted on their own (measured: about 95 %
right from one second up, unreliable below). The thresholds are people.voice_*.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from companion.perception.registry import ROLE_TEXT, Match
from companion.perception.tracker import Track

MIN_VOICE_S = 1.0
# Below this similarity to the one person around, a voice of a second or more is not theirs.
AGAINST = 0.2


@dataclass(frozen=True, slots=True)
class Speaker:
    person_id: str | None
    name: str | None
    role: str  # user | acquaintance | stranger | media | unknown
    confidence: str  # high | medium | low
    on_screen: bool
    how: str  # voice+lips, voice, lips, alone, media, none, push_to_talk
    track_id: str | None = None
    voice_score: float | None = None

    @property
    def known(self) -> bool:
        return self.person_id is not None

    def to_payload(self) -> dict[str, Any]:
        return {
            "person_id": self.person_id,
            "name": self.name,
            "role": self.role,
            "confidence": self.confidence,
            "on_screen": self.on_screen,
            "how": self.how,
        }

    def describe(self) -> str:
        """For the agent: "Yukari, the user", "a stranger", "the TV or another device"."""
        if self.name:
            return f"{self.name}, {ROLE_TEXT.get(self.role, self.role)}"
        if self.role == "stranger":
            return "a stranger"
        if self.role == "media":
            return "the TV or another device"
        return "someone not recognised"

    def label(self, text: str) -> str:
        """The line as the agent sees it: 「...」 (Yukari, the user)."""
        return f"「{text}」 ({self.describe()})"


def attribute(
    *,
    duration_s: float,
    voice: Match | None,
    voice_to: dict[str, float] | None,
    visible: list[Track],
    talking: Track | None,
    around: dict[str, tuple[str, str]],
    voice_margin: float,
) -> Speaker:
    """Decide who spoke.

    ``voice``: the best match of the utterance's voice (None without a voice model);
    ``voice_to``: its similarity to each person with voice samples; ``visible``: the
    person tracks seen during the utterance; ``talking``: the one whose lips clearly
    moved (needs >= 15 frames a second); ``around``: known people present now
    (id -> (name, role)).
    """
    long_enough = duration_s >= MIN_VOICE_S
    score = voice.score if voice is not None else None
    heard = voice if voice is not None and long_enough and voice.known else None
    seen = {t.person_id: t for t in visible if t.person_id}

    # 1. Voice and lips agree.
    if heard is not None and talking is not None and talking.person_id == heard.person_id:
        return Speaker(
            heard.person_id, heard.name, heard.role, "high", True, "voice+lips", talking.id, score
        )
    # 2. The voice alone, when it is clear.
    if heard is not None and heard.margin >= voice_margin:
        track = seen.get(heard.person_id)
        return Speaker(
            heard.person_id,
            heard.name,
            heard.role,
            "high",
            track is not None,
            "voice",
            track.id if track else None,
            score,
        )
    # 3. Lips clearly moving on one face.
    if talking is not None:
        if talking.person_id:
            return Speaker(
                talking.person_id,
                talking.name,
                talking.role,
                "medium",
                True,
                "lips",
                talking.id,
                score,
            )
        return Speaker(None, None, "stranger", "medium", True, "lips", talking.id, score)
    # 4. Only one known person around, and the voice does not say otherwise.
    people = dict(around)
    for pid, track in seen.items():
        if track.name:
            people.setdefault(pid, (track.name, track.role))
    strangers_in_view = any(t.person_id is None for t in visible)
    if len(people) == 1 and not strangers_in_view:
        ((pid, (name, role)),) = people.items()
        against = (
            long_enough and voice_to is not None and pid in voice_to and voice_to[pid] < AGAINST
        )
        other = heard is not None and heard.person_id != pid
        if not against and not other:
            return Speaker(pid, name, role, "medium", pid in seen, "alone", None, score)
    # 5. A voice nobody knows, and nobody visibly talking: a TV or radio.
    if long_enough and voice is not None and not voice.known and not strangers_in_view:
        return Speaker(None, None, "media", "low", False, "media", None, score)
    if heard is not None:  # known voice, but too close to someone else's to be sure
        return Speaker(
            heard.person_id,
            heard.name,
            heard.role,
            "low",
            heard.person_id in seen,
            "voice",
            None,
            score,
        )
    if strangers_in_view and voice is not None and long_enough and not voice.known:
        return Speaker(None, None, "stranger", "low", True, "voice+face", None, score)
    return Speaker(None, None, "unknown", "low", False, "none", None, score)
