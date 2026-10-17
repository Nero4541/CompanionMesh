"""Irodori-TTS-Server as the Companion Server's built-in text-to-speech.

The server starts this with the Python of builtin/tts/.venv, the settings in
IRODORI_* variables (see Irodori-TTS-Server's .env.example; a .env file next to
this one adds more) and its own process id: when the server ends, however it
ends, this process exits at once, so its GPU memory never outlives the server.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import threading
import time
from pathlib import Path

import uvicorn

log = logging.getLogger("builtin-tts")

MODELS = Path(__file__).resolve().parent / "models"


def checkpoint_file(name: str) -> Path:
    """The checkpoint: models/<name>/model.safetensors, downloaded there first if needed.

    ``name`` is a folder in models/ or a Hugging Face repository (owner/name, kept
    as models/name, with its tokenizer). An absolute path (a folder or the
    .safetensors file) is used as it is.
    """
    given = Path(name).expanduser()
    if given.is_absolute():
        return given if given.suffix == ".safetensors" else given / "model.safetensors"
    folder = MODELS / name.split("/")[-1]
    checkpoint = folder / "model.safetensors"
    if not checkpoint.is_file():
        if name.count("/") != 1:
            raise SystemExit(
                f"no model.safetensors in {folder}: put the checkpoint (and its tokenizer/ "
                "folder) there, or name a Hugging Face repository (owner/name)"
            )
        from huggingface_hub import snapshot_download

        log.info("downloading %s to %s", name, folder)
        snapshot_download(
            repo_id=name, local_dir=folder, allow_patterns=["model.safetensors", "tokenizer/*"]
        )
    return checkpoint


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
    parser.add_argument("--checkpoint", default="Aratako/Irodori-TTS-v4.1-Small-MF")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s:%(name)s:%(message)s")
    exit_with_parent(args.parent_pid)
    use_system_certificates()
    # Read by Irodori's settings when its app is imported below.
    os.environ["IRODORI_CHECKPOINT"] = str(checkpoint_file(args.checkpoint))
    # The model loads before the port opens (IRODORI_PRELOAD), so a server that
    # answers /health is ready to speak.
    uvicorn.run("irodori_openai_tts.app:app", host="127.0.0.1", port=args.port)


if __name__ == "__main__":
    main()
