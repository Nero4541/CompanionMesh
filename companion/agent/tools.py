"""Tools for the agent, served over MCP (/mcp) and as REST twins (POST /v1/tools/<name>).

    who_is_here()                      people present: name, role, since, camera, doing, speaking
    timeline(minutes, person)          what each person has been doing (episodes)
    recent_speech(minutes)             what was said, by whom, and whether it was for the companion
    look(camera)                       a camera's newest picture, described by the vision model
    remember_person(track_id, name, role)   add the stranger in view (needs the user's yes)
    forget_person(name)                delete a person and their samples
    list_people()                      the people known, without any face or voice data
    set_watch(...) / list_watches() / remove_watch(id)   timeline rules of the agent's own

The agent gets names, roles and descriptions; never face or voice embeddings.
"""

from __future__ import annotations

import base64
import re
import time
import uuid
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Any

from companion.core.errors import CompanionError

if TYPE_CHECKING:
    from companion.core.runtime import CompanionRuntime

# Words that mean yes, in the languages the companion speaks (checked in the user's last line).
CONSENT = re.compile(
    r"\b(yes|yeah|yep|ok|okay|sure|go ahead|please do|of course)\b"
    r"|はい|うん|いいよ|いいです|お願い|どうぞ|大丈夫|了解|覚えて|ええ"
    r"|好的|可以|是的|嗯|係|得|冇問題|没问题",
    re.IGNORECASE,
)


class ToolError(Exception):
    """A tool refused or failed; the message goes back to the agent."""


@dataclass
class SpokenLine:
    at: float
    who: str  # "Yukari (the user)", "the companion", "a stranger", ...
    role: str  # user | acquaintance | stranger | media | unknown | companion
    text: str
    route: str = "turn"  # turn | context | log
    typed: bool = False


class SpeechLog:
    """What was said lately, with who said it (in memory only)."""

    def __init__(self) -> None:
        self.lines: deque[SpokenLine] = deque(maxlen=500)

    def add(self, line: SpokenLine, keep_minutes: float) -> None:
        self.lines.append(line)
        cutoff = time.time() - keep_minutes * 60
        while self.lines and self.lines[0].at < cutoff:
            self.lines.popleft()

    def recent(self, minutes: float) -> list[SpokenLine]:
        cutoff = time.time() - minutes * 60
        return [line for line in self.lines if line.at >= cutoff]

    def user_said_yes(self, within_s: float) -> bool:
        """Whether the user's last line (typed, or heard from the user) was a yes, lately."""
        cutoff = time.time() - within_s
        for line in reversed(self.lines):
            if line.role == "companion":
                continue
            if line.role != "user" and not line.typed:
                continue
            return line.at >= cutoff and bool(CONSENT.search(line.text))
        return False


@dataclass
class EnrolRequest:
    """remember_person waiting for a confirmation on the People page (mcp.consent: dashboard)."""

    track_id: str
    name: str
    role: str
    at: float = field(default_factory=time.time)
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:10])

    def to_payload(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "track_id": self.track_id,
            "name": self.name,
            "role": self.role,
            "at": self.at,
        }


@dataclass
class ToolResult:
    data: dict[str, Any]
    image: bytes | None = None  # a JPEG, for look()


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    schema: dict[str, Any]
    run: Callable[[dict[str, Any]], Awaitable[ToolResult]]


def _obj(
    properties: dict[str, Any] | None = None, required: list[str] | None = None
) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": properties or {},
        "required": required or [],
        "additionalProperties": False,
    }


class AgentTools:
    def __init__(self, runtime: CompanionRuntime) -> None:
        self.runtime = runtime
        self.requests: dict[str, EnrolRequest] = {}
        activity = {
            "type": "string",
            "description": "desk_computer, phone, gaming, reading, eating, "
            "cooking, watching_tv, lying_down, sleeping, exercising, talking, away, other",
        }
        self.tools = {
            t.name: t
            for t in (
                Tool(
                    "who_is_here",
                    "Who is around now: name, role (user, acquaintance, stranger), "
                    "since when, on which camera, what they are doing, and who is speaking.",
                    _obj(),
                    self.who_is_here,
                ),
                Tool(
                    "timeline",
                    "What each person has been doing (episodes such as 'gaming "
                    "20:10-21:45'), over the last minutes, optionally for one person (a name or "
                    "'user').",
                    _obj(
                        {
                            "minutes": {"type": "number", "default": 120, "minimum": 1},
                            "person": {"type": ["string", "null"], "default": None},
                        }
                    ),
                    self.timeline,
                ),
                Tool(
                    "recent_speech",
                    "What was said lately, by whom, and whether it was said to "
                    "you (route turn) or overheard (context) or the TV (log).",
                    _obj({"minutes": {"type": "number", "default": 10, "minimum": 1}}),
                    self.recent_speech,
                ),
                Tool(
                    "look",
                    "A fresh look through a camera: its newest picture and a short "
                    "description. camera: a camera id, or empty for the newest picture.",
                    _obj({"camera": {"type": ["string", "null"], "default": None}}),
                    self.look,
                ),
                Tool(
                    "remember_person",
                    "Add the stranger in view (track_id from who_is_here) as "
                    "a known person. Ask the user first and call this only after they said yes; "
                    "the server checks.",
                    _obj(
                        {
                            "track_id": {"type": "string"},
                            "name": {"type": "string"},
                            "role": {
                                "type": "string",
                                "enum": ["acquaintance", "user"],
                                "default": "acquaintance",
                            },
                        },
                        ["track_id", "name"],
                    ),
                    self.remember_person,
                ),
                Tool(
                    "forget_person",
                    "Forget a person: their face and voice samples are deleted at once.",
                    _obj({"name": {"type": "string"}}, ["name"]),
                    self.forget_person,
                ),
                Tool(
                    "list_people",
                    "The people known (names, roles, notes, whether they are here).",
                    _obj(),
                    self.list_people,
                ),
                Tool(
                    "set_watch",
                    "Watch for an activity lasting too long; you get an event "
                    "(long_activity) when it does. when: during (it has lasted min_minutes) or "
                    "after (it just ended after at least min_minutes). after/before: HH:MM.",
                    _obj(
                        {
                            "activity": activity,
                            "min_minutes": {"type": "number", "minimum": 0},
                            "person": {"type": ["string", "null"], "default": None},
                            "when": {
                                "type": "string",
                                "enum": ["during", "after"],
                                "default": "during",
                            },
                            "after": {"type": ["string", "null"], "default": None},
                            "before": {"type": ["string", "null"], "default": None},
                            "posture": {"type": ["string", "null"], "default": None},
                            "note": {"type": "string", "default": ""},
                            "id": {"type": ["string", "null"], "default": None},
                        },
                        ["activity", "min_minutes"],
                    ),
                    self.set_watch,
                ),
                Tool(
                    "list_watches",
                    "The timeline rules: configured ones and your own watches.",
                    _obj(),
                    self.list_watches,
                ),
                Tool(
                    "remove_watch",
                    "Remove one of your watches by its id.",
                    _obj({"id": {"type": "string"}}, ["id"]),
                    self.remove_watch,
                ),
            )
        }

    # -- helpers ---------------------------------------------------------------------

    def _clock(self, t: float) -> str:
        from companion.attention.engine import resolve_zone

        zone = resolve_zone(self.runtime.config.attention.quiet_hours.timezone)
        return datetime.fromtimestamp(t, zone).strftime("%H:%M")

    def describe(self) -> list[dict[str, Any]]:
        return [
            {"name": t.name, "description": t.description, "inputSchema": t.schema}
            for t in self.tools.values()
        ]

    async def call(self, name: str, args: dict[str, Any] | None) -> ToolResult:
        tool = self.tools.get(name)
        if tool is None:
            raise ToolError(f"no tool {name!r}")
        args = dict(args or {})
        unknown = set(args) - set(tool.schema["properties"])
        if unknown:
            raise ToolError(f"{name}: unknown argument(s) {', '.join(sorted(unknown))}")
        missing = [k for k in tool.schema["required"] if args.get(k) in (None, "")]
        if missing:
            raise ToolError(f"{name}: missing {', '.join(missing)}")
        return await tool.run(args)

    # -- the tools ---------------------------------------------------------------------

    async def who_is_here(self, args: dict[str, Any]) -> ToolResult:
        perception = self.runtime.perception
        if not perception.ready:
            return ToolResult(
                {
                    "perception": "off",
                    "people": [],
                    "note": "perception is off: nobody can be recognised",
                }
            )
        from companion.perception.tracker import talking_track

        now = time.time()
        speaking: set[str] = set()
        for tracker in perception.trackers.values():
            seen = tracker.visible_between(now - 1.5, now)
            moving, _ = talking_track(seen, now - 1.5, now, tracker.fps)
            if moving is not None:
                speaking.add(moving.person_id or moving.id)
        doing = perception.state.doing
        people = []
        for p in perception.people_here():
            key = p["person_id"] or p.get("track_id")
            entry: dict[str, Any] = {
                "name": p["name"] or "a stranger",
                "role": p["role"],
                "since": self._clock(p["since"]),
                "camera": p["camera"],
                "speaking": key in speaking,
            }
            if p.get("track_id"):
                entry["track_id"] = p["track_id"]
            if key in doing:
                d = doing[key]
                entry["doing"] = d.activity
                entry["doing_since"] = self._clock(d.since)
                if d.looking_at:
                    entry["looking_at"] = d.looking_at
            people.append(entry)
        return ToolResult(
            {"now": self._clock(now), "people": people, "scene": dict(perception.state.scene)}
        )

    async def timeline(self, args: dict[str, Any]) -> ToolResult:
        if not self.runtime.config.timeline.enabled:
            return ToolResult({"timeline": "off", "episodes": []})
        minutes = max(1.0, float(args.get("minutes") or 120))
        episodes = self.runtime.perception.timeline.episodes(minutes, args.get("person"))
        return ToolResult(
            {
                "episodes": [
                    {
                        "name": e.name,
                        "role": e.role,
                        "activity": e.activity,
                        "from": self._clock(e.start),
                        "to": self._clock(e.end),
                        "minutes": round(e.minutes),
                        "ongoing": e is self.runtime.perception.timeline.current.get(e.person),
                    }
                    for e in episodes
                ]
            }
        )

    async def recent_speech(self, args: dict[str, Any]) -> ToolResult:
        minutes = max(1.0, float(args.get("minutes") or 10))
        lines = self.runtime.speech_log.recent(minutes)
        return ToolResult(
            {
                "lines": [
                    {
                        "at": self._clock(x.at),
                        "who": x.who,
                        "role": x.role,
                        "text": x.text,
                        "said_to_you": x.route == "turn",
                        "route": x.route,
                    }
                    for x in lines
                ]
            }
        )

    async def look(self, args: dict[str, Any]) -> ToolResult:
        try:
            seen = await self.runtime.perception.look(args.get("camera") or None)
        except CompanionError as exc:
            raise ToolError(str(exc)) from exc
        jpeg = seen.pop("jpeg")
        seen["at"] = self._clock(seen["at"])
        return ToolResult(seen, image=jpeg)

    async def remember_person(self, args: dict[str, Any]) -> ToolResult:
        from companion.perception.registry import RegistryError

        cfg = self.runtime.config.mcp
        perception = self.runtime.perception
        track = perception.find_track(str(args["track_id"]))
        if track is None or not track.samples:
            raise ToolError("that face is not in view any more (see who_is_here)")
        name, role = str(args["name"]).strip(), str(args.get("role") or "acquaintance")
        if cfg.consent == "dashboard":
            request = EnrolRequest(track.id, name, role)
            self.requests[request.id] = request
            return ToolResult(
                {
                    "status": "waiting",
                    "request": request.id,
                    "note": "the user confirms it on the People page",
                }
            )
        if not self.runtime.speech_log.user_said_yes(cfg.consent_window_s):
            raise ToolError(
                "not without consent: ask the user, and call this again right after they said yes"
            )
        try:
            person = perception.registry.add(
                name, role, consent=True, notes="added by the agent after the user agreed"
            )
        except RegistryError as exc:
            raise ToolError(str(exc)) from exc
        import numpy as np

        perception.registry.add_faces(person.id, np.vstack(track.samples))
        return ToolResult({"status": "remembered", "name": person.name, "role": person.role})

    def confirm(self, request_id: str) -> Any:
        """The People page confirmed an agent's remember_person request."""
        import numpy as np

        request = self.requests.pop(request_id, None)
        if request is None:
            raise ToolError("no such request")
        perception = self.runtime.perception
        track = perception.find_track(request.track_id)
        if track is None or not track.samples:
            raise ToolError("that face is not in view any more")
        person = perception.registry.add(
            request.name,
            request.role,
            consent=True,
            notes="added by the agent, confirmed on the People page",
        )
        perception.registry.add_faces(person.id, np.vstack(track.samples))
        return person

    async def forget_person(self, args: dict[str, Any]) -> ToolResult:
        registry = self.runtime.perception.registry
        person = registry.find(str(args["name"]))
        if person is None:
            raise ToolError(f"nobody called {args['name']!r} is known")
        registry.delete(person.id)
        self.runtime.perception.present.pop(person.id, None)
        return ToolResult({"status": "forgotten", "name": person.name})

    async def list_people(self, args: dict[str, Any]) -> ToolResult:
        perception = self.runtime.perception
        return ToolResult(
            {
                "people": [
                    {
                        "name": p.name,
                        "role": p.role,
                        "notes": p.notes,
                        "here": p.id in perception.present,
                        "recognised_by": [
                            k
                            for k, v in (
                                ("face", perception.registry.counts(p.id)["face_samples"]),
                                ("voice", perception.registry.counts(p.id)["voice_samples"]),
                            )
                            if v
                        ],
                    }
                    for p in perception.registry.people()
                ]
            }
        )

    async def set_watch(self, args: dict[str, Any]) -> ToolResult:
        data = {k: v for k, v in args.items() if v not in (None, "")}
        try:
            rule = self.runtime.perception.timeline.add_watch(data)
        except (ValueError, TypeError) as exc:
            raise ToolError(f"not a valid watch: {exc}") from exc
        note = (
            None
            if self.runtime.config.timeline.enabled
            else "the timeline is off: it will not fire until it is on"
        )
        return ToolResult({"watch": rule.to_dict(), **({"note": note} if note else {})})

    async def list_watches(self, args: dict[str, Any]) -> ToolResult:
        line = self.runtime.perception.timeline
        return ToolResult(
            {
                "rules": [r.to_dict() for r in line.rules],
                "watches": [w.to_dict() for w in line.watches],
            }
        )

    async def remove_watch(self, args: dict[str, Any]) -> ToolResult:
        if not self.runtime.perception.timeline.remove_watch(str(args["id"])):
            raise ToolError(f"no watch {args['id']!r} (only your own watches can be removed)")
        return ToolResult({"removed": args["id"]})


def image_b64(result: ToolResult) -> str | None:
    return base64.b64encode(result.image).decode() if result.image else None
