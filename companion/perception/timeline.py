"""What each person has been doing, as episodes, and rules for doing something for too long.

Samples come from glances (``{person, activity, posture}`` about once a second
with a local vision model) or, without a vision model, from presence alone
(``present`` / ``away``). Consecutive samples with the same activity merge into
an episode; a different activity has to last ``timeline.min_switch_s`` (two
minutes) before it ends the episode, so a short look at the phone does not cut
an evening of gaming in two.

Rules (``timeline.rules``, and watches the agent adds) turn episodes into
attention events:

- ``when: during`` (default): the episode has lasted ``min_minutes``
  (gaming >= 120 min), optionally only between ``after`` and ``before``
  (phone while lying down after 00:30);
- ``when: after``: an episode of at least ``min_minutes`` has just ended
  (away for four hours, then back).

Each rule fires once per episode. Episodes are kept in SQLite
(``data/timeline.db``) for ``timeline.retention_days``.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, tzinfo
from pathlib import Path
from typing import Any

ACTIVITIES = (
    "desk_computer",
    "phone",
    "gaming",
    "reading",
    "eating",
    "cooking",
    "watching_tv",
    "lying_down",
    "sleeping",
    "exercising",
    "talking",
    "away",
    "other",
)
PRESENT = "present"  # presence-only timelines (no vision model)
POSTURES = ("sitting", "standing", "lying_down", "unknown")


def normalise_activity(value: object) -> str:
    text = str(value or "").strip().lower().replace(" ", "_").replace("-", "_")
    if text in ACTIVITIES or text == PRESENT:
        return text
    for activity in ACTIVITIES:  # "playing a game" -> gaming, "on the phone" -> phone ...
        if activity.split("_")[0] in text:
            return activity
    if "game" in text:
        return "gaming"
    if "tv" in text or "watch" in text:
        return "watching_tv"
    if "computer" in text or "laptop" in text or "typing" in text:
        return "desk_computer"
    return "other"


@dataclass
class Episode:
    person: str  # person id, or a name for samples without one
    name: str
    role: str
    activity: str
    start: float
    end: float
    samples: int = 1
    posture: str = "unknown"
    description: str = ""
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    fired: set[str] = field(default_factory=set)  # rule ids that fired for this episode

    @property
    def minutes(self) -> float:
        return (self.end - self.start) / 60

    def to_payload(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "person_id": self.person,
            "name": self.name,
            "role": self.role,
            "activity": self.activity,
            "posture": self.posture,
            "start": round(self.start, 3),
            "end": round(self.end, 3),
            "minutes": round(self.minutes, 1),
            "samples": self.samples,
            "description": self.description,
        }


@dataclass
class WatchRule:
    id: str
    activity: str
    min_minutes: float
    person: str | None = None  # a name, "user", or None for anyone
    when: str = "during"  # during | after
    after: str | None = None  # HH:MM (local): only from then ...
    before: str | None = None  # ... until then
    posture: str | None = None
    salience: float = 0.7
    note: str = ""  # what the agent should know (from set_watch)
    source: str = "config"  # config | agent

    @classmethod
    def from_dict(cls, data: dict[str, Any], source: str = "config") -> WatchRule:
        rule = cls(
            id=str(data.get("id") or uuid.uuid4().hex[:8]),
            activity=normalise_activity(data.get("activity")),
            min_minutes=float(data.get("min_minutes", 0)),
            person=(str(data["person"]) if data.get("person") else None),
            when=str(data.get("when") or "during"),
            after=data.get("after") or None,
            before=data.get("before") or None,
            posture=data.get("posture") or None,
            salience=float(data.get("salience", 0.7)),
            note=str(data.get("note") or ""),
            source=source,
        )
        if rule.when not in ("during", "after"):
            raise ValueError("when must be during or after")
        return rule

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "activity": self.activity,
            "min_minutes": self.min_minutes,
            "person": self.person,
            "when": self.when,
            "after": self.after,
            "before": self.before,
            "posture": self.posture,
            "salience": self.salience,
            "note": self.note,
            "source": self.source,
        }

    def applies_to(self, episode: Episode) -> bool:
        if self.activity != episode.activity:
            return False
        if self.posture and self.posture != episode.posture:
            return False
        if self.person in (None, "", "anyone"):
            return True
        if self.person == "user":
            return episode.role == "user"
        return self.person.casefold() in (episode.name.casefold(), episode.person.casefold())

    def in_window(self, at: float, zone: tzinfo | None) -> bool:
        if not self.after and not self.before:
            return True
        now = datetime.fromtimestamp(at, zone).strftime("%H:%M")
        start, end = self.after or "00:00", self.before or "24:00"
        if start <= end:
            return start <= now < end
        return now >= start or now < end  # past midnight


@dataclass
class Fired:
    rule: WatchRule
    episode: Episode

    def description(self, zone: tzinfo | None) -> str:
        e = self.episode
        who = f"{e.name} ({'the user' if e.role == 'user' else 'an acquaintance'})"
        since = datetime.fromtimestamp(e.start, zone).strftime("%H:%M")
        hours, minutes = divmod(round(e.minutes), 60)
        span = f"{hours} h {minutes} min" if hours else f"{minutes} min"
        what = e.activity.replace("_", " ")
        if self.rule.when == "after":
            text = f"{who} is back after {what} for {span} (since {since})."
        else:
            text = f"{who} has been {what} since {since} ({span})."
        if self.rule.note:
            text += f" Watch note: {self.rule.note}"
        return text


class Timeline:
    """Episodes per person, built from samples; rules checked on every sample."""

    def __init__(
        self,
        path: Path | None,
        *,
        min_switch_s: float = 120.0,
        retention_days: float = 7.0,
        zone: tzinfo | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.path = path
        self.min_switch_s = min_switch_s
        self.retention_days = retention_days
        self.zone = zone
        self.clock = clock
        self.rules: list[WatchRule] = []
        self.watches: list[WatchRule] = []  # added by the agent
        self.current: dict[str, Episode] = {}  # person -> the open episode
        self._pending: dict[str, Episode] = {}  # person -> a different activity, not yet long
        self.closed: list[Episode] = []  # recent closed episodes (in memory)
        self._lock = threading.Lock()
        self._db: sqlite3.Connection | None = None

    # -- storage ---------------------------------------------------------------------

    def open(self) -> None:
        if self.path is None or self._db is not None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(self.path, check_same_thread=False)
        self._db.execute(
            "CREATE TABLE IF NOT EXISTS episodes (id TEXT PRIMARY KEY, person TEXT, name TEXT, "
            "role TEXT, activity TEXT, posture TEXT, start REAL, end REAL, samples INTEGER, "
            "description TEXT)"
        )
        self._db.execute("CREATE INDEX IF NOT EXISTS episodes_end ON episodes(end)")
        self._db.commit()
        self.purge()
        watches = self.path.with_name("timeline_watches.json")
        try:
            rows = json.loads(watches.read_text(encoding="utf-8"))
            self.watches = [WatchRule.from_dict(r, "agent") for r in rows]
        except (OSError, ValueError, TypeError):
            self.watches = []

    def close(self) -> None:
        with self._lock:
            for episode in self.current.values():
                self._save(episode)
            if self._db is not None:
                self._db.close()
                self._db = None

    def purge(self) -> None:
        if self._db is not None:
            cutoff = self.clock() - self.retention_days * 86400
            with self._lock:
                self._db.execute("DELETE FROM episodes WHERE end < ?", (cutoff,))
                self._db.commit()

    def _save(self, episode: Episode) -> None:
        if self._db is None:
            return
        self._db.execute(
            "INSERT OR REPLACE INTO episodes VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                episode.id,
                episode.person,
                episode.name,
                episode.role,
                episode.activity,
                episode.posture,
                episode.start,
                episode.end,
                episode.samples,
                episode.description,
            ),
        )
        self._db.commit()

    def _save_watches(self) -> None:
        if self.path is None:
            return
        target = self.path.with_name("timeline_watches.json")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps([w.to_dict() for w in self.watches]), encoding="utf-8")

    # -- watches ----------------------------------------------------------------------

    def all_rules(self) -> list[WatchRule]:
        return [*self.rules, *self.watches]

    def add_watch(self, data: dict[str, Any]) -> WatchRule:
        rule = WatchRule.from_dict(data, "agent")
        with self._lock:
            self.watches = [w for w in self.watches if w.id != rule.id] + [rule]
            self._save_watches()
        return rule

    def remove_watch(self, rule_id: str) -> bool:
        with self._lock:
            before = len(self.watches)
            self.watches = [w for w in self.watches if w.id != rule_id]
            self._save_watches()
            return len(self.watches) != before

    # -- samples ------------------------------------------------------------------------

    def add(
        self,
        person: str,
        name: str,
        role: str,
        activity: str,
        at: float,
        *,
        posture: str = "unknown",
        description: str = "",
    ) -> list[Fired]:
        """One observation of ``person`` doing ``activity`` at ``at``; the rules it fires."""
        activity = normalise_activity(activity)
        fired: list[Fired] = []
        with self._lock:
            current = self.current.get(person)
            if current is None:
                self.current[person] = Episode(
                    person, name, role, activity, at, at, posture=posture, description=description
                )
                return self._check(self.current[person], at)
            if activity == current.activity:
                current.end, current.samples = at, current.samples + 1
                if posture != "unknown":
                    current.posture = posture
                if description:
                    current.description = description
                self._pending.pop(person, None)
                return self._check(current, at)
            pending = self._pending.get(person)
            if pending is None or pending.activity != activity:
                self._pending[person] = Episode(
                    person, name, role, activity, at, at, posture=posture, description=description
                )
            else:
                pending.end, pending.samples = at, pending.samples + 1
                if posture != "unknown":
                    pending.posture = posture
            pending = self._pending[person]
            if at - pending.start < self.min_switch_s:
                current.end = max(current.end, at)  # a short change does not split it
                return self._check(current, at)
            # The new activity lasted: the old episode ended when it began.
            current.end = pending.start
            self._close(current)
            fired.extend(self._check_after(current, at))
            self.current[person] = pending
            del self._pending[person]
            fired.extend(self._check(pending, at))
            return fired

    def _close(self, episode: Episode) -> None:
        self.closed.append(episode)
        del self.closed[:-500]
        self._save(episode)

    def _check(self, episode: Episode, at: float) -> list[Fired]:
        out = []
        for rule in self.all_rules():
            if rule.when != "during" or rule.id in episode.fired or not rule.applies_to(episode):
                continue
            if episode.minutes >= rule.min_minutes and rule.in_window(at, self.zone):
                episode.fired.add(rule.id)
                out.append(Fired(rule, episode))
        return out

    def _check_after(self, episode: Episode, at: float) -> list[Fired]:
        out = []
        for rule in self.all_rules():
            if rule.when != "after" or rule.id in episode.fired or not rule.applies_to(episode):
                continue
            if episode.minutes >= rule.min_minutes and rule.in_window(at, self.zone):
                episode.fired.add(rule.id)
                out.append(Fired(rule, episode))
        return out

    def flush(self, at: float) -> None:
        """Save the open episodes (they go on; this keeps the database close to now)."""
        with self._lock:
            for episode in self.current.values():
                self._save(episode)

    # -- reading --------------------------------------------------------------------------

    def episodes(self, minutes: float = 120.0, person: str | None = None) -> list[Episode]:
        """Episodes that reach into the last ``minutes``, oldest first (open ones included)."""
        since = self.clock() - minutes * 60
        found: dict[str, Episode] = {}
        if self._db is not None:
            with self._lock:
                rows = self._db.execute(
                    "SELECT id, person, name, role, activity, posture, start, end, samples, "
                    "description FROM episodes WHERE end >= ? ORDER BY start",
                    (since,),
                ).fetchall()
            for r in rows:
                found[r[0]] = Episode(
                    r[1], r[2], r[3], r[4], r[6], r[7], r[8], r[5], r[9] or "", id=r[0]
                )
        for e in [*self.closed, *self.current.values()]:
            if e.end >= since:
                found[e.id] = e
        out = sorted(found.values(), key=lambda e: e.start)
        if person:
            key = person.casefold()
            out = [
                e
                for e in out
                if key in (e.name.casefold(), e.person.casefold())
                or (key == "user" and e.role == "user")
            ]
        return out

    def context_lines(self, limit: int = 4) -> list[str]:
        """What has been going on, for the agent: the open episode and the last few."""
        lines = []
        for episode in self.episodes(minutes=240)[-limit:]:
            since = datetime.fromtimestamp(episode.start, self.zone).strftime("%H:%M")
            open_ = episode is self.current.get(episode.person)
            span = f"{round(episode.minutes)} min"
            what = episode.activity.replace("_", " ")
            if open_:
                lines.append(f"{episode.name}: {what} since {since} ({span} so far).")
            else:
                until = datetime.fromtimestamp(episode.end, self.zone).strftime("%H:%M")
                lines.append(f"{episode.name}: {what} {since}-{until} ({span}).")
        if not lines:
            return []
        return ["What has been going on: " + " ".join(lines)]
