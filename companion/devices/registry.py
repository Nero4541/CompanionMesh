"""Enrolled devices and their own tokens (``data/devices.json``).

Each enrolled device gets a token of its own, so a board that is lost can be
locked out alone (revoke) without changing every other device. Only a hash
of the token is stored; the token itself is shown once, when it is created,
and written to the device by the installer.

A device token is bound to its ``device_id``: a connection that presents it
must announce that id. The shared ``COMPANION_TOKEN`` keeps working for the
dev client, cameras and devices set up by hand.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import threading
import time
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any

DEVICE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,62}$")


def _digest(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


@dataclass
class EnrolledDevice:
    device_id: str
    token_sha256: str
    created: float = field(default_factory=time.time)
    last_seen: float | None = None
    profile: str | None = None
    # Set by the installer: where it reached the board, and the root password
    # it gave it (the stock one is public). Shown in the dashboard on request.
    address: str | None = None
    root_password: str | None = None
    installed_version: str | None = None
    # The device's last reported settings (device.config), to restore after a reflash.
    settings: dict[str, Any] | None = None

    def public(self) -> dict[str, Any]:
        """What the dashboard lists: never the token hash or the password."""
        return {
            "device_id": self.device_id,
            "created": self.created,
            "last_seen": self.last_seen,
            "profile": self.profile,
            "address": self.address,
            "installed_version": self.installed_version,
            "has_root_password": bool(self.root_password),
            # The server address it last reported, which an update keeps unless changed.
            "server": (self.settings or {}).get("server"),
        }


class DeviceRegistry:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.Lock()
        self._devices: dict[str, EnrolledDevice] = {}
        self._loaded_mtime: float | None = None

    # The installer (CLI) and a running server may both write the file, so it
    # is read again whenever it changed on disk.
    def _load(self) -> None:
        try:
            mtime = self.path.stat().st_mtime
        except FileNotFoundError:
            self._devices, self._loaded_mtime = {}, None
            return
        if mtime == self._loaded_mtime:
            return
        data = json.loads(self.path.read_text(encoding="utf-8") or "{}")
        known = {f.name for f in fields(EnrolledDevice)}
        self._devices = {
            item["device_id"]: EnrolledDevice(**{k: v for k, v in item.items() if k in known})
            for item in data.get("devices", [])
        }
        self._loaded_mtime = mtime

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + ".tmp")
        body = {"devices": [asdict(d) for d in self._devices.values()]}
        tmp.write_text(json.dumps(body, indent=2) + "\n", encoding="utf-8")
        if os.name != "nt":
            tmp.chmod(0o600)
        os.replace(tmp, self.path)
        self._loaded_mtime = self.path.stat().st_mtime

    def list(self) -> list[EnrolledDevice]:
        with self._lock:
            self._load()
            return list(self._devices.values())

    def get(self, device_id: str) -> EnrolledDevice | None:
        with self._lock:
            self._load()
            return self._devices.get(device_id)

    def enroll(self, device_id: str, **info: Any) -> str:
        """Create the device, or give it a new token; returns the token (shown once)."""
        if not DEVICE_ID.match(device_id):
            raise ValueError(
                "device id: letters, digits, '.', '_' or '-', starting with a letter or digit"
            )
        token = secrets.token_urlsafe(32)
        with self._lock:
            self._load()
            current = self._devices.get(device_id)
            if current is None:
                current = EnrolledDevice(device_id=device_id, token_sha256=_digest(token))
                self._devices[device_id] = current
            current.token_sha256 = _digest(token)
            for key, value in info.items():
                setattr(current, key, value)
            self._save()
        return token

    def update(self, device_id: str, **info: Any) -> None:
        with self._lock:
            self._load()
            current = self._devices.get(device_id)
            if current is None:
                raise KeyError(device_id)
            for key, value in info.items():
                setattr(current, key, value)
            self._save()

    def remove(self, device_id: str) -> bool:
        with self._lock:
            self._load()
            if self._devices.pop(device_id, None) is None:
                return False
            self._save()
            return True

    def identify(self, token: str | None) -> EnrolledDevice | None:
        """The enrolled device this token belongs to, if any."""
        if not token:
            return None
        digest = _digest(token)
        with self._lock:
            self._load()
            found = None
            for device in self._devices.values():
                if hmac.compare_digest(device.token_sha256, digest):
                    found = device
            return found

    def seen(self, device_id: str) -> None:
        """Note a connection (kept in memory; saved with the next change)."""
        with self._lock:
            device = self._devices.get(device_id)
            if device is not None:
                device.last_seen = time.time()
