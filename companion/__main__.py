"""Command line entry point: ``uv run companion [serve|check|setup|edge|camera]``."""

from __future__ import annotations

import argparse
import asyncio
import errno
import logging
import socket
import ssl
import sys
from pathlib import Path

from companion import __version__
from companion.core.config import CompanionConfig, ConfigError, ConfigSource, config_source
from companion.core.errors import CompanionError
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
    sub.add_parser("setup", help="first-run questions: agent, speech, tokens (.env + local config)")
    edge = sub.add_parser("edge", help="set up a board (edge device) over SSH")
    edge_sub = edge.add_subparsers(dest="edge_command", required=True)
    edge_sub.add_parser("discover", help="list boards connected to this computer by USB")
    doc = edge_sub.add_parser(
        "doctor", help="show what a board connected by USB says about itself (log, Wi-Fi, check)"
    )
    doc.add_argument("--host", help="the board's address (default: the board found on USB)")
    inst = edge_sub.add_parser(
        "install", help="install the edge client on a board and connect it to this server"
    )
    upd = edge_sub.add_parser(
        "update",
        help="update a board this server set up, over USB: software, settings, Wi-Fi "
        "(keeps its token and password)",
    )
    inst.add_argument("--name", required=True, help="the device's id (letters, digits, - _ .)")
    inst.add_argument("--host", help="the board's address (default: the board found on USB)")
    inst.add_argument(
        "--profile", default="licheerv-nano", help="hardware profile (client/edge/profiles)"
    )
    inst.add_argument(
        "--server",
        help="realtime URL the board connects to "
        "(default: ws://<this machine's LAN address>:<port>/v1/realtime)",
    )
    inst.add_argument(
        "--wifi",
        action="append",
        default=[],
        metavar="SSID",
        help="add a Wi-Fi network (its password is asked); repeat for several",
    )
    inst.add_argument("--camera", action=argparse.BooleanOptionalAction, default=None)
    inst.add_argument("--rotate", type=int, choices=[0, 90, 180, 270])
    inst.add_argument(
        "--ask-password", action="store_true", help="the board's root password is not the stock one"
    )
    inst.add_argument(
        "--keep-password", action="store_true", help="do not replace the stock root password"
    )
    upd.add_argument("--name", required=True, help="the device's id (letters, digits, - _ .)")
    upd.add_argument("--host", help="the board's address (default: the board found on USB)")
    upd.add_argument(
        "--profile", default="licheerv-nano", help="hardware profile (client/edge/profiles)"
    )
    upd.add_argument(
        "--server",
        help="realtime URL the board connects to "
        "(default: ws://<this machine's LAN address>:<port>/v1/realtime)",
    )
    upd.add_argument(
        "--wifi",
        action="append",
        default=[],
        metavar="SSID",
        help="add a Wi-Fi network (its password is asked); repeat for several",
    )
    upd.add_argument("--camera", action=argparse.BooleanOptionalAction, default=None)
    upd.add_argument("--rotate", type=int, choices=[0, 90, 180, 270])
    upd.add_argument(
        "--ask-password", action="store_true", help="the board's root password is not the stock one"
    )
    upd.add_argument(
        "--keep-password", action="store_true", help="do not replace the stock root password"
    )
    cam = sub.add_parser("camera", help="feed an IP camera into the active session (vision device)")
    cam.add_argument(
        "source",
        help="camera URL: HTTP snapshot (.../shot.jpg), MJPEG stream (.../video) or rtsp://",
    )
    cam.add_argument("--server", help="realtime URL (default: this config's server)")
    cam.add_argument("--session", help="session id to join (default: the active session)")
    cam.add_argument("--device-id", default="ipcam")
    cam.add_argument(
        "--mode",
        choices=["change", "periodic"],
        default="change",
        help="change: send a frame only when the view changes (default); "
        "periodic: every --interval",
    )
    cam.add_argument(
        "--interval",
        type=float,
        default=5.0,
        help="periodic mode: seconds between frames; 0 = only on request",
    )
    cam.add_argument(
        "--heartbeat",
        type=float,
        default=300.0,
        help="change mode: send a routine frame at least this often (seconds)",
    )
    cam.add_argument(
        "--no-enable", action="store_true", help="do not switch vision on when joining"
    )
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


def _load_with_stdlib_ssl(config: object) -> None:
    """Load a uvicorn config that serves TLS with the standard library's SSLContext.

    truststore (injected for outgoing HTTPS, see _use_system_certificates) is
    made for clients; a server context built from it answers no requests.
    """
    try:
        import truststore
    except ImportError:
        config.load()  # type: ignore[attr-defined]
        return
    injected = ssl.SSLContext is truststore.SSLContext
    if injected:
        truststore.extract_from_ssl()
    try:
        config.load()  # type: ignore[attr-defined]
    finally:
        if injected:
            truststore.inject_into_ssl()


def _public_sockets(config: CompanionConfig) -> list[socket.socket]:
    """Listening sockets for the public listener; an address family the machine
    lacks (often IPv6) is skipped with a warning."""
    public = config.server.public
    sockets = []
    for host in public.hosts:
        family = socket.AF_INET6 if ":" in host else socket.AF_INET
        sock = socket.socket(family, socket.SOCK_STREAM)
        try:
            if family == socket.AF_INET6:  # IPv6 only, so "0.0.0.0" can have IPv4
                sock.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
            if sys.platform != "win32":
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind((host, public.port))
        except OSError as exc:
            sock.close()
            log.warning("public listener cannot use %s:%s: %s", host, public.port, exc)
            continue
        sockets.append(sock)
    return sockets


def _run_server(config: CompanionConfig, source: ConfigSource | None) -> bool:
    """Serve until stopped; True if the dashboard asked for a restart."""
    import contextlib

    import uvicorn

    from companion.api.app import create_app
    from companion.api.public import create_public_app
    from companion.core.tls import ensure_certificate

    class DeviceServer(uvicorn.Server):
        """The public listener: the main server handles signals for both."""

        @contextlib.contextmanager
        def capture_signals(self):  # type: ignore[no-untyped-def]
            yield

    host, port = config.server.host, config.server.port
    restart = False
    server: uvicorn.Server | None = None

    def request_restart() -> None:
        nonlocal restart
        restart = True
        if server is not None:
            server.should_exit = True

    app = create_app(config, source=source, restart=request_restart if source else None)
    server = uvicorn.Server(
        uvicorn.Config(
            app,
            host=host,
            port=port,
            log_config=None,
            timeout_graceful_shutdown=5,
            ws_max_size=config.vision.max_frame_bytes + 64 * 1024,
        )
    )
    log.info(
        "listening",
        extra=kv(
            url=f"http://{host}:{port}",
            dev_client=f"http://{host}:{port}/dev/",
            dashboard=f"http://{host}:{port}/admin/",
        ),
    )

    public = config.server.public
    devices: uvicorn.Server | None = None
    sockets: list[socket.socket] = []
    if public.enabled:
        if public.cert_file and public.key_file:
            cert_file, key_file = public.cert_file, public.key_file
        else:
            cert = ensure_certificate(config.storage.data_dir / "tls")
            cert_file, key_file = cert.cert_file, cert.key_file
            log.info("public listener certificate", extra=kv(sha256=cert.sha256))
        sockets = _public_sockets(config)
        if sockets:
            devices = DeviceServer(
                uvicorn.Config(
                    create_public_app(app.state.runtime),
                    log_config=None,
                    timeout_graceful_shutdown=5,
                    ws_max_size=config.vision.max_frame_bytes + 64 * 1024,
                    ssl_certfile=str(cert_file),
                    ssl_keyfile=str(key_file),
                    lifespan="off",
                )
            )
            _load_with_stdlib_ssl(devices.config)
            log.info(
                "public listener for devices",
                extra=kv(
                    addresses=",".join(f"{s.getsockname()[0]}:{public.port}" for s in sockets),
                    url=public.url or "(server.public.url not set)",
                ),
            )
        else:
            log.error("public listener enabled but no address could be used")

    async def serve_all() -> None:
        side = asyncio.create_task(devices.serve(sockets=sockets)) if devices else None
        try:
            await server.serve()
        finally:
            if side is not None and devices is not None:
                devices.should_exit = True
                await side

    try:
        asyncio.run(serve_all(), loop_factory=server.config.get_loop_factory())
    finally:
        for sock in sockets:
            sock.close()
    return restart


def serve(config: CompanionConfig, source: ConfigSource | None = None) -> int:
    """Run the server; with a config ``source`` it can restart itself (dashboard).

    A restart stops the server, reads the config files again and starts it
    anew in this process. If the new settings cannot start, the previous ones
    are used.
    """
    from companion.api.auth import exposure_problem

    previous: CompanionConfig | None = None
    while True:
        problem = exposure_problem(config.server) or _check_port(
            config.server.host, config.server.port
        )
        if problem:
            log.error(problem)
            if previous is None:
                return 2
            log.warning("starting again with the previous settings")
            config, previous = previous, None
            continue

        if not _run_server(config, source) or source is None:
            return 0

        log.info("restarting")
        previous = config
        try:
            config = source.load()
        except ConfigError as exc:
            log.error("cannot load the saved settings; keeping the previous ones: %s", exc)
            config = previous
        setup_logging(config.logging.level, config.logging.format)


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
        att = config.attention
        if att.proactive:
            from datetime import UTC, datetime

            from companion.attention.engine import in_quiet_hours, resolve_zone

            qh = att.quiet_hours
            zone = qh.timezone or f"host time zone ({resolve_zone(None)})"
            quiet = (
                f"quiet {qh.start}-{qh.end} in {zone}"
                + (", quiet now" if in_quiet_hours(qh, datetime.now(UTC)) else "")
                if qh.enabled
                else "no quiet hours"
            )
            print(f"attention  proactive on (every >= {att.min_interval_s / 60:g} min; {quiet})")
        else:
            print("attention  proactive off")
        if config.vision.mode == "agent":
            print("vision     agent mode (frames go to the agent; its model must accept images)")
        if runtime.vlm is not None:
            try:
                from companion.vision.image import test_card_jpeg

                seen = await runtime.vlm.describe(test_card_jpeg(), language="en")
                print(f"vlm        ok  ({seen.description[:60]!r})")
            except Exception as exc:
                ok = False
                print(f"vlm        FAILED: {exc}")
        elif config.vision.mode == "vlm":
            print("vlm        off (vlm.provider: none; vision unavailable in vlm mode)")
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
        source = config_source(args.config, overrides=overrides)
        if args.command == "setup":
            from companion import setup

            return setup.run(source)
        config = source.load()
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    setup_logging(config.logging.level, config.logging.format)
    if args.command == "camera":
        from companion.devices.camera import CameraDevice, CameraOptions, server_url

        options = CameraOptions(
            server=args.server or server_url(config.server.host, config.server.port),
            source_url=args.source,
            session_id=args.session,
            device_id=args.device_id,
            mode=args.mode,
            interval_s=args.interval,
            heartbeat_s=args.heartbeat,
            enable=not args.no_enable,
            token=config.server.auth_token(),
        )
        try:
            asyncio.run(CameraDevice(options).run())
        except KeyboardInterrupt:
            return 0
        except CompanionError as exc:
            log.error(str(exc))
            return 1
    if args.command == "check":
        return asyncio.run(_check(config))
    if args.command == "edge":
        from companion import edge_install

        return edge_install.run_cli(args, config)
    try:
        return serve(config, source)
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
