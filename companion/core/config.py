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
    provider: Literal["faster_whisper", "none"] = "faster_whisper"
    model: str = "large-v3-turbo"
    language: str | None = "ja"
    device: Literal["auto", "cuda", "cpu"] = "auto"
    compute_type: str = "auto"
    beam_size: int = Field(default=1, ge=1)
    preload: bool = True


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
    # Frames are downscaled (longer side, pixels) and re-encoded before sending.
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
    # "user": prepend new observations to the user's message (keeps the agent's
    # prompt cache intact); "system": add recent ones to the system prompt.
    inject: Literal["user", "system"] = "user"
    # A turn waits this long for a frame that is still being analyzed.
    wait_for_pending_s: float = Field(default=3.0, ge=0)
    # Images are never written to disk unless enabled here.
    store_images: bool = False
    # Retention of stored observations (direct backend) and stored images.
    retention_days: float = Field(default=7.0, gt=0)


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
