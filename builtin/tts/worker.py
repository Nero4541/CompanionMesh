"""Irodori-TTS-Server as the Companion Server's built-in text-to-speech.

The server starts this with the Python of data/builtin/tts, the settings in
IRODORI_* variables (see Irodori-TTS-Server's .env.example) and its own process
id: when the server ends, however it ends, this process exits at once, so its
GPU memory never outlives the server.
"""

from __future__ import annotations

import argparse
import os
import sys
import threading
import time

import uvicorn


def exit_with_parent(pid: int) -> None:
    """Exit as soon as process ``pid`` (the server) ends."""

    def watch() -> None:
        if sys.platform == "win32":
            import ctypes

            kernel32 = ctypes.windll.kernel32
            handle = kernel32.OpenProcess(0x00100000, False, pid)  # SYNCHRONIZE
            if handle:
                kernel32.WaitForSingleObject(handle, 0xFFFFFFFF)  # INFINITE
        else:
            while os.getppid() == pid:
                time.sleep(1)
        os._exit(0)

    threading.Thread(target=watch, name="parent-watch", daemon=True).start()


def use_system_certificates() -> None:
    try:
        import truststore
    except ImportError:
        return
    truststore.inject_into_ssl()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--parent-pid", type=int, required=True)
    args = parser.parse_args()
    exit_with_parent(args.parent_pid)
    use_system_certificates()
    # The model loads before the port opens (IRODORI_PRELOAD), so a server that
    # answers /health is ready to speak.
    uvicorn.run("irodori_openai_tts.app:app", host="127.0.0.1", port=args.port)


if __name__ == "__main__":
    main()
