"""Command line entry point: ``uv run companion [serve|check]``."""

from __future__ import annotations

import argparse
import asyncio
import errno
import logging
import socket
import sys
from pathlib import Path

from companion import __version__
from companion.core.config import CompanionConfig, ConfigError, load_config
from companion.core.logging import kv, setup_logging

log = logging.getLogger("companion")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="companion", description="Companion Server")
    parser.add_argument("--version", action="version", version=f"companion {__version__}")
    parser.add_argument("--config", type=Path, help="config YAML (default configs/companion.yaml)")
    parser.add_argument("--host", help="override server.host")
    parser.add_argument("--port", type=int, help="override server.port")
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("serve", help="run the server (default)")
    sub.add_parser("check", help="validate config and probe providers, then exit")
    return parser


def _check_port(host: str, port: int) -> str | None:
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    with socket.socket(family, socket.SOCK_STREAM) as sock:
        if sys.platform != "win32":
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind((host, port))
        except OSError as exc:
            if exc.errno in (errno.EADDRINUSE, getattr(errno, "WSAEADDRINUSE", -1)) or getattr(
                exc, "winerror", None
            ) in (10048, 10013):
                return (
                    f"cannot listen on {host}:{port}: the port is already in use or blocked "
                    "(another companion instance, or reserved by Windows/Hyper-V; try --port)."
                )
            return f"cannot listen on {host}:{port}: {exc}"
    return None


def serve(config: CompanionConfig) -> int:
    import uvicorn

    from companion.api.app import create_app

    host, port = config.server.host, config.server.port
    problem = _check_port(host, port)
    if problem:
        log.error(problem)
        return 2
    app = create_app(config)
    log.info(
        "listening", extra=kv(url=f"http://{host}:{port}", dev_client=f"http://{host}:{port}/dev/")
    )
    uvicorn.run(
        app,
        host=host,
        port=port,
        log_config=None,
        timeout_graceful_shutdown=5,
        ws_max_size=4 * 1024 * 1024,
    )
    return 0


async def _check(config: CompanionConfig) -> int:
    from companion.core.runtime import CompanionRuntime

    runtime = CompanionRuntime(config)
    ok = True
    try:
        runtime.store.open()
        print(f"config     ok  (transcripts: {runtime.storage_description()})")
        print(f"persona    ok  ({runtime.persona.name})")
        health = await runtime.agent.health()
        ok &= health.startswith("ok")
        print(f"agent      {health}  [{runtime.agent.name}]")
        if runtime.stt is not None:
            try:
                await runtime.stt.warmup()
                print(
                    f"stt        ok  ({config.stt.model} on {getattr(runtime.stt, 'device', '?')})"
                )
            except Exception as exc:
                ok = False
                print(f"stt        FAILED: {exc}")
        try:
            runtime.new_vad()
            print(f"vad        ok  ({config.vad.provider})")
        except Exception as exc:
            ok = False
            print(f"vad        FAILED: {exc}")
        if runtime.tts is not None:
            try:
                audio = await runtime.tts.synthesize("テスト。")
                print(f"tts        ok  ({audio.mime}, {len(audio.data)} bytes)")
            except Exception as exc:
                ok = False
                print(f"tts        FAILED: {exc}")
    finally:
        await runtime.stop()
    return 0 if ok else 1


def _use_system_certificates() -> None:
    """Verify TLS against the OS trust store (model downloads, HTTPS providers).

    Needed behind corporate proxies or antivirus HTTPS scanning, whose root
    certificates exist only in the OS store, not in Python's certifi bundle.
    """
    try:
        import truststore
    except ImportError:
        return
    truststore.inject_into_ssl()


def main(argv: list[str] | None = None) -> int:
    _use_system_certificates()
    args = _parser().parse_args(argv)
    overrides: dict[str, dict[str, object]] = {}
    if args.host:
        overrides.setdefault("server", {})["host"] = args.host
    if args.port:
        overrides.setdefault("server", {})["port"] = args.port
    try:
        config = load_config(args.config, overrides=overrides)
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    setup_logging(config.logging.level, config.logging.format)
    if args.command == "check":
        return asyncio.run(_check(config))
    try:
        return serve(config)
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
