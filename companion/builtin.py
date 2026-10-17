"""Built-in speech: STT and TTS engines the server installs and runs itself.

Each engine is a small project under ``builtin/<name>`` with its own locked
dependencies, installed with uv into ``data/builtin/<name>/venv`` and run as a
child process that serves the OpenAI audio API on 127.0.0.1. The runtime talks
to it as to any other OpenAI-compatible server.

Turning an engine off ends its process, which frees all of its GPU memory at
once. An engine also exits by itself when the server goes away, however it
ends: it waits on the server's process id. The engines belong to the process,
not to a runtime, so an in-process restart keeps a running engine whose
settings did not change.
"""

from __future__ import annotations

import asyncio
import atexit
import contextlib
import hashlib
import json
import logging
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

from companion.core.config import CompanionConfig
from companion.core.errors import CompanionError
from companion.core.logging import kv

log = logging.getLogger(__name__)

PROJECTS = Path(__file__).resolve().parents[1] / "builtin"
ENGINES = ("stt", "tts")
LOG_LINES = 200
STOP_WAIT_S = 5.0


def gpu_found() -> bool:
    return shutil.which("nvidia-smi") is not None


def resolve_libraries(config: CompanionConfig) -> str:
    """``cuda`` or ``cpu``: which libraries an install brings."""
    choice = config.builtin.libraries
    if choice == "auto":
        return "cuda" if gpu_found() else "cpu"
    return choice


def _nvidia_smi(query: str) -> list[list[str]]:
    try:
        out = subprocess.run(
            ["nvidia-smi", query, "--format=csv,noheader,nounits"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    return [[cell.strip() for cell in line.split(",")] for line in out.splitlines() if line]


@dataclass
class GpuMemory:
    # Processes on a GPU, with their memory in MB where the driver tells it
    # (Windows' WDDM driver does not: None).
    processes: dict[int, int | None] = field(default_factory=dict)
    used_mb: int | None = None  # all GPUs, all programs
    total_mb: int | None = None


def gpu_memory() -> GpuMemory:
    """What nvidia-smi says about GPU memory (nothing without an NVIDIA GPU)."""
    memory = GpuMemory()
    if not gpu_found():
        return memory
    for pid, mb in (row for row in _nvidia_smi("--query-compute-apps=pid,used_memory") if row):
        with contextlib.suppress(ValueError):
            used = int(mb) if mb.isdigit() else None
            known = memory.processes.get(int(pid))
            memory.processes[int(pid)] = None if used is None else (known or 0) + used
    for row in _nvidia_smi("--query-gpu=memory.used,memory.total"):
        with contextlib.suppress(ValueError, IndexError):
            memory.used_mb = (memory.used_mb or 0) + int(row[0])
            memory.total_mb = (memory.total_mb or 0) + int(row[1])
    return memory


def _uv() -> str:
    found = os.environ.get("UV") or shutil.which("uv")
    if not found:
        raise CompanionError(
            "uv was not found; install it from https://docs.astral.sh/uv/", code="no_uv"
        )
    return found


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@dataclass(frozen=True)
class WorkerSpec:
    """How to start an engine; a running engine with another spec is restarted."""

    args: tuple[str, ...] = ()
    env: tuple[tuple[str, str], ...] = ()
    libraries: str = "cpu"


def worker_spec(name: str, config: CompanionConfig) -> WorkerSpec | None:
    """The engine ``name`` as the config wants it, or None when it is off."""
    libraries = resolve_libraries(config)
    if name == "stt":
        stt = config.builtin.stt
        if not stt.enabled:
            return None
        return WorkerSpec(
            args=(
                "--model",
                stt.model,
                "--device",
                stt.device,
                "--compute-type",
                stt.compute_type,
                "--beam-size",
                str(stt.beam_size),
            ),
            libraries=libraries,
        )
    tts = config.builtin.tts
    if not tts.enabled:
        return None
    return WorkerSpec(
        env=(
            ("IRODORI_HF_CHECKPOINT", tts.checkpoint),
            ("IRODORI_MODEL_DEVICE", tts.device),
            ("IRODORI_CODEC_DEVICE", tts.device),
            ("IRODORI_MODEL_PRECISION", tts.precision),
            ("IRODORI_PRELOAD", "true"),
            ("IRODORI_ALLOW_NO_REF_VOICE", "true"),
        ),
        libraries=libraries,
    )


@dataclass
class EngineStatus:
    # off | not_installed | installing | starting | running | failed
    state: str = "off"
    error: str | None = None
    since: float = field(default_factory=time.time)

    def set(self, state: str, error: str | None = None) -> None:
        self.state, self.error, self.since = state, error, time.time()


class Engine:
    """One built-in engine: its environment, its process, its state."""

    def __init__(self, name: str, data_dir: Path) -> None:
        self.name = name
        self.project = PROJECTS / name
        self.home = data_dir / "builtin" / name
        self.venv = self.home / "venv"
        self.status = EngineStatus()
        self.log: deque[str] = deque(maxlen=LOG_LINES)
        self.url: str | None = None
        self.parent_pid = os.getpid()  # the worker exits when this process ends
        self._process: subprocess.Popen[bytes] | None = None
        self._spec: WorkerSpec | None = None
        self._lock = asyncio.Lock()

    # -- installation ------------------------------------------------------

    @property
    def python(self) -> Path:
        if sys.platform == "win32":
            return self.venv / "Scripts" / "python.exe"
        return self.venv / "bin" / "python"

    @property
    def _marker(self) -> Path:
        return self.home / "installed.json"

    def _lock_hash(self) -> str:
        return hashlib.sha256((self.project / "uv.lock").read_bytes()).hexdigest()

    def _install_info(self) -> dict[str, Any] | None:
        try:
            info = json.loads(self._marker.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return info if isinstance(info, dict) and self.python.is_file() else None

    def installed(self) -> dict[str, Any] | None:
        """What is installed (libraries, when), if it matches this version's lock."""
        info = self._install_info()
        return info if info and info.get("lock") == self._lock_hash() else None

    def install_command(self, libraries: str) -> list[str]:
        """``uv sync`` of this engine's project into its environment."""
        if libraries == "cuda":
            extras = ["--extra", "cuda"]
        else:  # TTS needs PyTorch's CPU build named; STT runs on CPU without extras
            extras = ["--extra", "cpu"] if self.name == "tts" else []
        return [_uv(), "sync", "--frozen", "--project", str(self.project), *extras]

    def install_env(self) -> dict[str, str]:
        env = {k: v for k, v in os.environ.items() if k != "VIRTUAL_ENV"}
        env["UV_PROJECT_ENVIRONMENT"] = str(self.venv)
        env.setdefault("UV_NATIVE_TLS", "1")  # antivirus or proxies that inspect HTTPS
        return env

    def mark_installed(self, libraries: str) -> None:
        info = {"lock": self._lock_hash(), "libraries": libraries, "installed_at": time.time()}
        self._marker.write_text(json.dumps(info), encoding="utf-8")

    async def install(self, libraries: str) -> None:
        """Install (or bring up to date) this engine's environment, with uv."""
        if self.status.state == "installing":
            raise CompanionError(f"{self.name} is already being installed", code="busy")
        command = self.install_command(libraries)
        self.home.mkdir(parents=True, exist_ok=True)
        self.status.set("installing")
        self.log.clear()
        self.log.append(f"$ uv sync --project builtin/{self.name} ({libraries})")
        log.info("builtin install", extra=kv(engine=self.name, libraries=libraries))
        code = await asyncio.to_thread(self._run_logged, command, self.install_env())
        if code != 0:
            self.status.set("failed", f"installing failed (uv exited with {code}); see the log")
            raise CompanionError(self.status.error or "", code="install_failed")
        self.mark_installed(libraries)
        self.status.set("off")
        log.info("builtin installed", extra=kv(engine=self.name))

    def _run_logged(self, command: list[str], env: dict[str, str]) -> int:
        try:
            process = subprocess.Popen(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                env=env,
            )
        except OSError as exc:
            self.log.append(str(exc))
            return -1
        assert process.stdout is not None
        for raw in process.stdout:
            line = raw.decode("utf-8", "replace").rstrip()
            if line:
                self.log.append(line)
        return process.wait()

    # -- running -----------------------------------------------------------

    @property
    def pid(self) -> int | None:
        return self._process.pid if self._process and self._process.poll() is None else None

    def matches(self, spec: WorkerSpec) -> bool:
        return self.pid is not None and self._spec == spec and self.url is not None

    async def ensure(self, spec: WorkerSpec, start_timeout_s: float) -> str | None:
        """Run the engine as ``spec`` says; its base URL, or None if it cannot run."""
        async with self._lock:
            if self.matches(spec):
                return self.url
            await asyncio.to_thread(self.shutdown)
            if self.status.state == "installing":
                return None
            installed = self.installed()
            earlier = self._install_info()
            if installed is None and earlier and earlier.get("libraries") == spec.libraries:
                # Installed by an earlier version of the server: bring it up to date
                # (uv fetches only what changed).
                with contextlib.suppress(CompanionError):
                    await self.install(spec.libraries)
                installed = self.installed()
                if installed is None:
                    return None
            if installed is None or installed.get("libraries") != spec.libraries:
                self.status.set(
                    "not_installed",
                    None
                    if installed is None
                    else f"installed for {installed.get('libraries')}, not {spec.libraries}",
                )
                return None
            return await self._start(spec, start_timeout_s)

    async def _start(self, spec: WorkerSpec, start_timeout_s: float) -> str | None:
        self.status.set("starting")
        port = _free_port()
        env = {k: v for k, v in os.environ.items() if k != "VIRTUAL_ENV"}
        env.update(spec.env)
        env["IRODORI_VOICES_DIR"] = str(self.home / "voices")
        env["PYTHONUNBUFFERED"] = "1"
        command = [str(self.python), str(self.project / "worker.py"), "--port", str(port)]
        command += ["--parent-pid", str(self.parent_pid)]
        command += spec.args
        self.log.clear()
        log_path = self.home / "worker.log"
        try:
            self._process = await asyncio.to_thread(self._spawn, command, env, log_path)
        except OSError as exc:
            self.status.set("failed", f"cannot start: {exc}")
            return None
        self._spec = spec
        url = f"http://127.0.0.1:{port}"
        log.info("builtin starting", extra=kv(engine=self.name, pid=self._process.pid, port=port))
        deadline = time.monotonic() + start_timeout_s
        async with httpx.AsyncClient(timeout=2.0) as client:
            while time.monotonic() < deadline:
                if self._process.poll() is not None:
                    self._read_log(log_path)
                    tail = next((line for line in reversed(self.log) if line.strip()), "")
                    self.status.set(
                        "failed", f"exited with {self._process.returncode}: {tail[-300:]}"
                    )
                    self._process = None
                    return None
                with contextlib.suppress(httpx.HTTPError):
                    if (await client.get(f"{url}/health")).status_code == 200:
                        self.url = f"{url}/v1"
                        self.status.set("running")
                        log.info("builtin running", extra=kv(engine=self.name, url=self.url))
                        return self.url
                self._read_log(log_path)
                await asyncio.sleep(0.5)
        await asyncio.to_thread(self.shutdown)
        self.status.set("failed", f"did not answer within {start_timeout_s:g} s")
        return None

    def _spawn(
        self, command: list[str], env: dict[str, str], log_path: Path
    ) -> subprocess.Popen[bytes]:
        with log_path.open("wb") as out:
            return subprocess.Popen(
                command,
                cwd=self.home,
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=out,
                stderr=subprocess.STDOUT,
            )

    def _read_log(self, path: Path) -> None:
        with contextlib.suppress(OSError):
            lines = path.read_bytes().decode("utf-8", "replace").splitlines()
            self.log.clear()
            self.log.extend(line for line in lines[-LOG_LINES:] if line.strip())

    async def stop(self) -> None:
        async with self._lock:
            await asyncio.to_thread(self.shutdown)
            if self.status.state in ("running", "starting", "failed"):
                self.status.set("off")

    def shutdown(self) -> None:
        """End the engine's process now (blocking; ``stop`` from async code)."""
        process, self._process = self._process, None
        self.url = None
        self._spec = None
        if process is None or process.poll() is not None:
            return
        with contextlib.suppress(OSError):
            process.terminate()  # Windows: at once; elsewhere SIGTERM, which uvicorn honours
        try:
            process.wait(timeout=STOP_WAIT_S)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
        log.info("builtin stopped", extra=kv(engine=self.name, pid=process.pid))

    def report(self, enabled: bool, gpu: GpuMemory) -> dict[str, Any]:
        if self.status.state == "running" and self.pid is None:
            self.status.set("failed", "the engine stopped by itself; see the log")
            self.url = None
        if self.status.state == "running":
            self._read_log(self.home / "worker.log")
        pid = self.pid
        return {
            "enabled": enabled,
            "state": self.status.state,
            "error": self.status.error,
            "since": self.status.since,
            "installed": self.installed(),
            "pid": pid,
            "url": self.url,
            # Linux reports it; on Windows the worker runs under the venv's launcher
            # (another pid) and WDDM reports no per-process memory: the total tells.
            "vram_mb": gpu.processes.get(pid) if pid else None,
            "log": list(self.log)[-40:],
        }


class Builtin:
    """The built-in engines of this process, for one data directory."""

    def __init__(self, data_dir: Path) -> None:
        self.engines = {name: Engine(name, data_dir) for name in ENGINES}

    def shutdown(self) -> None:
        for engine in self.engines.values():
            engine.shutdown()


_instances: dict[Path, Builtin] = {}
_instances_lock = threading.Lock()


def builtin_for(data_dir: Path) -> Builtin:
    with _instances_lock:
        if data_dir not in _instances:
            _instances[data_dir] = Builtin(data_dir)
        return _instances[data_dir]


@atexit.register
def shutdown_all() -> None:
    """Stop every engine (also done by the engines themselves when this process ends)."""
    with _instances_lock:
        instances = list(_instances.values())
    for instance in instances:
        instance.shutdown()


def install_here(name: str, config: CompanionConfig) -> int:
    """Install engine ``name`` in the foreground (``companion setup``), showing uv's output."""
    engine = Engine(name, config.storage.data_dir)
    libraries = resolve_libraries(config)
    engine.home.mkdir(parents=True, exist_ok=True)
    try:
        command = engine.install_command(libraries)
    except CompanionError as exc:
        print(f"  {exc}")
        return 1
    code = subprocess.run(command, env=engine.install_env(), check=False).returncode
    if code == 0:
        engine.mark_installed(libraries)
    return code
