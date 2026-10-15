"""``companion-edge``: run the device, or ``--check`` its tools and devices."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import logging.handlers
import os
import shutil
import signal
import sys
from pathlib import Path

from companion_edge import __version__, logship
from companion_edge.config import EdgeConfig, load

# A board's SD card should not fill up with logs.
LOG_FILE_BYTES = 512 * 1024


def check(config: EdgeConfig) -> int:
    ok = True
    for tool, package, needed in (
        ("arecord", "alsa-utils", config.audio.enabled),
        ("aplay", "alsa-utils", config.audio.enabled),
        ("ffmpeg", "ffmpeg", config.camera.enabled),
    ):
        if needed:
            found = shutil.which(tool)
            ok &= bool(found)
            print(f"{tool:<8} {found or f'MISSING (apt install {package})'}")
    if config.audio.enabled and config.audio.codec == "opus":
        from companion_edge.devices import ffmpeg_has_opus
        from companion_edge.opus import OpusEncoder, OpusUnavailable

        try:
            OpusEncoder().close()
            print("opus     libopus")
        except OpusUnavailable:
            if ffmpeg_has_opus():
                print("opus     ffmpeg's built-in encoder (no libopus)")
            else:
                print("opus     none: audio will be sent as uncompressed PCM")
    if config.camera.enabled and config.camera.driver == "cvi":
        from companion_edge import cvi

        found = cvi.available()
        ok &= found
        detail = "ok" if found else f"NOT FOUND (needs {cvi.SENSOR_TEST} and {cvi.SENSOR_CONFIG})"
        print(f"camera   cvi (MIPI CSI) {detail}")
    elif config.camera.enabled:
        found = os.path.exists(config.camera.device)
        ok &= found
        print(f"camera   {config.camera.device} {'ok' if found else 'NOT FOUND'}")
    print(f"server   {config.server}")
    print(f"token    {'set' if config.token() else f'not set (${config.token_env})'}")
    return 0 if ok else 1


async def _serve(client: object) -> str:
    """Run the client; SIGTERM (a service stopping) shuts it down cleanly, so
    devices such as the camera are released properly."""
    task = asyncio.current_task()
    assert task is not None
    with contextlib.suppress(NotImplementedError, RuntimeError):  # not on Windows
        asyncio.get_running_loop().add_signal_handler(signal.SIGTERM, task.cancel)
    try:
        return await client.run()  # type: ignore[attr-defined]
    except asyncio.CancelledError:
        return "stopped"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="companion-edge", description=__doc__)
    parser.add_argument("--version", action="version", version=f"companion-edge {__version__}")
    parser.add_argument("--config", type=Path, help="edge TOML config (see edge.example.toml)")
    parser.add_argument("--server", help="override the realtime URL")
    parser.add_argument("--check", action="store_true", help="check tools and devices, then exit")
    parser.add_argument("-v", "--verbose", action="store_true")
    parser.add_argument(
        "--log-file",
        type=Path,
        help="log to this file instead of stderr, rotated at 512 KB (3 old files kept)",
    )
    args = parser.parse_args(argv)

    # With a log file (a service), stderr is left for crashes before logging works.
    handler: logging.Handler = logging.StreamHandler()
    if args.log_file:
        args.log_file.parent.mkdir(parents=True, exist_ok=True)
        handler = logging.handlers.RotatingFileHandler(
            args.log_file, maxBytes=LOG_FILE_BYTES, backupCount=3, encoding="utf-8"
        )
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S" if args.log_file else "%H:%M:%S",
        handlers=[handler],
    )
    logging.getLogger().addHandler(logship.BUFFER)  # sent to the server (device.log)
    try:
        config = load(args.config)
    except (OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if args.server:
        config.server = args.server
    if args.check:
        return check(config)

    from companion_edge.client import EdgeClient

    # Settings changed from the server's dashboard are saved to --config and
    # taken by starting over in this process (no service manager needed).
    try:
        while (
            asyncio.run(_serve(EdgeClient(config, config_path=args.config, log_file=args.log_file)))
            == "restart"
        ):
            config = load(args.config)
            if args.server:
                config.server = args.server
            logging.getLogger("companion_edge").info("restarted with the new settings")
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
