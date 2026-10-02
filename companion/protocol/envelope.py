"""Realtime event envelope shared by every transport.

Text frames carry a JSON envelope. Binary frames carry an envelope header
followed by a raw payload (audio now, images later)::

    [uint32 big-endian header length][UTF-8 JSON envelope][payload bytes]

Unknown ``type`` values are valid envelopes; receivers must ignore them.
"""

from __future__ import annotations

import json
import struct
import uuid
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

NAMESPACES = ("conversation", "audio", "vision", "memory", "agent", "system", "session")

_HEADER_LEN = struct.Struct(">I")
MAX_HEADER_BYTES = 64 * 1024


class ProtocolError(ValueError):
    """Raised when a frame cannot be decoded into an envelope."""


def now_rfc3339() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class Envelope(BaseModel):
    model_config = ConfigDict(extra="allow")

    type: str = Field(min_length=1, max_length=128)
    session_id: str | None = None
    device_id: str | None = None
    timestamp: str = Field(default_factory=now_rfc3339)
    id: str = Field(default_factory=lambda: uuid.uuid4().hex)
    payload: dict[str, Any] = Field(default_factory=dict)

    @property
    def namespace(self) -> str:
        return self.type.split(".", 1)[0]

    def to_json(self) -> str:
        return self.model_dump_json(exclude_none=True)


def make_event(
    type_: str,
    payload: dict[str, Any] | None = None,
    *,
    session_id: str | None = None,
    device_id: str | None = None,
) -> Envelope:
    return Envelope(type=type_, payload=payload or {}, session_id=session_id, device_id=device_id)


def parse_text_frame(data: str | bytes) -> Envelope:
    try:
        raw = json.loads(data)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ProtocolError(f"invalid JSON: {exc}") from exc
    if not isinstance(raw, dict):
        raise ProtocolError("envelope must be a JSON object")
    try:
        return Envelope.model_validate(raw)
    except ValidationError as exc:
        raise ProtocolError(f"invalid envelope: {exc.errors()[0]['msg']}") from exc


def encode_binary_frame(envelope: Envelope, payload: bytes) -> bytes:
    header = envelope.to_json().encode("utf-8")
    return _HEADER_LEN.pack(len(header)) + header + payload


def decode_binary_frame(frame: bytes) -> tuple[Envelope, bytes]:
    if len(frame) < _HEADER_LEN.size:
        raise ProtocolError("binary frame too short")
    (header_len,) = _HEADER_LEN.unpack_from(frame)
    if header_len > MAX_HEADER_BYTES or _HEADER_LEN.size + header_len > len(frame):
        raise ProtocolError("binary frame header length out of range")
    start = _HEADER_LEN.size
    envelope = parse_text_frame(frame[start : start + header_len])
    return envelope, frame[start + header_len :]
