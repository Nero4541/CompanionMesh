"""Minimal realtime protocol helpers (JSON envelopes and binary frames), no pydantic."""

from __future__ import annotations

import json
import struct
import uuid
from datetime import UTC, datetime
from typing import Any

_HEADER = struct.Struct(">I")
_PACKET = struct.Struct(">H")


def envelope(type_: str, payload: dict[str, Any] | None = None, **extra: Any) -> dict[str, Any]:
    return {
        "type": type_,
        "timestamp": datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
        "id": uuid.uuid4().hex,
        "payload": payload or {},
        **{k: v for k, v in extra.items() if v is not None},
    }


def text(type_: str, payload: dict[str, Any] | None = None, **extra: Any) -> str:
    return json.dumps(envelope(type_, payload, **extra), ensure_ascii=False)


def binary(header: dict[str, Any], data: bytes) -> bytes:
    raw = json.dumps(header, separators=(",", ":")).encode()
    return _HEADER.pack(len(raw)) + raw + data


def compact_binary(type_: str, data: bytes, payload: dict[str, Any] | None = None) -> bytes:
    """A binary frame with the smallest possible header (for 50 Hz audio)."""
    header: dict[str, Any] = {"type": type_}
    if payload:
        header["payload"] = payload
    raw = json.dumps(header, separators=(",", ":")).encode()
    return _HEADER.pack(len(raw)) + raw + data


def parse_binary(frame: bytes) -> tuple[dict[str, Any], bytes]:
    (size,) = _HEADER.unpack_from(frame)
    return json.loads(frame[4 : 4 + size]), frame[4 + size :]


def join_packets(packets: list[bytes]) -> bytes:
    return b"".join(_PACKET.pack(len(p)) + p for p in packets)
