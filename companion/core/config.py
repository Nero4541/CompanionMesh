"""Configuration loading.

Non-secret behaviour lives in YAML (``configs/companion.yaml``). An optional
sibling ``*.local.yaml`` file is deep-merged on top for machine-specific
overrides and is never committed. Secrets live in ``.env``; config refers to
them by environment-variable *name* (``api_key_env``), never by value.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import yaml
from dotenv import load_dotenv
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator


class ConfigError(Exception):
    """Configuration is missing or invalid."""


class _Section(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PublicListenerConfig(_Section):
    """A second listener for devices outside this network (router port forward, IPv6).

    It serves only what devices need (/health and /v1/realtime), accepts only
    enrolled devices' own tokens, sent as a header, and uses TLS with this
    server's own certificate (data/tls), which the board installer copies to
    the boards. The dashboard and the dev client are never served on it.
    """

    enabled: bool = False
    # Both IPv4 and IPv6 by default; an address family the machine lacks is skipped.
    hosts: list[str] = Field(default_factory=lambda: ["0.0.0.0", "::"])
    port: int = Field(default=8766, ge=1, le=65535)
    # How boards reach it from outside, e.g. wss://home.example.net:8766/v1/realtime
    # (a dynamic DNS name or the public address); the installer gives it to boards.
    url: str | None = None
    # Your own certificate instead of the generated one (e.g. from a CA); boards then
    # check it against their system CAs and the name in url.
    cert_file: Path | None = None
    key_file: Path | None = None
    # Behind a reverse proxy or tunnel on this machine: take the client address from
    # X-Forwarded-For / CF-Connecting-IP (for the limits below). Off: the TCP peer.
    trusted_proxy: bool = False
    # An address that fails to authenticate this often within ban_s is refused for ban_s.
    max_failed_auth: int = Field(default=10, ge=1)
    ban_s: float = Field(default=900.0, gt=0)
    max_connections_per_address: int = Field(default=4, ge=1)

    @field_validator("url")
    @classmethod
    def _wss(cls, v: str | None) -> str | None:
        if v and not v.startswith(("wss://", "ws://")):
            raise ValueError("a ws:// or wss:// URL, e.g. wss://home.example.net:8766/v1/realtime")
        return v or None


class ServerConfig(_Section):
    host: str = "127.0.0.1"
    port: int = Field(default=8765, ge=1, le=65535)
    dev_client: bool = True
    # Token every client must present; required when host is not loopback.
    auth_token_env: str | None = "COMPANION_TOKEN"
    # Keep a session this long after its last mic/speaker device drops, so a
    # device that reconnects (e.g. after a Wi-Fi hiccup) resumes it.
    session_linger_s: float = Field(default=60.0, ge=0)
    # Serve the admin dashboard at /admin/.
    dashboard: bool = True
    # Token for the admin API (/v1/admin/*). The device token never grants admin
    # access; while this is unset, the admin API only answers this machine.
    admin_token_env: str | None = "COMPANION_ADMIN_TOKEN"
    # Show what was said (transcripts and replies) on the dashboard; off, the
    # dashboard only sees their length.
    dashboard_conversation: bool = False
    public: PublicListenerConfig = PublicListenerConfig()

    def auth_token(self) -> str | None:
        if not self.auth_token_env:
            return None
        return os.environ.get(self.auth_token_env) or None

    def admin_token(self) -> str | None:
        if not self.admin_token_env:
            return None
        return os.environ.get(self.admin_token_env) or None


class LoggingConfig(_Section):
    level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    format: Literal["text", "json"] = "text"


class StorageConfig(_Section):
    # String defaults (validated into Path) keep the JSON schema's defaults
    # serializable; pydantic cannot encode a WindowsPath default.
    data_dir: Path = Field(default="data", validate_default=True)


class HttpProviderConfig(_Section):
    """Shared fields for providers reached over HTTP."""

    base_url: str
    api_key_env: str | None = None
    connect_timeout: float = Field(default=5.0, gt=0)
    timeout: float = Field(default=120.0, gt=0)

    def api_key(self) -> str | None:
        if not self.api_key_env:
            return None
        return os.environ.get(self.api_key_env) or None


class GatewayAgentConfig(HttpProviderConfig):
    """An agent framework reached through an OpenAI-compatible chat endpoint.

    ``base_url`` may point at another machine (LAN or a private overlay
    network such as Tailscale); the agent then runs and stores memory there.
    """

    timeout: float = Field(default=300.0, gt=0)
    model: str
    # Prefix for the agent-side session id derived from the companion session.
    session_prefix: str = "companion-"
    # Extra request fields, e.g. {"max_tokens": 200}. Core fields (model,
    # messages, stream, user) cannot be overridden.
    extra_body: dict[str, Any] = Field(default_factory=dict)


class HermesConfig(GatewayAgentConfig):
    base_url: str = "http://127.0.0.1:8642/v1"
    api_key_env: str | None = "HERMES_API_KEY"
    model: str = "hermes-agent"
    # Stable long-term memory scope (X-Hermes-Session-Key). Shared across sessions.
    memory_scope: str | None = "companion"


class OpenClawConfig(GatewayAgentConfig):
    base_url: str = "http://127.0.0.1:18789/v1"
    api_key_env: str | None = "OPENCLAW_GATEWAY_TOKEN"
    model: str = "openclaw"
    agent_id: str = "main"


class AgentConfig(_Section):
    provider: Literal["hermes", "openclaw", "direct"] = "hermes"
    persona: str = "default"
    history_turns: int = Field(default=20, ge=0)
    hermes: HermesConfig = HermesConfig()
    openclaw: OpenClawConfig = OpenClawConfig()


class LLMConfig(HttpProviderConfig):
    provider: Literal["openai_compatible"] = "openai_compatible"
    base_url: str = "http://127.0.0.1:11434/v1"
    api_key_env: str | None = "LLM_API_KEY"
    model: str = "default"
    temperature: float | None = None
    max_tokens: int | None = None
    # Server-specific request fields, e.g. for llama.cpp / vLLM with Qwen3:
    # {"chat_template_kwargs": {"enable_thinking": false}}
    extra_body: dict[str, Any] = Field(default_factory=dict)


def strip_endpoint(url: str, endpoint: str) -> str:
    """``url`` without a trailing ``endpoint``: providers add it themselves."""
    trimmed = url.rstrip("/")
    return trimmed[: -len(endpoint)] if trimmed.endswith(endpoint) else url


class STTConfig(_Section):
    provider: Literal["faster_whisper", "openai_compatible", "none"] = "faster_whisper"
    model: str = "large-v3-turbo"
    language: str | None = "ja"
    device: Literal["auto", "cuda", "cpu"] = "auto"
    compute_type: str = "auto"
    beam_size: int = Field(default=1, ge=1)
    preload: bool = True
    # Text that steers recognition toward a writing style or vocabulary (Whisper's
    # initial prompt; `prompt` in the OpenAI API), e.g. for Cantonese in traditional
    # characters: 以下係廣東話對話，用香港繁體中文書寫。 Keep it short.
    prompt: str | None = None
    # openai_compatible only: POST {base_url}/audio/transcriptions on another host.
    base_url: str = "http://127.0.0.1:8000/v1"
    api_key_env: str | None = "STT_API_KEY"
    connect_timeout: float = Field(default=5.0, gt=0)
    timeout: float = Field(default=60.0, gt=0)
    # Keep each utterance as heard (WAV in data/recordings) with its transcript,
    # to check recognition from the dashboard; deleted after keep_audio_days.
    keep_audio: bool = False
    keep_audio_days: float = Field(default=3.0, gt=0)

    @field_validator("base_url")
    @classmethod
    def _no_endpoint(cls, v: str) -> str:
        return strip_endpoint(v, "/audio/transcriptions")

    def api_key(self) -> str | None:
        if not self.api_key_env:
            return None
        return os.environ.get(self.api_key_env) or None


class VADConfig(_Section):
    provider: Literal["silero", "energy"] = "silero"
    threshold: float = Field(default=0.5, gt=0, lt=1)
    # Energy VAD only: RMS (0..1) above which a frame counts as speech.
    energy_threshold: float = Field(default=0.015, gt=0, lt=1)
    min_speech_ms: int = Field(default=250, ge=0)
    min_silence_ms: int = Field(default=1000, ge=0)
    speech_pad_ms: int = Field(default=200, ge=0)
    max_utterance_s: float = Field(default=30.0, gt=0)


class TTSConfig(_Section):
    provider: Literal["openai_compatible", "none"] = "openai_compatible"
    base_url: str = "http://127.0.0.1:8088/v1"
    api_key_env: str | None = "TTS_API_KEY"
    connect_timeout: float = Field(default=5.0, gt=0)
    timeout: float = Field(default=120.0, gt=0)
    model: str = "irodori-tts"
    voice: str | None = None
    response_format: Literal["wav", "mp3", "opus", "flac", "aac", "pcm"] = "wav"
    speed: float | None = None
    # Vendor-specific request fields merged into the body (e.g. {"irodori": {...}}).
    extra_body: dict[str, Any] = Field(default_factory=dict)

    @field_validator("base_url")
    @classmethod
    def _no_endpoint(cls, v: str) -> str:
        return strip_endpoint(v, "/audio/speech")

    def api_key(self) -> str | None:
        if not self.api_key_env:
            return None
        return os.environ.get(self.api_key_env) or None


class BuiltinSTTConfig(_Section):
    """faster-whisper, installed and run by the server (data/builtin/stt)."""

    # On: used instead of stt.provider. Off: its process ends, freeing its GPU memory.
    enabled: bool = False
    model: str = "large-v3-turbo"
    device: Literal["auto", "cuda", "cpu"] = "auto"
    compute_type: str = "auto"
    beam_size: int = Field(default=1, ge=1)


class BuiltinTTSConfig(_Section):
    """Irodori-TTS (Japanese), installed and run by the server (data/builtin/tts)."""

    # On: used instead of tts.provider. Off: its process ends, freeing its GPU memory.
    enabled: bool = False
    # A Hugging Face repository; the MF models need 4 steps instead of 40.
    checkpoint: str = "Aratako/Irodori-TTS-v4.1-Small-MF"
    device: Literal["auto", "cuda", "cpu"] = "auto"
    # bf16 halves the memory but needs CUDA.
    precision: Literal["fp32", "bf16"] = "fp32"


class BuiltinConfig(_Section):
    """Speech engines the server installs and runs itself, for a setup without servers."""

    # Libraries to install: cuda (NVIDIA GPU) or cpu; auto picks cuda when nvidia-smi is found.
    libraries: Literal["auto", "cuda", "cpu"] = "auto"
    # How long an engine may take to start (its first start downloads the model).
    start_timeout_s: float = Field(default=900.0, gt=0)
    stt: BuiltinSTTConfig = BuiltinSTTConfig()
    tts: BuiltinTTSConfig = BuiltinTTSConfig()


class VLMConfig(HttpProviderConfig):
    """Vision-language model reached through OpenAI-compatible chat completions."""

    provider: Literal["openai_compatible", "none"] = "none"
    base_url: str = "http://127.0.0.1:8080/v1"
    api_key_env: str | None = "VLM_API_KEY"
    timeout: float = Field(default=60.0, gt=0)
    model: str = "default"
    # Frames are downscaled (longer side, pixels) and re-encoded before sending,
    # in both vision modes.
    max_image_side: int = Field(default=768, ge=64, le=4096)
    max_tokens: int = Field(default=300, ge=16)
    # Replaces the built-in instruction; {language} is substituted.
    prompt: str | None = None
    extra_body: dict[str, Any] = Field(default_factory=dict)


class VisionConfig(_Section):
    # Server-wide switch; each session must still opt in with vision.enable.
    enabled: bool = True
    max_frame_bytes: int = Field(default=4 * 1024 * 1024, ge=1024)
    max_pixels: int = Field(default=16_000_000, ge=64 * 64)
    # Accepted frames per session are at least this far apart (manual frames excepted).
    min_interval_s: float = Field(default=2.0, ge=0)
    # Skip frames that look like the last analyzed one (difference-hash distance).
    dedup: bool = True
    dedup_threshold: int = Field(default=6, ge=0, le=64)
    # Observations older than this are not offered to the agent.
    observation_max_age_s: float = Field(default=300.0, gt=0)
    max_context_observations: int = Field(default=3, ge=0)
    # "agent": attach the newest frame as an image to the user's next message, for
    # agents whose model can see. "vlm": describe frames with the vlm provider and
    # pass the descriptions as text.
    mode: Literal["agent", "vlm"] = "agent"
    # Agent mode: unseen frames attached to one turn, oldest first. When more
    # arrive, the least important (then oldest) ones are dropped.
    max_turn_images: int = Field(default=1, ge=1, le=16)
    # "user": prepend new observations to the user's message (keeps the agent's
    # prompt cache intact); "system": add recent ones to the system prompt.
    inject: Literal["user", "system"] = "user"
    # When a turn starts (speech or text), ask camera devices for a fresh frame.
    capture_on_turn: bool = True
    # A turn waits this long for a requested frame, or one still being analyzed.
    wait_for_pending_s: float = Field(default=3.0, ge=0)
    # Images are never written to disk unless enabled here.
    store_images: bool = False
    # Retention of stored observations (direct backend) and stored images.
    retention_days: float = Field(default=7.0, gt=0)


_HHMM = r"^([01]\d|2[0-3]):[0-5]\d$"


class QuietHoursConfig(_Section):
    """No proactive speech during these hours (normal conversation is unaffected)."""

    enabled: bool = True
    start: str = Field(default="23:00", pattern=_HHMM)
    end: str = Field(default="08:00", pattern=_HHMM)
    # IANA name such as "Asia/Tokyo"; null = the host's local time zone.
    timezone: str | None = None

    @field_validator("timezone")
    @classmethod
    def _known_zone(cls, v: str | None) -> str | None:
        if v is None:
            return v
        from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

        try:
            ZoneInfo(v)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError(f"unknown time zone {v!r}") from exc
        return v


class AttentionConfig(_Section):
    """When the companion may speak on its own (v0.3)."""

    # Master switch for proactive speech; conversation works either way.
    proactive: bool = True
    # Minimum time between proactive turns, and a per-hour budget.
    min_interval_s: float = Field(default=900.0, ge=0)
    max_per_hour: int = Field(default=4, ge=0)
    quiet_hours: QuietHoursConfig = QuietHoursConfig()
    # No proactive speech this soon after the user or the companion spoke.
    grace_after_activity_s: float = Field(default=120.0, ge=0)
    # Camera signals: difference-hash bits (0-64) that count as a scene change,
    # and how long a quiet scene must last before a change means "activity resumed".
    scene_change_threshold: int = Field(default=20, ge=1, le=64)
    absence_s: float = Field(default=1200.0, gt=0)
    # Idle check-ins: after a random wait between idle_min_s and idle_max_s
    # without anyone speaking, the agent may say something on its own.
    idle: bool = False
    idle_min_s: float = Field(default=1800.0, gt=0)
    idle_max_s: float = Field(default=3600.0, gt=0)
    # Repeats of the same kind of event within this window are ignored.
    event_dedup_s: float = Field(default=120.0, ge=0)
    # Salience thresholds (0-1) for REMEMBER, CONTEXT_ONLY and SPEAK.
    remember_threshold: float = Field(default=0.2, ge=0, le=1)
    context_threshold: float = Field(default=0.4, ge=0, le=1)
    speak_threshold: float = Field(default=0.6, ge=0, le=1)
    # Send every decision with its reasons to clients (attention.decision).
    debug: bool = False


class EchoConfig(_Section):
    """Keeping the companion's own voice out of its ears (v0.5)."""

    # Server-side echo canceller for microphones whose device has none:
    # "none" relies on gating and the transcript guard; "nlms" is a reference
    # adaptive filter driven by the audio the companion sent to the speaker.
    canceller: Literal["none", "nlms"] = "none"
    filter_ms: int = Field(default=64, ge=8, le=512)
    max_delay_ms: int = Field(default=600, ge=0, le=2000)
    # Drop a transcript that repeats what the companion said moments ago:
    # its own voice leaking from the speaker into the microphone.
    guard: bool = True
    guard_similarity: float = Field(default=0.6, gt=0, le=1)
    guard_window_s: float = Field(default=20.0, gt=0)


class RealtimeConfig(_Section):
    """Full-duplex conversation: barge-in, state deadlines, clock sync (v0.5)."""

    # Let the user cut in while the companion speaks. Needs a client that keeps
    # sending microphone audio during playback (full_duplex in audio.input.start).
    barge_in: bool = True
    # While audio plays on a device without echo cancellation, speech must be at
    # least this long and this likely (VAD) to count as the user cutting in.
    barge_in_min_speech_ms: int = Field(default=600, ge=0)
    barge_in_threshold: float = Field(default=0.8, gt=0, lt=1)
    # Watchdog: a state lasting longer than this means a message got lost.
    transcribe_timeout_s: float = Field(default=60.0, gt=0)
    interrupt_timeout_s: float = Field(default=3.0, gt=0)
    # SPEAKING ends this long after the audio sent should have finished playing
    # if the device never confirms playback.
    playback_slack_s: float = Field(default=8.0, ge=0)
    # Round-trip/clock-offset probes to each device (0 = off).
    ping_interval_s: float = Field(default=10.0, ge=0)
    echo: EchoConfig = EchoConfig()


class CameraConfig(_Section):
    """An IP camera or phone the server watches itself (instead of ``companion camera``).

    It joins the active conversation as a camera device. Credentials come from
    ``.env`` through ``username_env``/``password_env``, never from the URL.
    """

    id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,64}$")
    # HTTP snapshot (.../shot.jpg), MJPEG stream (.../video) or rtsp://
    url: str = Field(min_length=1)
    enabled: bool = True
    username_env: str | None = "CAMERA_USERNAME"
    password_env: str | None = "CAMERA_PASSWORD"
    # change: send a frame when the view changes; periodic: every interval_s.
    mode: Literal["change", "periodic"] = "change"
    interval_s: float = Field(default=5.0, ge=0)
    heartbeat_s: float = Field(default=300.0, gt=0)
    analysis_interval_s: float = Field(default=1.0, gt=0)
    # Join this session instead of the most recently started one.
    session_id: str | None = None
    # Switch vision on in the session it joins.
    enable_vision: bool = True

    @field_validator("url")
    @classmethod
    def _no_secret_in_url(cls, v: str) -> str:
        from urllib.parse import urlsplit

        parts = urlsplit(v)
        if parts.password is not None or parts.username is not None:
            raise ValueError(
                "do not put credentials in the URL; set username_env/password_env and .env"
            )
        return v

    def credentials(self) -> tuple[str | None, str | None]:
        username = os.environ.get(self.username_env) if self.username_env else None
        password = os.environ.get(self.password_env) if self.password_env else None
        return username or None, password or None


class MetricsConfig(_Section):
    """Latency instrumentation: numbers only, never audio, images or text."""

    enabled: bool = True
    # Samples kept per metric for the percentiles in /v1/metrics.
    window: int = Field(default=200, ge=10)
    # Also send per-turn timings to clients as metrics.turn events.
    emit_turn: bool = False


class CompanionConfig(_Section):
    server: ServerConfig = ServerConfig()
    logging: LoggingConfig = LoggingConfig()
    storage: StorageConfig = StorageConfig()
    agent: AgentConfig = AgentConfig()
    llm: LLMConfig = LLMConfig()
    stt: STTConfig = STTConfig()
    vad: VADConfig = VADConfig()
    tts: TTSConfig = TTSConfig()
    builtin: BuiltinConfig = BuiltinConfig()
    vlm: VLMConfig = VLMConfig()
    vision: VisionConfig = VisionConfig()
    attention: AttentionConfig = AttentionConfig()
    realtime: RealtimeConfig = RealtimeConfig()
    metrics: MetricsConfig = MetricsConfig()
    cameras: list[CameraConfig] = Field(default_factory=list)
    personas_dir: Path = Field(default="personas", validate_default=True)

    @field_validator("cameras")
    @classmethod
    def _unique_cameras(cls, v: list[CameraConfig]) -> list[CameraConfig]:
        seen: set[str] = set()
        for camera in v:
            if camera.id.lower() in seen:
                raise ValueError(f"camera id {camera.id!r} is used twice")
            seen.add(camera.id.lower())
        return v

    @field_validator("personas_dir")
    @classmethod
    def _expand(cls, v: Path) -> Path:
        return v.expanduser()

    def resolve_paths(self, base: Path) -> CompanionConfig:
        """Make relative paths absolute against ``base`` (the project root)."""
        data_dir = self.storage.data_dir.expanduser()
        if not data_dir.is_absolute():
            data_dir = base / data_dir
        personas = self.personas_dir
        if not personas.is_absolute():
            personas = base / personas
        return self.model_copy(
            update={
                "storage": self.storage.model_copy(update={"data_dir": data_dir}),
                "personas_dir": personas,
            }
        )

    def public_view(self) -> dict[str, Any]:
        """Config safe to expose over HTTP (env var names only, never values)."""
        return self.model_dump(mode="json")


# Settings a running server can take without a restart, as dotted paths; a
# section covers everything inside it and the longest matching path wins.
_LIVE_SETTINGS = {
    "vad": True,
    "agent.persona": True,  # read on every turn
    "vision": True,
    "vision.mode": False,  # the VLM and frames held for the agent are set up per mode
    "vision.max_frame_bytes": False,  # sizes the WebSocket limit at startup
    "vision.store_images": False,
    "vision.retention_days": False,
    "attention": True,
    "realtime": True,
    "metrics.emit_turn": True,
    "cameras": True,  # started, stopped and replaced while running
    "server.dashboard_conversation": True,
    "stt.keep_audio": True,
    "stt.keep_audio_days": True,
    "stt.prompt": True,  # read on every utterance
    "builtin": True,  # engines start, stop and restart while running
}


def needs_restart(path: str) -> bool:
    """Whether changing the setting at dotted ``path`` takes a server restart."""
    parts = path.split(".")
    for end in range(len(parts), 0, -1):
        live = _LIVE_SETTINGS.get(".".join(parts[:end]))
        if live is not None:
            return not live
    return True


def secret_status(value: Any) -> dict[str, bool]:
    """Every ``*_env`` setting in a config dump: variable name -> whether it is set."""
    found: dict[str, bool] = {}
    if isinstance(value, dict):
        for key, item in value.items():
            if key.endswith("_env") and isinstance(item, str) and item:
                found[item] = bool(os.environ.get(item))
            else:
                found.update(secret_status(item))
    elif isinstance(value, list):
        for item in value:
            found.update(secret_status(item))
    return found


def changed_settings(old: Any, new: Any, prefix: str = "") -> list[str]:
    """Dotted paths whose values differ between two config dumps."""
    if isinstance(old, dict) and isinstance(new, dict):
        paths: list[str] = []
        for key in dict.fromkeys([*old, *new]):
            paths.extend(changed_settings(old.get(key), new.get(key), f"{prefix}{key}."))
        return paths
    return [] if old == new else [prefix.rstrip(".")]


def get_setting(data: dict[str, Any], path: str) -> Any:
    for part in path.split("."):
        data = data[part]
    return data


def set_setting(data: dict[str, Any], path: str, value: Any) -> None:
    *parents, last = path.split(".")
    for part in parents:
        data = data.setdefault(part, {})
    data[last] = value


def deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def _read_yaml(path: Path) -> dict[str, Any]:
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise ConfigError(f"{path}: invalid YAML: {exc}") from exc
    if not isinstance(data, dict):
        raise ConfigError(f"{path}: top level must be a mapping")
    return data


def local_override_path(path: Path) -> Path:
    return path.with_name(f"{path.stem}.local{path.suffix}")


def validation_errors(exc: ValidationError) -> list[dict[str, str]]:
    """Pydantic errors as ``{"path": "vad.threshold", "message": ...}``."""
    return [
        {"path": ".".join(map(str, e["loc"])), "message": e["msg"].removeprefix("Value error, ")}
        for e in exc.errors()
    ]


def _validate(data: dict[str, Any], where: Path) -> CompanionConfig:
    try:
        return CompanionConfig.model_validate(data)
    except ValidationError as exc:
        lines = [f"  {e['path']}: {e['message']}" for e in validation_errors(exc)]
        raise ConfigError(f"invalid configuration ({where}):\n" + "\n".join(lines)) from exc


@dataclass(frozen=True)
class ConfigSource:
    """Where a configuration comes from, so it can be read again and edited.

    The effective config is ``path`` deep-merged with its ``*.local.yaml``
    sibling, then with ``overrides`` (command-line flags), and finally paths
    resolved against ``project_root``.
    """

    path: Path
    project_root: Path
    overrides: dict[str, Any] = field(default_factory=dict)
    local_override: bool = True

    @property
    def local_path(self) -> Path:
        return local_override_path(self.path)

    def read_base(self) -> dict[str, Any]:
        return _read_yaml(self.path) if self.path.is_file() else {}

    def read_local(self) -> dict[str, Any]:
        if self.local_override and self.path.is_file() and self.local_path.is_file():
            return _read_yaml(self.local_path)
        return {}

    def saved(self) -> CompanionConfig:
        """The files' settings as written: no command-line overrides, paths unresolved."""
        return _validate(deep_merge(self.read_base(), self.read_local()), self.path)

    def load(self) -> CompanionConfig:
        """The config a (re)started server runs with."""
        data = deep_merge(self.read_base(), self.read_local())
        if self.overrides:
            data = deep_merge(data, self.overrides)
        return _validate(data, self.path).resolve_paths(self.project_root)


def config_source(
    path: Path | None = None,
    *,
    project_root: Path | None = None,
    env_file: Path | None = None,
    overrides: dict[str, Any] | None = None,
    local_override: bool = True,
) -> ConfigSource:
    """Locate the config files (and load ``.env``) without validating anything yet."""
    root = (project_root or Path.cwd()).resolve()
    env_path = env_file or root / ".env"
    if env_path.is_file():
        load_dotenv(env_path, override=False, encoding="utf-8")

    if path is None:
        env_cfg = os.environ.get("COMPANION_CONFIG")
        path = Path(env_cfg) if env_cfg else root / "configs" / "companion.yaml"
    if not path.is_file() and path != root / "configs" / "companion.yaml":
        raise ConfigError(f"config file not found: {path}")
    return ConfigSource(
        path=path,
        project_root=root,
        overrides=dict(overrides or {}),
        local_override=local_override,
    )


def load_config(
    path: Path | None = None,
    *,
    project_root: Path | None = None,
    env_file: Path | None = None,
    overrides: dict[str, Any] | None = None,
    local_override: bool = True,
) -> CompanionConfig:
    return config_source(
        path,
        project_root=project_root,
        env_file=env_file,
        overrides=overrides,
        local_override=local_override,
    ).load()
