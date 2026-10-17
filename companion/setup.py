"""``companion setup``: first-run questions that write ``.env`` and the local config.

Asks for what the server cannot guess (the agent's address and key, speech,
whether devices connect over the network), tests the agent, and saves:
secrets into ``.env`` under the names the config already uses, everything
else into ``configs/companion.local.yaml`` as the dashboard would. Running it
again shows the current values as defaults, so each step can be kept.
"""

from __future__ import annotations

import asyncio
import getpass
import secrets
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

import httpx

from companion.core.config import ConfigSource, get_setting, set_setting
from companion.core.config_edit import ConfigEditError, save_settings
from companion.core.envfile import read_values, set_values

AGENTS = ("hermes", "openclaw", "direct")
AGENT_LABELS = {
    "hermes": "Hermes Agent (its API server, usually http://127.0.0.1:8642/v1)",
    "openclaw": "OpenClaw (its gateway, usually http://127.0.0.1:18789/v1)",
    "direct": "No agent framework: any OpenAI-compatible LLM (LM Studio, Ollama, llama.cpp)",
}
LANGUAGES = {
    "ja": "Japanese",
    "yue": "Cantonese",
    "zh": "Mandarin",
    "en": "English",
    "ko": "Korean",
    "auto": "detect per utterance",
}


class Prompter(Protocol):
    def say(self, text: str = "") -> None: ...

    def ask(self, question: str, default: str = "") -> str: ...

    def secret(self, question: str, *, has_value: bool) -> str | None: ...

    def choose(self, question: str, options: dict[str, str], default: str) -> str: ...

    def confirm(self, question: str, default: bool = True) -> bool: ...


class ConsolePrompter:
    def say(self, text: str = "") -> None:
        print(text)

    def ask(self, question: str, default: str = "") -> str:
        shown = f" [{default}]" if default else ""
        answer = input(f"{question}{shown}: ").strip()
        return answer or default

    def secret(self, question: str, *, has_value: bool) -> str | None:
        """The typed value, or None to keep the current one (Enter)."""
        hint = " (Enter keeps the current one)" if has_value else " (Enter for none)"
        answer = getpass.getpass(f"{question}{hint}: ").strip()
        return answer or None

    def choose(self, question: str, options: dict[str, str], default: str) -> str:
        keys = list(options)
        print(question)
        for number, key in enumerate(keys, 1):
            mark = "*" if key == default else " "
            print(f"  {mark}{number}. {options[key]}")
        while True:
            answer = input(f"Choose 1-{len(keys)} [{keys.index(default) + 1}]: ").strip()
            if not answer:
                return default
            if answer.isdigit() and 1 <= int(answer) <= len(keys):
                return keys[int(answer) - 1]
            if answer in options:
                return answer

    def confirm(self, question: str, default: bool = True) -> bool:
        answer = input(f"{question} [{'Y/n' if default else 'y/N'}]: ").strip().lower()
        return default if not answer else answer.startswith("y")


@dataclass
class ProbeResult:
    ok: bool
    message: str


async def probe_openai_api(base_url: str, key: str | None, *, wait_s: float = 5.0) -> ProbeResult:
    """Reach ``{base_url}/models``: does the endpoint answer, and take the key?"""
    headers = {"Authorization": f"Bearer {key}"} if key else {}
    url = f"{base_url.rstrip('/')}/models"
    try:
        async with httpx.AsyncClient(timeout=wait_s) as client:
            response = await client.get(url, headers=headers)
    except httpx.HTTPError as exc:
        return ProbeResult(False, f"cannot connect to {url}: {exc or type(exc).__name__}")
    if response.status_code in (401, 403):
        return ProbeResult(False, "it answered but refused the key")
    if response.status_code == 200:
        try:
            models = [m.get("id") for m in response.json().get("data", [])]
        except (ValueError, AttributeError):
            models = []
        named = f": {', '.join(str(m) for m in models[:5])}" if models else ""
        return ProbeResult(True, f"ok ({len(models)} model(s){named})")
    return ProbeResult(
        True, f"reachable (HTTP {response.status_code}); the key is checked on the first reply"
    )


def _token() -> str:
    return secrets.token_urlsafe(32)


class Setup:
    def __init__(
        self,
        source: ConfigSource,
        prompter: Prompter,
        *,
        env_path: Path | None = None,
        probe: Any = probe_openai_api,
    ) -> None:
        self.source = source
        self.p = prompter
        self.env_path = env_path or source.project_root / ".env"
        self.example = source.project_root / ".env.example"
        self.probe = probe
        self.env = read_values(self.env_path)
        self.secrets: dict[str, str] = {}
        self.settings: dict[str, Any] = source.saved().model_dump(mode="json")
        self.install: list[str] = []  # built-in engines to install after saving

    # -- helpers -------------------------------------------------------------

    def get(self, path: str) -> Any:
        return get_setting(self.settings, path)

    def put(self, path: str, value: Any) -> None:
        set_setting(self.settings, path, value)

    def has_secret(self, name: str | None) -> bool:
        return bool(name and (self.secrets.get(name) or self.env.get(name)))

    def secret_value(self, name: str | None) -> str | None:
        if not name:
            return None
        return self.secrets.get(name) or self.env.get(name) or None

    def ask_secret(self, name: str | None, question: str) -> None:
        if not name:
            return
        value = self.p.secret(
            f"{question} (saved in .env as {name})", has_value=self.has_secret(name)
        )
        if value is not None:
            self.secrets[name] = value

    # -- steps -----------------------------------------------------------------

    def tokens(self) -> None:
        for path, what in (
            ("server.auth_token_env", "device token (browsers, boards, cameras)"),
            ("server.admin_token_env", "admin token (dashboard from other machines)"),
        ):
            name = self.get(path)
            if name and not self.has_secret(name):
                self.secrets[name] = _token()
                self.p.say(f"Created a {what} in .env ({name}).")

    def agent(self) -> None:
        self.p.say("\n== Agent ==")
        provider = self.p.choose("Which agent answers?", AGENT_LABELS, self.get("agent.provider"))
        self.put("agent.provider", provider)
        section = "llm" if provider == "direct" else f"agent.{provider}"
        while True:
            url = self.p.ask("Its OpenAI-compatible URL", self.get(f"{section}.base_url"))
            self.put(f"{section}.base_url", url.rstrip("/"))
            key_env = self.get(f"{section}.api_key_env")
            label = {
                "hermes": "API_SERVER_KEY from ~/.hermes/.env",
                "openclaw": "gateway.auth.token from openclaw.json",
            }.get(provider, "API key")
            self.ask_secret(key_env, f"Key ({label})")
            if provider == "openclaw":
                self.put(
                    "agent.openclaw.agent_id",
                    self.p.ask("OpenClaw agent id", self.get("agent.openclaw.agent_id")),
                )
            if provider == "direct":
                self.put("llm.model", self.p.ask("Model name", self.get("llm.model")))
            result = asyncio.run(self.probe(url, self.secret_value(key_env)))
            self.p.say(f"  agent: {result.message}")
            if result.ok or not self.p.confirm("Change these settings?", True):
                return

    def speech(self) -> None:
        self.p.say("\n== Speech ==")
        gpu = shutil.which("nvidia-smi") is not None
        where = " (NVIDIA GPU found)" if gpu else " (no NVIDIA GPU: on the CPU)"
        options = {
            "builtin": f"Built-in: Whisper, installed and run by the companion{where}",
            "openai_compatible": "A speech-to-text server elsewhere (OpenAI-compatible)",
            "none": "None (text only)",
        }
        current = "builtin" if self.get("builtin.stt.enabled") else self.get("stt.provider")
        if current == "faster_whisper":  # kept for setups made before the built-in engines
            options["faster_whisper"] = (
                "Whisper inside the server (uv sync --extra stt --extra cuda)"
            )
        stt = self.p.choose("Speech recognition", options, current)
        self.put("builtin.stt.enabled", stt == "builtin")
        # Built-in: nothing else, so turning it off in the dashboard frees the GPU.
        self.put("stt.provider", "none" if stt == "builtin" else stt)
        if stt == "openai_compatible":
            self.put("stt.base_url", self.p.ask("Its URL", self.get("stt.base_url")))
            self.ask_secret(self.get("stt.api_key_env"), "Key, if it needs one")
        if stt in ("builtin", "faster_whisper") and not gpu:
            path = "builtin.stt.model" if stt == "builtin" else "stt.model"
            if self.get(path) == "large-v3-turbo" and self.p.confirm(
                "No GPU: use the smaller 'small' model?", True
            ):
                self.put(path, "small")
        if stt == "builtin":
            self.install.append("stt")
        if stt != "none":
            current = self.get("stt.language") or "auto"
            language = self.p.choose(
                "Language you speak", LANGUAGES, current if current in LANGUAGES else "auto"
            )
            self.put("stt.language", None if language == "auto" else language)

        tts = self.p.choose(
            "Voice (text-to-speech)",
            {
                "builtin": "Built-in: Irodori-TTS, a Japanese voice, installed and run by the "
                "companion" + ("" if gpu else " (slow without an NVIDIA GPU)"),
                "openai_compatible": "A text-to-speech server elsewhere (OpenAI-compatible)",
                "none": "None (text replies only)",
            },
            "builtin" if self.get("builtin.tts.enabled") else self.get("tts.provider"),
        )
        self.put("builtin.tts.enabled", tts == "builtin")
        self.put("tts.provider", "none" if tts == "builtin" else tts)
        if tts == "openai_compatible":
            self.put("tts.base_url", self.p.ask("Its URL", self.get("tts.base_url")))
            self.put("tts.model", self.p.ask("Model", self.get("tts.model")))
            self.put(
                "tts.voice",
                self.p.ask("Voice (Enter for its default)", self.get("tts.voice") or "") or None,
            )
            self.ask_secret(self.get("tts.api_key_env"), "Key, if it needs one")
        if tts == "builtin":
            self.install.append("tts")

    def install_builtin(self) -> bool:
        """Install the built-in engines chosen above (uv; several GB the first time)."""
        if not self.install:
            return True
        from companion.builtin import Engine, install_here

        config = self.source.load()
        missing = [
            name
            for name in self.install
            if Engine(name, config.storage.data_dir).installed() is None
        ]
        if not missing:
            return True
        names = " and ".join({"stt": "speech recognition", "tts": "voice"}[n] for n in missing)
        if not self.p.confirm(
            f"\nInstall the built-in {names} now? It downloads several GB the first time.", True
        ):
            self.p.say("  Later: Speech in the dashboard installs it, with a button.")
            return True
        for name in missing:
            self.p.say(f"\nInstalling the built-in {name} (data/builtin/{name}) ...")
            if install_here(name, config) != 0:
                self.p.say("  Installing failed; try again from Speech in the dashboard.")
                return False
        self.p.say("Installed. The models download when the server first starts them.")
        return True

    def network(self) -> None:
        self.p.say("\n== Devices ==")
        listening = self.get("server.host") not in ("127.0.0.1", "localhost", "::1")
        if self.p.confirm(
            "Will boards, phones or other computers connect to this server?", listening
        ):
            if not listening:
                self.put("server.host", "0.0.0.0")
            self.p.say(
                "  The server will listen on every network of this machine, "
                "with the device token required."
            )
        else:
            self.put("server.host", "127.0.0.1")
            self.put("server.public.enabled", False)
            return
        self.remote()

    def remote(self) -> None:
        """The public listener, for boards away from home (see README)."""
        enabled = bool(self.get("server.public.enabled"))
        if not self.p.confirm(
            "Should boards reach this server away from home (mobile data, other Wi-Fi)?", enabled
        ):
            self.put("server.public.enabled", False)
            return
        port = self.p.ask("Port for devices from outside", str(self.get("server.public.port")))
        self.put("server.public.port", int(port) if port.isdigit() else 8766)
        port = str(self.get("server.public.port"))
        current = self.get("server.public.url") or ""
        address = self.p.ask(
            "Your home's address on the Internet: a dynamic DNS name (e.g. myhome.duckdns.org) "
            "or the public IP address",
            current,
        ).strip()
        if address and "://" not in address:
            host = f"[{address}]" if ":" in address else address
            address = f"wss://{host}:{port}/v1/realtime"
        self.put("server.public.enabled", True)
        self.put("server.public.url", address or None)
        from companion.edge_install import lan_addresses

        here = (lan_addresses() or ["<this computer's address>"])[0]
        for line in (
            "",
            "  Boards then try home first, then this address, with TLS and this server's own",
            "  certificate (set them up with  uv run companion edge install  or the dashboard).",
            "  Still to do by hand:",
            f"  1. Router, IPv4: forward TCP port {port} to {here}.",
            f"     IPv6: allow incoming TCP port {port} to this computer only (no other ports).",
            "  2. Firewall of this computer, as administrator:",
            '     Windows: New-NetFirewallRule -DisplayName "Companion devices" -Direction Inbound'
            f" -Protocol TCP -LocalPort {port} -Action Allow -Profile Private",
            f"     Linux:   sudo ufw allow {port}/tcp",
            "  3. Check from outside: on your phone with Wi-Fi off, open",
            f"     https://<your address>:{port}/health  (the browser warns about the certificate,",
            "     which is expected: it is this server's own). It should say status ok.",
            "  Only devices set up by this server get in, and the dashboard is never on that port.",
        ):
            self.p.say(line)

    def save(self) -> bool:
        try:
            path = save_settings(self.source, self.settings)
        except ConfigEditError as exc:
            for error in exc.errors:
                self.p.say(f"  cannot save {error['path'] or 'settings'}: {error['message']}")
            return False
        if self.secrets:
            set_values(self.env_path, self.secrets, example=self.example)
        self.p.say(f"\nSaved {path.name}" + (" and .env." if self.secrets else "."))
        return True

    def run(self) -> int:
        self.p.say("Companion Server setup. Enter keeps the value in [brackets].")
        self.tokens()
        self.agent()
        self.speech()
        self.network()
        if not self.save():
            return 1
        installed = self.install_builtin()
        self.p.say("Next: uv run companion check   (tests every part), then start the server.")
        return 0 if installed else 1


def run(source: ConfigSource, prompter: Prompter | None = None) -> int:
    try:
        return Setup(source, prompter or ConsolePrompter()).run()
    except (KeyboardInterrupt, EOFError):
        print("\nSetup cancelled; nothing was saved.")
        return 1
