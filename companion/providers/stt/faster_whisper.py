"""Local STT via faster-whisper (CTranslate2). Requires the ``stt`` extra."""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import numpy as np

from companion.core.config import STTConfig
from companion.core.errors import ProviderError
from companion.core.logging import kv
from companion.providers.stt.base import SAMPLE_RATE, Transcript

log = logging.getLogger(__name__)

_dll_dirs_registered = False


def register_nvidia_dll_dirs() -> list[Path]:
    """Expose pip-installed cuBLAS/cuDNN (``cuda`` extra) to CTranslate2 on Windows."""
    global _dll_dirs_registered
    if _dll_dirs_registered or sys.platform != "win32":
        return []
    _dll_dirs_registered = True
    try:
        import nvidia  # type: ignore[import-not-found]
    except ImportError:
        return []
    added: list[Path] = []
    for root in map(Path, nvidia.__path__):
        for bin_dir in root.glob("*/bin"):
            os.add_dll_directory(str(bin_dir))
            os.environ["PATH"] = f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}"
            added.append(bin_dir)
    return added


class FasterWhisperSTT:
    name = "faster_whisper"

    def __init__(self, config: STTConfig) -> None:
        self._config = config
        self._model: Any = None
        self._device = "unloaded"
        # One worker: transcriptions are serialized; the model is shared.
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="stt")
        self._load_lock = asyncio.Lock()

    @property
    def device(self) -> str:
        return self._device

    def _load_blocking(self) -> None:
        register_nvidia_dll_dirs()
        try:
            from faster_whisper import WhisperModel
        except ImportError as exc:
            raise ProviderError(
                self.name, "faster-whisper is not installed; run `uv sync --extra stt`"
            ) from exc

        cfg = self._config
        attempts: list[tuple[str, str]] = []
        if cfg.device in ("auto", "cuda"):
            attempts.append(("cuda", "float16" if cfg.compute_type == "auto" else cfg.compute_type))
        if cfg.device in ("auto", "cpu"):
            attempts.append(("cpu", "int8" if cfg.compute_type == "auto" else cfg.compute_type))

        last_error: Exception | None = None
        for device, compute_type in attempts:
            try:
                model = WhisperModel(cfg.model, device=device, compute_type=compute_type)
                # Run one tiny inference so missing CUDA DLLs fail here, not mid-conversation.
                segments, _ = model.transcribe(np.zeros(SAMPLE_RATE // 2, dtype=np.float32))
                list(segments)
            except Exception as exc:  # CTranslate2 raises plain RuntimeError
                last_error = exc
                log.warning("stt device unavailable", extra=kv(device=device, error=str(exc)))
                continue
            self._model = model
            self._device = f"{device}/{compute_type}"
            log.info("stt model loaded", extra=kv(model=cfg.model, device=self._device))
            return
        raise ProviderError(self.name, f"could not load model {cfg.model!r}: {last_error}")

    async def warmup(self) -> None:
        async with self._load_lock:
            if self._model is None:
                loop = asyncio.get_running_loop()
                await loop.run_in_executor(self._executor, self._load_blocking)

    def _transcribe_blocking(self, audio: np.ndarray, language: str | None) -> Transcript:
        segments, info = self._model.transcribe(
            audio,
            language=language,
            beam_size=self._config.beam_size,
            vad_filter=False,
            condition_on_previous_text=False,
        )
        text = "".join(segment.text for segment in segments).strip()
        return Transcript(text=text, language=info.language, duration_s=info.duration)

    async def transcribe(self, audio: np.ndarray, *, language: str | None = None) -> Transcript:
        await self.warmup()
        loop = asyncio.get_running_loop()
        lang = language or self._config.language
        try:
            return await loop.run_in_executor(
                self._executor, self._transcribe_blocking, audio.astype(np.float32), lang
            )
        except ProviderError:
            raise
        except Exception as exc:
            raise ProviderError(self.name, f"transcription failed: {exc}") from exc

    async def aclose(self) -> None:
        self._executor.shutdown(wait=False, cancel_futures=True)
        self._model = None
