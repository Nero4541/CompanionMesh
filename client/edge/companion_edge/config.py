"""Edge configuration: a TOML file (stdlib ``tomllib``) plus the token from the environment."""

from __future__ import annotations

import os
import socket
import tomllib
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any


@dataclass
class AudioConfig:
    enabled: bool = True
    input_enabled: bool = True  # false: a speaker-only device (no microphone)
    input_device: str = "default"  # ALSA device for arecord, e.g. "hw:0,0" or "plughw:1,0"
    output_device: str = "default"  # ALSA device for aplay
    # Silence before speech when the output stream opens: Bluetooth speakers
    # chop the first audio after their stream starts (~300 ms helps).
    output_lead_in_ms: int = 0
    output_hold_s: float = 2.0  # keep the output stream open this long after speech
    codec: str = "opus"  # opus | pcm
    # Who encodes Opus: libopus (ctypes), ffmpeg (its own encoder), or auto
    # (libopus, then ffmpeg, then fall back to PCM).
    opus_encoder: str = "auto"
    opus_bitrate: int = 24000
    packets_per_frame: int = 5  # 20 ms Opus packets batched per WebSocket frame
    buffer_s: float = 3.0  # mic audio kept while disconnected, sent on reconnect
    # Keep the microphone open while the companion speaks, so the user can cut in
    # (when the server supports barge-in). False: half duplex, never hear ourselves.
    full_duplex: bool = True
    # "device" if this hardware cancels its own speaker's echo (e.g. a USB
    # speakerphone); the server is then less strict about speech during playback.
    aec: str = "none"


@dataclass
class CameraConfig:
    enabled: bool = True
    device: str = "/dev/video0"
    input_format: str = "mjpeg"  # v4l2 input format; "" lets ffmpeg choose
    size: str = "640x480"
    # change: watch the camera locally and send frames only when the view changes
    # (plus one every heartbeat_s); periodic: adaptive interval below.
    mode: str = "change"
    analysis_fps: float = 2.0  # change mode: tiny greyscale frames checked per second
    heartbeat_s: float = 300.0  # change mode: a routine frame at least this often
    motion_threshold: float = 0.02  # share of pixels that must change to count as motion
    scene_threshold: float = 0.25  # share differing from the background = scene change
    cooldown_s: float = 10.0  # between two change events of the same kind
    min_interval_s: float = 5.0  # periodic mode: fastest frame rate
    max_interval_s: float = 60.0  # periodic mode: backs off to this while nothing changes
    quality: int = 5  # ffmpeg -q:v (2 = best, 31 = worst)


@dataclass
class EdgeConfig:
    server: str = "ws://127.0.0.1:8765/v1/realtime"
    token_env: str = "COMPANION_TOKEN"
    device_id: str = field(default_factory=socket.gethostname)
    state_file: Path = Path("~/.local/state/companion-edge/state.json")
    heartbeat_s: float = 30.0
    audio: AudioConfig = field(default_factory=AudioConfig)
    camera: CameraConfig = field(default_factory=CameraConfig)

    def token(self) -> str | None:
        return os.environ.get(self.token_env) or None


def _fill(cls: type, data: dict[str, Any]) -> Any:
    known = {f.name: f for f in fields(cls)}
    unknown = set(data) - set(known)
    if unknown:
        raise ValueError(f"unknown {cls.__name__} keys: {', '.join(sorted(unknown))}")
    values: dict[str, Any] = {}
    for name, value in data.items():
        default = getattr(cls(), name) if name in ("audio", "camera") else None
        if isinstance(value, dict) and default is not None:
            values[name] = _fill(type(default), value)
        elif name == "state_file":
            values[name] = Path(value)
        else:
            values[name] = value
    return cls(**values)


def load(path: Path | None) -> EdgeConfig:
    if path is None:
        return EdgeConfig()
    with path.open("rb") as fh:
        data = tomllib.load(fh)
    return _fill(EdgeConfig, data)
