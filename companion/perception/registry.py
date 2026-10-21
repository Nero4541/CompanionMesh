"""People the companion knows, with their face and voice samples (``data/people``).

    data/people/
      registry.json      [{id, name, role, created, updated, notes, consent}]
      <id>/face.npy      ArcFace embeddings (unit length, at most people.max_face_samples)
      <id>/voice.npy     WeSpeaker embeddings, one per clip
      <id>/voice/*.wav   the clips themselves (16 kHz), to embed again with another model
      strangers.json     recent unknown faces (embedding, first/last seen), dropped after
                         people.stranger_retention_h
      pictures.npy       faces found to be posters or screens

There is exactly one ``user``; any number of ``acquaintance``s. Deleting a
person removes their folder at once. Embeddings never leave this module's
callers on the server: the agent gets names and roles only.

A face matches a person by its highest similarity to their samples; a voice by
its similarity to the mean of theirs. Either must reach the threshold and be
ahead of the next person by the margin (people.*).
"""

from __future__ import annotations

import base64
import json
import os
import re
import shutil
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal

import numpy as np

Role = Literal["user", "acquaintance"]
ROLES: tuple[str, ...] = ("user", "acquaintance")
ROLE_TEXT = {"user": "the user", "acquaintance": "an acquaintance", "stranger": "a stranger"}
PICTURE_SIMILARITY = 0.6  # a face this close to a remembered picture is that picture


class RegistryError(ValueError):
    """A change the registry refuses (bad name, a second user, unknown person)."""


@dataclass
class Person:
    id: str
    name: str
    role: str
    created: float
    updated: float
    notes: str = ""
    consent: bool = False  # the person agreed to be recognised

    def public(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class Match:
    """Who a face or voice is: a person, or nobody (``person_id`` None)."""

    person_id: str | None
    name: str | None
    role: str  # user | acquaintance | stranger
    score: float  # similarity to the best person
    margin: float  # ahead of the second-best person
    closest: str | None = None  # the best person's name, even when not accepted

    @property
    def known(self) -> bool:
        return self.person_id is not None


NOBODY = Match(None, None, "stranger", 0.0, 0.0)


@dataclass
class Stranger:
    id: str
    embedding: np.ndarray
    first_seen: float
    last_seen: float
    camera: str | None = None


def _unit(rows: np.ndarray) -> np.ndarray:
    rows = np.asarray(rows, dtype=np.float32)
    if rows.ndim == 1:
        return rows / (np.linalg.norm(rows) + 1e-12)
    return rows / (np.linalg.norm(rows, axis=1, keepdims=True) + 1e-12)


def spread(rows: np.ndarray, keep: int) -> np.ndarray:
    """At most ``keep`` rows that cover the set: farthest-point sampling from the first."""
    if len(rows) <= keep:
        return rows
    chosen = [0]
    best = rows @ rows[0]
    for _ in range(keep - 1):
        nxt = int(np.argmin(best))
        chosen.append(nxt)
        best = np.maximum(best, rows @ rows[nxt])
    return rows[sorted(chosen)]


def _slug(name: str) -> str:
    base = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:24]
    return f"{base or 'person'}-{uuid.uuid4().hex[:6]}"


def b64(array: np.ndarray) -> str:
    return base64.b64encode(np.ascontiguousarray(array, dtype="<f4").tobytes()).decode()


def from_b64(text: str) -> np.ndarray:
    return np.frombuffer(base64.b64decode(text), dtype="<f4").copy()


@dataclass
class _Index:
    """Stacked samples for matching, rebuilt after every change."""

    people: list[Person] = field(default_factory=list)
    faces: np.ndarray = field(default_factory=lambda: np.zeros((0, 512), np.float32))
    face_owner: np.ndarray = field(default_factory=lambda: np.zeros(0, np.int32))
    voices: np.ndarray = field(default_factory=lambda: np.zeros((0, 256), np.float32))
    voice_owner: list[int] = field(default_factory=list)


class PeopleRegistry:
    def __init__(self, root: Path, *, max_face_samples: int = 40) -> None:
        self.root = root
        self.max_face_samples = max_face_samples
        self._lock = threading.RLock()
        self._people: list[Person] = []
        self._index = _Index()
        self._strangers: list[Stranger] = []
        self._pictures = np.zeros((0, 512), np.float32)
        self._load()

    # -- files -------------------------------------------------------------------

    @property
    def _file(self) -> Path:
        return self.root / "registry.json"

    def _load(self) -> None:
        with self._lock:
            try:
                rows = json.loads(self._file.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                rows = []
            self._people = [
                Person(**{k: r[k] for k in Person.__dataclass_fields__ if k in r})
                for r in rows
                if isinstance(r, dict) and r.get("id") and r.get("role") in ROLES
            ]
            try:
                self._pictures = _unit(np.load(self.root / "pictures.npy")).reshape(-1, 512)
            except (OSError, ValueError):
                self._pictures = np.zeros((0, 512), np.float32)
            self._strangers = self._read_strangers()
            self._rebuild()

    def _save(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        tmp = self._file.with_suffix(".tmp")
        tmp.write_text(
            json.dumps([p.public() for p in self._people], ensure_ascii=False, indent=1),
            encoding="utf-8",
        )
        for attempt in range(20):
            try:
                os.replace(tmp, self._file)
                return
            except PermissionError:  # Windows: a virus scanner still has the file open
                if attempt == 19:
                    raise
                time.sleep(0.05)

    def _folder(self, person_id: str) -> Path:
        if not re.fullmatch(r"[a-z0-9-]{1,64}", person_id):
            raise RegistryError(f"bad person id {person_id!r}")
        return self.root / person_id

    def _samples(self, person_id: str, kind: str) -> np.ndarray:
        try:
            return _unit(np.load(self._folder(person_id) / f"{kind}.npy"))
        except (OSError, ValueError):
            return np.zeros((0, 512 if kind == "face" else 256), np.float32)

    def _rebuild(self) -> None:
        index = _Index(people=list(self._people))
        faces, owners, voices, voice_owner = [], [], [], []
        for i, person in enumerate(self._people):
            f = self._samples(person.id, "face")
            if len(f):
                faces.append(f)
                owners.extend([i] * len(f))
            v = self._samples(person.id, "voice")
            if len(v):
                voices.append(_unit(v.mean(axis=0)))
                voice_owner.append(i)
        if faces:
            index.faces = np.vstack(faces)
            index.face_owner = np.array(owners, np.int32)
        if voices:
            index.voices = np.vstack(voices)
            index.voice_owner = voice_owner
        self._index = index

    # -- people ------------------------------------------------------------------

    def people(self) -> list[Person]:
        with self._lock:
            return list(self._people)

    def get(self, person_id: str) -> Person | None:
        with self._lock:
            return next((p for p in self._people if p.id == person_id), None)

    def find(self, name: str) -> Person | None:
        """By id or by name (case and surrounding spaces ignored)."""
        key = name.strip().casefold()
        with self._lock:
            return next((p for p in self._people if p.id == name or p.name.casefold() == key), None)

    @property
    def user(self) -> Person | None:
        with self._lock:
            return next((p for p in self._people if p.role == "user"), None)

    def counts(self, person_id: str) -> dict[str, int]:
        folder = self._folder(person_id)
        clips = len(list((folder / "voice").glob("*.wav"))) if (folder / "voice").is_dir() else 0
        return {
            "face_samples": len(self._samples(person_id, "face")),
            "voice_samples": len(self._samples(person_id, "voice")),
            "voice_clips": clips,
        }

    def add(self, name: str, role: str, *, notes: str = "", consent: bool = False) -> Person:
        name = name.strip()
        if not name or len(name) > 64:
            raise RegistryError("a name of 1 to 64 characters is needed")
        if role not in ROLES:
            raise RegistryError("role must be user or acquaintance")
        with self._lock:
            if self.find(name) is not None:
                raise RegistryError(f"someone named {name!r} is already known")
            if role == "user" and self.user is not None:
                raise RegistryError("there is already a user; add others as acquaintances")
            now = time.time()
            person = Person(_slug(name), name, role, now, now, notes.strip(), consent)
            self._people.append(person)
            self._save()
            self._rebuild()
            return person

    def update(
        self,
        person_id: str,
        *,
        name: str | None = None,
        role: str | None = None,
        notes: str | None = None,
        consent: bool | None = None,
    ) -> Person:
        with self._lock:
            person = self.get(person_id)
            if person is None:
                raise RegistryError(f"no person {person_id!r}")
            if name is not None:
                name = name.strip()
                other = self.find(name)
                if not name or (other is not None and other.id != person_id):
                    raise RegistryError(f"the name {name!r} is empty or taken")
                person.name = name
            if role is not None:
                if role not in ROLES:
                    raise RegistryError("role must be user or acquaintance")
                current = self.user
                if role == "user" and current is not None and current.id != person_id:
                    raise RegistryError(f"{current.name} is the user already")
                person.role = role
            if notes is not None:
                person.notes = notes.strip()
            if consent is not None:
                person.consent = consent
            person.updated = time.time()
            self._save()
            self._rebuild()
            return person

    def delete(self, person_id: str) -> bool:
        """Forget a person and delete their samples at once."""
        with self._lock:
            person = self.get(person_id)
            if person is None:
                return False
            self._people.remove(person)
            self._save()
            shutil.rmtree(self._folder(person_id), ignore_errors=True)
            self._rebuild()
            return True

    # -- samples -------------------------------------------------------------------

    def add_faces(self, person_id: str, embeddings: np.ndarray) -> int:
        """Add face samples (keeping the spread-out ones); the number kept."""
        rows = _unit(np.asarray(embeddings, np.float32).reshape(-1, 512))
        with self._lock:
            if self.get(person_id) is None:
                raise RegistryError(f"no person {person_id!r}")
            folder = self._folder(person_id)
            folder.mkdir(parents=True, exist_ok=True)
            merged = np.vstack([self._samples(person_id, "face"), rows])
            kept = spread(merged, self.max_face_samples)
            np.save(folder / "face.npy", kept.astype(np.float32))
            self._touch(person_id)
            return len(kept)

    def add_voice(self, person_id: str, embedding: np.ndarray, clip: bytes | None = None) -> int:
        """Add a voice sample (and its clip, a WAV); the number of samples."""
        row = _unit(np.asarray(embedding, np.float32).reshape(1, -1))
        with self._lock:
            if self.get(person_id) is None:
                raise RegistryError(f"no person {person_id!r}")
            folder = self._folder(person_id)
            folder.mkdir(parents=True, exist_ok=True)
            current = self._samples(person_id, "voice")
            if len(current) and current.shape[1] != row.shape[1]:
                current = np.zeros((0, row.shape[1]), np.float32)  # another model: start over
            merged = np.vstack([current, row])
            np.save(folder / "voice.npy", merged.astype(np.float32))
            if clip is not None:
                clips = folder / "voice"
                clips.mkdir(exist_ok=True)
                (clips / f"{len(merged):03d}-{uuid.uuid4().hex[:6]}.wav").write_bytes(clip)
            self._touch(person_id)
            return len(merged)

    def clear(self, person_id: str, kind: Literal["face", "voice"]) -> None:
        with self._lock:
            folder = self._folder(person_id)
            (folder / f"{kind}.npy").unlink(missing_ok=True)
            if kind == "voice":
                shutil.rmtree(folder / "voice", ignore_errors=True)
            self._touch(person_id)

    def _touch(self, person_id: str) -> None:
        person = self.get(person_id)
        if person is not None:
            person.updated = time.time()
            self._save()
        self._rebuild()

    # -- matching ------------------------------------------------------------------

    def match_face(self, embedding: np.ndarray, threshold: float, margin: float) -> Match:
        index = self._index
        if not len(index.faces):
            return NOBODY
        sims = index.faces @ _unit(embedding)
        best = np.full(len(index.people), -1.0, np.float32)
        np.maximum.at(best, index.face_owner, sims)
        return self._decide(best, index.people, threshold, margin)

    def match_voice(self, embedding: np.ndarray, threshold: float, margin: float) -> Match:
        index = self._index
        if not len(index.voices) or index.voices.shape[1] != np.asarray(embedding).size:
            return NOBODY
        sims = index.voices @ _unit(embedding)
        best = np.full(len(index.people), -1.0, np.float32)
        best[index.voice_owner] = sims
        return self._decide(best, index.people, threshold, margin)

    @staticmethod
    def _decide(best: np.ndarray, people: list[Person], threshold: float, margin: float) -> Match:
        order = np.argsort(-best)
        top = float(best[order[0]])
        second = float(best[order[1]]) if len(order) > 1 else -1.0
        person = people[int(order[0])]
        gap = top - max(second, 0.0) if second > -1.0 else top
        if top >= threshold and top - second >= margin:
            return Match(person.id, person.name, person.role, top, gap, person.name)
        return Match(None, None, "stranger", top, gap, person.name)

    def voice_people(self) -> int:
        """How many people have voice samples."""
        return len(self._index.voice_owner)

    # -- pictures --------------------------------------------------------------------

    def is_picture(self, embedding: np.ndarray) -> bool:
        pictures = self._pictures
        return bool(len(pictures)) and float((pictures @ _unit(embedding)).max()) >= (
            PICTURE_SIMILARITY
        )

    def add_picture(self, embedding: np.ndarray) -> None:
        with self._lock:
            self._pictures = np.vstack([self._pictures, _unit(embedding)[None, :]])[-200:]
            self.root.mkdir(parents=True, exist_ok=True)
            np.save(self.root / "pictures.npy", self._pictures)

    def pictures(self) -> int:
        return len(self._pictures)

    # -- strangers -------------------------------------------------------------------

    def _read_strangers(self) -> list[Stranger]:
        try:
            rows = json.loads((self.root / "strangers.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        out = []
        for r in rows if isinstance(rows, list) else []:
            try:
                out.append(
                    Stranger(
                        r["id"],
                        from_b64(r["embedding"]),
                        float(r["first_seen"]),
                        float(r["last_seen"]),
                        r.get("camera"),
                    )
                )
            except (KeyError, TypeError, ValueError):
                continue
        return out

    def note_stranger(
        self, embedding: np.ndarray, at: float, camera: str | None = None, *, same: float = 0.5
    ) -> Stranger:
        """Remember an unknown face (or update the stranger it looks like); returns them."""
        emb = _unit(embedding)
        with self._lock:
            for stranger in self._strangers:
                if float(stranger.embedding @ emb) >= same:
                    stranger.last_seen = max(stranger.last_seen, at)
                    stranger.embedding = _unit(stranger.embedding * 0.9 + emb * 0.1)
                    return stranger
            stranger = Stranger(f"s-{uuid.uuid4().hex[:8]}", emb, at, at, camera)
            self._strangers.append(stranger)
            del self._strangers[:-500]
            return stranger

    def strangers(self) -> list[Stranger]:
        with self._lock:
            return list(self._strangers)

    def save_strangers(self, retention_h: float, now: float | None = None) -> int:
        """Drop strangers older than the retention and write the rest; how many are kept."""
        now = time.time() if now is None else now
        with self._lock:
            self._strangers = [
                s for s in self._strangers if now - s.last_seen <= retention_h * 3600
            ]
            path = self.root / "strangers.json"
            if not self._strangers and not path.exists():
                return 0  # nothing to keep: leave no files behind
            self.root.mkdir(parents=True, exist_ok=True)
            rows = [
                {
                    "id": s.id,
                    "embedding": b64(s.embedding),
                    "first_seen": round(s.first_seen, 3),
                    "last_seen": round(s.last_seen, 3),
                    "camera": s.camera,
                }
                for s in self._strangers
            ]
            path.write_text(json.dumps(rows), encoding="utf-8")
            return len(rows)
