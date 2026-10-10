"""Configuration loading.

Non-secret behaviour lives in YAML (``configs/companion.yaml``). An optional
sibling ``*.local.yaml`` file is deep-merged on top for machine-specific
overrides and is never committed. Secrets live in ``.env``; config refers to
them by environment-variable *name* (``api_key_env``), never by value.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Literal

import yaml
from dotenv import load_dotenv
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator


class ConfigError(Exception):
    """Configuration is missing or invalid."""


class _Section(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ServerConfig(_Section):
    host: str = "127.0.0.1"
    port: int = Field(default=8765, ge=1, le=65535)
    dev_client: bool = True
    # Token every client must present; required when host is not loopback.
    auth_token_env: str | None = "COMPANION_TOKEN"
    # Keep a session this long after its last mic/speaker device drops, so a
    # device that reconnects (e.g. after a Wi-Fi hiccup) resumes it.
    session_linger_s: float = Field(default=60.0, ge=0)

    def auth_token(self) -> str | None:
        if not self.auth_token_env:
            return None
        return os.environ.get(self.auth_token_env) or None


class LoggingConfig(_Section):
    level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    format: Literal["text", "json"] = "text"


class StorageConfig(_Section):
    data_dir: Path = Path("data")


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


class STTConfig(_Section):
    provider: Literal["faster_whisper", "openai_compatible", "none"] = "faster_whisper"
    model: str = "large-v3-turbo"
    language: str | None = "ja"
    device: Literal["auto", "cuda", "cpu"] = "auto"
    compute_type: str = "auto"
    beam_size: int = Field(default=1, ge=1)
    preload: bool = True
    # openai_compatible only: POST {base_url}/audio/transcriptions on another host.
    base_url: str = "http://127.0.0.1:8000/v1"
    api_key_env: str | None = "STT_API_KEY"
    connect_timeout: float = Field(default=5.0, gt=0)
    timeout: float = Field(default=60.0, gt=0)

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

    def api_key(self) -> str | None:
        if not self.api_key_env:
            return None
        return os.environ.get(self.api_key_env) or None


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
    vlm: VLMConfig = VLMConfig()
    vision: VisionConfig = VisionConfig()
    attention: AttentionConfig = AttentionConfig()
    realtime: RealtimeConfig = RealtimeConfig()
    metrics: MetricsConfig = MetricsConfig()
    personas_dir: Path = Path("personas")

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


def load_config(
    path: Path | None = None,
    *,
    project_root: Path | None = None,
    env_file: Path | None = None,
    overrides: dict[str, Any] | None = None,
    local_override: bool = True,
) -> CompanionConfig:
    root = (project_root or Path.cwd()).resolve()
    env_path = env_file or root / ".env"
    if env_path.is_file():
        load_dotenv(env_path, override=False, encoding="utf-8")

    if path is None:
        env_cfg = os.environ.get("COMPANION_CONFIG")
        path = Path(env_cfg) if env_cfg else root / "configs" / "companion.yaml"

    data: dict[str, Any] = {}
    if path.is_file():
        data = _read_yaml(path)
        local = local_override_path(path)
        if local_override and local.is_file():
            data = deep_merge(data, _read_yaml(local))
    elif path != root / "configs" / "companion.yaml":
        raise ConfigError(f"config file not found: {path}")

    if overrides:
        data = deep_merge(data, overrides)

    try:
        config = CompanionConfig.model_validate(data)
    except ValidationError as exc:
        lines = [f"  {'.'.join(map(str, e['loc']))}: {e['msg']}" for e in exc.errors()]
        raise ConfigError(f"invalid configuration ({path}):\n" + "\n".join(lines)) from exc
    return config.resolve_paths(root)
