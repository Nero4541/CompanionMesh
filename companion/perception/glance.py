"""Glances: a short, structured look at a camera picture by the vision model, often.

Every ``perception.glance_s`` seconds (1 s with the built-in vision model, 10 s
with another), the newest picture of a camera goes to the vision model with the
people in it marked (a box and a letter per face the tracker follows, named
where known) and one question with a small JSON answer, one key per letter:

    {"A": {"activity": "gaming", "posture": "sitting", "looking_at": "screen"},
     "scene": "living room, TV on", "change": "sat down on the sofa"}

About 30 tokens: with Mage-VL in FP8 on an RTX 4070 Ti one glance takes about
0.9 s (pretty-printed JSON with more fields took twice as long). A glance is
skipped, never queued, while the previous one runs, and pictures that look the
same as the last glanced one are not asked about again. Glances keep the
perception state (who is doing what, since when) and feed the timeline.
"""

from __future__ import annotations

import io
from dataclasses import dataclass, field
from typing import Any

from companion.attention.decisions import first_json_object
from companion.perception.timeline import ACTIVITIES, POSTURES, normalise_activity
from companion.perception.tracker import Track

LETTERS = "ABCDEFGHJKLMNPQRSTUVWXYZ"
COLOURS = {"user": "#2f6fec", "acquaintance": "#2c8a4b", "stranger": "#d23c3c"}
QUESTION = (
    "People in this camera picture are marked with a box and a letter: {legend}.\n"
    "Answer with one line of compact JSON and nothing else, a key per marked letter:\n"
    '{{{example}"scene":"<3-5 words>","change":"<3-5 words, or none>"}}'
)
PERSON = (
    '"{letter}":{{"activity":"<one of: {activities}>",'
    '"posture":"<sitting|standing|lying_down>","looking_at":"<1-2 words>"}},'
)
NOBODY = "nobody is marked"
EMPTY = ("", "none", "nothing", "no change", "-", "n/a", "unknown")


@dataclass
class Mark:
    letter: str
    track: Track
    label: str  # "Yukari (the user)", "a stranger"


@dataclass
class Glimpse:
    """One person in one glance."""

    track_id: str
    person_id: str | None
    name: str | None
    role: str
    activity: str
    posture: str = "unknown"
    looking_at: str = ""


@dataclass
class GlanceResult:
    camera: str
    at: float
    people: list[Glimpse] = field(default_factory=list)
    scene: str = ""
    change: str = ""
    parsed: bool = True
    seconds: float = 0.0


def label_of(track: Track) -> str:
    if track.name:
        role = "the user" if track.role == "user" else "an acquaintance"
        return f"{track.name} ({role})"
    return "a stranger"


def marks_for(tracks: list[Track]) -> list[Mark]:
    people = [t for t in tracks if t.kind == "person"]
    people.sort(key=lambda t: t.box[0])
    return [Mark(LETTERS[i], t, label_of(t)) for i, t in enumerate(people[: len(LETTERS)])]


def question(marks: list[Mark]) -> str:
    legend = "; ".join(f"[{m.letter}] {m.label}" for m in marks) or NOBODY
    activities = ", ".join(a for a in ACTIVITIES if a != "away")
    example = "".join(PERSON.format(letter=m.letter, activities=activities) for m in marks[:1])
    if len(marks) > 1:
        example += '"' + marks[1].letter + '":{...},'
    return QUESTION.format(legend=legend, example=example)


def marked_picture(jpeg: bytes, marks: list[Mark], max_side: int = 768) -> bytes:
    """The picture with each marked person's box and letter drawn in, at most ``max_side``."""
    from PIL import Image, ImageDraw

    with Image.open(io.BytesIO(jpeg)) as opened:
        image = opened.convert("RGB")
    draw = ImageDraw.Draw(image)
    width = max(2, image.width // 320)
    for mark in marks:
        x0, y0, x1, y1 = mark.track.box
        colour = COLOURS.get(mark.track.role, "#d23c3c")
        draw.rectangle((x0, y0, x1, y1), outline=colour, width=width)
        size = max(14, int((y1 - y0) * 0.35))
        draw.rectangle((x0, max(0, y0 - size - 4), x0 + size, max(size + 4, y0)), fill=colour)
        draw.text(
            (x0 + size * 0.25, max(0, y0 - size - 2)), mark.letter, fill="white", font_size=size
        )
    if max(image.size) > max_side:
        scale = max_side / max(image.size)
        image = image.resize((round(image.width * scale), round(image.height * scale)))
    out = io.BytesIO()
    image.save(out, format="JPEG", quality=85)
    return out.getvalue()


def _text(value: object, limit: int) -> str:
    """A short answer value; placeholders copied from the question count as nothing."""
    text = str(value or "").strip()
    if "<" in text or text.lower() in EMPTY:
        return ""
    return text[:limit]


def parse(answer: str, marks: list[Mark], camera: str, at: float) -> GlanceResult:
    """Read the vision model's answer; unknown letters are ignored, unknown activities are other.

    Takes a key per letter ({"A": {...}}) and also a list ({"people": [{"mark": "A", ...}]}).
    """
    result = GlanceResult(camera, at)
    data = first_json_object(answer or "")
    if data is None:
        result.parsed = False
        return result
    entries: list[tuple[str, Any]] = [(k, v) for k, v in data.items() if isinstance(v, dict)]
    people = data.get("people")
    if isinstance(people, list):
        entries += [(str(e.get("mark") or ""), e) for e in people if isinstance(e, dict)]
    by_letter = {m.letter: m for m in marks}
    for key, entry in entries:
        mark = by_letter.get(key.strip().strip("[]").upper())
        if mark is None:
            continue
        posture = str(entry.get("posture") or "unknown").strip().lower().replace(" ", "_")
        track = mark.track
        result.people.append(
            Glimpse(
                track_id=track.id,
                person_id=track.person_id,
                name=track.name,
                role=track.role,
                activity=normalise_activity(entry.get("activity")),
                posture=posture if posture in POSTURES else "unknown",
                looking_at=_text(entry.get("looking_at"), 80),
            )
        )
    result.scene = _text(data.get("scene"), 160)
    result.change = _text(data.get("change"), 160)
    return result


@dataclass
class Doing:
    """What one person is doing, as the perception state keeps it."""

    name: str | None
    role: str
    activity: str
    since: float
    updated: float
    camera: str
    posture: str = "unknown"
    looking_at: str = ""

    def to_payload(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "role": self.role,
            "activity": self.activity,
            "since": round(self.since, 3),
            "updated": round(self.updated, 3),
            "camera": self.camera,
            "posture": self.posture,
            "looking_at": self.looking_at,
        }


@dataclass
class PerceptionState:
    """Who is doing what since when, and the scene, from the glances."""

    doing: dict[str, Doing] = field(default_factory=dict)  # person id (or track id) -> Doing
    scene: dict[str, str] = field(default_factory=dict)  # camera -> scene
    change: dict[str, tuple[float, str]] = field(default_factory=dict)  # camera -> last change
    updated: float | None = None
    glances: int = 0
    unparsed: int = 0
    last_seconds: float | None = None
    description: dict[str, tuple[float, str]] = field(default_factory=dict)  # camera -> look

    def take(self, result: GlanceResult) -> None:
        self.glances += 1
        self.last_seconds = result.seconds
        if not result.parsed:
            self.unparsed += 1
            return
        self.updated = result.at
        if result.scene:
            self.scene[result.camera] = result.scene
        if result.change:
            self.change[result.camera] = (result.at, result.change)
        for g in result.people:
            key = g.person_id or g.track_id
            current = self.doing.get(key)
            since = current.since if current and current.activity == g.activity else result.at
            self.doing[key] = Doing(
                g.name,
                g.role,
                g.activity,
                since,
                result.at,
                result.camera,
                g.posture,
                g.looking_at,
            )

    def forget_older(self, before: float) -> None:
        for key in [k for k, d in self.doing.items() if d.updated < before]:
            del self.doing[key]

    def to_payload(self) -> dict[str, Any]:
        return {
            "updated": self.updated,
            "glances": self.glances,
            "unparsed": self.unparsed,
            "last_seconds": None if self.last_seconds is None else round(self.last_seconds, 3),
            "doing": {k: d.to_payload() for k, d in self.doing.items()},
            "scene": dict(self.scene),
            "change": {c: {"at": t, "text": x} for c, (t, x) in self.change.items()},
            "description": {c: {"at": t, "text": x} for c, (t, x) in self.description.items()},
        }
