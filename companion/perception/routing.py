"""Answer only what is meant for the companion: the routing gate for each sentence heard.

    speaker              signals                                          route
    user / acquaintance  its name heard, the companion spoke a moment     turn
                         ago, or the face turned to the companion
    user / acquaintance  none of these, two or more people around          context ("overheard")
    user / acquaintance  none of these, alone                              turn (if
                                                                           alone_means_addressed)
    stranger             -                                                  context, and an event
    unknown / media      -                                                  log only

A turn is answered; context goes with the next turn as "Overheard: ..."; log
only is kept on the dashboard and nothing else. Without a speaker (speakers
off, or attribution failed) every sentence is a turn, as before.
"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass, field
from typing import Any, Literal

from companion.perception.speakers import Speaker

RouteName = Literal["turn", "context", "log"]
FACING_YAW = 0.25  # nose offset in eye distances: about 25 degrees of head turn


@dataclass(frozen=True, slots=True)
class Route:
    route: RouteName
    reasons: list[str] = field(default_factory=list)

    def to_payload(self) -> dict[str, Any]:
        return {"route": self.route, "reasons": self.reasons}


def _fold(text: str) -> str:
    return unicodedata.normalize("NFKC", text).casefold()


def heard_name(text: str, names: list[str]) -> str | None:
    """The first of ``names`` said in ``text`` (case and width ignored)."""
    folded = _fold(text)
    return next((n for n in names if n and _fold(n) in folded), None)


def decide(
    speaker: Speaker | None,
    text: str,
    *,
    wake_names: list[str],
    since_companion_spoke_s: float | None,
    followup_s: float,
    people_around: int,
    facing: bool,
    alone_means_addressed: bool = True,
    answer_acquaintances: bool = True,
    answer_strangers: bool = False,
) -> Route:
    """Where one sentence goes: answered (turn), kept for the next turn (context), or logged."""
    if speaker is None:
        return Route("turn", ["speaker not known: answered as before"])
    role = speaker.role
    if role in ("unknown", "media"):
        why = "the TV or another device" if role == "media" else "nobody recognised said it"
        return Route("log", [why])
    if role == "stranger":
        if answer_strangers:
            return Route("turn", ["a stranger; routing.answer_strangers is on"])
        return Route("context", ["a stranger said it"])
    reasons: list[str] = []
    name = heard_name(text, wake_names)
    if name:
        reasons.append(f"the companion's name ({name}) was said")
    if since_companion_spoke_s is not None and since_companion_spoke_s < followup_s:
        reasons.append(f"the companion spoke {round(since_companion_spoke_s)} s ago")
    if facing:
        reasons.append("the speaker faces the companion")
    if role == "acquaintance" and not answer_acquaintances:
        return Route("context", ["an acquaintance; routing.answer_acquaintances is off", *reasons])
    if reasons:
        return Route("turn", reasons)
    if people_around >= 2:
        return Route("context", [f"{people_around} people around: talk between them"])
    if alone_means_addressed:
        return Route("turn", ["alone in the room: talking to the companion"])
    return Route("context", ["alone, but routing.alone_means_addressed is off"])
