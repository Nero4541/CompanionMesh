from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Protocol

import numpy as np

SAMPLE_RATE = 16_000

# What Whisper writes for noise or silence, learnt from video subtitles: almost never said to a
# companion. Compared without spaces, punctuation and case.
MADE_UP = (
    "ご視聴ありがとうございました",
    "ご清聴ありがとうございました",
    "チャンネル登録よろしくお願いします",
    "請不吝點讚訂閱轉發打賞支持明鏡與點點欄目",
    "请不吝点赞订阅转发打赏支持明镜与点点栏目",
    "字幕由amaraorg社區提供",
    "字幕由amaraorg社区提供",
    "thanksforwatching",
    "thankyouforwatching",
    "subtitlesbytheamaraorgcommunity",
    "cảmơncácbạnđãtheodõi",
    "terimakasihtelahmenonton",
    "시청해주셔서감사합니다",
)


def _bare(text: str) -> str:
    text = unicodedata.normalize("NFKC", text).casefold()
    return re.sub(r"[\W_]+", "", text)


def made_up(text: str) -> bool:
    """Whether ``text`` is one of Whisper's well-known inventions, not something said."""
    bare = _bare(text)
    return any(
        bare == phrase or (bare.startswith(phrase) and len(bare) - len(phrase) <= 4)
        for phrase in MADE_UP
    )


@dataclass(slots=True)
class Transcript:
    text: str
    language: str | None = None
    duration_s: float = 0.0


class STTProvider(Protocol):
    name: str

    async def transcribe(
        self, audio: np.ndarray, *, language: str | None = None, prompt: str | None = None
    ) -> Transcript:
        """Transcribe mono float32 PCM at 16 kHz; ``prompt`` steers style and vocabulary."""
        ...

    async def warmup(self) -> None: ...

    async def aclose(self) -> None: ...
