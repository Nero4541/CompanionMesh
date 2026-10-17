"""faster-whisper as the Companion Server's built-in speech recognition.

Serves the OpenAI ``POST /v1/audio/transcriptions`` API on 127.0.0.1. The
server starts this with the Python of data/builtin/stt and its own process id:
when the server ends, however it ends, this process exits at once, so its GPU
memory never outlives the server.
"""

from __future__ import annotations

import argparse
import io
import logging
import os
import sys
import threading
import time
import wave
from pathlib import Path
from typing import Annotated, Any

import uvicorn
from fastapi import FastAPI, File, Form, HTTPException, UploadFile

log = logging.getLogger("builtin-stt")


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


def register_nvidia_libraries() -> None:
    """Let CTranslate2 find the pip-installed cuBLAS/cuDNN (the cuda extra)."""
    try:
        import nvidia  # type: ignore[import-not-found]
    except ImportError:
        return
    for root in map(Path, nvidia.__path__):
        if sys.platform == "win32":
            for bin_dir in root.glob("*/bin"):
                os.add_dll_directory(str(bin_dir))
                os.environ["PATH"] = f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}"
        elif sys.platform == "linux":
            import ctypes

            pending = sorted(
                {*root.glob("cublas/lib/libcublas*.so.*"), *root.glob("cudnn/lib/*.so.*")}
            )
            # Load in passes: a library fails until the ones it links against are loaded.
            while pending:
                failed = []
                for lib in pending:
                    try:
                        ctypes.CDLL(str(lib), mode=ctypes.RTLD_GLOBAL)
                    except OSError:
                        failed.append(lib)
                if len(failed) == len(pending):
                    break
                pending = failed


def load_model(name: str, device: str, compute_type: str) -> tuple[Any, str]:
    """The model on the first device that works: CUDA, then CPU for ``auto``."""
    import numpy as np
    from faster_whisper import WhisperModel

    attempts = []
    if device in ("auto", "cuda"):
        attempts.append(("cuda", "float16" if compute_type == "auto" else compute_type))
    if device in ("auto", "cpu"):
        attempts.append(("cpu", "int8" if compute_type == "auto" else compute_type))
    error: Exception | None = None
    for where, kind in attempts:
        try:
            model = WhisperModel(name, device=where, compute_type=kind)
            # One tiny inference, so missing CUDA libraries fail here.
            segments, _ = model.transcribe(np.zeros(8000, dtype=np.float32))
            list(segments)
        except Exception as exc:  # CTranslate2 raises plain RuntimeError
            log.warning("cannot use %s: %s", where, exc)
            error = exc
            continue
        return model, f"{where}/{kind}"
    raise SystemExit(f"could not load {name!r}: {error}")


def read_wav(data: bytes) -> Any:
    """16-bit PCM WAV as float32 mono at 16 kHz (what the companion sends)."""
    import numpy as np

    with wave.open(io.BytesIO(data)) as wav:
        if wav.getsampwidth() != 2:
            raise ValueError("send 16-bit PCM WAV")
        rate, channels = wav.getframerate(), wav.getnchannels()
        pcm = np.frombuffer(wav.readframes(wav.getnframes()), dtype="<i2")
    audio = pcm.reshape(-1, channels).mean(axis=1).astype(np.float32) / 32768.0
    if rate != 16000:  # linear resampling: enough for speech recognition
        target = np.arange(0, len(audio), rate / 16000)
        audio = np.interp(target, np.arange(len(audio)), audio).astype(np.float32)
    return audio


def create_app(model: Any, name: str, device: str, beam_size: int) -> FastAPI:
    app = FastAPI(title="Built-in speech recognition")
    lock = threading.Lock()  # one transcription at a time: the model is shared

    @app.get("/health")
    def health() -> dict[str, Any]:
        return {"status": "ok", "model": name, "device": device}

    @app.get("/v1/models")
    def models() -> dict[str, Any]:
        return {"object": "list", "data": [{"id": name, "object": "model"}]}

    @app.post("/v1/audio/transcriptions")
    def transcribe(
        file: Annotated[UploadFile, File()],
        language: Annotated[str | None, Form()] = None,
        prompt: Annotated[str | None, Form()] = None,
        response_format: Annotated[str, Form()] = "json",
    ) -> Any:
        try:
            audio = read_wav(file.file.read())
        except (wave.Error, ValueError, EOFError) as exc:
            raise HTTPException(status_code=400, detail=f"not a WAV file: {exc}") from exc
        try:
            with lock:
                segments, info = model.transcribe(
                    audio,
                    language=language or None,
                    initial_prompt=prompt or None,
                    beam_size=beam_size,
                    vad_filter=False,  # the companion sends finished utterances
                    condition_on_previous_text=False,
                )
                text = "".join(segment.text for segment in segments).strip()
        except Exception as exc:
            raise HTTPException(status_code=400, detail=f"cannot transcribe: {exc}") from exc
        if response_format == "text":
            from fastapi.responses import PlainTextResponse

            return PlainTextResponse(text)
        return {"text": text, "language": info.language, "duration": info.duration}

    return app


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--parent-pid", type=int, required=True)
    parser.add_argument("--model", default="large-v3-turbo")
    parser.add_argument("--device", choices=["auto", "cuda", "cpu"], default="auto")
    parser.add_argument("--compute-type", default="auto")
    parser.add_argument("--beam-size", type=int, default=1)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s:%(name)s:%(message)s")
    exit_with_parent(args.parent_pid)
    use_system_certificates()
    register_nvidia_libraries()
    # Loaded before the port opens, so a server that answers /health is ready.
    model, device = load_model(args.model, args.device, args.compute_type)
    log.info("loaded %s on %s", args.model, device)
    app = create_app(model, args.model, device, args.beam_size)
    uvicorn.run(app, host="127.0.0.1", port=args.port)


if __name__ == "__main__":
    main()
