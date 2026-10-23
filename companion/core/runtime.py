"""Companion runtime: owns providers, storage and live sessions."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from companion import __version__
from companion.agent.base import AgentBackend
from companion.audio.segmenter import UtteranceSegmenter
from companion.audio.vad import VAD, create_vad
from companion.builtin import ENGINES, WorkerSpec, builtin_for, worker_spec
from companion.core.bus import EventBus
from companion.core.config import (
    CompanionConfig,
    changed_settings,
    get_setting,
    needs_restart,
    set_setting,
)
from companion.core.device_logs import DeviceLogs
from companion.core.errors import CompanionError
from companion.core.feed import AdminFeed
from companion.core.logging import kv
from companion.core.metrics import Metrics
from companion.core.persona import Persona, load_persona
from companion.core.recordings import Recording, RecordingStore
from companion.core.session import DEFAULT_ROLES, Device, Outbox, Session
from companion.devices.manager import START_DELAY_S, CameraManager
from companion.devices.registry import DeviceRegistry
from companion.memory.store import EphemeralStore, SQLiteStore, TranscriptStore
from companion.protocol import events as ev
from companion.protocol import make_event
from companion.providers.stt.base import STTProvider
from companion.providers.tts.base import TTSProvider
from companion.providers.vlm.base import VLMProvider
from companion.vision.archive import ImageArchive
from companion.vision.pipeline import LastFrame

log = logging.getLogger(__name__)

# How long a device may take to check and save settings sent to it.
DEVICE_CONFIG_TIMEOUT_S = 15.0
DIAGNOSTICS_TIMEOUT_S = 30.0
UPDATE_TIMEOUT_S = 180.0  # download and unpack on a slow board
BLUETOOTH_TIMEOUT_S = 75.0  # find, pair, trust and connect a speaker
TAILSCALE_TIMEOUT_S = 60.0  # start tailscaled and wait for a login link
TAILSCALE_INSTALL_TIMEOUT_S = 900.0  # ~40 MB over a board's Wi-Fi, unpacked onto its SD card


@dataclass
class Providers:
    """Explicit provider instances; any left as ``None`` are built from config."""

    agent: AgentBackend | None = None
    stt: STTProvider | None = None
    tts: TTSProvider | None = None
    vlm: VLMProvider | None = None
    vad_factory: Any = None  # Callable[[], VAD]
    camera_factory: Any = None  # companion.devices.manager.DeviceFactory
    disabled: set[str] = field(default_factory=set)


def build_store(config: CompanionConfig) -> TranscriptStore:
    """External agents own the transcript and memory; only ``direct`` persists locally."""
    if config.agent.provider == "direct":
        return SQLiteStore(config.storage.data_dir / "companion.db")
    return EphemeralStore()


def build_agent(config: CompanionConfig, store: TranscriptStore) -> AgentBackend:
    if config.agent.provider == "hermes":
        from companion.agent.hermes import HermesBackend

        return HermesBackend(config.agent.hermes)
    if config.agent.provider == "openclaw":
        from companion.agent.openclaw import OpenClawBackend

        return OpenClawBackend(config.agent.openclaw)
    from companion.agent.direct import DirectLLMBackend
    from companion.providers.llm.openai_compatible import OpenAICompatibleLLM

    assert isinstance(store, SQLiteStore)
    return DirectLLMBackend(OpenAICompatibleLLM(config.llm), store)


def build_stt(config: CompanionConfig) -> STTProvider | None:
    if config.stt.provider == "none":
        return None
    from companion.providers.stt.openai_compatible import OpenAICompatibleSTT

    return OpenAICompatibleSTT(config.stt)


def build_tts(config: CompanionConfig) -> TTSProvider | None:
    if config.tts.provider == "none":
        return None
    from companion.providers.tts.openai_compatible import OpenAICompatibleTTS

    return OpenAICompatibleTTS(config.tts)


def build_builtin(name: str, config: CompanionConfig, url: str) -> Any:
    """A client for a running built-in engine: the stt/tts settings, its address."""
    if name == "stt":
        from companion.providers.stt.openai_compatible import OpenAICompatibleSTT

        stt = OpenAICompatibleSTT(
            config.stt.model_copy(
                update={"base_url": url, "api_key_env": None, "model": config.builtin.stt.model}
            )
        )
        stt.name = "builtin_stt"
        return stt
    from companion.providers.tts.openai_compatible import OpenAICompatibleTTS

    tts = OpenAICompatibleTTS(
        config.tts.model_copy(update={"base_url": url, "api_key_env": None, "model": "irodori-tts"})
    )
    tts.name = "builtin_tts"
    return tts


def build_vlm(config: CompanionConfig) -> VLMProvider | None:
    if config.vlm.provider == "none":
        return None
    from companion.providers.vlm.openai_compatible import OpenAICompatibleVLM

    return OpenAICompatibleVLM(config.vlm)


def _brief(result: dict[str, Any]) -> dict[str, Any]:
    return {k: result.get(k) for k in ("ok", "error") if k in result}


class CompanionRuntime:
    def __init__(self, config: CompanionConfig, providers: Providers | None = None) -> None:
        providers = providers or Providers()
        self.config = config
        self.bus = EventBus()
        self.feed = AdminFeed(self.bus, show_text=lambda: self.config.server.dashboard_conversation)
        self.store: TranscriptStore = build_store(config)
        self.persona: Persona = load_persona(config.personas_dir, config.agent.persona)
        self.agent: AgentBackend = providers.agent or build_agent(config, self.store)
        # Built-in engines replace stt/tts while on (not ones passed in, e.g. by tests).
        self.builtin = builtin_for(config.storage.data_dir)
        self._managed = {
            name
            for name in ENGINES
            if getattr(providers, name) is None and name not in providers.disabled
        }
        self._on_builtin = {n for n in self._managed if getattr(config.builtin, n).enabled}
        self._builtin_urls: dict[str, str] = {}
        self._builtin_specs: dict[str, WorkerSpec | None] = {}
        self._builtin_tasks: dict[str, asyncio.Task[None]] = {}
        self._closing: set[asyncio.Task[None]] = set()
        self.stt: STTProvider | None = (
            None
            if "stt" in providers.disabled or "stt" in self._on_builtin
            else providers.stt or build_stt(config)
        )
        self.tts: TTSProvider | None = (
            None
            if "tts" in providers.disabled or "tts" in self._on_builtin
            else providers.tts or build_tts(config)
        )
        self.vlm: VLMProvider | None = (
            None if "vlm" in providers.disabled else providers.vlm or build_vlm(config)
        )
        self.image_archive: ImageArchive | None = (
            ImageArchive(config.storage.data_dir / "vision", config.vision.retention_days)
            if config.vision.store_images
            else None
        )
        # Reads self.config at call time, so new utterances follow live VAD changes.
        self._vad_factory = providers.vad_factory or (lambda: create_vad(self.config.vad))
        self.sessions: dict[str, Session] = {}
        self.devices = DeviceRegistry(config.storage.data_dir / "devices.json")
        self.device_logs = DeviceLogs(config.storage.data_dir / "devices")
        self.recordings = RecordingStore(config.storage.data_dir / "recordings")
        self.frames: dict[str, LastFrame] = {}  # device_id -> newest frame
        self.cameras = CameraManager(providers.camera_factory)
        self.metrics = Metrics(enabled=config.metrics.enabled, window=config.metrics.window)
        self.started_at = time.time()

    async def start(self) -> None:
        self.store.open()
        await self.store.purge_observations(self.config.vision.retention_days)
        if self.image_archive is not None:
            await asyncio.to_thread(self.image_archive.purge)
        await asyncio.to_thread(self.recordings.purge, self.config.stt.keep_audio_days)
        log.info(
            "runtime started",
            extra=kv(
                agent=self.agent.name,
                stt=getattr(self.stt, "name", None),
                tts=getattr(self.tts, "name", None),
                vlm=getattr(self.vlm, "name", None),
                images="stored" if self.image_archive else "not stored",
                transcripts=self.storage_description(),
            ),
        )
        if self.stt is not None and self.config.stt.preload:
            # Load in the background so the HTTP server comes up immediately.
            self._warmup = asyncio.create_task(self._warm_stt())
        self.cameras.apply(self.config, delay=START_DELAY_S)
        # Also stops engines a previous run in this process left on and this one turns off.
        self.schedule_builtin()

    async def _warm_stt(self) -> None:
        assert self.stt is not None
        try:
            await self.stt.warmup()
        except Exception as exc:
            log.error("stt warmup failed", extra=kv(error=str(exc)))

    async def stop(self) -> None:
        await self.cameras.stop()
        for session in list(self.sessions.values()):
            if session.linger is not None:
                session.linger.cancel()
            with contextlib.suppress(Exception):
                await session.close()
        self.sessions.clear()
        warmup = getattr(self, "_warmup", None)
        if warmup is not None:
            warmup.cancel()
        # The engines themselves keep running: a restart in this process takes them
        # over, and they end with the process (companion.builtin.shutdown_all).
        for task in self._builtin_tasks.values():
            task.cancel()
        for provider in (self.agent, self.stt, self.tts, self.vlm):
            if provider is not None:
                with contextlib.suppress(Exception):
                    await provider.aclose()
        self.store.close()
        self.device_logs.close()
        log.info("runtime stopped")

    # -- built-in speech engines (companion.builtin) ----------------------------

    def schedule_builtin(self, *names: str, force: bool = False) -> None:
        """Bring the built-in engines in line with the config, in the background.

        Only an engine whose settings changed is touched (or every one named,
        with ``force``); a start still under way for it is cancelled first.
        """
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        for name in names or ENGINES:
            if name not in self._managed:
                continue
            spec = worker_spec(name, self.config)
            if not force and name in self._builtin_specs and self._builtin_specs[name] == spec:
                continue
            self._builtin_specs[name] = spec
            running = self._builtin_tasks.get(name)
            if running is not None:
                running.cancel()
            self._builtin_tasks[name] = loop.create_task(self._apply_engine(name))

    def uses_builtin(self, name: str) -> bool:
        """Whether ``name`` (stt or tts) is the running built-in engine right now."""
        return name in self._on_builtin and getattr(self, name) is not None

    async def builtin_settled(self, name: str) -> None:
        """Wait until the latest change to engine ``name`` is done (started or stopped)."""
        task = self._builtin_tasks.get(name)
        if task is not None:
            with contextlib.suppress(asyncio.CancelledError):
                await asyncio.shield(task)

    async def apply_builtin(self) -> None:
        """Bring every built-in engine in line with the config and wait for them."""
        await asyncio.gather(*(self._apply_engine(n) for n in ENGINES if n in self._managed))

    async def _apply_engine(self, name: str) -> None:
        engine = self.builtin.engines[name]
        spec = worker_spec(name, self.config)
        try:
            if spec is None:
                if name in self._on_builtin:  # back to what stt/tts.provider says
                    self._use(
                        name, build_stt(self.config) if name == "stt" else build_tts(self.config)
                    )
                await engine.stop()
                return
            url = await engine.ensure(spec, self.config.builtin.start_timeout_s)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.error("builtin engine failed", extra=kv(engine=name, error=str(exc)))
            url = None
        if url is None:
            self._use(name, None, builtin=True)
        elif self._builtin_urls.get(name) != url or getattr(self, name) is None:
            self._use(name, build_builtin(name, self.config, url), builtin=True)
            self._builtin_urls[name] = url

    def _use(self, name: str, provider: Any, *, builtin: bool = False) -> None:
        old = getattr(self, name)
        if old is provider:
            return
        setattr(self, name, provider)
        if builtin:
            self._on_builtin.add(name)
        else:
            self._on_builtin.discard(name)
            self._builtin_urls.pop(name, None)
        if old is not None:
            task = asyncio.get_running_loop().create_task(old.aclose())
            self._closing.add(task)
            task.add_done_callback(self._closing.discard)
        log.info("speech provider", extra=kv(kind=name, provider=getattr(provider, "name", None)))

    def new_vad(self) -> VAD:
        return self._vad_factory()

    def new_segmenter(self) -> UtteranceSegmenter:
        return UtteranceSegmenter(self.new_vad(), self.config.vad)

    async def open_session(
        self,
        outbox: Outbox,
        *,
        session_id: str | None = None,
        device_id: str | None = None,
        speak: bool = True,
    ) -> tuple[Session, bool]:
        """A standalone session for one request (HTTP); not joinable."""
        sid, resumed = await self.store.ensure_session(session_id, device_id)
        return Session(self, sid, outbox, device_id=device_id, speak=speak), resumed

    async def connect(
        self,
        outbox: Outbox,
        *,
        session_id: str | None = None,
        device_id: str | None = None,
        roles: frozenset[str] = DEFAULT_ROLES,
        join: bool = False,
    ) -> tuple[Session, Device, bool]:
        """Attach a realtime device to a live session, or start/resume one.

        Returns ``(session, device, resumed)``.
        """
        if join and not session_id:
            if not self.sessions:
                raise CompanionError("there is no active session to join", code="no_session")
            session_id = next(reversed(self.sessions))  # most recently started
        live = self.sessions.get(session_id) if session_id else None
        if live is not None:
            if live.linger is not None:  # a device came back in time
                live.linger.cancel()
                live.linger = None
            device = live.attach(outbox, device_id=device_id, roles=roles)
            return live, device, True
        sid, resumed = await self.store.ensure_session(session_id, device_id)
        session = Session(self, sid, outbox, device_id=device_id, roles=roles)
        self.sessions[sid] = session
        return session, session.devices[0], resumed

    async def notice(
        self, kind: str, payload: dict[str, Any], *, session_id: str | None = None
    ) -> None:
        """Tell observers (the dashboard's events page) about something the server did."""
        await self.bus.publish(make_event(f"notice.{kind}", payload, session_id=session_id))

    async def disconnect(self, session: Session, device: Device) -> None:
        """Detach a device; the session ends with its last mic/speaker device.

        It lingers for ``server.session_linger_s`` first, so a device that drops
        off the network and reconnects resumes the same conversation.
        """
        if device.device_id and self.devices.get(device.device_id) is not None:
            with contextlib.suppress(OSError):  # "last seen" in the dashboard
                self.devices.update(device.device_id, last_seen=time.time())
        await self.notice(
            "device_left",
            {"device_id": device.device_id, "roles": sorted(device.roles)},
            session_id=session.session_id,
        )
        if not await session.detach(device):
            return
        linger = self.config.server.session_linger_s
        if linger <= 0:
            await self._end(session)
            return
        loop = asyncio.get_running_loop()
        session.linger = loop.call_later(
            linger, lambda: loop.create_task(self._end_if_still_away(session))
        )
        log.info("session lingering", extra=kv(session=session.session_id, seconds=linger))

    async def _end_if_still_away(self, session: Session) -> None:
        session.linger = None
        if not session.has_conversation_device and self.sessions.get(session.session_id) is session:
            await self._end(session)

    async def _end(self, session: Session) -> None:
        await session.end("conversation ended")
        await self.close_session(session)

    async def close_session(self, session: Session) -> None:
        await session.close()
        current = self.sessions.get(session.session_id)
        if current is session:
            del self.sessions[session.session_id]
        if current is None or current is session:
            # Not persistent: the transcript lives only as long as its connection.
            await self.store.forget(session.session_id)

    def reconfigure(self, new: CompanionConfig) -> tuple[list[str], list[str]]:
        """Take the live settings of ``new`` now; return (applied, needing a restart).

        ``new`` is a fully loaded (path-resolved) config. Settings that need a
        restart keep their running values until then.
        """
        running = self.config.model_dump(mode="json")
        target = new.model_dump(mode="json")
        changed = changed_settings(running, target)
        applied = [p for p in changed if not needs_restart(p)]
        pending = [p for p in changed if needs_restart(p)]
        if applied:
            for path in applied:
                set_setting(running, path, get_setting(target, path))
            config = CompanionConfig.model_validate(running)
            if "agent.persona" in applied:
                self.persona = load_persona(config.personas_dir, config.agent.persona)
            self.config = config
            for session in self.sessions.values():
                session.reconfigure(self.config)
            if "cameras" in applied:
                self.cameras.apply(self.config)
            if any(p.split(".")[0] == "builtin" for p in applied):
                self.schedule_builtin()
            log.info("settings applied", extra=kv(settings=",".join(applied)))
        return applied, pending

    async def disconnect_device(self, device_id: str) -> int:
        """Close every connection of ``device_id`` (e.g. its token was revoked)."""
        closed = 0
        for session in list(self.sessions.values()):
            for device in list(session.devices):
                if device.device_id == device_id:
                    await device.outbox.close()
                    closed += 1
        return closed

    def find_device(self, device_id: str) -> tuple[Session, Device] | None:
        for session in self.sessions.values():
            for device in session.devices:
                if device.device_id == device_id:
                    return session, device
        return None

    async def _ask_device(
        self, device_id: str, type_: str, payload: dict[str, Any], timeout_s: float
    ) -> tuple[Session, dict[str, Any]]:
        """Send ``type_`` to a connected device and wait for its answer (by request_id)."""
        found = self.find_device(device_id)
        if found is None:
            raise CompanionError(f"device {device_id!r} is not connected", code="not_connected")
        session, device = found
        request_id = uuid.uuid4().hex[:12]
        answer: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        device.settings_requests[request_id] = answer
        try:
            await session.emit_to([device], type_, {"request_id": request_id, **payload})
            return session, await asyncio.wait_for(answer, timeout_s)
        finally:
            device.settings_requests.pop(request_id, None)

    async def device_diagnostics(self, device_id: str) -> dict[str, Any]:
        """Ask a device (protocol 1.2) for its diagnostics; TimeoutError if it does not answer."""
        _, result = await self._ask_device(
            device_id, ev.DEVICE_DIAGNOSTICS_REQUEST, {}, DIAGNOSTICS_TIMEOUT_S
        )
        sections = result.get("sections")
        return sections if isinstance(sections, dict) else {}

    def remember_settings(self, device_id: str | None, config: dict[str, Any]) -> None:
        """Keep an enrolled device's settings, to restore them after a reflash."""
        if device_id and self.devices.get(device_id) is not None:
            with contextlib.suppress(OSError):
                self.devices.update(device_id, settings=config)

    async def update_device(self, device_id: str) -> dict[str, Any]:
        """Offer a device this server's edge client; returns its device.update.result."""
        from companion import edge_install

        version = edge_install.edge_version()
        sha256 = hashlib.sha256(edge_install.edge_package()).hexdigest()
        session, result = await self._ask_device(
            device_id, ev.DEVICE_UPDATE, {"version": version, "sha256": sha256}, UPDATE_TIMEOUT_S
        )
        await self.notice(
            "device_update",
            {"device_id": device_id, "version": version, **_brief(result)},
            session_id=session.session_id,
        )
        return result

    async def device_updated(self, device: Device, payload: dict[str, Any]) -> None:
        """A device came back with a new version after device.update."""
        version = payload.get("version")
        if device.device_id and self.devices.get(device.device_id) is not None:
            with contextlib.suppress(OSError):
                self.devices.update(device.device_id, installed_version=version)
        await self.notice(
            "device_updated",
            {
                "device_id": device.device_id,
                "version": version,
                "previous": payload.get("previous"),
            },
        )
        log.info("device updated", extra=kv(device=device.device_id, version=version))

    async def restart_device(self, device_id: str, what: str) -> dict[str, Any]:
        _, result = await self._ask_device(
            device_id, ev.DEVICE_RESTART, {"what": what}, DEVICE_CONFIG_TIMEOUT_S
        )
        await self.notice("device_restart", {"device_id": device_id, "what": what})
        return result

    async def device_wifi(
        self, device_id: str, add: list[dict[str, Any]], remove: list[str]
    ) -> dict[str, Any]:
        """Change (or with nothing to change, list) a device's Wi-Fi networks.

        ``add`` holds {ssid, password}; only the WPA key derived from it goes to the device.
        """
        from companion import edge_install

        networks = [
            {
                "ssid": n["ssid"],
                "psk": edge_install.wpa_psk(n["ssid"], n["password"])
                if n.get("password")
                else None,
            }
            for n in add
        ]
        _, result = await self._ask_device(
            device_id,
            ev.DEVICE_WIFI_SET,
            {"add": networks, "remove": remove},
            DEVICE_CONFIG_TIMEOUT_S,
        )
        if add or remove:
            await self.notice(
                "device_wifi",
                {
                    "device_id": device_id,
                    "added": [n["ssid"] for n in add],
                    "removed": remove,
                    **_brief(result),
                },
            )
        return result

    async def device_bluetooth(
        self, device_id: str, action: str, *, address: str | None = None, seconds: float = 10.0
    ) -> dict[str, Any]:
        """A device's Bluetooth: state, scan, pair (its speaker from now on) or remove."""
        payload: dict[str, Any] = {"action": action}
        if address:
            payload["address"] = address
        if action == "scan":
            payload["seconds"] = seconds
        timeout = seconds + 20 if action == "scan" else BLUETOOTH_TIMEOUT_S
        _, result = await self._ask_device(device_id, ev.DEVICE_BLUETOOTH, payload, timeout)
        if action in ("pair", "remove"):
            await self.notice(
                "device_bluetooth",
                {"device_id": device_id, "action": action, "address": address, **_brief(result)},
            )
        return result

    async def device_tailscale(
        self, device_id: str, action: str, *, auth_key: str | None = None
    ) -> dict[str, Any]:
        """A device's Tailscale: state, up (installing it first if needed) or logout.

        The server's own Tailscale address goes along, so a device on the tailnet
        adds it to the addresses it tries (and reaches this server when away).
        """
        from companion import edge_install, tailscale

        addresses = await asyncio.to_thread(edge_install.tailscale_addresses)
        common: dict[str, Any] = {}
        if addresses:
            common["server_url"] = f"ws://{addresses[0]}:{self.config.server.port}/v1/realtime"
        payload: dict[str, Any] = {"action": action, **common}
        timeout = TAILSCALE_TIMEOUT_S
        if action == "up":
            _, state = await self._ask_device(
                device_id,
                ev.DEVICE_TAILSCALE,
                {"action": "state", **common},
                DEVICE_CONFIG_TIMEOUT_S,
            )
            if not state.get("ok"):
                return state
            if not state.get("installed"):
                arch = state.get("arch")
                if not arch:
                    raise CompanionError(
                        f"Tailscale has no build for this board ({state.get('machine')})",
                        code="unsupported",
                    )
                package = await tailscale.package(str(arch), self.config.storage.data_dir)
                payload.update(action="install", package=package.name, sha256=package.sha256)
                timeout = TAILSCALE_INSTALL_TIMEOUT_S
            if auth_key:
                payload["auth_key"] = auth_key
        _, result = await self._ask_device(device_id, ev.DEVICE_TAILSCALE, payload, timeout)
        if result.get("ok") and self.devices.get(device_id) is not None:
            # Remembered for the Boards page (an update keeps it on the tailnet).
            self.devices.update(device_id, tailscale=tailscale.board_address(result))
        if action in ("up", "logout"):
            await self.notice(
                "device_tailscale",
                {
                    "device_id": device_id,
                    "action": payload["action"],
                    "state": result.get("state"),
                    **_brief(result),
                },
            )
        return result

    async def speaker_test(self, device_id: str, text: str) -> None:
        found = self.find_device(device_id)
        if found is None:
            raise CompanionError(f"device {device_id!r} is not connected", code="not_connected")
        session, device = found
        if "speaker" not in device.roles:
            raise CompanionError(f"device {device_id!r} has no speaker", code="no_speaker")
        await session.say_test(device, text)

    async def mic_test(self, device_id: str, seconds: float) -> Recording:
        """Record ``seconds`` of what the device's microphone sends, as a recording."""
        found = self.find_device(device_id)
        if found is None:
            raise CompanionError(f"device {device_id!r} is not connected", code="not_connected")
        session, device = found
        if session.mic_device is not device:
            raise CompanionError(
                f"device {device_id!r} sends no microphone audio (is audio.input_enabled off?)",
                code="no_mic",
            )
        tap: list[np.ndarray] = []
        session.mic_taps.append(tap)
        try:
            await asyncio.sleep(seconds)
        finally:
            session.mic_taps.remove(tap)
        audio = np.concatenate(tap) if tap else np.zeros(0, dtype=np.float32)
        if not audio.size:
            raise CompanionError("no microphone audio arrived from the device", code="no_audio")
        return await asyncio.to_thread(
            self.recordings.save,
            audio,
            session_id=session.session_id,
            device_id=device_id,
            text="",
            outcome="test",
        )

    async def live_view(self, device_id: str, seconds: float, fps: float) -> None:
        """Ask a camera device for pictures in a row while someone watches (camera.live)."""
        found = self.find_device(device_id)
        if found is None:
            raise CompanionError(f"device {device_id!r} is not connected", code="not_connected")
        session, device = found
        if "camera" not in device.roles:
            raise CompanionError(
                f"device {device_id!r} has no camera turned on (its camera.enabled setting)",
                code="no_camera",
            )
        await session.emit_to([device], ev.CAMERA_LIVE, {"seconds": seconds, "fps": fps})

    async def capture(self, device_id: str) -> None:
        """Ask a camera device for a fresh frame now."""
        found = self.find_device(device_id)
        if found is None:
            raise CompanionError(f"device {device_id!r} is not connected", code="not_connected")
        session, device = found
        if "camera" not in device.roles:
            raise CompanionError(
                f"device {device_id!r} has no camera turned on (its camera.enabled setting)",
                code="no_camera",
            )
        await session.emit_to(
            [device],
            ev.VISION_CAPTURE_REQUEST,
            {"request_id": uuid.uuid4().hex[:12], "reason": "dashboard"},
        )

    async def configure_device(self, device_id: str, config: dict[str, Any]) -> dict[str, Any]:
        """Send new settings to a connected device; return its device.config.result.

        Raises TimeoutError if the device does not answer in DEVICE_CONFIG_TIMEOUT_S.
        """
        found = self.find_device(device_id)
        if found is None:
            raise CompanionError(f"device {device_id!r} is not connected", code="not_connected")
        if found[1].settings is None:
            raise CompanionError(
                f"device {device_id!r} does not offer remote settings", code="not_configurable"
            )
        session, result = await self._ask_device(
            device_id, ev.DEVICE_CONFIG_SET, {"config": config}, DEVICE_CONFIG_TIMEOUT_S
        )
        await self.notice(
            "device_settings",
            {
                "device_id": device_id,
                "ok": result.get("ok"),
                "restarting": result.get("restarting"),
                "error": result.get("error"),
            },
            session_id=session.session_id,
        )
        log.info(
            "device settings sent",
            extra=kv(device=device_id, ok=result.get("ok"), restarting=result.get("restarting")),
        )
        return result

    def storage_description(self) -> str:
        if isinstance(self.store, SQLiteStore):
            return str(self.store.path)
        return f"in-memory only (owned by {self.config.agent.provider})"

    async def status(self, *, probe: bool = False) -> dict[str, Any]:
        out: dict[str, Any] = {
            "version": __version__,
            "uptime_s": round(time.time() - self.started_at, 1),
            "active_sessions": len(self.sessions),
            "sessions": [
                {"session_id": s.session_id, "state": s.state.value} for s in self.sessions.values()
            ],
            "devices": [
                {"session_id": s.session_id, **d.report()}
                for s in self.sessions.values()
                for d in s.devices
            ],
            "stored_sessions": await self.store.session_count(),
            "transcripts": self.storage_description(),
            "persona": self.persona.name,
            "providers": {
                "agent": self.agent.name,
                "stt": getattr(self.stt, "name", None),
                "stt_device": getattr(self.stt, "device", None),
                "tts": getattr(self.tts, "name", None),
                "vlm": getattr(self.vlm, "name", None),
            },
            "builtin": {name: engine.status.state for name, engine in self.builtin.engines.items()},
            "vision": {
                "mode": self.config.vision.mode,
                "enabled": self.config.vision.enabled
                and (self.config.vision.mode == "agent" or self.vlm is not None),
                "store_images": self.image_archive is not None,
                "active_sessions": sum(s.vision.enabled for s in self.sessions.values()),
            },
        }
        if probe:
            out["agent_health"] = await self.agent.health()
        return out
