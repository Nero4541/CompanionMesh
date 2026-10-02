"""Frame-level voice activity detection (512-sample frames at 16 kHz)."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Protocol

import numpy as np

from companion.audio.pcm import rms
from companion.core.config import VADConfig
from companion.core.errors import ProviderError

FRAME_SAMPLES = 512
_CONTEXT = 64


class VAD(Protocol):
    def speech_prob(self, frame: np.ndarray) -> float:
        """Probability (0..1) that a 512-sample frame contains speech."""
        ...

    def reset(self) -> None: ...


class EnergyVAD:
    """Dependency-free RMS threshold VAD; fine for tests and quiet rooms."""

    def __init__(self, threshold: float) -> None:
        self.threshold = threshold

    def speech_prob(self, frame: np.ndarray) -> float:
        return 1.0 if rms(frame) >= self.threshold else 0.0

    def reset(self) -> None:
        pass


def bundled_silero_path() -> Path | None:
    try:
        from faster_whisper.utils import get_assets_path
    except ImportError:
        return None
    path = Path(get_assets_path()) / "silero_vad_v6.onnx"
    return path if path.is_file() else None


class SileroVAD:
    """Streaming Silero VAD over onnxruntime.

    Supports both the faster-whisper export (inputs ``input``/``h``/``c``) and
    the upstream Silero v5 export (inputs ``input``/``state``/``sr``).
    """

    def __init__(self, model_path: Path | None = None) -> None:
        try:
            import onnxruntime
        except ImportError as exc:
            raise ProviderError("silero_vad", "onnxruntime is not installed") from exc
        path = model_path or bundled_silero_path()
        if path is None or not path.is_file():
            raise ProviderError(
                "silero_vad",
                "Silero model not found; install the `stt` extra or set vad.provider: energy",
            )
        opts = onnxruntime.SessionOptions()
        opts.inter_op_num_threads = 1
        opts.intra_op_num_threads = 1
        opts.log_severity_level = 4
        self._session = onnxruntime.InferenceSession(
            str(path), providers=["CPUExecutionProvider"], sess_options=opts
        )
        names = {i.name for i in self._session.get_inputs()}
        self._split_state = {"h", "c"} <= names
        self.reset()

    def reset(self) -> None:
        self._context = np.zeros((1, _CONTEXT), dtype=np.float32)
        self._h = np.zeros((1, 1, 128), dtype=np.float32)
        self._c = np.zeros((1, 1, 128), dtype=np.float32)
        self._state = np.zeros((2, 1, 128), dtype=np.float32)

    def speech_prob(self, frame: np.ndarray) -> float:
        x = np.concatenate([self._context, frame.reshape(1, -1).astype(np.float32)], axis=1)
        self._context = x[:, -_CONTEXT:]
        feeds: dict[str, Any]
        if self._split_state:
            out, self._h, self._c = self._session.run(
                None, {"input": x, "h": self._h, "c": self._c}
            )
        else:
            feeds = {"input": x, "state": self._state, "sr": np.array(16000, dtype=np.int64)}
            out, self._state = self._session.run(None, feeds)
        return float(np.ravel(out)[-1])


def create_vad(config: VADConfig) -> VAD:
    if config.provider == "energy":
        return EnergyVAD(config.energy_threshold)
    return SileroVAD()
