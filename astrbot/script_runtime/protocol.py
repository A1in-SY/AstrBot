"""Length-prefixed JSON IPC protocol between host and script worker."""

from __future__ import annotations

import json
from typing import Any

from astrbot.script_runtime.errors import ScriptProtocolError

PROTOCOL_VERSION = 1
_LENGTH_BYTES = 8


def _strict_decoder() -> json.JSONDecoder:
    def reject_constant(value: str) -> Any:
        raise ScriptProtocolError(f"invalid JSON constant: {value}")

    return json.JSONDecoder(
        parse_int=int,
        parse_float=float,
        parse_constant=reject_constant,
    )


def encode_frame(payload: dict[str, Any]) -> bytes:
    if not isinstance(payload, dict):
        raise ScriptProtocolError("frame payload must be an object")
    try:
        body = json.dumps(
            payload,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ScriptProtocolError(
            f"frame payload is not JSON-serializable: {exc}"
        ) from None
    return len(body).to_bytes(_LENGTH_BYTES, "big") + body


def decode_frame(data: bytes) -> dict[str, Any]:
    if len(data) < _LENGTH_BYTES:
        raise ScriptProtocolError("frame too short")
    length = int.from_bytes(data[:_LENGTH_BYTES], "big")
    body = data[_LENGTH_BYTES:]
    if length != len(body):
        raise ScriptProtocolError("frame length mismatch")
    try:
        text = body.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ScriptProtocolError("frame body is not valid UTF-8") from exc
    try:
        payload = _strict_decoder().decode(text)
    except ScriptProtocolError:
        raise
    except Exception as exc:
        raise ScriptProtocolError(f"frame body is not valid JSON: {exc}") from None
    if not isinstance(payload, dict):
        raise ScriptProtocolError("frame payload must be an object")
    return payload


def read_frame_blocking(stream: Any) -> dict[str, Any]:
    try:
        header = stream.read(_LENGTH_BYTES)
    except Exception as exc:
        raise ScriptProtocolError(f"failed to read frame header: {exc}") from None
    if not header:
        raise ScriptProtocolError("EOF before frame header")
    if len(header) != _LENGTH_BYTES:
        raise ScriptProtocolError("truncated frame header")
    length = int.from_bytes(header, "big")
    body = stream.read(length)
    if len(body) != length:
        raise ScriptProtocolError("truncated frame body")
    return decode_frame(header + body)


def write_frame_blocking(stream: Any, payload: dict[str, Any]) -> None:
    data = encode_frame(payload)
    try:
        stream.write(data)
        stream.flush()
    except Exception as exc:
        raise ScriptProtocolError(f"failed to write frame: {exc}") from None


def validate_common_fields(
    payload: dict[str, Any],
    *,
    expected_type: str | None = None,
    expected_run_id: str | None = None,
) -> None:
    if payload.get("protocol_version") != PROTOCOL_VERSION:
        raise ScriptProtocolError(
            f"unsupported protocol version: {payload.get('protocol_version')!r}"
        )
    frame_type = payload.get("type")
    if not isinstance(frame_type, str) or not frame_type:
        raise ScriptProtocolError("frame missing type")
    if expected_type is not None and frame_type != expected_type:
        raise ScriptProtocolError(
            f"unexpected frame type {frame_type!r}, expected {expected_type!r}"
        )
    run_id = payload.get("run_id")
    if expected_run_id is not None:
        if run_id != expected_run_id:
            raise ScriptProtocolError("run_id mismatch")


__all__ = [
    "PROTOCOL_VERSION",
    "decode_frame",
    "encode_frame",
    "read_frame_blocking",
    "validate_common_fields",
    "write_frame_blocking",
]
