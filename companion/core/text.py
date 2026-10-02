"""Incremental sentence splitting so TTS can start before the reply finishes."""

from __future__ import annotations

import re

_TERMINATORS = "。！？!?…\n"
_CLOSERS = "」』）)】\"'”’"
_MARKDOWN = [
    (re.compile(r"```.*?```", re.S), " "),
    (re.compile(r"!\[([^\]]*)\]\([^)]*\)"), r"\1"),
    (re.compile(r"\[([^\]]+)\]\([^)]*\)"), r"\1"),
    (re.compile(r"https?://\S+"), " "),
    (re.compile(r"`([^`]*)`"), r"\1"),
    (re.compile(r"^\s{0,3}(#{1,6}|[-*+]|\d+\.)\s+", re.M), ""),
    (re.compile(r"[*_~]{1,3}"), ""),
    (re.compile(r"[ \t]+"), " "),
]


def clean_for_speech(text: str) -> str:
    """Strip Markdown/URLs that should not be read aloud. Emoji are kept
    (some TTS models, e.g. Irodori-TTS, use them as style hints)."""
    for pattern, repl in _MARKDOWN:
        text = pattern.sub(repl, text)
    return text.strip()


def _has_speakable(text: str) -> bool:
    return any(ch.isalnum() for ch in text)


class SentenceSplitter:
    """Feed streamed text; get back complete sentences as they close."""

    def __init__(self, *, min_chars: int = 2, max_chars: int = 160) -> None:
        self._buf = ""
        self._min = min_chars
        self._max = max_chars

    def feed(self, text: str) -> list[str]:
        self._buf += text
        out: list[str] = []
        while True:
            cut = self._find_cut()
            if cut is None:
                break
            sentence, self._buf = self._buf[:cut], self._buf[cut:]
            cleaned = clean_for_speech(sentence)
            if len(cleaned) >= self._min and _has_speakable(cleaned):
                out.append(cleaned)
            elif out and cleaned:
                out[-1] += cleaned
        return out

    def flush(self) -> list[str]:
        rest, self._buf = clean_for_speech(self._buf), ""
        return [rest] if rest and _has_speakable(rest) else []

    def _find_cut(self) -> int | None:
        buf = self._buf
        for i, ch in enumerate(buf):
            is_end = ch in _TERMINATORS
            # ASCII period ends a sentence only when followed by whitespace.
            if ch == "." and i + 1 < len(buf) and buf[i + 1].isspace():
                is_end = True
            if not is_end:
                continue
            j = i + 1
            while j < len(buf) and (buf[j] in _TERMINATORS or buf[j] in _CLOSERS):
                j += 1
            if j == len(buf) and buf[i] != "\n":
                return None  # more punctuation/closers may still arrive
            return j
        if len(buf) > self._max:
            # No terminator in a long run: break at the last comma or space.
            window = buf[: self._max]
            k = max(window.rfind("、"), window.rfind("，"), window.rfind(","), window.rfind(" "))
            return (k + 1) if k > 0 else self._max
        return None
