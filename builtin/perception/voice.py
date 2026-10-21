"""Voice embeddings with pyannote's WeSpeaker model (inside speaker-diarization-community-1).

The model is gated on Hugging Face: accept its terms with your account, then
either put ``HF_TOKEN=<your token>`` in the server's ``.env`` (it is downloaded
into ``models/speaker-diarization-community-1`` on first start), or download
the repository into that folder yourself.

Clips shorter than one second are looped to one second first, as in the
measurements (shorter speech is not reliable anyway).
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

import numpy as np

log = logging.getLogger("builtin-perception")

REPO = "pyannote/speaker-diarization-community-1"
SAMPLE_RATE = 16000


def model_folder(models: Path, name: str) -> Path:
    """models/<name>, downloaded there first when it is not there yet."""
    given = Path(name).expanduser()
    if given.is_absolute():
        return given
    folder = models / name.split("/")[-1]
    if not (folder / "config.yaml").is_file():
        from huggingface_hub import snapshot_download

        repo = name if "/" in name else REPO
        token = os.environ.get("HF_TOKEN") or None
        log.info("downloading %s to %s", repo, folder)
        try:
            snapshot_download(repo, local_dir=str(folder), token=token)
        except Exception as exc:
            raise RuntimeError(
                f"cannot download {repo}: {exc}. It is gated: accept its terms on Hugging Face "
                f"and put HF_TOKEN in .env, or download it into {folder}"
            ) from exc
    return folder


def fit(audio: np.ndarray, min_s: float = 1.0) -> np.ndarray:
    n = int(min_s * SAMPLE_RATE)
    if len(audio) >= n:
        return audio
    return np.tile(audio, int(np.ceil(n / max(len(audio), 1))))[:n]


class VoiceEmbedder:
    def __init__(self, folder: Path, device: str) -> None:
        import warnings

        import torch

        # pyannote warns that torchcodec (for reading files) cannot load without FFmpeg;
        # the engine hands it waveforms, so that does not matter.
        warnings.filterwarnings("ignore", message=r"(?s).*torchcodec")
        from pyannote.audio import Pipeline

        self.torch = torch
        pipeline = Pipeline.from_pretrained(str(folder))
        if pipeline is None:
            raise RuntimeError(f"no pyannote pipeline in {folder}")
        self.device = "cuda" if device in ("auto", "cuda") and torch.cuda.is_available() else "cpu"
        pipeline.to(torch.device(self.device))
        self._embedding: Any = pipeline._embedding
        self.dimension = int(self.embed(np.zeros(SAMPLE_RATE, np.float32)).size)

    def embed(self, audio: np.ndarray) -> np.ndarray:
        """One unit-length embedding for the whole clip (16 kHz mono float32)."""
        wave = self.torch.from_numpy(fit(audio.astype(np.float32))).float()[None, None, :]
        out = np.asarray(self._embedding(wave)).reshape(-1).astype(np.float32)
        return out / (np.linalg.norm(out) + 1e-12)

    def windows(self, audio: np.ndarray, window_s: float, hop_s: float) -> np.ndarray:
        """Embeddings of sliding windows (for clips that may hold more than one voice)."""
        win, hop = int(window_s * SAMPLE_RATE), int(hop_s * SAMPLE_RATE)
        if len(audio) < win:
            return self.embed(audio)[None, :]
        starts = range(0, len(audio) - win + 1, max(hop, 1))
        batch = np.stack([audio[s : s + win] for s in starts]).astype(np.float32)
        out = np.asarray(self._embedding(self.torch.from_numpy(batch)[:, None, :]))
        return (out / (np.linalg.norm(out, axis=1, keepdims=True) + 1e-12)).astype(np.float32)
