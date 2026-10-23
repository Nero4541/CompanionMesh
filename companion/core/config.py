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
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)

# This installation's folder, wherever the server is started from: its configs/,
# .env and data/ are the ones used, never those of the current folder.
PROJECT_ROOT = Path(__file__).resolve().parents[2]


class ConfigError(Exception):
    """Configuration is missing or invalid."""


class _Section(BaseModel):
    # Each field's docstring is its explanation in the JSON schema, which the
    # dashboard shows under the setting; write them for someone new to this.
    model_config = ConfigDict(extra="forbid", use_attribute_docstrings=True)


class PublicListenerConfig(_Section):
    """A second, locked-down entrance for boards outside your home network.

    Use it when a board connects over the internet (a router port forward or
    IPv6). It serves only what boards need, accepts only boards this server set
    up (each with its own token), encrypts everything with TLS, and never serves
    the dashboard or the dev client. See docs/remote-access.md.
    """

    enabled: bool = False
    """Turn the public entrance on. Leave off if every board is on your home network or on
    Tailscale.
    """
    hosts: list[str] = Field(default_factory=lambda: ["0.0.0.0", "::"])
    """Network addresses to listen on. "0.0.0.0" means every IPv4 address of this machine and "::"
    every IPv6 address; one this machine lacks is skipped.
    """
    port: int = Field(default=8766, ge=1, le=65535)
    """Port number of the public entrance. Forward this port on your router to this machine."""
    url: str | None = None
    """The address boards use to reach this entrance from outside, e.g.
    wss://home.example.net:8766/v1/realtime (a dynamic DNS name or your public IP). The board
    installer gives it to the boards.
    """
    cert_file: Path | None = None
    """Optional: your own TLS certificate file (e.g. from Let's Encrypt) instead of the one this
    server makes itself. Boards then check it against the name in url. Leave empty to use the
    generated one.
    """
    key_file: Path | None = None
    """The private key that belongs to cert_file. Only needed together with cert_file."""
    trusted_proxy: bool = False
    """Turn on only when a reverse proxy or tunnel (e.g. Cloudflare Tunnel) on this machine forwards
    the connections. The real client address is then read from the proxy's headers, so the limits
    below apply to the right address.
    """
    max_failed_auth: int = Field(default=10, ge=1)
    """How many wrong tokens one address may send within ban_s before it is blocked. Protects
    against someone guessing tokens.
    """
    ban_s: float = Field(default=900.0, gt=0)
    """Seconds an address stays blocked after too many wrong tokens (900 = 15 minutes)."""
    max_connections_per_address: int = Field(default=4, ge=1)
    """The most connections one address may have open at the same time."""

    @field_validator("url")
    @classmethod
    def _wss(cls, v: str | None) -> str | None:
        if v and not v.startswith(("wss://", "ws://")):
            raise ValueError("a ws:// or wss:// URL, e.g. wss://home.example.net:8766/v1/realtime")
        return v or None


class ServerConfig(_Section):
    """Where the server listens, who may connect, and which web pages it serves."""

    host: str = "127.0.0.1"
    """Network address the server listens on. 127.0.0.1 accepts only this computer; 0.0.0.0 accepts
    every device on your network (boards, phones), which requires auth_token_env to be set.
    """
    port: int = Field(default=8765, ge=1, le=65535)
    """Port number of the server. Devices and the dashboard connect to http://<this machine>:<port>.
    """
    dev_client: bool = True
    """Serve the browser test client at /dev/, for talking to the companion from a web browser
    without a board.
    """
    auth_token_env: str | None = "COMPANION_TOKEN"
    """Name of the .env variable holding the password (token) every device must send. Required when
    host is not 127.0.0.1. Put the token itself in .env, not here.
    """
    session_linger_s: float = Field(default=60.0, ge=0)
    """Seconds a conversation is kept after its microphone or speaker disconnects, so a device that
    reconnects (e.g. after a Wi-Fi hiccup) carries on where it left off.
    """
    dashboard: bool = True
    """Serve this admin dashboard at /admin/. Turning it off hides this page after the next restart.
    """
    admin_token_env: str | None = "COMPANION_ADMIN_TOKEN"
    """Name of the .env variable holding the dashboard password. While it is not set, the dashboard
    only opens on this computer. The device token never opens the dashboard.
    """
    dashboard_conversation: bool = False
    """Show what was said (your words and the replies) on the dashboard. Off, the dashboard sees
    only how long they were, for privacy.
    """
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
    """What the server writes to its console and log."""

    level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    """How much detail to log. DEBUG: everything, for troubleshooting, also every web request.
    INFO: normal (includes what was said; web requests only when they fail). WARNING: only
    problems, so no transcripts. ERROR: only failures.
    """
    format: Literal["text", "json"] = "text"
    """text is easy to read; json is one JSON object per line, for log collection tools."""


class StorageConfig(_Section):
    """Where the server keeps its own files."""

    # String defaults (validated into Path) keep the JSON schema's defaults
    # serializable; pydantic cannot encode a WindowsPath default.
    data_dir: Path = Field(default="data", validate_default=True)
    """Folder for the server's data: the conversation database (direct backend), kept recordings,
    set-up boards, certificates. Relative paths start at the project folder. Keep it private.
    """


class HttpProviderConfig(_Section):
    """Shared fields for providers reached over HTTP."""

    base_url: str
    """Web address of the service."""
    api_key_env: str | None = None
    """Name of the .env variable holding this service's API key. Leave empty if it needs none."""
    connect_timeout: float = Field(default=5.0, gt=0)
    """Seconds to wait for the service to answer the connection before giving up."""
    timeout: float = Field(default=120.0, gt=0)
    """Seconds to wait for a whole answer before giving up."""

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
    """Seconds to wait for the agent's whole reply. Agents that use tools can take a while."""
    model: str
    """Model name sent to the agent."""
    session_prefix: str = "companion-"
    """Text put in front of the conversation id the agent sees, so these conversations are easy to
    recognise among the agent's other ones.
    """
    extra_body: dict[str, Any] = Field(default_factory=dict)
    """Extra fields added to every request, as JSON, e.g. {"max_tokens": 200} for shorter replies.
    The basic fields (model, messages, stream, user) cannot be changed here.
    """


class HermesConfig(GatewayAgentConfig):
    """Hermes Agent: used when agent.provider is hermes. It keeps the conversations and long-term
    memory on its own machine.
    """

    base_url: str = "http://127.0.0.1:8642/v1"
    """Web address of the Hermes API, ending in /v1. May be another machine on your network or on
    Tailscale.
    """
    api_key_env: str | None = "HERMES_API_KEY"
    """Name of the .env variable holding the Hermes API key."""
    model: str = "hermes-agent"
    """Model name sent to Hermes. Hermes chooses the real model itself; keep hermes-agent unless you
    changed it there.
    """
    memory_scope: str | None = "companion"
    """Name of the long-term memory Hermes uses for the companion (sent as X-Hermes-Session-Key).
    Every conversation with the same name shares one memory; empty = no shared memory.
    """


class OpenClawConfig(GatewayAgentConfig):
    """OpenClaw: used when agent.provider is openclaw. Needs
    gateway.http.endpoints.chatCompletions.enabled: true in openclaw.json.
    """

    base_url: str = "http://127.0.0.1:18789/v1"
    """Web address of the OpenClaw gateway, ending in /v1. May be another machine on your network or
    on Tailscale.
    """
    api_key_env: str | None = "OPENCLAW_GATEWAY_TOKEN"
    """Name of the .env variable holding the gateway token (gateway.auth.token, or its password, in
    openclaw.json).
    """
    model: str = "openclaw"
    """Model name sent to OpenClaw. Keep openclaw unless you set up something else there."""
    agent_id: str = "main"
    """Which OpenClaw agent answers (its id in openclaw.json)."""


class AgentConfig(_Section):
    """The brain that writes the replies."""

    provider: Literal["hermes", "openclaw", "direct"] = "hermes"
    """Who writes the replies. hermes or openclaw: an agent framework (settings below) that
    remembers things and can use tools. direct: no framework; the server talks to the model in
    llm itself and keeps the memory in its own database.
    """
    persona: str = "default"
    """Which character to use: the name of a file in the personas folder (without .yaml). Edit
    personas on the Personas page.
    """
    history_turns: int = Field(default=20, ge=0)
    """How many earlier exchanges (your message plus its reply) are sent along with each new
    message, so the companion remembers the conversation. More = better memory but slower and
    costlier; 0 = none.
    """
    hermes: HermesConfig = HermesConfig()
    openclaw: OpenClawConfig = OpenClawConfig()


class LLMConfig(HttpProviderConfig):
    """The language model for agent.provider direct. Hermes and OpenClaw bring their own."""

    provider: Literal["openai_compatible"] = "openai_compatible"
    """How to talk to the model. openai_compatible works with Ollama, llama.cpp, vLLM, LM Studio and
    cloud APIs.
    """
    base_url: str = "http://127.0.0.1:11434/v1"
    """Web address of the model's API, ending in /v1 (11434 is Ollama's port)."""
    api_key_env: str | None = "LLM_API_KEY"
    """Name of the .env variable holding the API key. Local servers usually need none."""
    model: str = "default"
    """Model name as the server knows it, e.g. qwen3:8b on Ollama or gpt-4o-mini on OpenAI."""
    temperature: float | None = None
    """Creativity, usually 0 to 1: lower is steadier, higher is more varied. Empty = the server's
    default.
    """
    max_tokens: int | None = None
    """Longest reply, in tokens (roughly words or characters). Empty = no limit."""
    extra_body: dict[str, Any] = Field(default_factory=dict)
    """Server-specific fields added to every request, as JSON. For example, to stop Qwen3 thinking
    on llama.cpp or vLLM: {"chat_template_kwargs": {"enable_thinking": false}}
    """


def strip_endpoint(url: str, endpoint: str) -> str:
    """``url`` without a trailing ``endpoint``: providers add it themselves."""
    trimmed = url.rstrip("/")
    return trimmed[: -len(endpoint)] if trimmed.endswith(endpoint) else url


class STTConfig(_Section):
    """Hearing: speech recognition turns what you say into text."""

    provider: Literal["openai_compatible", "none"] = "openai_compatible"
    """openai_compatible: a speech recognition server at base_url, your own (speaches, the
    whisper.cpp server) or a service (OpenAI, Groq). none: no speech input, text only. When
    builtin.stt is on, it is used instead.
    """
    model: str = "large-v3-turbo"
    """Model name the speech recognition server expects."""
    language: str | None = "ja"
    """Language you speak, as a code: ja Japanese, en English, zh Chinese, yue Cantonese. Empty =
    detect it each time (slower and less reliable).
    """
    preload: bool = True
    """Ask the speech recognition server for one tiny transcript when the server starts, so it
    loads its model and the first sentence is not slow. Off: it loads on first use.
    """
    prompt: str | None = None
    """A short sample sentence that nudges recognition toward a writing style or words, e.g. for
    Cantonese in traditional characters: 以下係廣東話對話，用香港繁體中文書寫。
    Names you use often help too. Keep it short.
    """
    base_url: str = "http://127.0.0.1:8000/v1"
    """Web address of the speech recognition server, ending in /v1."""
    api_key_env: str | None = "STT_API_KEY"
    """Name of the .env variable holding the speech recognition server's API key. Local servers
    usually need none.
    """
    connect_timeout: float = Field(default=5.0, gt=0)
    """Seconds to wait for the speech recognition server to answer the connection."""
    timeout: float = Field(default=60.0, gt=0)
    """Seconds to wait for a transcript."""
    keep_audio: bool = False
    """Save each sentence as heard (a WAV file in data/recordings) with its transcript, so you can
    check recognition on the Speech page. Off by default for privacy.
    """
    keep_audio_days: float = Field(default=3.0, gt=0)
    """Days saved recordings are kept before they are deleted."""

    @field_validator("base_url")
    @classmethod
    def _no_endpoint(cls, v: str) -> str:
        return strip_endpoint(v, "/audio/transcriptions")

    def api_key(self) -> str | None:
        if not self.api_key_env:
            return None
        return os.environ.get(self.api_key_env) or None


class VADConfig(_Section):
    """Voice activity detection: decides when you start and stop talking, so each sentence is sent
    at the right moment.
    """

    provider: Literal["silero", "energy"] = "silero"
    """silero: a small model that tells speech from noise well (recommended). energy: plain
    loudness, simpler but fooled by noise.
    """
    threshold: float = Field(default=0.5, gt=0, lt=1)
    """silero only: how sure (0 to 1) it must be that a sound is speech. Higher ignores more noise
    but may miss quiet speech.
    """
    energy_threshold: float = Field(default=0.015, gt=0, lt=1)
    """energy only: loudness (0 to 1) above which sound counts as speech. Raise it in a noisy room.
    """
    min_speech_ms: int = Field(default=250, ge=0)
    """Milliseconds of speech needed before it counts as talking, so short noises (a cough, a click)
    are ignored.
    """
    min_silence_ms: int = Field(default=1000, ge=0)
    """Milliseconds of silence that end a sentence. Lower answers sooner but may cut you off
    mid-sentence when you pause.
    """
    speech_pad_ms: int = Field(default=200, ge=0)
    """Milliseconds of audio kept before and after the speech, so the first and last sounds are not
    clipped.
    """
    max_utterance_s: float = Field(default=30.0, gt=0)
    """Longest single sentence, in seconds; longer speech is cut and sent in parts."""


class TTSConfig(_Section):
    """Speaking: the companion's voice, which reads its replies aloud."""

    provider: Literal["openai_compatible", "none"] = "openai_compatible"
    """openai_compatible: a speech server at base_url (e.g. Irodori-TTS-Server). none: text replies
    only, no voice. When builtin.tts is on, it is used instead.
    """
    base_url: str = "http://127.0.0.1:8088/v1"
    """Web address of the speech server, ending in /v1 (8088 is Irodori-TTS-Server's default)."""
    api_key_env: str | None = "TTS_API_KEY"
    """Name of the .env variable holding the speech server's API key. Local servers usually need
    none.
    """
    connect_timeout: float = Field(default=5.0, gt=0)
    """Seconds to wait for the speech server to answer the connection."""
    timeout: float = Field(default=120.0, gt=0)
    """Seconds to wait for the audio of one sentence."""
    model: str = "irodori-tts"
    """Model name the speech server expects."""
    voice: str | None = None
    """Which voice to use: a voice id from the speech server (for Irodori, a reference clip in its
    voices folder) or a file in builtin/tts/voices. "none" or empty = the model's own voice. The
    persona can choose a voice too.
    """
    response_format: Literal["wav", "mp3", "opus", "flac", "aac", "pcm"] = "wav"
    """Audio format asked from the speech server. wav works everywhere; keep it unless your server
    needs another.
    """
    speed: float | None = None
    """Speaking speed: 1 is normal, 1.2 faster, 0.8 slower. Empty = the server's default."""
    extra_body: dict[str, Any] = Field(default_factory=dict)
    """Server-specific fields added to every request, as JSON. For Irodori, {"irodori": {"seed":
    1234}} keeps the voice the same every time.
    """

    @field_validator("base_url")
    @classmethod
    def _no_endpoint(cls, v: str) -> str:
        return strip_endpoint(v, "/audio/speech")

    def api_key(self) -> str | None:
        if not self.api_key_env:
            return None
        return os.environ.get(self.api_key_env) or None


class BuiltinSTTConfig(_Section):
    """Built-in hearing: Whisper speech recognition, installed and run by the server in builtin/stt.
    stt.language and stt.prompt still apply.
    """

    enabled: bool = False
    """On: use it instead of stt.provider. Off: its process stops and its graphics memory is freed
    at once.
    """
    model: str = "large-v3-turbo"
    """A model folder in builtin/stt/models, a Whisper size (tiny, small, medium, large-v3,
    large-v3-turbo) or a Hugging Face name (owner/name). One that is not there yet is downloaded
    first.
    """
    device: Literal["auto", "cuda", "cpu"] = "auto"
    """Where it runs: cuda = NVIDIA graphics card (fast), cpu = processor (slow), auto = the
    graphics card when there is one.
    """
    compute_type: str = "auto"
    """Number precision, e.g. float16, int8_float16 or int8. Lower uses less memory, slightly less
    accurate. auto suits the device.
    """
    beam_size: int = Field(default=1, ge=1)
    """How many guesses are compared. 1 is fastest; 5 is a little more accurate but slower."""


class BuiltinTTSConfig(_Section):
    """Built-in voice: Irodori-TTS (Japanese), installed and run by the server in builtin/tts.

    Voices (reference clips, .speaker.safetensors, .pt) go in builtin/tts/voices;
    tts.voice picks one.
    """

    enabled: bool = False
    """On: use it instead of tts.provider. Off: its process stops and its graphics memory is freed
    at once.
    """
    checkpoint: str = "Aratako/Irodori-TTS-v4.1-Small-MF"
    """A model folder in builtin/tts/models (holding model.safetensors) or a Hugging Face name
    (owner/name), downloaded first if it is not there yet. Models ending in MF need 4 steps
    instead of 40, so they are much faster.
    """
    device: Literal["auto", "cuda", "cpu"] = "auto"
    """Where it runs: cuda = NVIDIA graphics card (fast), cpu = processor (slow), auto = the
    graphics card when there is one.
    """
    precision: Literal["fp32", "bf16"] = "fp32"
    """Number precision. bf16 uses half the graphics memory but needs an NVIDIA card; fp32 works
    everywhere.
    """


class BuiltinConfig(_Section):
    """Speech engines the server installs and runs itself, so you need no separate speech servers.
    The Speech page installs them and turns them on and off.
    """

    libraries: Literal["auto", "cuda", "cpu"] = "auto"
    """Which libraries to install: cuda for an NVIDIA graphics card, cpu for none. auto chooses cuda
    when it finds an NVIDIA card.
    """
    start_timeout_s: float = Field(default=900.0, gt=0)
    """Seconds an engine may take to start. The first start downloads the model, which can take a
    while.
    """
    stt: BuiltinSTTConfig = BuiltinSTTConfig()
    tts: BuiltinTTSConfig = BuiltinTTSConfig()


class VLMConfig(HttpProviderConfig):
    """A vision model that describes camera pictures in words. Used only when vision.mode is vlm."""

    provider: Literal["openai_compatible", "none"] = "none"
    """openai_compatible: a vision model server at base_url (llama.cpp with --mmproj, vLLM, LM
    Studio, Ollama, or a cloud API). none: no vision model.
    """
    base_url: str = "http://127.0.0.1:8080/v1"
    """Web address of the vision model's API, ending in /v1."""
    api_key_env: str | None = "VLM_API_KEY"
    """Name of the .env variable holding the API key. Local servers usually need none."""
    timeout: float = Field(default=60.0, gt=0)
    """Seconds to wait for a picture's description."""
    model: str = "default"
    """Model name as the server knows it."""
    max_image_side: int = Field(default=768, ge=64, le=4096)
    """Pictures are shrunk so their longer side is at most this many pixels before sending (in both
    vision modes). Smaller is faster; larger shows more detail.
    """
    max_tokens: int = Field(default=300, ge=16)
    """Longest description, in tokens (roughly words)."""
    prompt: str | None = None
    """Your own instruction for describing pictures, replacing the built-in one; {language} is
    replaced by the persona's language. Empty = built-in.
    """
    extra_body: dict[str, Any] = Field(default_factory=dict)
    """Server-specific fields added to every request, as JSON."""


class VisionConfig(_Section):
    """Camera pictures, so the companion can see. Each conversation still has to turn vision on."""

    enabled: bool = True
    """Master switch for pictures from cameras and devices. Off, all pictures are refused."""
    max_frame_bytes: int = Field(default=4 * 1024 * 1024, ge=1024)
    """Largest picture accepted, in bytes (4194304 = 4 MB)."""
    max_pixels: int = Field(default=16_000_000, ge=64 * 64)
    """Largest picture accepted, in pixels (width × height)."""
    min_interval_s: float = Field(default=2.0, ge=0)
    """At least this many seconds between pictures taken from one conversation; extra ones are
    skipped. Pictures you send by hand are always taken.
    """
    dedup: bool = True
    """Skip pictures that look the same as the last one, to save work."""
    dedup_threshold: int = Field(default=6, ge=0, le=64)
    """How different (0 to 64) a picture must be from the last one to count as new. Higher skips
    more pictures.
    """
    observation_max_age_s: float = Field(default=300.0, gt=0)
    """What was seen more than this many seconds ago is no longer mentioned to the companion."""
    max_context_observations: int = Field(default=3, ge=0)
    """How many recent things seen (picture descriptions and camera events) are given to the
    companion with a message. 0 = none.
    """
    mode: Literal["agent", "vlm"] = "agent"
    """agent: send the picture itself with your next message; the agent's model must understand
    images. vlm: the vision model in vlm describes the picture and the description is sent as
    text.
    """
    max_turn_images: int = Field(default=1, ge=1, le=16)
    """agent mode: the most pictures sent with one message. When more arrive, the least important
    (then oldest) are dropped.
    """
    inject: Literal["user", "system"] = "user"
    """Where new descriptions go: user puts them in front of your message (faster, keeps the agent's
    cache); system puts recent ones in the system prompt.
    """
    capture_on_turn: bool = True
    """When you speak or type, ask the cameras for a fresh picture so the companion sees the present
    moment.
    """
    wait_for_pending_s: float = Field(default=3.0, ge=0)
    """Seconds a reply waits for that fresh picture (or its description) before going ahead without
    it.
    """
    store_images: bool = False
    """Save pictures to disk (data/vision). Off by default for privacy: pictures stay in memory
    only.
    """
    retention_days: float = Field(default=7.0, gt=0)
    """Days saved pictures and saved descriptions (direct backend) are kept before they are deleted.
    """


_HHMM = r"^([01]\d|2[0-3]):[0-5]\d$"


class QuietHoursConfig(_Section):
    """Hours when the companion never starts talking by itself. Answering you is not affected."""

    enabled: bool = True
    """Turn quiet hours on."""
    start: str = Field(default="23:00", pattern=_HHMM)
    """When quiet hours begin, as HH:MM in 24-hour time."""
    end: str = Field(default="08:00", pattern=_HHMM)
    """When quiet hours end, as HH:MM. May be earlier than start (overnight)."""
    timezone: str | None = None
    """Time zone name such as Asia/Tokyo or Asia/Hong_Kong. Empty = this computer's time zone."""

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
    """When the companion may start talking by itself, e.g. when you come home or after a long
    silence. See docs/proactive.md.
    """

    proactive: bool = True
    """Allow the companion to start talking by itself. Normal conversation works either way."""
    min_interval_s: float = Field(default=900.0, ge=0)
    """At least this many seconds between remarks it makes by itself (900 = 15 minutes)."""
    max_per_hour: int = Field(default=4, ge=0)
    """The most remarks it may make by itself in one hour."""
    quiet_hours: QuietHoursConfig = QuietHoursConfig()
    grace_after_activity_s: float = Field(default=120.0, ge=0)
    """Stay quiet this many seconds after anyone spoke, so it does not interrupt."""
    scene_change_threshold: int = Field(default=20, ge=1, le=64)
    """How different (1 to 64) a camera picture must be to count as something happening. Higher
    reacts only to bigger changes.
    """
    absence_s: float = Field(default=1200.0, gt=0)
    """A change after the camera view stayed still this many seconds counts as someone coming back.
    """
    idle: bool = False
    """Check in after a silence: when nobody has spoken for a random time between idle_min_s and
    idle_max_s, the companion may say something.
    """
    idle_min_s: float = Field(default=1800.0, gt=0)
    """Shortest silence, in seconds, before an idle check-in (1800 = 30 minutes)."""
    idle_max_s: float = Field(default=3600.0, gt=0)
    """Longest silence, in seconds, before an idle check-in (3600 = 1 hour)."""
    event_dedup_s: float = Field(default=120.0, ge=0)
    """The same kind of event again within this many seconds is ignored."""
    remember_threshold: float = Field(default=0.2, ge=0, le=1)
    """Each event gets an importance score from 0 to 1. Below this it is ignored."""
    context_threshold: float = Field(default=0.4, ge=0, le=1)
    """Importance from which an event is told to the companion as background (for its next reply)
    instead of only being noted.
    """
    speak_threshold: float = Field(default=0.6, ge=0, le=1)
    """Importance from which the companion may speak up about an event (if the limits above allow
    it).
    """
    debug: bool = False
    """Send every decision and its reasons to connected clients, to see why it spoke or stayed
    quiet.
    """


class EchoConfig(_Section):
    """Stopping the companion from hearing its own voice through your microphone."""

    canceller: Literal["none", "nlms"] = "none"
    """Echo removal done by the server, for microphones without their own. none: rely on the guard
    below. nlms: subtract the companion's voice from the microphone audio (a basic version).
    """
    filter_ms: int = Field(default=64, ge=8, le=512)
    """nlms: length of the echo it can remove, in milliseconds. Larger handles echoey rooms but
    needs more processing.
    """
    max_delay_ms: int = Field(default=600, ge=0, le=2000)
    """nlms: the longest delay, in milliseconds, between sending audio to the speaker and hearing it
    in the microphone.
    """
    guard: bool = True
    """Drop anything heard that repeats what the companion just said: its own voice leaking from the
    speaker.
    """
    guard_similarity: float = Field(default=0.6, gt=0, le=1)
    """How similar (0 to 1) heard text must be to the companion's recent words to be dropped. Lower
    drops more.
    """
    guard_window_s: float = Field(default=20.0, gt=0)
    """How many seconds back the guard compares against."""


class RealtimeConfig(_Section):
    """Natural back-and-forth: interrupting the companion, and keeping devices in step."""

    barge_in: bool = True
    """Let you interrupt: when you start talking while the companion speaks, it stops. Needs a
    device that keeps listening while it plays audio.
    """
    barge_in_min_speech_ms: int = Field(default=600, ge=0)
    """On devices without echo removal: milliseconds you must speak before it counts as
    interrupting, so its own voice does not stop it.
    """
    barge_in_threshold: float = Field(default=0.8, gt=0, lt=1)
    """On devices without echo removal: how sure (0 to 1) it must be that you are talking to count
    as interrupting.
    """
    transcribe_timeout_s: float = Field(default=60.0, gt=0)
    """Safety limit: if turning speech into text takes longer than this many seconds, something went
    wrong and it gives up.
    """
    interrupt_timeout_s: float = Field(default=3.0, gt=0)
    """Seconds to wait for a device to confirm it stopped playing after an interruption."""
    playback_slack_s: float = Field(default=8.0, ge=0)
    """If a device never confirms it finished playing, the companion stops counting as speaking this
    many seconds after the audio should have ended.
    """
    ping_interval_s: float = Field(default=10.0, ge=0)
    """Seconds between checks of each device's delay and clock, shown on the Latency page. 0 = off.
    """
    echo: EchoConfig = EchoConfig()


class CameraConfig(_Section):
    """An IP camera or phone the server watches itself (instead of ``companion camera``).

    It joins the active conversation as a camera device. Credentials come from
    ``.env`` through ``username_env``/``password_env``, never from the URL.
    """

    id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,64}$")
    """A short name for the camera (letters, digits, - and _), e.g. livingroom."""
    url: str = Field(min_length=1)
    """The camera's picture address: an HTTP snapshot (.../shot.jpg), an MJPEG stream (.../video) or
    rtsp://. No password in it.
    """
    enabled: bool = True
    """Watch this camera."""
    username_env: str | None = "CAMERA_USERNAME"
    """Name of the .env variable holding the camera's user name."""
    password_env: str | None = "CAMERA_PASSWORD"
    """Name of the .env variable holding the camera's password."""
    mode: Literal["change", "periodic"] = "change"
    """change: send a picture when the view changes. periodic: send one every interval_s."""
    interval_s: float = Field(default=5.0, ge=0)
    """periodic: seconds between pictures."""
    heartbeat_s: float = Field(default=300.0, gt=0)
    """change: send a routine picture at least this often, even when nothing changes."""
    analysis_interval_s: float = Field(default=1.0, gt=0)
    """change: seconds between looks for changes."""
    session_id: str | None = None
    """Join this conversation instead of the most recently started one."""
    enable_vision: bool = True
    """Switch vision on in the conversation it joins."""

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
    """Timing measurements for the Latency page: numbers only, never audio, pictures or text."""

    enabled: bool = True
    """Measure how long each step (hearing, thinking, speaking) takes."""
    window: int = Field(default=200, ge=10)
    """How many recent measurements per step the averages on the Latency page are based on."""
    emit_turn: bool = False
    """Also send each reply's timings to connected clients (metrics.turn), for debugging."""


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
    """IP cameras and phones the server watches itself. Easier to manage on the Cameras page."""
    personas_dir: Path = Field(default="personas", validate_default=True)
    """Folder holding the persona files (one .yaml per character). Relative paths start at the
    project folder.
    """

    @model_validator(mode="before")
    @classmethod
    def _whisper_in_server(cls, data: Any) -> Any:
        """Settings of 1.4.0 and older: stt.provider faster_whisper ran Whisper inside the server.
        builtin.stt runs it now, so that turns it on with the same model and settings.
        """
        if not isinstance(data, dict) or not isinstance(data.get("stt"), dict):
            return data
        stt = dict(data["stt"])
        moved = {key: stt.pop(key) for key in ("device", "compute_type", "beam_size") if key in stt}
        if stt.get("provider") != "faster_whisper":
            return {**data, "stt": stt}
        stt["provider"] = "none"
        if "model" in stt:
            moved["model"] = stt["model"]
        builtin = dict(data.get("builtin") or {})
        builtin["stt"] = {**(builtin.get("stt") or {}), **moved, "enabled": True}
        return {**data, "stt": stt, "builtin": builtin}

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
    root = (project_root or PROJECT_ROOT).resolve()
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
