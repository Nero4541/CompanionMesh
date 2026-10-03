"""``companion-edge``: run the device, or ``--check`` its tools and devices."""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import shutil
import sys
from pathlib import Path

from companion_edge import __version__
from companion_edge.config import EdgeConfig, load


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
    if config.camera.enabled:
        found = os.path.exists(config.camera.device)
        ok &= found
        print(f"camera   {config.camera.device} {'ok' if found else 'NOT FOUND'}")
    print(f"server   {config.server}")
    print(f"token    {'set' if config.token() else f'not set (${config.token_env})'}")
    return 0 if ok else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="companion-edge", description=__doc__)
    parser.add_argument("--version", action="version", version=f"companion-edge {__version__}")
    parser.add_argument("--config", type=Path, help="edge TOML config (see edge.example.toml)")
    parser.add_argument("--server", help="override the realtime URL")
    parser.add_argument("--check", action="store_true", help="check tools and devices, then exit")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
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

    try:
        asyncio.run(EdgeClient(config).run())
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
