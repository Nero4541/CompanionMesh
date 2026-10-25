"""What each stage of the voice pipeline did last: the dashboard's pipeline monitor.

mic (audio arriving from a device, and how loud it is) -> vad (speech found in it) -> stt (its
words) -> agent (an answer) -> tts (the answer's voice) -> speaker (played on a device). Every
stage keeps its last success and its last failure, so one that stopped working stands out.
mic, vad and speaker are kept per device; stt, agent and tts once for the server.
"""

from __future__ import annotations

import asyncio
import math
import time
from dataclasses import dataclass, field
from typing import Any

import numpy as np

SAMPLE_RATE = 16000
STALE_AUDIO_S = 3.0  # a microphone that sent nothing for this long is down
SILENT_DB = -70.0  # quieter than this for a while: muted, or the wrong input
LEVEL_WINDOW_S = 1.0  # the level shown is about the last second


@dataclass
class Mark:
    at: float
    detail: str


@dataclass
class Stage:
    ok: Mark | None = None
    failed: Mark | None = None

    def note(self, ok: bool, detail: str) -> None:
        mark = Mark(time.time(), detail)
        if ok:
            self.ok = mark
        else:
            self.failed = mark

    def report(self, now: float) -> dict[str, Any]:
        """The newer of the last success and the last failure decides the status."""
        last = max((m for m in (self.ok, self.failed) if m), key=lambda m: m.at, default=None)
        if last is None:
            return {"status": "idle", "detail": "nothing yet", "age_s": None}
        status = "ok" if last is self.ok else "fail"
        out: dict[str, Any] = {
            "status": status,
            "detail": last.detail,
            "age_s": round(now - last.at, 1),
        }
        if status == "ok" and self.failed is not None:
            out["last_failure"] = {
                "detail": self.failed.detail,
                "age_s": round(now - self.failed.at, 1),
            }
        return out


@dataclass
class _Ear:
    """One device's microphone: when audio last came, and how loud."""

    last_audio: float | None = None
    power: float = 0.0  # mean square, smoothed over about LEVEL_WINDOW_S
    quiet_since: float | None = None
    vad: Stage = field(default_factory=Stage)
    speaker: Stage = field(default_factory=Stage)

    @property
    def level_db(self) -> float:
        return 10 * math.log10(self.power) if self.power > 1e-12 else -120.0


class PipelineMonitor:
    def __init__(self) -> None:
        self.stt = Stage()
        self.agent = Stage()
        self.tts = Stage()
        self._ears: dict[str, _Ear] = {}

    def _ear(self, device_id: str | None) -> _Ear:
        return self._ears.setdefault(device_id or "device", _Ear())

    def audio(self, device_id: str | None, samples: np.ndarray) -> None:
        """Microphone audio arrived from ``device_id`` (float32, 16 kHz)."""
        if not samples.size:
            return
        ear = self._ear(device_id)
        now = time.time()
        power = float(np.mean(np.square(samples, dtype=np.float32)))
        weight = min(1.0, samples.size / SAMPLE_RATE / LEVEL_WINDOW_S)
        ear.power = power if ear.last_audio is None else ear.power + weight * (power - ear.power)
        ear.last_audio = now
        if ear.level_db < SILENT_DB:
            ear.quiet_since = ear.quiet_since or now
        else:
            ear.quiet_since = None

    def speech(self, device_id: str | None, detail: str = "speech heard") -> None:
        self._ear(device_id).vad.note(True, detail)

    def played(self, device_id: str | None, ok: bool, detail: str) -> None:
        self._ear(device_id).speaker.note(ok, detail)

    def report(
        self, devices: list[dict[str, Any]], engines: dict[str, str | None]
    ) -> dict[str, Any]:
        """``devices``: {device_id, roles, session_id, listen?} of the connected ones;
        ``engines``: stt/agent/tts -> None when running, else why it is not."""
        now = time.time()
        out_devices = []
        for d in devices:
            ear = self._ears.get(d.get("device_id") or "device") or _Ear()
            roles = set(d.get("roles") or [])
            out_devices.append(
                {
                    "device_id": d.get("device_id"),
                    "session_id": d.get("session_id"),
                    "roles": sorted(roles),
                    "listen": d.get("listen"),
                    "mic": self._mic(ear, roles, d.get("listen"), now),
                    "vad": ear.vad.report(now)
                    if "mic" in roles or d.get("listen")
                    else _off("no microphone"),
                    "speaker": ear.speaker.report(now)
                    if "speaker" in roles
                    else _off("no speaker"),
                }
            )
        stages = {}
        for name, stage in (("stt", self.stt), ("agent", self.agent), ("tts", self.tts)):
            down = engines.get(name)
            stages[name] = (
                {"status": "fail", "detail": down, "age_s": None} if down else stage.report(now)
            )
            if down == "off":
                stages[name] = _off("turned off")
        return {"devices": out_devices, **stages}

    def _mic(self, ear: _Ear, roles: set[str], listen: str | None, now: float) -> dict[str, Any]:
        if "mic" not in roles:
            if listen == "waiting":
                return {
                    "status": "warn",
                    "detail": "has a microphone but sends nothing (the phone: tap to listen)",
                    "age_s": None,
                }
            if listen == "push_to_talk":
                if ear.last_audio is None:
                    return {
                        "status": "idle",
                        "detail": "push to talk: nothing said yet",
                        "age_s": None,
                    }
                return {
                    "status": "ok",
                    "detail": f"push to talk, {ear.level_db:.0f} dBFS",
                    "age_s": round(now - ear.last_audio, 1),
                    "level_db": round(ear.level_db, 1),
                }
            return _off("no microphone")
        if ear.last_audio is None:
            return {"status": "fail", "detail": "no audio has arrived", "age_s": None}
        age = now - ear.last_audio
        level = round(ear.level_db, 1)
        if age > STALE_AUDIO_S:
            return {
                "status": "fail",
                "detail": f"no audio for {age:.0f} s",
                "age_s": round(age, 1),
                "level_db": level,
            }
        if ear.quiet_since is not None and now - ear.quiet_since > 10:
            return {
                "status": "warn",
                "detail": f"silent ({level:.0f} dBFS): muted, or the wrong input?",
                "age_s": round(age, 1),
                "level_db": level,
            }
        return {
            "status": "ok",
            "detail": f"audio arriving, {level:.0f} dBFS",
            "age_s": round(age, 1),
            "level_db": level,
        }


def _off(detail: str) -> dict[str, Any]:
    return {"status": "off", "detail": detail, "age_s": None}


@dataclass
class VoiceTest:
    """The dashboard's voice input test of one device: its next sentence is only recognised.

    What each step saw: audio arriving and how loud, speech found in it, its words.
    """

    device_id: str | None
    pushed: bool = False  # push to talk: the sentence comes whole, held to the mouth
    chunks: int = 0
    peak_db: float = -120.0
    speech: bool = False
    utterance_s: float | None = None
    text: str | None = None
    stt_s: float | None = None
    error: str | None = None
    done: asyncio.Event = field(default_factory=asyncio.Event)

    def heard(self, samples: np.ndarray) -> None:
        if not samples.size:
            return
        self.chunks += 1
        power = float(np.mean(np.square(samples, dtype=np.float32)))
        if power > 1e-12:
            self.peak_db = max(self.peak_db, 10 * math.log10(power))

    def finish(self, *, text: str | None = None, error: str | None = None) -> None:
        self.text, self.error = text, error
        self.done.set()

    def report(self) -> dict[str, Any]:
        steps = []
        if not self.chunks:
            what = (
                "nothing was said with push to talk"
                if self.pushed
                else "no audio arrived from the microphone"
            )
            steps.append({"stage": "mic", "status": "fail", "detail": what})
        elif self.peak_db < SILENT_DB + 10:
            steps.append(
                {
                    "stage": "mic",
                    "status": "fail",
                    "detail": f"only silence ({self.peak_db:.0f} dBFS): muted, or the wrong input?",
                }
            )
        else:
            steps.append(
                {
                    "stage": "mic",
                    "status": "ok",
                    "detail": f"audio arrived, loudest {self.peak_db:.0f} dBFS",
                }
            )
        if steps[0]["status"] == "ok":
            if self.utterance_s is not None:
                steps.append(
                    {
                        "stage": "vad",
                        "status": "ok",
                        "detail": f"a sentence of {self.utterance_s:.1f} s",
                    }
                )
            elif self.speech:
                steps.append(
                    {
                        "stage": "vad",
                        "status": "fail",
                        "detail": "speech began but did not end in time: speak a short sentence",
                    }
                )
            else:
                steps.append(
                    {
                        "stage": "vad",
                        "status": "fail",
                        "detail": "no speech found: speak louder or nearer, or lower vad.threshold",
                    }
                )
        if self.utterance_s is not None:
            if self.error:
                steps.append({"stage": "stt", "status": "fail", "detail": self.error})
            elif self.text:
                steps.append(
                    {
                        "stage": "stt",
                        "status": "ok",
                        "detail": f"「{self.text}」 in {self.stt_s:.1f} s",
                    }
                )
            elif self.done.is_set():
                steps.append(
                    {
                        "stage": "stt",
                        "status": "fail",
                        "detail": "recognized nothing: noise, or a language not in stt.languages?",
                    }
                )
            else:
                steps.append(
                    {
                        "stage": "stt",
                        "status": "fail",
                        "detail": "speech recognition did not answer in time",
                    }
                )
        ok = len(steps) == 3 and all(s["status"] == "ok" for s in steps)
        return {"device_id": self.device_id, "ok": ok, "steps": steps}
