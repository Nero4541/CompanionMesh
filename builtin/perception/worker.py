"""Faces and voices for the Companion Server's perception: the built-in perception engine.

Serves on 127.0.0.1:

    GET  /health            what is loaded, on which device
    POST /v1/faces          a JPEG (body) -> every face: box, score, key points,
                            ArcFace embedding, mouth gap, yaw
    POST /v1/voice/embed    a 16 kHz WAV (body) -> WeSpeaker embedding of the clip
                            (?window_s=1.5&hop_s=0.75: one per sliding window)

Embeddings travel as base64 of little-endian float32. The server starts this
with the Python of builtin/perception/.venv and its own process id: when the
server ends, however it ends, this process exits at once.

Faces work without the voice model; when the voice model cannot load (it is
gated, see voice.py), /health says why and /v1/voice/embed answers 503.
"""

from __future__ import annotations

import argparse
import base64
import io
import logging
import os
import sys
import threading
import time
import zipfile
from pathlib import Path
from typing import Any

import numpy as np
import uvicorn
from fastapi import FastAPI, HTTPException, Request

log = logging.getLogger("builtin-perception")

HERE = Path(__file__).resolve().parent
MODELS = HERE / "models"
BUFFALO_URL = "https://github.com/deepinsight/insightface/releases/download/v0.7/buffalo_l.zip"


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


def face_folder(name: str) -> Path:
    """models/<name> with the buffalo_l ONNX files, downloaded there first when missing."""
    from faces import MODEL_FILES

    given = Path(name).expanduser()
    folder = given if given.is_absolute() else MODELS / name
    if all((folder / f).is_file() for f in MODEL_FILES.values()):
        return folder
    if name != "buffalo_l":
        raise SystemExit(f"no face models in {folder}: it needs {', '.join(MODEL_FILES.values())}")
    import urllib.request

    log.info("downloading %s to %s", BUFFALO_URL, folder)
    folder.mkdir(parents=True, exist_ok=True)
    with urllib.request.urlopen(BUFFALO_URL, timeout=60) as response:
        data = response.read()
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        for member in archive.namelist():
            base = member.rsplit("/", 1)[-1]
            if base in MODEL_FILES.values():
                (folder / base).write_bytes(archive.read(member))
    return folder


def providers_for(device: str) -> list[str]:
    import onnxruntime as ort

    available = ort.get_available_providers()
    wanted = []
    if device in ("auto", "cuda") and "CUDAExecutionProvider" in available:
        wanted.append("CUDAExecutionProvider")
    if device == "cuda" and not wanted:
        raise SystemExit("device cuda asked for, but onnxruntime has no CUDA here")
    return [*wanted, "CPUExecutionProvider"]


def preload_cuda() -> None:
    """Let onnxruntime find CUDA and cuDNN: the ones PyTorch brings, loaded with torch."""
    try:
        import torch

        if torch.cuda.is_available():
            torch.zeros(1, device="cuda")  # loads cuDNN/cuBLAS into this process
    except Exception as exc:  # no torch, or no GPU: onnxruntime falls back to the CPU
        log.info("CUDA libraries not preloaded: %s", exc)


def b64(array: np.ndarray) -> str:
    return base64.b64encode(np.ascontiguousarray(array, dtype="<f4").tobytes()).decode()


def read_wav(data: bytes) -> np.ndarray:
    """A WAV (16-bit PCM or float) as float32 mono at 16 kHz."""
    import wave

    try:
        with wave.open(io.BytesIO(data)) as wav:
            width, rate, channels = wav.getsampwidth(), wav.getframerate(), wav.getnchannels()
            raw = wav.readframes(wav.getnframes())
        if width != 2:
            raise ValueError("send 16-bit PCM WAV")
        audio = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
    except wave.Error:
        import soundfile as sf

        audio, rate = sf.read(io.BytesIO(data), dtype="float32", always_2d=True)
        channels = audio.shape[1]
        audio = audio.reshape(-1)
    audio = audio.reshape(-1, channels).mean(axis=1)
    if rate != 16000:
        target = np.arange(0, len(audio), rate / 16000)
        audio = np.interp(target, np.arange(len(audio)), audio).astype(np.float32)
    return audio.astype(np.float32)


def create_app(analyzer: Any, voice: Any, voice_error: str | None, info: dict[str, Any]) -> FastAPI:
    import cv2

    app = FastAPI(title="Built-in perception")
    face_lock = threading.Lock()
    voice_lock = threading.Lock()

    @app.get("/health")
    def health() -> dict[str, Any]:
        return {
            "status": "ok",
            **info,
            "voice": voice is not None,
            "voice_error": voice_error,
        }

    @app.post("/v1/faces")
    async def faces(request: Request, landmarks: bool = True) -> dict[str, Any]:
        data = await request.body()
        img = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR)
        if img is None:
            raise HTTPException(status_code=400, detail="not an image (send a JPEG or PNG)")
        started = time.perf_counter()
        with face_lock:
            found = analyzer.analyze(img, landmarks=landmarks)
        return {
            "width": int(img.shape[1]),
            "height": int(img.shape[0]),
            "ms": round(1000 * (time.perf_counter() - started), 1),
            "faces": [
                {
                    "box": [round(v, 2) for v in f.box],
                    "score": round(f.score, 4),
                    "kps": [[round(float(x), 2), round(float(y), 2)] for x, y in f.kps],
                    "embedding": b64(f.embedding),
                    "mouth": None if f.mouth is None else round(f.mouth, 4),
                    "yaw": round(f.yaw, 4),
                }
                for f in found
            ],
        }

    @app.post("/v1/voice/embed")
    async def embed(
        request: Request, window_s: float | None = None, hop_s: float | None = None
    ) -> dict[str, Any]:
        if voice is None:
            raise HTTPException(status_code=503, detail=voice_error or "no voice model")
        try:
            audio = read_wav(await request.body())
        except Exception as exc:
            raise HTTPException(status_code=400, detail=f"not a WAV file: {exc}") from exc
        if audio.size < 1600:
            raise HTTPException(status_code=400, detail="the clip is shorter than 0.1 s")
        started = time.perf_counter()
        with voice_lock:
            if window_s:
                rows = voice.windows(audio, window_s, hop_s or window_s / 2)
            else:
                rows = voice.embed(audio)[None, :]
        return {
            "duration_s": round(audio.size / 16000, 3),
            "ms": round(1000 * (time.perf_counter() - started), 1),
            "dimension": int(rows.shape[1]),
            "embeddings": [b64(r) for r in rows],
        }

    return app


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--parent-pid", type=int, required=True)
    parser.add_argument("--faces", default="buffalo_l", help="face model folder in models/")
    parser.add_argument("--voice", default="speaker-diarization-community-1")
    parser.add_argument("--device", choices=["auto", "cuda", "cpu"], default="auto")
    parser.add_argument("--det-size", type=int, default=640)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s:%(name)s:%(message)s")
    exit_with_parent(args.parent_pid)
    use_system_certificates()
    sys.path.insert(0, str(HERE))
    from faces import FaceAnalyzer

    if args.device != "cpu":
        preload_cuda()
    analyzer = FaceAnalyzer(
        face_folder(args.faces), providers_for(args.device), det_size=args.det_size
    )
    log.info("faces on %s", analyzer.providers[0])
    voice, voice_error = None, None
    if args.voice and args.voice != "none":
        try:
            from voice import VoiceEmbedder, model_folder

            voice = VoiceEmbedder(model_folder(MODELS, args.voice), args.device)
            log.info("voices on %s (%d numbers)", voice.device, voice.dimension)
        except Exception as exc:  # gated model without a token, or no torch: faces still work
            voice_error = str(exc)
            log.warning("no voice model: %s", exc)
    info = {
        "faces": args.faces,
        "face_device": "cuda" if analyzer.providers[0].startswith("CUDA") else "cpu",
        "voice_model": args.voice,
        "voice_device": voice.device if voice else None,
    }
    # Loaded before the port opens, so a server that answers /health is ready.
    uvicorn.run(create_app(analyzer, voice, voice_error, info), host="127.0.0.1", port=args.port)


if __name__ == "__main__":
    main()
