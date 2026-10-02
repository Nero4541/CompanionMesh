"""Working state (session transcripts) and lightweight memory.

Two concepts only (see v0.1 spec):

* Working state: per-session transcript.
* Persistent agent memory: owned by the agent backend.

Which store is used depends on who owns memory:

* External agents (Hermes, OpenClaw) keep transcripts and memory on their own
  host, so the companion uses :class:`EphemeralStore` and writes nothing to
  disk; the agent is the single source of truth.
* The ``direct`` backend has no framework behind it, so :class:`SQLiteStore`
  persists transcripts and provides ``recall``, a small full-text lookup over
  past messages (deliberately not a vector memory system).
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import time
import uuid
from collections import OrderedDict, deque
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, Protocol

from companion.vision.observation import VisualObservation

Role = Literal["user", "assistant"]

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id          TEXT PRIMARY KEY,
    device_id   TEXT,
    created_at  REAL NOT NULL,
    updated_at  REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS messages (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id  TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    turn_id     TEXT,
    role        TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
    content     TEXT NOT NULL,
    created_at  REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS messages_session ON messages(session_id, id);
CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(
    content, content='messages', content_rowid='id', tokenize='trigram'
);
CREATE TRIGGER IF NOT EXISTS messages_ai AFTER INSERT ON messages BEGIN
    INSERT INTO messages_fts(rowid, content) VALUES (new.id, new.content);
END;
CREATE TRIGGER IF NOT EXISTS messages_ad AFTER DELETE ON messages BEGIN
    INSERT INTO messages_fts(messages_fts, rowid, content) VALUES ('delete', old.id, old.content);
END;
CREATE TABLE IF NOT EXISTS observations (
    rowid       INTEGER PRIMARY KEY AUTOINCREMENT,
    id          TEXT NOT NULL UNIQUE,
    session_id  TEXT,
    device_id   TEXT NOT NULL,
    timestamp   REAL NOT NULL,
    description TEXT NOT NULL,
    tags        TEXT NOT NULL,
    confidence  REAL,
    source      TEXT NOT NULL,
    source_event_id TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS observations_session ON observations(session_id, timestamp);
CREATE VIRTUAL TABLE IF NOT EXISTS observations_fts USING fts5(
    description, content='observations', content_rowid='rowid', tokenize='trigram'
);
CREATE TRIGGER IF NOT EXISTS observations_ai AFTER INSERT ON observations BEGIN
    INSERT INTO observations_fts(rowid, description) VALUES (new.rowid, new.description);
END;
CREATE TRIGGER IF NOT EXISTS observations_ad AFTER DELETE ON observations BEGIN
    INSERT INTO observations_fts(observations_fts, rowid, description)
    VALUES ('delete', old.rowid, old.description);
END;
"""


@dataclass(slots=True)
class StoredMessage:
    role: Role
    content: str
    session_id: str
    turn_id: str | None
    created_at: float


class MemoryStore(Protocol):
    async def recall(self, query: str, *, limit: int = 10) -> list[StoredMessage]: ...

    async def remember(self, item: StoredMessage) -> None: ...

    async def recall_observations(
        self, query: str, *, limit: int = 5
    ) -> list[VisualObservation]: ...


def fts_match(query: str) -> str | None:
    """Build a trigram FTS5 query; ``None`` if there is nothing to match."""
    # The trigram tokenizer matches substrings of >= 3 characters. Text
    # without word boundaries (Japanese) is broken into overlapping trigrams.
    terms: list[str] = []
    for token in query.replace('"', " ").split():
        if token.isascii():
            if len(token) >= 3:
                terms.append(token)
        else:
            terms += [token[i : i + 3] for i in range(max(len(token) - 2, 0))]
    terms = list(dict.fromkeys(terms))[:32]
    return " OR ".join(f'"{t}"' for t in terms) if terms else None


class TranscriptStore(Protocol):
    persistent: bool

    def open(self) -> None: ...

    def close(self) -> None: ...

    async def ensure_session(
        self, session_id: str | None, device_id: str | None
    ) -> tuple[str, bool]: ...

    async def add_message(
        self, session_id: str, role: Role, content: str, *, turn_id: str | None = None
    ) -> StoredMessage: ...

    async def history(self, session_id: str, *, limit: int = 50) -> list[StoredMessage]: ...

    async def forget(self, session_id: str) -> None: ...

    async def session_count(self) -> int: ...

    # Episodic visual memory (text only; images are never stored here).
    async def add_observation(self, obs: VisualObservation) -> None: ...

    async def observations(
        self, session_id: str, *, limit: int = 20
    ) -> list[VisualObservation]: ...

    async def purge_observations(self, older_than_days: float) -> int: ...


class EphemeralStore:
    """In-process transcripts only; nothing is written to disk.

    A session's transcript is dropped when its connection closes (``forget``),
    and the number of sessions/messages held is bounded.
    """

    persistent = False

    def __init__(self, *, max_sessions: int = 64, max_messages: int = 200) -> None:
        self._sessions: OrderedDict[str, list[StoredMessage]] = OrderedDict()
        self._observations: dict[str, deque[VisualObservation]] = {}
        self._max_sessions = max_sessions
        self._max_messages = max_messages

    def open(self) -> None:
        pass

    def close(self) -> None:
        self._sessions.clear()
        self._observations.clear()

    async def ensure_session(
        self, session_id: str | None, device_id: str | None
    ) -> tuple[str, bool]:
        sid = session_id or uuid.uuid4().hex
        resumed = sid in self._sessions
        self._sessions.setdefault(sid, [])
        self._sessions.move_to_end(sid)
        while len(self._sessions) > self._max_sessions:
            dropped, _ = self._sessions.popitem(last=False)
            self._observations.pop(dropped, None)
        return sid, resumed

    async def add_message(
        self, session_id: str, role: Role, content: str, *, turn_id: str | None = None
    ) -> StoredMessage:
        msg = StoredMessage(role, content, session_id, turn_id, time.time())
        messages = self._sessions.setdefault(session_id, [])
        messages.append(msg)
        del messages[: -self._max_messages]
        return msg

    async def history(self, session_id: str, *, limit: int = 50) -> list[StoredMessage]:
        return list(self._sessions.get(session_id, [])[-limit:]) if limit else []

    async def forget(self, session_id: str) -> None:
        self._sessions.pop(session_id, None)
        self._observations.pop(session_id, None)

    async def session_count(self) -> int:
        return len(self._sessions)

    async def add_observation(self, obs: VisualObservation) -> None:
        if obs.session_id is not None:
            self._observations.setdefault(obs.session_id, deque(maxlen=50)).append(obs)

    async def observations(self, session_id: str, *, limit: int = 20) -> list[VisualObservation]:
        return list(self._observations.get(session_id, ()))[-limit:] if limit else []

    async def purge_observations(self, older_than_days: float) -> int:
        return 0  # nothing outlives its connection


class SQLiteStore:
    """Single-connection store; all access is serialized through a lock."""

    persistent = True

    def __init__(self, path: Path) -> None:
        self.path = path
        self._conn: sqlite3.Connection | None = None
        self._lock = asyncio.Lock()

    def open(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.executescript(SCHEMA)
        self._conn = conn

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    @property
    def conn(self) -> sqlite3.Connection:
        if self._conn is None:
            raise RuntimeError("store is not open")
        return self._conn

    async def _run[T](self, fn: Callable[..., T], *args: object) -> T:
        async with self._lock:
            return await asyncio.to_thread(fn, *args)

    # --- sessions -------------------------------------------------------

    def _ensure_session(self, session_id: str | None, device_id: str | None) -> tuple[str, bool]:
        now = time.time()
        if session_id:
            row = self.conn.execute(
                "SELECT id FROM sessions WHERE id = ?", (session_id,)
            ).fetchone()
            if row:
                self.conn.execute(
                    "UPDATE sessions SET updated_at = ? WHERE id = ?", (now, session_id)
                )
                return session_id, True
        new_id = session_id or uuid.uuid4().hex
        self.conn.execute(
            "INSERT INTO sessions (id, device_id, created_at, updated_at) VALUES (?, ?, ?, ?)",
            (new_id, device_id, now, now),
        )
        return new_id, False

    async def ensure_session(
        self, session_id: str | None, device_id: str | None
    ) -> tuple[str, bool]:
        """Return ``(session_id, resumed)``."""
        return await self._run(self._ensure_session, session_id, device_id)

    async def forget(self, session_id: str) -> None:
        """Persistent transcripts outlive connections; nothing to drop."""

    async def session_count(self) -> int:
        def q() -> int:
            return self.conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]

        return await self._run(q)

    # --- messages -------------------------------------------------------

    def _add(self, m: StoredMessage) -> None:
        self.conn.execute(
            "INSERT INTO messages (session_id, turn_id, role, content, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (m.session_id, m.turn_id, m.role, m.content, m.created_at),
        )
        self.conn.execute(
            "UPDATE sessions SET updated_at = ? WHERE id = ?", (m.created_at, m.session_id)
        )

    async def add_message(
        self, session_id: str, role: Role, content: str, *, turn_id: str | None = None
    ) -> StoredMessage:
        msg = StoredMessage(role, content, session_id, turn_id, time.time())
        await self._run(self._add, msg)
        return msg

    def _history(self, session_id: str, limit: int) -> list[StoredMessage]:
        rows = self.conn.execute(
            "SELECT role, content, session_id, turn_id, created_at FROM messages "
            "WHERE session_id = ? ORDER BY id DESC LIMIT ?",
            (session_id, limit),
        ).fetchall()
        return [StoredMessage(*row) for row in reversed(rows)]

    async def history(self, session_id: str, *, limit: int = 50) -> list[StoredMessage]:
        return await self._run(self._history, session_id, limit)

    # --- MemoryStore ----------------------------------------------------

    def _recall(self, query: str, limit: int) -> list[StoredMessage]:
        match = fts_match(query)
        if match is None:
            return []
        rows = self.conn.execute(
            "SELECT m.role, m.content, m.session_id, m.turn_id, m.created_at "
            "FROM messages_fts f JOIN messages m ON m.id = f.rowid "
            "WHERE messages_fts MATCH ? ORDER BY bm25(messages_fts) LIMIT ?",
            (match, limit),
        ).fetchall()
        return [StoredMessage(*row) for row in rows]

    async def recall(self, query: str, *, limit: int = 10) -> list[StoredMessage]:
        return await self._run(self._recall, query, limit)

    async def remember(self, item: StoredMessage) -> None:
        await self._run(self._add, item)

    # --- episodic visual memory ----------------------------------------

    _OBS_COLUMNS = (
        "id",
        "session_id",
        "device_id",
        "timestamp",
        "description",
        "tags",
        "confidence",
        "source",
        "source_event_id",
    )

    @staticmethod
    def _obs_from_row(row: tuple[object, ...]) -> VisualObservation:
        oid, sid, device, ts, desc, tags, conf, source, src_id = row
        return VisualObservation(
            timestamp=datetime.fromtimestamp(float(ts), UTC),  # type: ignore[arg-type]
            device_id=str(device),
            description=str(desc),
            confidence=conf,  # type: ignore[arg-type]
            tags=json.loads(str(tags)),
            source_event_id=str(src_id),
            source=source,  # type: ignore[arg-type]
            session_id=sid,  # type: ignore[arg-type]
            id=str(oid),
        )

    def _add_observation(self, obs: VisualObservation) -> None:
        self.conn.execute(
            f"INSERT OR IGNORE INTO observations ({', '.join(self._OBS_COLUMNS)}) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                obs.id,
                obs.session_id,
                obs.device_id,
                obs.timestamp.timestamp(),
                obs.description,
                json.dumps(obs.tags, ensure_ascii=False),
                obs.confidence,
                obs.source,
                obs.source_event_id,
            ),
        )

    async def add_observation(self, obs: VisualObservation) -> None:
        await self._run(self._add_observation, obs)

    def _observations(self, session_id: str, limit: int) -> list[VisualObservation]:
        rows = self.conn.execute(
            f"SELECT {', '.join(self._OBS_COLUMNS)} FROM observations WHERE session_id = ? "
            "ORDER BY timestamp DESC LIMIT ?",
            (session_id, limit),
        ).fetchall()
        return [self._obs_from_row(r) for r in reversed(rows)]

    async def observations(self, session_id: str, *, limit: int = 20) -> list[VisualObservation]:
        return await self._run(self._observations, session_id, limit)

    def _recall_observations(self, query: str, limit: int) -> list[VisualObservation]:
        match = fts_match(query)
        if match is None:
            return []
        columns = ", ".join(f"o.{c}" for c in self._OBS_COLUMNS)
        rows = self.conn.execute(
            f"SELECT {columns} FROM observations_fts f JOIN observations o ON o.rowid = f.rowid "
            "WHERE observations_fts MATCH ? ORDER BY bm25(observations_fts) LIMIT ?",
            (match, limit),
        ).fetchall()
        return [self._obs_from_row(r) for r in rows]

    async def recall_observations(self, query: str, *, limit: int = 5) -> list[VisualObservation]:
        return await self._run(self._recall_observations, query, limit)

    def _purge_observations(self, older_than_days: float) -> int:
        cutoff = time.time() - older_than_days * 86400
        return self.conn.execute("DELETE FROM observations WHERE timestamp < ?", (cutoff,)).rowcount

    async def purge_observations(self, older_than_days: float) -> int:
        return await self._run(self._purge_observations, older_than_days)
