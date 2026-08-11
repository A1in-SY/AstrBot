"""IPC protocol tests."""

from __future__ import annotations

import io

import pytest

from astrbot.script_runtime.errors import ScriptProtocolError
from astrbot.script_runtime.protocol import (
    decode_frame,
    encode_frame,
    read_frame_blocking,
    write_frame_blocking,
)


def test_roundtrip():
    payload = {"protocol_version": 1, "type": "ok", "text": "中文😀"}
    data = encode_frame(payload)
    assert decode_frame(data) == payload


def test_large_frame():
    payload = {"protocol_version": 1, "type": "ok", "blob": "x" * 100_000}
    assert decode_frame(encode_frame(payload)) == payload


def test_nan_rejected():
    with pytest.raises(ScriptProtocolError):
        encode_frame({"protocol_version": 1, "type": "ok", "v": float("nan")})


def test_bad_utf8_rejected():
    data = (5).to_bytes(8, "big") + b"\xff\xfe"
    with pytest.raises(ScriptProtocolError):
        decode_frame(data)


def test_length_mismatch_rejected():
    data = (5).to_bytes(8, "big") + b"123"
    with pytest.raises(ScriptProtocolError):
        decode_frame(data)


def test_non_object_payload_rejected():
    body = b"[1,2]"
    data = len(body).to_bytes(8, "big") + body
    with pytest.raises(ScriptProtocolError):
        decode_frame(data)


def test_blocking_io_roundtrip():
    stream = io.BytesIO()
    write_frame_blocking(stream, {"protocol_version": 1, "type": "ack", "ok": True})
    stream.seek(0)
    assert read_frame_blocking(stream) == {
        "protocol_version": 1,
        "type": "ack",
        "ok": True,
    }


def test_truncated_header():
    with pytest.raises(ScriptProtocolError):
        read_frame_blocking(io.BytesIO(b"\x00\x00"))


def test_eof():
    with pytest.raises(ScriptProtocolError):
        read_frame_blocking(io.BytesIO(b""))
